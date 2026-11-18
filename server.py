import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from functools import partial
from typing import List, Optional
from uuid import UUID

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastmcp import FastMCP
from pydantic import BaseModel, Field

from postgresql_db.database import close_pool, create_pool, fetch_one, get_pool
from services.shared.core.logging_service import (
    asyncio_exception_handler,
    configure_logging,
    log_background_task_result,
)
from services.shared.core.cache_service import start_cache_invalidation_listener
from services.shared.core.http_middleware import register_http_middlewares
from services.shared.auth.auth_service import (
    current_user_id_ctx,
    get_current_user_id_dependency,
)

# FastAPI routers
from tools.shared.search import router as search_router
from tools.shared.rbac_router import router as rbac_router, get_current_user_id
from tools.shared.auth_router import router as auth_router
from tools.notifications.notification_router import notification_router
from tools.notifications.notification_ws_router import notification_ws_router
from tools.shared.observability import router as observability_router
from tools.shared.log_router import router as log_router
from tools.shared.upload_files import router as upload_files_router
from tools.microsoft_graph.graph_router import router as graph_router
from tools.sec_filings.sec_filings import router as sec_filings_router
from services.sec_filings.documents.sec_filing_template_service import bootstrap_sec_filing_table_templates

# MCP tools & AI functions
from mcp_tools import register_all as register_mcp_tools, sync_mcp_tools_to_vector_db
from services.ai.observability import ai_observability
from services.ai.core import assistant_chat_service
from services.microsoft_graph.graph_sync_job import graph_sync_loop

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
        try:
            from postgresql_db.performance_indexes import ensure_performance_indexes
            await ensure_performance_indexes()
        except Exception as idx_err:
            logger.warning(f"Performance indexes verification note: {idx_err}")
        try:
            await bootstrap_sec_filing_table_templates()
        except Exception as sec_err:
            logger.warning(f"SEC filing table templates bootstrap note: {sec_err}")
        try:
            from postgresql_db.schemas.setup_ai_observability_schema import setup as setup_ai_observability_schema
            await setup_ai_observability_schema()
        except Exception as ai_err:
            logger.warning(f"AI observability schema setup note: {ai_err}")
        try:
            from postgresql_db.setup_rls_security import ensure_rls_security
            pool = get_pool()
            async with pool.acquire() as conn:
                await ensure_rls_security(conn)
        except Exception as rls_err:
            logger.warning(f"RDS RLS security setup skipped or deferred: {rls_err}")
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

    # 4. Initialize Event-Driven MQTT Service Presence & Bus
    from services.notifications import mqtt_presence as mqtt_module
    from services.notifications.mqtt_presence import MqttServicePresence
    presence_instance = MqttServicePresence(
        service_name=os.getenv("SERVICE_NAME", "share-document"),
        instance_id=os.getenv("SERVICE_INSTANCE_ID", "share-document-01"),
    )
    mqtt_module.share_document_presence = presence_instance
    await presence_instance.start()

    # 5. Initialize RabbitMQ Manager (Cross-Service Asynchronous Messaging)
    from services.notifications.rabbitmq_service import rabbitmq_manager
    await rabbitmq_manager.connect()

    # AI traces and prompt logs: written in batches as they arrive (waits on a queue, no polling)
    trace_task = asyncio.create_task(ai_observability.writer_loop(), name="ai-trace-writer")
    trace_task.add_done_callback(partial(log_background_task_result, task_name="ai-trace-writer"))
    background_tasks.append(trace_task)

    logger.info("Application startup complete", extra={"event": "application_started"})
    try:
        yield
    finally:
        logger.info("Application shutdown beginning", extra={"event": "application_stopping"})
        try:
            await rabbitmq_manager.close()
        except Exception as rmq_err:
            logger.warning(f"Error closing RabbitMQ manager: {rmq_err}")
        try:
            await presence_instance.stop()
        except Exception as mqtt_err:
            logger.warning(f"Error stopping MQTT presence: {mqtt_err}")
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await ai_observability.flush()
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
mcp = FastMCP("Share Document Portal MCP Server")
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

# The whole conversation sent to Gemini in one request, so one request can't use a huge share of the quota.
CHAT_MAX_TOTAL_CHARS = 60_000

@app.post("/ai/chat")
async def ai_chat(req: ChatRequest, user_id: UUID = Depends(get_current_user_id)):
    if len(req.message) + sum(len(h.content) for h in req.history) > CHAT_MAX_TOTAL_CHARS:
        raise HTTPException(status_code=413, detail="The conversation is too long. Start a new one.")
    try:
        current_user_id_ctx.set(user_id)
        # Input screening and the semantic cache first: a blocked message or a cached answer uses no AI quota.
        prepared = await assistant_chat_service.prepare(req.message, req.history, user_id)
    except Exception:
        logger.exception("AI chat request failed", extra={"event": "ai_chat_failed"})
        return JSONResponse(
            content={"reply": "The AI service is temporarily unavailable. Please try again."},
            status_code=500
        )
    if not prepared.blocked and prepared.cached_answer is None:
        # Counts toward the daily AI limits (429 with when it frees up).
        from services.ai.observability import ai_usage_service
        await ai_usage_service.reserve("CHAT", user_id)
    try:
        async def stream_generator():
            try:
                async for chunk in assistant_chat_service.stream(prepared):
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
    return {"status": "running", "service": "Share Document Portal Backend"}

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
app.include_router(notification_ws_router, tags=["Notifications"])
app.include_router(log_router, prefix="/api", tags=["Logs"])
app.include_router(observability_router, prefix="/api", tags=["Observability"])
app.include_router(graph_router, prefix="/api", tags=["Microsoft Graph"], dependencies=authenticated)
app.include_router(upload_files_router, prefix="/api/upload-files", tags=["Upload Files"], dependencies=authenticated)
app.include_router(sec_filings_router, prefix="/api", tags=["SEC Filings"])

register_http_middlewares(
    app,
    is_prod=_is_prod,
    content_security_policy=CONTENT_SECURITY_POLICY,
    hsts_enabled=_HSTS_ENABLED,
)

if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=os.getenv("MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("MCP_PORT", "8002")),
    )
