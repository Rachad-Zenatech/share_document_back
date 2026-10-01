import asyncio
import hashlib
import logging
import os
import re
import sys
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from functools import partial
from typing import List, Optional
from uuid import UUID, uuid4

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastmcp import FastMCP
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from postgresql_db.database import close_pool, create_pool, fetch_one, get_pool
from services.logging_service import (
    asyncio_exception_handler,
    configure_logging,
    log_background_task_result,
    request_id_ctx,
)
from services.cache_service import start_cache_invalidation_listener
from services.auth_service import (
    current_user_id_ctx,
    get_current_user_id_dependency,
)

# FastAPI routers
from tools.search import router as search_router
from tools.rbac_router import router as rbac_router, get_current_user_id
from tools.auth_router import router as auth_router
from tools.notification_router import notification_router
from tools.observability import router as observability_router
from tools.log_router import router as log_router
from tools.graph_router import router as graph_router
from tools.sec_filings import router as sec_filings_router
from services.sec_filing_template_service import bootstrap_sec_filing_table_templates

# MCP tools & AI functions
from mcp_tools import register_all as register_mcp_tools, ask_gemini, ask_gemini_stream, sync_mcp_tools_to_vector_db
from services.graph_sync_job import graph_sync_loop

configure_logging()
logger = logging.getLogger(__name__)


def _graph_background_sync_enabled() -> bool:
    return os.getenv("GRAPH_BACKGROUND_SYNC_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _env_flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application startup beginning", extra={"event": "application_starting"})
    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()
    loop.set_exception_handler(asyncio_exception_handler)
    try:
        await create_pool()
        # Ensure schema tables exist
        try:
            from postgresql_db.setup_pbac_schema import setup as setup_schema
            # Non-blocking schema initialization check
        except Exception as schema_err:
            logger.warning(f"Schema verification note: {schema_err}")
        try:
            await bootstrap_sec_filing_table_templates()
        except Exception as sec_err:
            logger.warning(f"SEC filing table templates bootstrap note: {sec_err}")
    except Exception:
        logger.exception("Application startup failed", extra={"event": "application_startup_failed"})
        raise

    background_tasks = []

    # 1. Start distributed cache invalidation listener (LISTEN/NOTIFY)
    cache_task = asyncio.create_task(start_cache_invalidation_listener(), name="cache-listener")
    cache_task.add_done_callback(
        partial(log_background_task_result, task_name="cache-listener")
    )
    background_tasks.append(cache_task)

    # 2. Sync MCP tools to PostgreSQL table
    sync_task = asyncio.create_task(sync_mcp_tools_to_vector_db(), name="mcp-tool-sync")
    sync_task.add_done_callback(
        partial(log_background_task_result, task_name="mcp-tool-sync")
    )
    background_tasks.append(sync_task)

    # 3. Microsoft Graph user sync (if enabled)
    if _graph_background_sync_enabled():
        graph_sync_task = asyncio.create_task(graph_sync_loop(), name="graph-user-sync")
        graph_sync_task.add_done_callback(
            partial(log_background_task_result, task_name="graph-user-sync")
        )
        background_tasks.append(graph_sync_task)
    else:
        logger.info(
            "Graph background sync disabled (set GRAPH_BACKGROUND_SYNC_ENABLED=true to enable)",
            extra={"event": "graph_sync_disabled"},
        )

    logger.info("Application startup complete", extra={"event": "application_started"})
    try:
        yield
    finally:
        logger.info("Application shutdown beginning", extra={"event": "application_stopping"})
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await close_pool()
        loop.set_exception_handler(previous_exception_handler)
        logger.info("Application shutdown complete", extra={"event": "application_stopped"})


_is_prod = os.getenv("APP_ENV", "development").lower() == "production"
HTTPS_MODE = _env_flag("HTTPS_MODE", "true" if _is_prod else "false")
_HSTS_ENABLED = HTTPS_MODE
_expose_docs = _env_flag("EXPOSE_API_DOCS", "false" if _is_prod else "true")

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https:; "
    "worker-src 'self' blob:; frame-ancestors 'none'"
    if _expose_docs
    else "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)

app = FastAPI(
    title="Share Document Portal API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _expose_docs else None,
    redoc_url="/redoc" if _expose_docs else None,
    openapi_url="/openapi.json" if _expose_docs else None,
)

# MCP server setup
mcp = FastMCP("Admin Portal MCP Server")
register_mcp_tools(mcp)


# --- AI Chat / Agent Endpoint ---
class MessageItem(BaseModel):
    role: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=20_000)

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    history: List[MessageItem] = Field(default_factory=list, max_length=50)
    file_data: Optional[str] = Field(default=None, max_length=20_000_000)
    mime_type: Optional[str] = Field(default=None, max_length=255)

@app.post("/ai/chat")
async def ai_chat(req: ChatRequest, user_id: UUID = Depends(get_current_user_id)):
    try:
        current_user_id_ctx.set(user_id)
        async def stream_generator():
            try:
                async for chunk in ask_gemini_stream(req.message, req.history, req.file_data, req.mime_type):
                    yield chunk
            except Exception as e:
                import json
                logger.exception("AI stream request failed", extra={"event": "ai_chat_stream_failed"})
                yield f"data: {json.dumps({'type': 'error', 'message': 'The AI service is temporarily unavailable.'})}\n\n"

        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )
    except Exception:
        logger.exception("AI chat request failed", extra={"event": "ai_chat_failed"})
        return JSONResponse(
            content={"reply": "The AI service is temporarily unavailable. Please try again."},
            status_code=500
        )


# --- Basic Endpoints ---
@app.get("/")
async def root():
    return {"status": "running", "service": "Admin Portal Backend Template"}

@app.get("/health/live", include_in_schema=False)
async def health_live():
    return {"status": "ok"}

@app.get("/health/ready", include_in_schema=False)
async def health_ready():
    try:
        await asyncio.wait_for(fetch_one("SELECT 1 AS ready"), timeout=2.0)
    except Exception as exc:
        logger.warning("Readiness check failed", extra={"event": "readiness_check_failed", "error_type": type(exc).__name__})
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ready"}

@app.get("/api/integration-status")
@app.get("/api/integration/status")
async def integration_status():
    db_ok = True
    try:
        await asyncio.wait_for(fetch_one("SELECT 1 AS ready"), timeout=2.0)
    except Exception:
        db_ok = False

    return {
        "status": "healthy" if db_ok else "degraded",
        "services": [
            {
                "id": "database_pool",
                "name": "PostgreSQL Connection Pool",
                "state": "CONNECTED" if db_ok else "DISCONNECTED",
                "status": "online" if db_ok else "offline",
                "description": "Asyncpg connection pool with Tier 1 in-memory caching",
                "mode": "Active Pool",
                "failure_count": 0 if db_ok else 1,
            },
            {
                "id": "realtime_sse",
                "name": "Real-time Notification Stream (SSE)",
                "state": "STREAMING",
                "status": "online",
                "description": "Server-Sent Events with 30s zero-DB keep-alive heartbeat",
                "mode": "Live Stream",
                "failure_count": 0,
            },
        ],
    }


# --- Routers ---
authenticated = [Depends(get_current_user_id_dependency)]

app.include_router(auth_router, prefix="/api", tags=["Authentication"])
app.include_router(rbac_router, prefix="/api", tags=["RBAC & Configuration"])
app.include_router(search_router, prefix="/api/search", tags=["Global Search"], dependencies=authenticated)
app.include_router(notification_router, prefix="/api", tags=["Notifications"], dependencies=authenticated)
app.include_router(log_router, prefix="/api", tags=["Logs"])
app.include_router(observability_router, prefix="/api", tags=["Observability"])
app.include_router(graph_router, prefix="/api", tags=["Microsoft Graph"], dependencies=authenticated)
app.include_router(sec_filings_router, prefix="/api", tags=["SEC Filings"])


# --- Session Middleware ---
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "default_session_secret_key_needs_32_characters!"),
    same_site="lax",
    https_only=os.getenv("SESSION_COOKIE_SECURE", "true" if _is_prod else "false").lower() in ("true", "1", "yes"),
    max_age=3600,
)


# --- Rate Limiting Middleware ---
_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
_RATE_LIMIT_DEFAULT = int(os.getenv("RATE_LIMIT_REQUESTS_PER_WINDOW", "1200"))
_RATE_LIMIT_IP_CEILING = int(os.getenv("RATE_LIMIT_IP_CEILING_PER_WINDOW", "6000"))
_RATE_LIMIT_AUTH = int(os.getenv("RATE_LIMIT_AUTH_PER_WINDOW", "60"))
_RATE_LIMIT_ENABLED = _env_flag("RATE_LIMIT_ENABLED", "true")

_SENSITIVE_PREFIXES = (
    "/api/auth/dev-login",
    "/api/auth/microsoft/callback",
    "/api/auth/login",
    "/api/login",
    "/api/token",
)

_rate_buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
_RATE_SWEEP_EVERY = 1000
_rate_requests_since_sweep = 0

def _sweep_rate_buckets(cutoff: float) -> None:
    for key in [k for k, hits in _rate_buckets.items() if not any(t > cutoff for t in hits)]:
        del _rate_buckets[key]

def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded and _env_flag("TRUST_FORWARDED_FOR", "false"):
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def _session_identity(request: Request) -> Optional[str]:
    token = ""
    auth_header = request.headers.get("Authorization", "")
    if auth_header[:7].lower() == "bearer ":
        token = auth_header[7:].strip()
    if not token:
        from services.auth_service import AUTH_COOKIE_NAME
        token = request.cookies.get(AUTH_COOKIE_NAME, "") or request.cookies.get("zenatech_access_token", "")
    if not token:
        return None
    return "session:" + hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()[:32]

@app.middleware("http")
async def rate_limit(request: Request, call_next):
    if not _RATE_LIMIT_ENABLED or request.method == "OPTIONS":
        return await call_next(request)

    path = request.url.path
    if path.startswith("/health"):
        return await call_next(request)

    source_ip = _client_ip(request)
    identity = _session_identity(request)

    if path.startswith(_SENSITIVE_PREFIXES):
        checks = [(("ip:" + source_ip, "auth"), _RATE_LIMIT_AUTH)]
    elif identity:
        checks = [
            ((identity, "session"), _RATE_LIMIT_DEFAULT),
            (("ip:" + source_ip, "ceiling"), _RATE_LIMIT_IP_CEILING),
        ]
    else:
        checks = [(("ip:" + source_ip, "anon"), _RATE_LIMIT_DEFAULT)]

    now = time.monotonic()
    cutoff = now - _RATE_LIMIT_WINDOW_SECONDS

    global _rate_requests_since_sweep
    _rate_requests_since_sweep += 1
    if _rate_requests_since_sweep >= _RATE_SWEEP_EVERY:
        _rate_requests_since_sweep = 0
        _sweep_rate_buckets(cutoff)

    exceeded = None
    for bucket_key, budget in checks:
        hits = _rate_buckets[bucket_key]
        hits[:] = [t for t in hits if t > cutoff]
        if len(hits) >= budget:
            exceeded = (bucket_key, budget, hits[0])
            break

    if exceeded is None:
        for bucket_key, _ in checks:
            _rate_buckets[bucket_key].append(now)

    if exceeded is not None:
        bucket_key, budget, oldest_hit = exceeded
        retry_after = max(1, int(oldest_hit - cutoff))
        logger.warning(
            "Rate limit exceeded",
            extra={
                "event": "rate_limit_exceeded",
                "path": path,
                "scope": bucket_key[1],
                "budget": budget,
                "window_seconds": _RATE_LIMIT_WINDOW_SECONDS,
            },
        )
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests. Please slow down."},
            headers={"Retry-After": str(retry_after)},
        )

    return await call_next(request)


# --- Security Headers Middleware ---
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    if _HSTS_ENABLED:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


# --- Request Observability & Tracing Middleware ---
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

def _slow_request_threshold_ms() -> int:
    try:
        configured = int(os.getenv("SLOW_REQUEST_MS", "2000"))
    except (TypeError, ValueError):
        configured = 2000
    return max(100, configured)

SLOW_REQUEST_THRESHOLD_MS = _slow_request_threshold_ms()

def _should_log_request_completion(path: str, status_code: int, duration_ms: float) -> bool:
    if path in {"/health/live", "/health/ready"}:
        return False
    return status_code >= 400 or duration_ms >= SLOW_REQUEST_THRESHOLD_MS

@app.middleware("http")
async def request_observability(request: Request, call_next):
    supplied_request_id = request.headers.get("x-request-id", "")
    request_id = (
        supplied_request_id
        if _REQUEST_ID_PATTERN.fullmatch(supplied_request_id)
        else str(uuid4())
    )
    token = request_id_ctx.set(request_id)
    started = time.perf_counter()
    client_ip = request.client.host if request.client else None
    trace_id = request.headers.get("x-amzn-trace-id", "")[:256] or None
    response = None
    unhandled = False
    try:
        response = await call_next(request)
    except (asyncio.CancelledError, ConnectionResetError):
        request_id_ctx.reset(token)
        return JSONResponse(status_code=499, content={"detail": "Client closed request"})
    except Exception:
        unhandled = True
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.exception(
            "Unhandled request exception",
            extra={
                "event": "unhandled_request_exception",
                "request_id": request_id,
                "trace_id": trace_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": 500,
                "duration_ms": duration_ms,
                "client_ip": client_ip,
            },
        )
        response = JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "request_id": request_id},
        )
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)

    response.headers["X-Request-ID"] = request_id
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    log_level = logging.WARNING if duration_ms >= SLOW_REQUEST_THRESHOLD_MS else logging.INFO
    if response.status_code >= 500 and not unhandled:
        log_level = logging.ERROR
    path = request.url.path
    if _should_log_request_completion(path, response.status_code, duration_ms):
        logger.log(
            log_level,
            "Request completed",
            extra={
                "event": "request_completed",
                "request_id": request_id,
                "trace_id": trace_id,
                "method": request.method,
                "path": path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "client_ip": client_ip,
            },
        )
    request_id_ctx.reset(token)
    return response


# --- CORS Configuration ---
_cors_env = os.getenv("CORS_ALLOWED_ORIGINS", "") or os.getenv("CORS_ORIGINS", "")
cors_origins = [orig.strip() for orig in _cors_env.split(",") if orig.strip()] if _cors_env else []
_cors_regex = None if _is_prod else r"^https?://(localhost|127\.0\.0\.1|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.\d+\.\d+\.\d+)(:\d+)?$"

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_origin_regex=_cors_regex,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=os.getenv("MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("MCP_PORT", "8002")),
    )
