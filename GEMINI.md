# Agent Guidelines & Repository Rules

This document defines the architectural standards, performance requirements, and execution guidelines for all AI agents working on this codebase.

---

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

## 1. Core Architecture & Services

### Database Connection Pool (`postgresql_db/database.py`)
* **Synchronous Pool Access:** `get_pool()` is a synchronous helper returning `_pool: asyncpg.Pool`. **NEVER** write `await get_pool()`.
* **Acquiring Connections:** Always use `pool = get_pool()` followed by `async with pool.acquire() as conn:`.
* **Configurable Limits:** Pool parameters (`DB_POOL_MIN_SIZE`, `DB_POOL_MAX_SIZE`, `DB_POOL_MAX_QUERIES`, `DB_POOL_MAX_INACTIVE_LIFETIME`, `DB_COMMAND_TIMEOUT`) must be loaded from `.env` with fallback defaults.

### Tier 1 In-Memory Caching & Event Invalidation (`services/shared/cache_service.py`)
* **Local In-Memory Cache:**
  * `permission_cache` (60s TTL) for `get_my_permissions(user_id)`.
  * `master_data_cache` (300s TTL) for reference/master data.
  * `config_cache` (300s TTL) for `get_all_roles()`, `get_role_tree()`, `get_permission_modules()`.
* **Distributed PostgreSQL LISTEN/NOTIFY Pub/Sub:**
  * Whenever a mutation occurs (CRUD on users, roles, permissions, master data), **always** call `await emit_cache_invalidation(scope="...", key="...")`.
  * Do not bypass the event system with raw in-memory mutations.

### Server-Sent Events (SSE) & Real-Time Event Streaming
* **"Wait for Event" Model Only (Mandatory):**
  * **NEVER** use polling loops (`check -> sleep -> check`) or database polling heartbeats inside SSE streaming endpoints.
  * **NEVER** query the database repeatedly inside SSE stream generators.
  * **Always** use `await event_queue.get()` (or `asyncio.wait_for(q.get(), timeout=30.0)` for lightweight ping) to suspend the coroutine at the event loop level with zero CPU/DB overhead until a published event arrives.

* **Architecture Recommendation:**
  > **SSE + event-driven backend + 30–60 second lightweight heartbeat + automatic reconnect.**
  >
  > Don't use the heartbeat to poll the database.
  >
  > That gives you:
  > ```text
  > No notification
  >       ↓
  > Server mostly waits
  >       ↓
  > Tiny heartbeat occasionally
  >       ↓
  > Notification occurs
  >       ↓
  > Immediate SSE event
  > ```
  > That is a much better balance between server load and connection reliability.
  > And your CloudFront test strongly suggests that completely silent SSE connections are not viable in your current setup.

  * Standard SSE generator implementation (with lightweight zero-DB heartbeat):
    ```python
    async def event_generator():
        q = asyncio.Queue()
        broadcaster.add_listener(q)
        try:
            yield ": connected

"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=30.0)
                    if msg.user_id == "*" or str(msg.user_id).lower() == str(user_id).lower():
                        yield f"data: {msg.model_dump_json()}

"
                except asyncio.TimeoutError:
                    # Lightweight keep-alive comment/ping to prevent CloudFront/proxy timeouts without querying the database
                    yield ": ping

"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            broadcaster.remove_listener(q)
    ```

---

## 2. Database & SQL Performance Standards

### Eliminating N+1 Queries
* **NEVER execute SQL queries inside loops.**
* Use PostgreSQL `UNNEST` or single atomic batch statements for multi-row operations:
  ```python
  # Good (Batch atomic update)
  await conn.execute("""
      INSERT INTO user_roles (user_id, role_id, assigned_at, assigned_by, is_active)
      SELECT $1, unnest($2::uuid[]), now(), $3, true
      ON CONFLICT (user_id, role_id)
      DO UPDATE SET is_active = true, assigned_at = now()
  """, user_id, role_ids, actor_id)
  ```

---

## 3. Checks & Verifications

* Python: run `wsl python3 -m py_compile <touched files>` after backend changes.
* Run `git diff --check` on touched files.
* LLM-created task-specific test, probe, fixture, snapshot, and scratch files are temporary by default. Run them, record the result, and remove them before finishing.
* Never remove or rewrite pre-existing repository tests as cleanup.

---

## 4. Architecture Notice: Web SPA vs Universal App (Expo + Vite)

> **CRITICAL PRE-SETUP DECISION**: This choice must be made **BEFORE** using the template to write application features.
> - **Option 1 (Default)**: Pure Web SPA (React 19 + Vite + Tailwind CSS v4) matching Admin & Finance portals.
> - **Option 2**: Universal Web + Mobile App (Expo 57 + React Native Web + Vite) matching CEO Dashboard.
>
> If the project requires mobile support (iOS/Android), convert the frontend immediately upon initial project setup before writing UI components to avoid refactoring later.

### Prompt Instructions to Convert Frontend into Universal Web + App:
When the user asks to convert the companion frontend (`Template_Automation_Front`) into a Universal Web + Mobile App like CEO Dashboard, execute the following:
1. **Dependencies**: In `package.json`, add Expo (v57+), React Native (0.86+), React Native Web (0.21+), `@react-navigation/native`, `@react-navigation/native-stack`, `expo-linking`, `expo-web-browser`, `react-native-safe-area-context`, `react-native-screens`, `react-native-svg`, and `concurrently`.
2. **Dual-Runner Scripts**: Configure concurrent Expo Metro bundler on port `:8090` and Vite web server on port `:6000`:
   ```json
   "start": "concurrently -n \"EXPO,WEB\" -c \"cyan,magenta\" \"expo start --host lan --port 8090\" \"vite\"",
   "dev": "vite",
   "ios": "expo start --ios --host lan --port 8090",
   "android": "node scripts/start-android.js"
   ```
3. **Vite Bundler Aliases**: Update `vite.config.ts` with `react-native-web` aliases and `codegenShim`.
4. **Navigation Container**: Wrap native mobile entrypoints with React Navigation `<NavigationContainer>` while maintaining the web React Router hierarchy.
