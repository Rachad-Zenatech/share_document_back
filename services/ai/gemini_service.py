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
}

_ROTATABLE_MESSAGES = (
    "404", "not found", "no longer available", "not available",
    "429", "503", "resource_exhausted", "quota exceeded",
    "too many requests", "unavailable", "high demand",
    "is not supported", "invalid model", "deprecated",
)


def _normalize_ai_model_name(name: str) -> str:
    """E.g. "Gemini 2.5 Flash" -> "gemini-2.5-flash"."""
    return name.strip().lower().replace(" ", "-")


# Text-generation models only (e.g. gemini-2.5-flash, gemini-3.1-pro, gemini-3-flash-preview).
# GEMINI_AVAILABLE_MODELS may also list image, video, speech, embedding, live-audio and
# agent models, which cannot answer a text request and would only slow the rotation down.
_TEXT_MODEL_RE = re.compile(r"^gemini-\d+(\.\d+)?-(flash|flash-lite|pro)(-preview)?(-[\d-]+)?$")


def get_gemini_fallback_models() -> List[str]:
    """Configured text models in rotation order; deprecated and non-text models removed."""
    models_str = os.getenv(
        "GEMINI_AVAILABLE_MODELS",
        "gemini-2.5-flash,gemini-3.1-flash-lite,gemini-2.5-flash-lite,gemini-3.1-pro-preview,gemini-3-flash-preview",
    )
    models = [_normalize_ai_model_name(m) for m in models_str.split(",") if m.strip()]
    models = [m for m in models if m not in DEPRECATED_MODELS and _TEXT_MODEL_RE.match(m)]
    return list(dict.fromkeys(models)) or ["gemini-2.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"]


def is_rotatable_model_error(e: Exception) -> bool:
    """Rate limit, quota, overload or unavailable/deprecated model (400/404/429/503)."""
    if isinstance(e, errors.APIError) and getattr(e, "code", getattr(e, "status", None)) in (400, 404, 429, 503):
        return True
    msg = str(e).lower()
    return any(k in msg for k in _ROTATABLE_MESSAGES)


# Backward compatibility alias (admin portal name)
is_quota_error = is_rotatable_model_error


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
    for i, model_name in enumerate(get_gemini_fallback_models()):
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
            logger.warning("Gemini model %s did not answer within %ss; trying the next model.", model_name, timeout)
            continue
        except Exception as e:
            last_exception = e
            if is_rotatable_model_error(e):
                logger.warning("Gemini model %s unavailable or over quota (%s); trying the next model.", model_name, e)
                continue
            raise
    raise RuntimeError(f"No Gemini model could handle the request: {last_exception!r}")
