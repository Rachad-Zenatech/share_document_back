# Antigravity Project Instructions

Use these rules when changing the share-document backend (SEC filing documents, proposals, spreadsheets, notifications) or its frontend.

<!-- project-structure:start -->
## Project Structure

```text
server.py             # FastAPI app: startup/shutdown, middleware, router registration
tools/                # HTTP routers (FastAPI APIRouter), one file per feature
services/             # Business logic, grouped by feature (below)
  shared/             # Used by several features: auth, RBAC, caching, logging, audit, security
    audit_service.py, auth_service.py, cache_service.py
    logging_service.py, rbac_service.py, service_security_policy.py
  ai/                 # Gemini clients, assistant chat, guardrails, tracing, semantic cache, usage limits
    ai_guardrails.py, ai_observability.py, ai_usage_service.py
    assistant_chat_service.py, gemini_service.py, model_router.py
    semantic_cache.py
  notifications/      # Notifications and messaging (SSE, RabbitMQ, MQTT, cross-portal events)
    mqtt_presence.py, notification_service.py, rabbitmq_service.py
  microsoft_graph/    # Microsoft Graph (Entra users/org) client and background sync
    graph_service.py, graph_sync_job.py
  sec_filings/        # SEC filing documents: templates, workflow, signatures, spreadsheets, PDF import, guest access
    pdf_extraction_service.py, sec_document_service.py, sec_filing_signature_service.py
    sec_filing_spreadsheet_service.py, sec_filing_template_service.py, sec_filing_workflow_service.py
    sec_guest_service.py, sec_pdf_import_service.py
models/               # Pydantic request/response models
postgresql_db/        # database.py (asyncpg pool) and setup_*_schema.py table scripts
mcp_tools/            # MCP tools and prompts (also used by the AI assistant via run_tool)
alembic/              # Database migrations
scripts/              # Dev/ops scripts (seed, reset, pull prod data)
docs/                 # Longer design and integration docs
```

### Where new code goes

* **New feature `<x>`:** logic in `services/<x>/`, endpoints in `tools/<x>_router.py`, models in `models/`, tables in `postgresql_db/setup_<x>_schema.py`.
* **Code two or more features use** goes in `services/shared/` (or `services/ai/`, `services/notifications/` when it fits there). A feature folder must not import another feature's folder; move the common piece to `services/shared/` instead.
* **Never add files at the root of `services/`.** Every module lives in a feature or shared folder.
* **Imports are absolute** (`from services.shared.auth_service import ...`). The folders are namespace packages with no `__init__.py`, like the rest of the repo.
* **When you move a module,** update every import, `mock.patch("...")` string and doc path, and fix any `__file__`-relative path in it (one more folder deep means one more `dirname`).
<!-- project-structure:end -->

## Workspace & Stack

- Backend: `share_document_back` (`server.py`, `tools/`, `services/`, `models/`, `postgresql_db/`)
- Frontend: `../share_document_front` (React + Vite + TypeScript)
- FastAPI routers belong in `tools/`, async business and database logic in `services/`, Pydantic schemas in `models/`, and database helpers in `postgresql_db/database.py`.
- Keep backend request and response contracts synchronized with frontend `src/types`.
- `CLAUDE.md`, `AGENTS.md` and `GEMINI.md` hold the same rules for other agents; keep them consistent when changing one.

## Core Architecture & Services

### Database Connection Pool (`postgresql_db/database.py`)
- **Synchronous Pool Access:** `get_pool()` is a synchronous helper returning `_pool: asyncpg.Pool`. **NEVER** write `await get_pool()`.
- **Acquiring Connections:** Always use `pool = get_pool()` followed by `async with pool.acquire() as conn:`.
- **Configurable Limits:** Pool parameters (`DB_POOL_MIN_SIZE`, `DB_POOL_MAX_SIZE`, `DB_POOL_MAX_QUERIES`, `DB_POOL_MAX_INACTIVE_LIFETIME`, `DB_COMMAND_TIMEOUT`, `DATABASE_SSL`) must be loaded from `.env` with fallback defaults.

### Tier 1 In-Memory Caching & Event Invalidation (`services/shared/cache_service.py`)
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

## Real-Time Notifications

- Notifications reach the browser over the WebSocket at `/ws/notifications` (`tools/notification_ws_router.py`); CloudFront cuts streaming HTTP (SSE) responses after about 60 seconds.
- Event-driven only: wait on the broadcaster queue (`await asyncio.wait_for(queue.get(), timeout=...)`) with a lightweight ping on timeout. **Never** poll the database or loop `check -> sleep -> check` inside a stream.

## Access Rules

- Document endpoints check access per document (`services/sec_filings/sec_document_service.py`): owner, invitees, proposal authors, super admins.
- Guest contributors (no login) are limited to one proposal by their invite token (`services/sec_filings/sec_guest_service.py`). Never widen what a guest token can read or write.
- Never invent email addresses for invitees or proposal authors: an address grants access to the whole document.

## AI (Gemini)

- Call Gemini through `services/ai/gemini_service.py` (`generate_content_with_fallback`): it rotates through the text models in `GEMINI_AVAILABLE_MODELS` when one errors, is over quota or does not answer within `GEMINI_TIMEOUT_SECONDS`.
- Check AI output against the source before using it, as `services/sec_filings/sec_pdf_import_service.py` does for PDF import (words and figures must be kept; invented figures are rejected).

## Validation & Cleanup

- After backend changes, run `wsl python3 -m py_compile <touched files>`.
- Run `git diff --check` on touched files before reporting completion.
- Any task-specific test, probe, fixture, snapshot, generated output, or scratch file created by an LLM is temporary by default. Run validation and remove temporary files before finishing.
- Never delete or modify pre-existing repository tests merely as cleanup.
