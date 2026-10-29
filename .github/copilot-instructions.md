# GitHub Copilot Instructions

- Use FastAPI routers in `tools/`, services in `services/`, models in `models/`, and database helpers in `postgresql_db/database.py`.
- Synchronous database pool access: `pool = get_pool()`.
- Use the caching layer (`services.shared.cache_service`) for high-frequency queries and emit invalidation events on data modification.
- Never write SQL queries inside loops; batch multi-row operations using PostgreSQL `UNNEST`.
- Validate code with `wsl python3 -m py_compile <files>`.

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
