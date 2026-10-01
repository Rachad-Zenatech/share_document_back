import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

logger = logging.getLogger(__name__)

CACHE_CHANNEL = os.getenv("CACHE_INVALIDATION_CHANNEL", "zenatech_cache_events")


class AsyncInMemoryCache:
    """Lightweight in-memory cache with TTL expiration, LRU eviction, and key/prefix invalidation."""

    def __init__(self, maxsize: int = 1000):
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._maxsize = maxsize
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            expires_at, value = entry
            if time.monotonic() > expires_at:
                del self._cache[key]
                return None
            return value

    async def set(self, key: str, value: Any, ttl_seconds: int = 60) -> None:
        async with self._lock:
            if len(self._cache) >= self._maxsize:
                # Evict expired or oldest entries
                now = time.monotonic()
                expired = [k for k, (exp, _) in self._cache.items() if exp <= now]
                for k in expired:
                    del self._cache[k]

                if len(self._cache) >= self._maxsize:
                    sorted_keys = sorted(
                        self._cache.keys(), key=lambda k: self._cache[k][0]
                    )
                    for k in sorted_keys[: max(1, self._maxsize // 7)]:
                        del self._cache[k]

            self._cache[key] = (time.monotonic() + ttl_seconds, value)

    async def invalidate(self, key: str) -> None:
        async with self._lock:
            self._cache.pop(key, None)

    async def invalidate_prefix(self, prefix: str) -> None:
        async with self._lock:
            keys_to_del = [k for k in self._cache.keys() if k.startswith(prefix)]
            for k in keys_to_del:
                del self._cache[k]

    async def clear(self) -> None:
        async with self._lock:
            self._cache.clear()


# Pre-configured cache domains
permission_cache = AsyncInMemoryCache(maxsize=1000)
master_data_cache = AsyncInMemoryCache(maxsize=500)
config_cache = AsyncInMemoryCache(maxsize=500)


async def _apply_cache_invalidation(scope: str, key: Optional[str] = None) -> None:
    """Apply invalidation to local process in-memory caches."""
    try:
        if scope == "permissions":
            if key:
                await permission_cache.invalidate(key)
            else:
                await permission_cache.clear()
        elif scope in ("config", "roles"):
            if key:
                await config_cache.invalidate(key)
            else:
                await config_cache.clear()
            # Changing roles/configs also invalidates derived permissions
            await permission_cache.clear()
        elif scope == "master_data":
            if key:
                await master_data_cache.invalidate(key)
            else:
                await master_data_cache.clear()
        elif scope == "all":
            await permission_cache.clear()
            await config_cache.clear()
            await master_data_cache.clear()
    except Exception as e:
        logger.warning(f"Error applying cache invalidation for scope={scope}, key={key}: {e}")


async def emit_cache_invalidation(scope: str, key: Optional[str] = None) -> None:
    """
    Broadcast a cache invalidation event.
    1. Immediately invalidates this local worker's cache.
    2. Sends a PostgreSQL NOTIFY event so all other workers/servers invalidate theirs.
    """
    # 1. Local invalidation
    await _apply_cache_invalidation(scope, key)

    # 2. Distributed notification via PostgreSQL
    payload = json.dumps({"scope": scope, "key": key})
    try:
        from postgresql_db.database import get_pool

        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute("SELECT pg_notify($1, $2)", CACHE_CHANNEL, payload)
    except Exception as e:
        # Non-blocking if DB pool is not ready or during early bootstrap
        logger.debug(f"Distributed cache NOTIFY skipped/failed: {e}")


def _create_notification_handler():
    """Create a callback for PostgreSQL NOTIFY events received from other workers."""
    def handle_notification(connection, pid, channel, payload):
        try:
            data = json.loads(payload)
            scope = data.get("scope", "all")
            key = data.get("key")
            # Schedule cache invalidation on the running event loop
            loop = asyncio.get_running_loop()
            loop.create_task(_apply_cache_invalidation(scope, key))
        except Exception as e:
            logger.warning(f"Failed to process cache invalidation payload: {e}")

    return handle_notification


async def start_cache_invalidation_listener():
    """
    Background worker that listens for PostgreSQL NOTIFY events on CACHE_CHANNEL.
    Keeps a dedicated connection open with automatic reconnection on drops.
    """
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.warning("DATABASE_URL not configured. Cache invalidation listener disabled.")
        return

    logger.info("Starting distributed cache invalidation listener...")
    handler = _create_notification_handler()

    ssl_mode = os.getenv("DATABASE_SSL", "false").lower()
    ssl_opt = "require" if (ssl_mode in ("true", "require", "1") or "rds.amazonaws.com" in db_url or "supabase.com" in db_url) else False

    while True:
        conn = None
        try:
            conn = await asyncpg.connect(db_url, ssl=ssl_opt)
            await conn.add_listener(CACHE_CHANNEL, handler)
            logger.info("Distributed cache invalidation listener connected.")

            # Keep connection alive while listening
            while not conn.is_closed():
                await asyncio.sleep(15)

        except asyncio.CancelledError:
            logger.info("Stopping distributed cache invalidation listener...")
            if conn and not conn.is_closed():
                try:
                    await conn.remove_listener(CACHE_CHANNEL, handler)
                    await conn.close()
                except Exception:
                    pass
            break
        except Exception as e:
            logger.warning(f"Cache invalidation listener disconnected ({e}), reconnecting in 5s...")
            if conn and not conn.is_closed():
                try:
                    await conn.close()
                except Exception:
                    pass
            await asyncio.sleep(5)
