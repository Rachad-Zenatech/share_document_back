import os
import asyncio
import logging
from contextlib import asynccontextmanager
import asyncpg

# Optional dotenv import for loading .env and local overrides during development
try:
    from dotenv import load_dotenv
    _root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(_root_dir, ".env"))
    load_dotenv(os.path.join(_root_dir, ".env.local"), override=True)
except ImportError:
    pass
# Global reference to the connection pool
_pool: asyncpg.Pool | None = None
logger = logging.getLogger(__name__)

async def create_pool():
    """Creates the connection pool if it doesn't exist yet, then returns it."""
    global _pool
    if _pool is None:
        logger.info("Initializing PostgreSQL connection pool", extra={"event": "database_pool_initializing"})
        retries = 5
        for attempt in range(retries):
            try:
                min_size = int(os.getenv("DB_POOL_MIN_SIZE", "2"))
                max_size = int(os.getenv("DB_POOL_MAX_SIZE", "10"))
                max_queries = int(os.getenv("DB_POOL_MAX_QUERIES", "50000"))
                max_inactive_lifetime = float(os.getenv("DB_POOL_MAX_INACTIVE_LIFETIME", "300.0"))
                cmd_timeout = float(os.getenv("DB_COMMAND_TIMEOUT", "120.0"))

                ssl_env = os.getenv("DATABASE_SSL", "").lower().strip()
                if ssl_env in ("false", "0", "no", "disable", "off"):
                    ssl_mode = False
                elif ssl_env in ("true", "1", "yes", "require"):
                    ssl_mode = "require"
                else:
                    dsn = os.getenv("DATABASE_URL", "")
                    ssl_mode = False if any(h in dsn for h in ("localhost", "127.0.0.1", "host.docker.internal")) else "require"

                _pool = await asyncpg.create_pool(
                    dsn=os.environ["DATABASE_URL"],
                    ssl=ssl_mode,
                    min_size=min_size,
                    max_size=max_size,
                    max_queries=max_queries,
                    command_timeout=cmd_timeout,
                    max_inactive_connection_lifetime=max_inactive_lifetime,
                )
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
async def get_conn():
    pool = get_pool()
    t0 = asyncio.get_running_loop().time()
    async with pool.acquire() as conn:
        acquire_ms = (asyncio.get_running_loop().time() - t0) * 1000
        if acquire_ms > 50:
            logger.debug(f"[DB:POOL] Connection acquired in {acquire_ms:.2f}ms")
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
