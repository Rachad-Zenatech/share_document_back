"""HTTP middleware registration for session, rate limiting, security headers, observability, and CORS."""

import asyncio
import hashlib
import logging
import os
import re
import time
from collections import defaultdict
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from services.shared.core.logging_service import request_id_ctx

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


# --- Rate Limiting Helpers ---
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
        from services.shared.auth.auth_service import AUTH_COOKIE_NAME
        token = request.cookies.get(AUTH_COOKIE_NAME, "") or request.cookies.get("zenatech_access_token", "")
    if not token:
        return None
    return "session:" + hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()[:32]


# --- Request Observability Helpers ---
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


def _register_session_middleware(app: FastAPI, *, is_prod: bool) -> None:
    session_secret = os.environ.get("SESSION_SECRET")
    if not session_secret:
        if is_prod:
            raise RuntimeError("CRITICAL: SESSION_SECRET must be configured in production!")
        session_secret = "dev_ephemeral_session_secret_minimum_32_chars_ok!"

    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        same_site="lax",
        https_only=os.getenv("SESSION_COOKIE_SECURE", "true" if is_prod else "false").lower() in ("true", "1", "yes"),
        max_age=3600,
    )


def _register_rate_limit_middleware(app: FastAPI) -> None:
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


def _register_security_headers_middleware(
    app: FastAPI,
    *,
    content_security_policy: str,
    hsts_enabled: bool,
) -> None:
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        response.headers.setdefault("X-XSS-Protection", "0")
        response.headers.setdefault("Content-Security-Policy", content_security_policy)
        if hsts_enabled:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


def _register_observability_middleware(app: FastAPI) -> None:
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


def _register_cors_middleware(app: FastAPI, *, is_prod: bool) -> None:
    cors_env = os.getenv("CORS_ALLOWED_ORIGINS", "") or os.getenv("CORS_ORIGINS", "")
    cors_origins = [orig.strip() for orig in cors_env.split(",") if orig.strip()] if cors_env else []
    cors_regex = None if is_prod else r"^https?://(localhost|127\.0\.0\.1|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.\d+\.\d+\.\d+)(:\d+)?$"

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_origin_regex=cors_regex,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )


def register_http_middlewares(
    app: FastAPI,
    *,
    is_prod: bool,
    content_security_policy: str,
    hsts_enabled: bool,
) -> None:
    """Register HTTP middlewares in the exact Starlette LIFO order expected by server.py."""
    _register_session_middleware(app, is_prod=is_prod)
    _register_rate_limit_middleware(app)
    _register_security_headers_middleware(
        app,
        content_security_policy=content_security_policy,
        hsts_enabled=hsts_enabled,
    )
    _register_observability_middleware(app)
    _register_cors_middleware(app, is_prod=is_prod)
