# Antigravity Project Instructions

Use these rules when changing the enterprise backend or its internal portal.

## Workspace & Stack

- Backend: `internal_portal_administration_backend` (`server.py`, `tools/`, `services/`, `models/`, `postgresql_db/`)
- FastAPI routers belong in `tools/`, async business and database logic in `services/`, Pydantic schemas in `models/`, and database helpers in `postgresql_db/database.py`.
- Keep backend request and response contracts synchronized with frontend `src/types`.

## Core Architecture & Services

### Database Connection Pool (`postgresql_db/database.py`)
- **Synchronous Pool Access:** `get_pool()` is a synchronous helper returning `_pool: asyncpg.Pool`. **NEVER** write `await get_pool()`.
- **Acquiring Connections:** Always use `pool = get_pool()` followed by `async with pool.acquire() as conn:`.
- **Configurable Limits:** Pool parameters (`DB_POOL_MIN_SIZE`, `DB_POOL_MAX_SIZE`, `DB_POOL_MAX_QUERIES`, `DB_POOL_MAX_INACTIVE_LIFETIME`, `DB_COMMAND_TIMEOUT`, `DATABASE_SSL`) must be loaded from `.env` with fallback defaults.

### Tier 1 In-Memory Caching & Event Invalidation (`services/cache_service.py`)
- **Local In-Memory Cache:**
  - `permission_cache` (60s TTL) for `get_my_permissions(user_id)`.
  - `master_data_cache` (300s TTL) for reference/master data.
  - `config_cache` (300s TTL) for `get_all_roles()`, `get_role_tree()`, `get_permission_modules()`.
- **Distributed PostgreSQL LISTEN/NOTIFY Pub/Sub:**
  - Whenever a mutation occurs (CRUD on users, roles, permissions, master data), **always** call `await emit_cache_invalidation(scope="...", key="...")`.
  - Do not bypass the event system with raw in-memory mutations.

## Database & SQL Performance Standards

### Eliminating N+1 Queries
- **NEVER execute SQL queries inside loops.**
- Use PostgreSQL `UNNEST` or single atomic batch statements for multi-row operations:
  ```python
  # Good (Batch atomic update)
  await conn.execute("""
      INSERT INTO user_roles (user_id, role_id, assigned_at, assigned_by, is_active)
      SELECT $1, unnest($2::uuid[]), now(), $3, true
      ON CONFLICT (user_id, role_id)
      DO UPDATE SET is_active = true, assigned_at = now()
  """, user_id, role_ids, actor_id)
  ```
- Use asyncpg placeholders `$1, $2, ...`. Never interpolate user-supplied values into SQL.
- Use transactions for related writes, imports, saves, and other atomic operations.

## Validation & Cleanup

- After backend changes, run `wsl python3 -m py_compile <touched files>`.
- Run `git diff --check` on touched files before reporting completion.
- Any task-specific test, probe, fixture, snapshot, generated output, or scratch file created by an LLM is temporary by default. Run validation and remove temporary files before finishing.
- Never delete or modify pre-existing repository tests merely as cleanup.
