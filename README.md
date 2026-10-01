# Enterprise Portal Administration Backend Template

[![Python Version](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-14%2B%20%2F%2016-336791.svg)](https://www.postgresql.org/)
[![asyncpg](https://img.shields.io/badge/driver-asyncpg%200.30%2B-2b5b84.svg)](https://github.com/MagicStack/asyncpg)
[![FastMCP](https://img.shields.io/badge/MCP-FastMCP%203.3%2B-orange.svg)](https://github.com/jlowin/fastmcp)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg)](https://www.docker.com/)

A production-ready, high-concurrency enterprise FastAPI backend template featuring hierarchical Role & Permission-Based Access Control (RBAC & PBAC), Microsoft Entra ID SSO, tier-1 distributed in-memory caching with PostgreSQL `LISTEN`/`NOTIFY` invalidation, real-time Server-Sent Events (SSE), FastMCP AI integration, and comprehensive audit observability.

> [!IMPORTANT]
> **Architecture Decision Required Before Using Template (Web SPA vs Universal App)**
> **Make this choice BEFORE building your application features:**
> 1. **Pure Web Application (Default)**: Keep as React 19 + Vite + Tailwind CSS v4 SPA (matching Admin & Finance portals).
> 2. **Universal Web + Mobile App**: Convert to Expo 57 + React Native Web + Vite (matching CEO Dashboard) *prior to writing any UI code* to avoid component refactoring.
>
> *To convert the frontend into a Universal Web + Mobile App, run the conversion prompt provided in [AGENTS.md](file:///c:/dev/template/back/Template_Automation_Back/AGENTS.md) or the Frontend README immediately upon project setup.*

---

## 🌟 Key Features

- **🛡️ Granular RBAC & PBAC**: Hierarchical roles with parent-child trees, permission modules, fine-grained action mapping (`VIEW`, `CREATE`, `UPDATE`, `DELETE`, `EXECUTE`, `APPROVE`), frontend navigation gating, and AI tool permissions.
- **⚡ High-Performance Async Architecture**: Powered by raw `asyncpg` connection pooling with synchronous pool retrieval (`get_pool()`) and strict N+1 query prevention standards.
- **🧠 Tier 1 In-Memory Caching & Pub/Sub Invalidation**:
  - High-frequency permissions cache (60s TTL) for near-instant authorization checks.
  - Multi-instance distributed cache invalidation backed by PostgreSQL `LISTEN`/`NOTIFY`.
- **📡 Event-Driven Real-Time SSE**:
  - Pure event queue dispatching (`event_queue.get()`) without wasteful database polling loops.
  - 30-second zero-DB keep-alive heartbeat (`: ping\n\n`) ensuring persistent connections through CloudFront, proxies, and load balancers.
- **🤖 FastMCP & Gemini AI Integration**: FastMCP server integration exposing context-aware tools and prompts governed by role permissions.
- **🔒 Enterprise Security Standards**:
  - Secure `HttpOnly`, `SameSite=Lax` JWT authentication cookies with automatic user activity tracking.
  - Microsoft Entra ID (Azure AD) OAuth2 SSO with configurable domain filtering and optional background profile synchronization.
  - Built-in local developer mock authentication for offline/local workflows.
  - Production-grade security headers (CSP, HSTS, X-Frame-Options, X-Content-Type-Options) and CORS protection.
- **📊 Observability & Audit Logging**: Structured JSON logging with request tracing contextvars, slow-request detectors, client telemetry endpoints, and database-backed audit logging.
- **🔄 Database Migrations & Seeding**: Alembic migration setup alongside idempotent standalone bootstrap scripts for tables, triggers, initial modules, and super-admin accounts.

---

## 🏛️ Architecture Overview

```mermaid
flowchart TB
    subgraph Clients["Clients & Frontend"]
        SPA["Single Page Application (React / Vue)"]
        AI["AI Agents / Claude / Cursor (MCP)"]
    end

    subgraph Gateway["FastAPI Server (:8900)"]
        AuthMiddleware["Auth & Security Middleware\n(JWT Cookies / Headers)"]
        
        subgraph Routers["API Routers"]
            AuthR["/api/auth"]
            RbacR["/api/configuration"]
            NotifR["/api/notifications (SSE)"]
            SearchR["/api/search"]
            LogR["/api/logs"]
            ObsR["/api/observability"]
        end
        
        subgraph Services["Core Services & Tier 1 Cache"]
            CacheService["Cache Service\n(In-Memory TTL)"]
            AuthService["Auth & PBAC Resolver"]
            NotifService["Notification Broadcaster\n(asyncio.Queue)"]
            AuditService["Audit Logger"]
        end
        
        FastMCPServer["FastMCP Server (:8002)\nTools & Prompts Registry"]
    end

    subgraph Database["PostgreSQL 14+ (:5432)"]
        AsyncPool["asyncpg Connection Pool"]
        PubSub["PostgreSQL LISTEN / NOTIFY\n(cache_invalidation channel)"]
        Tables[("Tables:\nusers, roles, permissions,\nnotifications, audit_logs")]
    end

    subgraph External["External Services"]
        EntraID["Microsoft Entra ID / Graph API"]
        GeminiAPI["Google Gemini AI"]
    end

    SPA -->|HTTP / SSE| AuthMiddleware
    AI -->|SSE / STDIO| FastMCPServer
    AuthMiddleware --> Routers
    Routers --> Services
    FastMCPServer --> Services
    Services --> AsyncPool
    AsyncPool --> Tables
    
    Services -.->|Invalidation Events| PubSub
    PubSub -.->|Instant Eviction| CacheService
    
    AuthService -.-> EntraID
    FastMCPServer -.-> GeminiAPI
```

---

## 📁 Repository Structure

```text
.
├── alembic/                      # Alembic migration configuration and versions
├── models/                       # Pydantic schemas and domain models
│   ├── notification_model.py     # Real-time notifications and SSE event models
│   └── rbac_model.py             # User, Role, PBAC, and Permission schemas
├── postgresql_db/                # Database layer
│   ├── database.py               # asyncpg connection pool & synchronous get_pool()
│   ├── notifications_schema.py   # Standalone notification schema definitions
│   ├── schema_metadata.py        # SQLAlchemy Core metadata mirror for Alembic
│   └── setup_pbac_schema.py      # Idempotent database schema & initial seed script
├── mcp_tools/                    # Model Context Protocol (MCP) tool registrations
│   ├── __init__.py               # FastMCP tool handlers and prompt definitions
│   ├── permissions.py            # MCP tool authorization validator
│   └── prompts.py                # Pre-configured LLM prompt templates
├── scripts/                      # Utility and automation scripts
│   └── seed_database.py          # Database initialization & super-admin seed entrypoint
├── services/                     # Business logic and cross-cutting concerns
│   ├── audit_service.py          # Structured audit trail logging service
│   ├── auth_service.py           # JWT auth, Microsoft SSO & PBAC permission resolver
│   ├── cache_service.py          # Local in-memory caches + PostgreSQL LISTEN/NOTIFY
│   ├── gemini_service.py         # Google Gemini LLM client with model fallback
│   ├── graph_service.py          # Microsoft Graph API client for user profile sync
│   ├── graph_sync_job.py         # Background worker for Microsoft Entra ID sync
│   ├── logging_service.py        # Contextual JSON logging & slow query tracing
│   ├── notification_service.py   # Event-driven SSE dispatcher & Notification broadcaster
│   ├── rbac_service.py           # User management, role hierarchy, login logs
│   └── service_security_policy.py# Security policy & action verification
├── tools/                        # FastAPI Route Handlers (Routers)
│   ├── auth_router.py            # /api/auth (Login, SSO, Dev login, Session, Logout)
│   ├── graph_router.py           # /api/graph (Microsoft Entra user autocomplete)
│   ├── log_router.py             # /api/logs (Structured audit logs API)
│   ├── notification_router.py    # /api/notifications & SSE streaming endpoint
│   ├── observability.py          # /api/observability (Client telemetry & metrics)
│   ├── rbac_router.py            # /api/configuration (Users, Roles, PBAC, Nav, MCP)
│   └── search.py                 # /api/search (Permission-filtered global search)
├── Dockerfile                    # Production multi-stage container build
├── docker-compose.yml            # Multi-service local environment (API + PostgreSQL)
├── requirements.txt              # Production and development dependencies
├── server.py                     # Unified FastAPI application entrypoint
├── run_server.sh                 # Production execution script
└── run_server_dev.sh             # Development startup script with hot reload
```

---

## 🚀 Getting Started

### Prerequisites
- **Python**: 3.11 or higher
- **PostgreSQL**: Version 14, 15, or 16
- **Docker & Docker Compose** *(optional, for containerized setup)*

---

### Option A: Local Development Setup

#### 1. Clone & Setup Virtual Environment

**Linux / macOS / WSL:**
```bash
git clone <repository-url>
cd template-back
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell):**
```powershell
git clone <repository-url>
cd template-back
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

#### 2. Configure Environment Variables

Copy the example environment configuration:
```bash
cp .env.example .env
```

Edit `.env` to match your local PostgreSQL credentials and configuration.

#### 3. Initialize & Seed Database Schema

Execute the initialization script to generate database tables, constraints, default roles, permission actions, and the initial super-admin user:

```bash
python scripts/seed_database.py
```

#### 4. Run the Development Server

```bash
# Direct uvicorn execution with hot-reloading:
python -m uvicorn server:app --host 0.0.0.0 --port 8900 --reload

# Or using the helper script:
chmod +x run_server_dev.sh
./run_server_dev.sh
```

---

### Option B: Docker Compose Setup

Run both the FastAPI application and PostgreSQL database inside Docker:

```bash
# Build and start services in background
docker compose up -d --build

# Run database seeding inside container
docker compose exec api python scripts/seed_database.py

# Check container logs
docker compose logs -f api
```

---

## ⚙️ Environment Configuration

| Variable | Default Value | Description |
| :--- | :--- | :--- |
| `APP_ENV` | `development` | Environment mode (`development`, `staging`, `production`) |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `SLOW_REQUEST_MS` | `2000` | Slow request warning threshold in milliseconds |
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/portal_template_db` | PostgreSQL asyncpg connection string |
| `DATABASE_SSL` | `false` | Enable SSL for PostgreSQL connection (`true`/`false`) |
| `DB_POOL_MIN_SIZE` | `2` | Minimum connection pool size |
| `DB_POOL_MAX_SIZE` | `10` | Maximum connection pool size |
| `DB_POOL_MAX_QUERIES` | `50000` | Max queries per connection before renewal |
| `DB_POOL_MAX_INACTIVE_LIFETIME` | `300.0` | Max inactive lifetime (seconds) before connection recycling |
| `DB_COMMAND_TIMEOUT` | `120.0` | Command execution timeout in seconds |
| `JWT_SECRET` | *(required)* | Secret key for signing JWT tokens (min 32 characters) |
| `SESSION_SECRET` | *(required)* | Secret key for session cookie encryption (min 32 characters) |
| `AUTH_TOKEN_TTL_SECONDS` | `7200` | JWT expiration duration in seconds (default: 2 hours) |
| `AUTH_COOKIE_NAME` | `access_token` | Name of the authentication cookie |
| `ALLOWED_EMAIL_DOMAINS` | `*` | Allowed email domains for SSO (e.g. `company.com` or `*`) |
| `INITIAL_SUPER_ADMIN_EMAILS` | `admin@example.com` | Initial super admin email(s) seeded on setup |
| `PORT` | `8900` | HTTP port for FastAPI backend service |
| `FRONTEND_URL` | `http://localhost:6000` | Allowed frontend URL for CORS and redirects |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:6000,...` | Comma-separated list of CORS origins |
| `JWT_ISSUER` | `zenatech-internal-portal` | JWT issuer identifier for cross-portal SSO |
| `AUTH_COOKIE_NAME` | `access_token` | Name of authentication cookie (`access_token` or `zenatech_access_token`) |
| `AUTH_COOKIE_SECURE` | `false` | Force HTTPS secure cookie (`true` in production) |
| `SESSION_COOKIE_SECURE` | `false` | Force HTTPS session cookie (`true` in production) |
| `ADMIN_PORTAL_API_URL` | `http://127.0.0.1:8002` | Zenatech Administration Portal API bridge URL |
| `FINANCE_PORTAL_API_URL` | `http://127.0.0.1:8001` | Enterprise System / Finance Portal API bridge URL |
| `MA_PORTAL_API_URL` | `http://127.0.0.1:8000` | M&A (M7A) Portal API bridge URL |
| `CEO_DASHBOARD_API_URL` | `http://127.0.0.1:8005` | CEO Executive Dashboard API bridge URL |
| `MQTT_HOST` / `MQTT_PORT` | `127.0.0.1` / `1883` | MQTT Broker for real-time presence & status discovery |
| `RATE_LIMIT_ENABLED` | `true` | Enable in-memory sliding window rate limiting |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Rate limiting evaluation window in seconds |
| `RATE_LIMIT_REQUESTS_PER_WINDOW`| `1200` | Max requests per window for authenticated session / IP |
| `RATE_LIMIT_IP_CEILING_PER_WINDOW` | `6000` | Max global requests per IP per window |
| `RATE_LIMIT_AUTH_PER_WINDOW` | `60` | Max attempts per window for sensitive auth endpoints |
| `TRUST_FORWARDED_FOR` | `false` | Trust `X-Forwarded-For` header for client IP resolution |
| `MICROSOFT_CLIENT_ID` | `""` | Microsoft Azure App Registration Client ID |
| `MICROSOFT_CLIENT_SECRET` | `""` | Microsoft Azure App Registration Client Secret |
| `MICROSOFT_TENANT_ID` | `common` | Microsoft Azure Tenant ID |
| `GEMINI_API_KEY` | `""` | Google Gemini API Key for AI features |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8002` | Host and port for FastMCP Server |

---

## 📡 API Endpoints Overview

| Area | Method & Route | Description | Auth / Permissions |
| :--- | :--- | :--- | :--- |
| **System** | `GET /health/live` | Service liveness health check | Public |
| **System** | `GET /docs` | Swagger / OpenAPI Interactive Documentation | Public |
| **Auth** | `POST /api/auth/dev-login` | Local development mock authentication | Dev mode only |
| **Auth** | `GET /api/auth/microsoft/login` | Initiate Microsoft Entra ID OAuth flow | Public |
| **Auth** | `GET /api/auth/microsoft/callback`| Microsoft OAuth callback & token generation | Public |
| **Auth** | `GET /api/auth/me` | Current authenticated user profile & permissions | Authenticated |
| **Auth** | `POST /api/auth/heartbeat` | Session heartbeat & activity updater | Authenticated |
| **Auth** | `POST /api/auth/logout` | Invalidate cookie session & log out | Authenticated |
| **RBAC** | `GET /api/configuration/users` | List portal users (search, filter, pagination) | `USERS:VIEW` |
| **RBAC** | `POST /api/configuration/users` | Create a new user | `USERS:CREATE` |
| **RBAC** | `GET /api/configuration/roles` | List all roles & role hierarchy tree | `ROLES:VIEW` |
| **RBAC** | `PUT /api/configuration/roles/{id}`| Update role details and permissions | `ROLES:UPDATE` |
| **RBAC** | `GET /api/configuration/permissions`| Get permission modules and available actions | `ROLES:VIEW` |
| **Events** | `GET /api/notifications/stream` | Real-time Server-Sent Events (SSE) stream | Authenticated |
| **Events** | `GET /api/notifications` | Fetch notification history and unread count | Authenticated |
| **Events** | `POST /api/notifications/{id}/read`| Mark notification as read | Authenticated |
| **Search** | `GET /api/search` | Permission-gated global portal search | Authenticated |
| **Logs** | `GET /api/logs` | Audit trail and system logs query | `LOGS:VIEW` |
| **Telemetry**| `POST /api/observability/client-log`| Ingest frontend errors and client metrics | Authenticated |

---

## 🧩 Architectural Standards & Best Practices

### 1. Connection Pool Access
- `get_pool()` from `postgresql_db/database.py` is **synchronous**. Never call `await get_pool()`.
- Connections must always be acquired using context managers:
  ```python
  from postgresql_db.database import get_pool

  pool = get_pool()
  async with pool.acquire() as conn:
      rows = await conn.fetch("SELECT * FROM users WHERE is_active = true")
  ```

### 2. Elimination of N+1 Queries
- Never execute database queries inside iteration loops.
- Use atomic `UNNEST` queries for batch multi-row insertions or updates:
  ```python
  await conn.execute("""
      INSERT INTO user_roles (user_id, role_id, assigned_at, assigned_by, is_active)
      SELECT $1, unnest($2::uuid[]), now(), $3, true
      ON CONFLICT (user_id, role_id)
      DO UPDATE SET is_active = true, assigned_at = now()
  """, user_id, role_ids, actor_id)
  ```

### 3. Tier 1 In-Memory Caching & Invalidation
- When modifying permissions, roles, or users, trigger cache invalidation to propagate across all instances:
  ```python
  from services.cache_service import emit_cache_invalidation

  # Automatically broadcasts via PostgreSQL NOTIFY
  await emit_cache_invalidation(scope="permissions", key=str(user_id))
  ```

### 4. Server-Sent Events (SSE) Guidelines
- Use queue-based awaiting (`await event_queue.get()`) to suspend coroutines with zero CPU and zero DB overhead.
- Include the lightweight 30s ping timeout to keep connections alive:
  ```python
  async def event_generator():
      q = asyncio.Queue()
      broadcaster.add_listener(q)
      try:
          yield ": connected\n\n"
          while True:
              try:
                  msg = await asyncio.wait_for(q.get(), timeout=30.0)
                  if msg.user_id in ("*", str(user_id)):
                      yield f"data: {msg.model_dump_json()}\n\n"
              except asyncio.TimeoutError:
                  yield ": ping\n\n"
      finally:
          broadcaster.remove_listener(q)
  ```

---

## 🛠️ Adding a New Domain Module

To add a new business domain (e.g. `products`, `orders`, `projects`):

1. **Define Pydantic Models**: Create request/response schemas in `models/your_module.py`.
2. **Define Database Tables**: Add table definitions in `postgresql_db/` or create an Alembic migration.
3. **Implement Service**: Write async database logic in `services/your_service.py` using `pool = get_pool()`.
4. **Create Router**: Implement endpoints in `tools/your_router.py` with permission dependencies:
   ```python
   from fastapi import APIRouter, Depends
   from services.service_security_policy import require_permission

   router = APIRouter(prefix="/api/projects", tags=["Projects"])

   @router.get("", dependencies=[Depends(require_permission("PROJECTS:VIEW"))])
   async def list_projects():
       ...
   ```
5. **Register Router in `server.py`**:
   ```python
   from tools.your_router import router as projects_router
   app.include_router(projects_router)
   ```
6. **Register Permission Actions**: Add domain permission modules and codes to `postgresql_db/setup_pbac_schema.py`.

---

## 🧪 Testing & Validation

```bash
# Validate Python syntax across all touched files (WSL / Linux):
python3 -m py_compile server.py services/*.py tools/*.py models/*.py

# Check git formatting and diff whitespace:
git diff --check
```

---

## 📄 License

This project is licensed under the MIT License.
