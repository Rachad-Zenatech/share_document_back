import os
import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import UUID
import asyncpg

# Optional dotenv import for loading .env and local overrides during development
try:
    from dotenv import load_dotenv
    _root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(_root_dir, ".env"))
    load_dotenv(os.path.join(_root_dir, ".env.local"), override=True)
except ImportError:
    pass


def acquire_timeout() -> float:
    """Seconds a caller will wait for a free pooled connection before giving up."""
    return float(os.getenv("DB_POOL_ACQUIRE_TIMEOUT", "10.0"))


def _resolve_rls_identity(
    user_id: str | UUID | None = None,
    is_super_admin: bool | None = None,
) -> tuple[str, bool]:
    """Resolve verified user identity and super-admin status from explicit parameters or auth ContextVars."""
    uid_val = user_id
    super_val = is_super_admin
    if uid_val is None or super_val is None:
        try:
            from services.shared.auth.auth_service import (
                current_user_id_ctx,
                current_user_is_super_admin_ctx,
            )
            if uid_val is None:
                uid_val = current_user_id_ctx.get()
            if super_val is None:
                super_val = current_user_is_super_admin_ctx.get()
        except Exception:
            pass
    uid_str = str(uid_val).strip() if uid_val is not None else ""
    return uid_str, bool(super_val)


async def set_rls_context(
    conn: asyncpg.Connection,
    user_id: str | UUID | None = None,
    *,
    is_super_admin: bool | None = None,
    is_local: bool = True,
) -> None:
    """Supply the verified user identity to PostgreSQL Row-Level Security (RLS) using bound parameters.

    When `is_local=True` (default), settings apply only to the current transaction (`BEGIN ... COMMIT`)
    and are automatically cleared when the transaction ends.
    """
    uid_str, super_flag = _resolve_rls_identity(user_id, is_super_admin)
    await conn.execute(
        "SELECT set_config('app.user_id', $1, $3), set_config('app.is_super_admin', $2, $3)",
        uid_str,
        "true" if super_flag else "false",
        bool(is_local),
    )


class _RLSPoolAcquireContext:
    """Wraps asyncpg PoolAcquireContext to propagate verified RLS context on connection checkout."""

    __slots__ = ("_acquire_ctx", "_conn", "_applied")

    def __init__(self, acquire_ctx):
        self._acquire_ctx = acquire_ctx
        self._conn = None
        self._applied = False

    def __await__(self):
        return self._acquire_ctx.__await__()

    async def __aenter__(self) -> asyncpg.Connection:
        conn = await self._acquire_ctx.__aenter__()
        self._conn = conn
        uid_str, super_flag = _resolve_rls_identity()
        if uid_str:
            try:
                await conn.execute(
                    "SELECT set_config('app.user_id', $1, false), set_config('app.is_super_admin', $2, false)",
                    uid_str,
                    "true" if super_flag else "false",
                )
                self._applied = True
            except Exception:
                pass
        return conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # Session-level settings must not leak to the next (possibly context-free) borrower of this connection
        if self._applied and self._conn is not None:
            try:
                await self._conn.execute(
                    "SELECT set_config('app.user_id', '', false), set_config('app.is_super_admin', 'false', false)"
                )
            except Exception:
                pass
        return await self._acquire_ctx.__aexit__(exc_type, exc_val, exc_tb)


class PoolWrapper:
    """Wraps an asyncpg.Pool to provide a default acquire timeout and RLS context across all callers."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    def acquire(self, *, timeout=None):
        t = acquire_timeout() if timeout is None else timeout
        return _RLSPoolAcquireContext(self._pool.acquire(timeout=t))

    async def close(self):
        return await self._pool.close()

    def terminate(self):
        return self._pool.terminate()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def __getattr__(self, name):
        return getattr(self._pool, name)


# Global reference to the connection pool
_pool: PoolWrapper | None = None
logger = logging.getLogger(__name__)

async def create_pool():
    """Creates the connection pool if it doesn't exist yet, then returns it."""
    global _pool
    if _pool is None:
        logger.info("Initializing PostgreSQL connection pool", extra={"event": "database_pool_initializing"})
        retries = 5
        for attempt in range(retries):
            try:
                min_size = int(os.getenv("DB_POOL_MIN_SIZE", "1"))
                max_size = int(os.getenv("DB_POOL_MAX_SIZE", "5"))
                max_queries = int(os.getenv("DB_POOL_MAX_QUERIES", "50000"))
                max_inactive_lifetime = float(os.getenv("DB_POOL_MAX_INACTIVE_LIFETIME", "60.0"))
                cmd_timeout = float(os.getenv("DB_COMMAND_TIMEOUT", "30.0"))

                ssl_env = os.getenv("DATABASE_SSL", "").lower().strip()
                if ssl_env in ("false", "0", "no", "disable", "off"):
                    ssl_mode = False
                elif ssl_env in ("true", "1", "yes", "require"):
                    ssl_mode = "require"
                else:
                    dsn = os.getenv("DATABASE_URL", "")
                    ssl_mode = False if any(h in dsn for h in ("localhost", "127.0.0.1", "host.docker.internal")) else "require"

                raw_pool = await asyncpg.create_pool(
                    dsn=os.environ["DATABASE_URL"],
                    ssl=ssl_mode,
                    min_size=min_size,
                    max_size=max_size,
                    max_queries=max_queries,
                    command_timeout=cmd_timeout,
                    max_inactive_connection_lifetime=max_inactive_lifetime,
                )
                _pool = PoolWrapper(raw_pool)
                break
            except Exception as e:
                if "EMAXCONNSESSION" in str(e) and attempt < retries - 1:
                    logger.warning(
                        "Database connection limit reached; retrying",
                        extra={"event": "database_pool_retry"},
                    )
                    await asyncio.sleep(5)
                else:
                    raise
    return _pool

async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("Closed PostgreSQL connection pool", extra={"event": "database_pool_closed"})


async def reset_pool():
    """Drop the current pool and create a fresh one after connection failures."""
    global _pool
    old_pool = _pool
    _pool = None
    if old_pool:
        try:
            await asyncio.wait_for(old_pool.close(), timeout=5)
        except Exception:
            old_pool.terminate()
    return await create_pool()


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialised. Call create_pool() first.")
    return _pool


@asynccontextmanager
async def get_rls_conn(
    user_id: str | UUID | None = None,
    *,
    is_super_admin: bool | None = None,
):
    """Acquire a pooled connection, start a transaction, and bind `app.user_id` locally to that transaction."""
    pool = get_pool()
    t0 = asyncio.get_running_loop().time()
    async with pool.acquire() as conn:
        acquire_ms = (asyncio.get_running_loop().time() - t0) * 1000
        if acquire_ms > 50:
            logger.debug(f"[DB:POOL] Connection acquired in {acquire_ms:.2f}ms")
        async with conn.transaction():
            await set_rls_context(conn, user_id, is_super_admin=is_super_admin, is_local=True)
            yield conn


@asynccontextmanager
async def get_conn():
    pool = get_pool()
    t0 = asyncio.get_running_loop().time()
    async with pool.acquire() as conn:
        acquire_ms = (asyncio.get_running_loop().time() - t0) * 1000
        if acquire_ms > 50:
            logger.debug(f"[DB:POOL] Connection acquired in {acquire_ms:.2f}ms")
        uid_str, super_flag = _resolve_rls_identity()
        if uid_str:
            async with conn.transaction():
                await set_rls_context(conn, uid_str, is_super_admin=super_flag, is_local=True)
                yield conn
        else:
            yield conn


async def fetch_one(sql: str, *args):
    t0 = asyncio.get_running_loop().time()
    async with get_conn() as conn:
        row = await conn.fetchrow(sql, *args)
        dur_ms = (asyncio.get_running_loop().time() - t0) * 1000
        q_snippet = " ".join(sql.strip().split()[:6])
        logger.debug(f"[DB:FETCH_ONE] {q_snippet}... (took {dur_ms:.2f}ms)")
        return dict(row) if row else None


async def fetch_all(sql: str, *args):
    t0 = asyncio.get_running_loop().time()
    async with get_conn() as conn:
        rows = await conn.fetch(sql, *args)
        dur_ms = (asyncio.get_running_loop().time() - t0) * 1000
        q_snippet = " ".join(sql.strip().split()[:6])
        logger.debug(f"[DB:FETCH_ALL] {q_snippet}... (rows={len(rows)}, took {dur_ms:.2f}ms)")
        return [dict(r) for r in rows]


async def execute(sql: str, *args):
    t0 = asyncio.get_running_loop().time()
    async with get_conn() as conn:
        row = await conn.fetchrow(sql, *args)
        dur_ms = (asyncio.get_running_loop().time() - t0) * 1000
        q_snippet = " ".join(sql.strip().split()[:6])
        logger.debug(f"[DB:EXECUTE] {q_snippet}... (took {dur_ms:.2f}ms)")
        return dict(row) if row else None


async def execute_many(sql: str, args_list: list):
    t0 = asyncio.get_running_loop().time()
    async with get_conn() as conn:
        await conn.executemany(sql, args_list)
        dur_ms = (asyncio.get_running_loop().time() - t0) * 1000
        q_snippet = " ".join(sql.strip().split()[:6])
        logger.debug(f"[DB:EXECUTE_MANY] {q_snippet}... (items={len(args_list)}, took {dur_ms:.2f}ms)")
