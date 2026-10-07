# Collaborative Document & SEC Filings Backend (`share_document_back`)

Enterprise collaborative document authoring, SEC regulatory filing generator, spreadsheet integration hub, and mobile cryptographic signature backend for ZenaTech. Built with **FastAPI**, **PostgreSQL** (`asyncpg`), **Alembic**, **Google Gemini AI**, **Microsoft Graph API**, and **Server-Sent Events (SSE)**.

---

## System Architecture Diagram

```mermaid
flowchart TD
    subgraph Clients [Client Applications]
        WebPortal[Web App :5176 Desktop Browser]
        MobileSigner[Mobile Device QR Code Signer]
        GraphSync[Microsoft Entra & OneDrive Sync]
    end

    subgraph FastAPIService [FastAPI Backend Service :8006]
        AuthRouter[Auth & Microsoft SSO Router]
        SecRouter[SEC Filings & Block Builder Router]
        SpreadsheetRouter[Spreadsheet Hub & Formula Router]
        SignRouter[Mobile Digital Signature Router]
        NotificationRouter[SSE Real-Time Stream Router]
        RBACRouter[PBAC & Permission Matrix Router]
        LogRouter[Audit & System Log Router]
    end

    subgraph CoreServices [Business Services Layer]
        SecTemplateService[SEC Filing Template & Block Engine]
        SecSpreadsheetService[Live Spreadsheet & Cell Linker]
        SecSignatureService[Mobile Signature & Token Validator]
        GeminiService[Google Gemini AI Assistant Service]
        GraphService[Microsoft Graph API Sync Service]
        RBACService[PBAC Security Policy Service]
        AuditService[Audit Log & Diff Tracker]
        CacheService[PostgreSQL In-Memory L1 Cache + NOTIFY]
    end

    subgraph Storage [Database & Cloud Storage]
        PostgreSQL[(PostgreSQL asyncpg with JSONB)]
        Alembic[Alembic Database Migrations]
        S3Storage[(AWS S3 Document & Image Bucket)]
    end

    WebPortal -->|REST & Cookie Session| FastAPIService
    MobileSigner -->|QR Signature Token /sign| SignRouter
    GraphSync -->|Webhook / Sync Job| FastAPIService

    FastAPIService --> CoreServices
    CoreServices --> GeminiService
    CoreServices --> GraphService
    CoreServices --> PostgreSQL
    CoreServices --> S3Storage
    CoreServices --> CacheService
```

---

## Technologies & System Specifications

| Category | Technology | Description |
| :--- | :--- | :--- |
| **Framework & Engine** | [FastAPI](https://fastapi.tiangolo.com/), [Uvicorn](https://www.uvicorn.org/) | High-throughput asynchronous ASGI web server running on port `8006` |
| **Language** | Python 3.12+ | Asynchronous event loop with typed Pydantic v2 schemas |
| **Database & ORM** | [PostgreSQL](https://www.postgresql.org/), [asyncpg](https://github.com/MagicStack/asyncpg), [Alembic](https://alembic.sqlalchemy.org/) | Connection pooling, multi-statement transactions, JSONB document trees, and schema revisions |
| **AI & LLM Services** | [Google GenAI SDK](https://github.com/google/generative-ai-python) (`google-genai`) | Automated section drafting, disclosure generation, and filing consistency checking |
| **Enterprise Cloud Sync**| [Microsoft Graph API](https://developer.microsoft.com/en-us/graph), `msal` | Synchronizing live tables and reports with OneDrive & SharePoint |
| **Real-Time Streaming**| [Server-Sent Events (SSE)](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events) | Zero-polling collaborative block update broadcasts and signature completions |
| **Document Formats** | `python-docx`, `openpyxl`, `reportlab`, `pypdf` | Exporting regulatory filings into styled Word (.docx), Excel (.xlsx), and PDF formats |
| **Security & Auth** | `Authlib`, `PyJWT`, `passlib`, `cryptography` | Microsoft Entra ID OAuth 2.0 PKCE, HttpOnly secure cookies, PBAC route policies |
| **Cloud Storage** | AWS S3 (`boto3`) | Encrypted storage for document attachments, signatures, and filing artifacts |

---

## Key Modules & Capabilities

1. **Structured Block Builder**:
   - Section hierarchies, headings, dynamic tables, markdown text, and variable interpolations.
   - Proposal creation, inline commenting, merge reviews, and full revision history tracking.
2. **Spreadsheet Hub & Dynamic Cell Linking**:
   - Link live spreadsheet values directly into regulatory text (e.g. balance sheet line items in 10-Q/10-K disclosures).
   - Cell formula evaluation, sheet dependencies, and automated refresh triggers.
3. **Mobile Cryptographic Signer**:
   - QR code generation for document signing sessions.
   - Mobile canvas touch/stylus signature capture, IP validation, timestamping, and SHA-256 integrity sealing.
4. **SEC Filing Life Cycle**:
   - Draft, Contributor Review, Legal Review, Executive Signoff, and Export packages (EDGAR-ready format).
5. **RBAC & Granular PBAC**:
   - Module access controls (`SEC_FILINGS:VIEW`, `SEC_FILINGS:CREATE`, `SEC_FILINGS:SIGN`, `LOGS:VIEW`).

---

## Directory Structure

```text
share_document_back/
+-- alembic/                    # Database migration scripts and versions
+-- models/                     # Pydantic v2 domain schemas
�   +-- sec_filing_model.py     # Filing, block, proposal, and signature models
�   +-- rbac_model.py           # User and role permission definitions
�   +-- notification_model.py   # SSE event payloads
+-- postgresql_db/              # Database connection pool & table setup
�   +-- database.py             # asyncpg connection pooling
�   +-- sec_filing_schema.py    # PostgreSQL DDL statements
+-- services/                   # Business logic implementations
�   +-- sec_filing_template_service.py   # Document rendering and block tree
�   +-- sec_filing_spreadsheet_service.py# Dynamic spreadsheet engine
�   +-- sec_filing_signature_service.py  # QR codes and mobile signing
�   +-- gemini_service.py       # AI generation and review workflows
�   +-- graph_service.py        # Microsoft Graph sync service
�   +-- auth_service.py         # Session cookies & Entra ID OAuth
�   +-- rbac_service.py         # Permission checking
+-- tools/                      # FastAPI endpoint routers
�   +-- sec_filings.py          # /api/sec-filings endpoints
�   +-- auth_router.py          # Authentication routes
�   +-- graph_router.py         # Microsoft Graph endpoints
�   +-- notification_router.py  # SSE stream endpoint
+-- server.py                   # Application entry point
+-- requirements.txt
+-- run_server.sh
```

---

## API Endpoints Overview

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/health/live` | Health probe for load balancers |
| `GET` | `/api/sec-filings` | List regulatory filings with metadata and status |
| `POST` | `/api/sec-filings` | Create a new filing draft |
| `GET` | `/api/sec-filings/{id}/blocks` | Fetch ordered block tree for a filing |
| `POST` | `/api/sec-filings/{id}/blocks` | Add an inline block (text, table, formula, callout) |
| `POST` | `/api/sec-filings/{id}/proposals` | Submit a merge proposal for review |
| `GET` | `/api/sec-filings/{id}/signing-session` | Generate QR code session for mobile signing |
| `POST` | `/api/sec-filings/sign/{token}` | Submit cryptographic digital signature from mobile |
| `POST` | `/ai/generate-disclosure` | AI-assisted disclosure drafting via Gemini |
| `GET` | `/api/notifications/stream` | Real-time SSE collaboration event stream |

---

## Local Development & Setup

### 1. Python Virtual Environment
```bash
python3 -m venv venv
# On Windows:
venv\Scripts\activate
# On Linux / WSL:
source venv/bin/activate
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Environment Variables (.env)
```env
PORT=8006
FRONTEND_URL=http://localhost:5176
DATABASE_URL=postgresql://postgres:postgres@localhost:3004/zenatech_share_document
DATABASE_SSL=false
SESSION_SECRET=your-32-character-session-secret-key
GEMINI_API_KEY=your-gemini-api-key
MICROSOFT_CLIENT_ID=your-azure-client-id
MICROSOFT_CLIENT_SECRET=your-azure-client-secret
MICROSOFT_TENANT_ID=common
AWS_ACCESS_KEY_ID=your-aws-access-key
AWS_SECRET_ACCESS_KEY=your-aws-secret-key
AWS_REGION=us-west-2
S3_BUCKET_NAME=zenatech-document-filings
```

### 4. Run Server
```bash
uvicorn server:app --host 0.0.0.0 --port 8006 --reload
```
API Documentation: `http://localhost:8006/docs`.


## Local Database (Docker), DataGrip & Migrations

Each project in the ZenaTech ecosystem runs an isolated local PostgreSQL container mapped to a dedicated 3000-series host port to prevent port collisions.

### 1. Local Database Configuration
* **Container Name**: `share_document_db_local`
* **Host Port**: `localhost:3004` (mapped to internal PostgreSQL 5432)
* **Database Name**: `zenatech_share_document`
* **User / Password**: `postgres` / `postgres`
* **Connection String**: `postgresql://postgres:postgres@localhost:3004/zenatech_share_document`

### 2. Managing the Local Database
```bash
# Start local database container
docker compose up -d db

# Stop local database container
docker compose down
```
### 3. Connecting in DataGrip
1. Create a new **PostgreSQL** Data Source in DataGrip.
2. Settings:
   * **Host**: `localhost`
   * **Port**: `3004`
   * **Database**: `zenatech_share_document`
   * **User**: `postgres`
   * **Password**: `postgres`
3. Click **Test Connection** and apply.

### 4. Syncing Latest Live Data from AWS RDS (Optional)
To pull a copy of real live records from AWS RDS into your local Docker DB for realistic development:
```powershell
.\scripts\pull_prod_to_local.ps1
```
* Safely downloads an RDS snapshot and restores it into `localhost:3004`.
* Read-only pull: live AWS RDS remains untouched and safe.

### 5. Creating Schema Changes & Migrations (PR Workflow)
When making table or column changes on a new branch:

1. **Create and Switch to Your New Feature Branch**:
   ```bash
   git checkout -b feature/your-feature-name
   ```
   *(The Git `post-checkout` hook in `.git/hooks/post-checkout` automatically triggers to ensure your local database is synced and ready).*

2. **Sync Live Data into Local DataGrip (Optional / Recommended)**:
   ```powershell
   .\scripts\pull_prod_to_local.ps1
   ```
   *(Safely pulls real current rows into your local Docker DB on port 3004 so you can build and test queries with actual data).*

3. **Update Code Schema**:
   Edit `postgresql_db/schema_metadata.py` to add or modify columns/tables.

4. **Auto-Generate Migration Diff**:
   ```bash
   alembic revision --autogenerate -m "describe changes"
   ```
   Alembic inspects your local database and writes a versioned migration in `alembic/versions/` containing **only the differences**.

5. **Apply & Test Locally**:
   ```bash
   alembic upgrade head
   ```
   Verify your changes and tables in DataGrip on `127.0.0.1:3004` with your test data.

6. **Submit in PR**:
   Commit and push:
   * `postgresql_db/schema_metadata.py`
   * `alembic/versions/<revision_id>_*.py`
   *(Local test data stays on your machine; only the table structure diff is merged to production!)*

### 6. Disposable Database Reset
To tear down and recreate your local container from scratch to match the current branch:
```powershell
.\scripts\reset_local_db.ps1
```
* A Git `post-checkout` hook is installed in `.git/hooks/post-checkout` to trigger this automatically on branch switches.
* Built-in safety guards immediately block resets if `DATABASE_URL` points to a remote AWS RDS host.



