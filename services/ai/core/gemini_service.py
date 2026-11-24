"""Gemini calls with model rotation (ported from the admin portal's gemini_service).

Rotates through GEMINI_AVAILABLE_MODELS when a model is rate limited, overloaded or
retired, so one exhausted quota does not fail the request.
"""

import asyncio
import logging
import os
import re
from typing import Any, List, Optional

from google import genai
from google.genai import errors, types

logger = logging.getLogger(__name__)

DEPRECATED_MODELS = {
    "gemini-2.0-flash",
    "gemini-2.0-pro",
    "gemini-2.0-flash-lite",
    "gemini-2-flash",
    "gemini-2-flash-lite",
    "gemini-2",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gemini-1.0-pro",
    "gemini-2.5-pro",
    # Google answers these with 404 ("no longer available to new users"); trying them only wastes a call and a wait.
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
}

_ROTATABLE_MESSAGES = (
    "404", "not found", "no longer available", "not available",
    "429", "503", "504", "resource_exhausted", "quota exceeded",
    "too many requests", "unavailable", "high demand", "deadline_exceeded",
    "deadline expired", "timed out", "is not supported", "invalid model", "deprecated",
)


def without_retired(models):
    """Return models that are not retired, preserving order (or original list if all are retired)."""
    kept = [m for m in models if m.strip().lower() not in DEPRECATED_MODELS]
    return kept or list(models)


def _normalize_ai_model_name(name: str) -> str:
    """E.g. "Gemini 2.5 Flash" -> "gemini-2.5-flash"."""
    return name.strip().lower().replace(" ", "-")


# Text-generation models only (e.g. gemini-3-flash-preview, gemini-3.1-flash-lite).
# GEMINI_AVAILABLE_MODELS may also list image, video, speech, embedding, live-audio and
# agent models, which cannot answer a text request and would only slow the rotation down.
_TEXT_MODEL_RE = re.compile(r"^gemini-\d+(\.\d+)?-(flash|flash-lite|pro)(-preview)?(-[\d-]+)?$")


def get_gemini_fallback_models() -> List[str]:
    """Configured text models in priority rotation order; deprecated and non-text models removed."""
    models_str = os.getenv(
        "GEMINI_AVAILABLE_MODELS",
        "gemini-3-flash-preview,gemini-3.1-flash-lite,gemini-3.5-flash-lite,gemini-3.1-pro-preview",
    )
    raw = [_normalize_ai_model_name(m) for m in models_str.split(",") if m.strip()]
    models = [m for m in dict.fromkeys(raw) if m not in DEPRECATED_MODELS and _TEXT_MODEL_RE.match(m)]
    priority_order = ["gemini-3-flash-preview", "gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.1-pro-preview"]
    ordered = [m for m in priority_order if m in models] + [m for m in models if m not in priority_order]
    return ordered or ["gemini-3-flash-preview", "gemini-3.1-flash-lite"]


def is_rotatable_model_error(e: Exception) -> bool:
    """Rate limit, quota, overload, timeout or unavailable/deprecated model (400/404/429/503/504)."""
    try:
        from botocore.exceptions import ClientError
        if isinstance(e, ClientError):
            code = e.response.get("Error", {}).get("Code", "")
            if code in (
                "ThrottlingException",
                "ModelNotReadyException",
                "TooManyRequestsException",
                "ServiceUnavailableException",
                "AccessDeniedException",
                "ValidationException",
                "ResourceNotFoundException",
            ):
                return True
    except ImportError:
        pass
    if isinstance(e, errors.APIError) and getattr(e, "code", getattr(e, "status", None)) in (400, 404, 429, 503, 504):
        return True
    if isinstance(e, TimeoutError) or "Timeout" in type(e).__name__:
        return True
    msg = str(e).lower()
    return any(k in msg for k in _ROTATABLE_MESSAGES)


# Backward compatibility alias (admin portal name)
is_quota_error = is_rotatable_model_error

import time

_model_cooldowns: dict[str, float] = {}


def _model_failure_cooldown_seconds(error: Exception) -> int:
    """Avoid retrying a model known to be unavailable while a request burst is in progress."""
    status_code = str(getattr(error, "code", "") or getattr(error, "status_code", ""))
    message = f"{status_code} {error}".lower()
    if any(part in message for part in ("404", "not found", "not available", "resourcenotfound", "accessdenied")):
        return 300
    if any(part in message for part in (
        "429", "500", "503", "504", "resource_exhausted", "quota", "high demand", "deadline",
        "throttl", "too many requests", "toomanyrequests", "serviceunavailable", "modelnotready",
    )):
        return 60
    if isinstance(error, TimeoutError) or "timeout" in type(error).__name__.lower() or "timed out" in message:
        return 30
    if is_rotatable_model_error(error):
        return 60
    return 0


def model_is_available(model_name: str) -> bool:
    """Return false while a short provider-failure cooldown for this model is active."""
    until = _model_cooldowns.get(model_name)
    if until is None:
        return True
    if until > time.monotonic():
        return False
    _model_cooldowns.pop(model_name, None)
    return True


def record_model_failure(model_name: str, error: Exception) -> None:
    cooldown = _model_failure_cooldown_seconds(error)
    if cooldown:
        _model_cooldowns[model_name] = time.monotonic() + cooldown



def _attempt_timeout_seconds() -> float:
    return float(os.getenv("GEMINI_TIMEOUT_SECONDS", "180"))


def get_gemini_client() -> Optional[genai.Client]:
    """A client when GEMINI_API_KEY is configured, otherwise None."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    timeout_ms = int(_attempt_timeout_seconds() * 1000)
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_ms))


async def generate_content_with_fallback(
    client: genai.Client,
    contents: Any,
    config: Optional[Any] = None,
) -> Any:
    """client.models.generate_content, moving to the next model when one is rate
    limited, over quota, retired, or does not answer within GEMINI_TIMEOUT_SECONDS."""
    timeout = _attempt_timeout_seconds()
    last_exception: Optional[Exception] = None
    models = get_gemini_fallback_models()
    active_models = [m for m in models if model_is_available(m)] or models
    for i, model_name in enumerate(active_models):
        logger.info("Gemini attempt with model %s", model_name)
        try:
            # The HTTP timeout does not cover every stall (e.g. a dead network route), so
            # the attempt as a whole is bounded too. A timed-out call is abandoned.
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    client.models.generate_content,
                    model=model_name,
                    contents=contents,
                    config=config,
                ),
                timeout=timeout + 5,
            )
            if i > 0:
                logger.info("Gemini fallback succeeded with model: %s", model_name)
            return response
        except asyncio.TimeoutError as e:
            last_exception = e
            record_model_failure(model_name, e)
            logger.warning("Gemini model %s did not answer within %ss; trying the next model.", model_name, timeout)
            continue
        except Exception as e:
            last_exception = e
            if is_rotatable_model_error(e):
                record_model_failure(model_name, e)
                logger.warning("Gemini model %s unavailable or over quota (%s); trying the next model.", model_name, e)
                continue
            raise
    raise RuntimeError(f"No Gemini model could handle the request: {last_exception!r}")
