import os
import asyncio
import logging
from typing import Any, Optional, List
from google import genai
from google.genai import errors

logger = logging.getLogger(__name__)

def _normalize_ai_model_name(name: str) -> str:
    """
    Normalizes a model name from the environment variable list
    into the format expected by the Google GenAI SDK.
    E.g. "Gemini 2.5 Flash" -> "gemini-2.5-flash"
    """
    return name.strip().lower().replace(" ", "-")
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

def get_gemini_fallback_models() -> List[str]:
    """
    Retrieves and normalizes the list of available Gemini models
    from the environment configuration, filtering out deprecated/retired models.
    Guarantees active, proven general-purpose models are prioritized at the front of the rotation chain.
    """
    models_str = os.getenv("GEMINI_AVAILABLE_MODELS", "gemini-2.5-flash,gemini-3.1-flash-lite,gemini-2.5-flash-lite,gemini-3.1-pro-preview,gemini-3-flash-preview")
    raw_models = [_normalize_ai_model_name(m) for m in models_str.split(",") if m.strip()]

    # Filter out deprecated models that are no longer available in the Google GenAI API
    models = [m for m in raw_models if m not in DEPRECATED_MODELS]
    priority_order = ["gemini-2.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite", "gemini-3.1-pro-preview", "gemini-3-flash-preview"]
    ordered = [m for m in priority_order if m in models or m not in DEPRECATED_MODELS]
    for m in models:
        if m not in ordered:
            ordered.append(m)
    return ordered or ["gemini-2.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"]

def is_rotatable_model_error(e: Exception) -> bool:
    """
    Determines if a given exception represents a rate limit, quota exhaustion,
    temporary service spike, or deprecated/unavailable model (404 / 503 / 429).
    """
    if isinstance(e, errors.APIError):
        code = getattr(e, "code", getattr(e, "status", None))
        if code in (400, 404, 429, 503):
            return True

        msg = str(e).lower()
        if any(k in msg for k in [
            "404", "not found", "no longer available", "not available",
            "429", "503", "resource_exhausted", "quota exceeded",
            "too many requests", "unavailable", "high demand",
            "is not supported", "invalid model", "deprecated"
        ]):
            return True

    msg = str(e).lower()
    if any(k in msg for k in [
        "404", "not found", "no longer available", "not available",
        "429", "503", "resource_exhausted", "quota exceeded",
        "too many requests", "unavailable", "high demand",
        "is not supported", "invalid model", "deprecated"
    ]):
        return True

    return False

# Backward compatibility alias
is_quota_error = is_rotatable_model_error

async def generate_content_with_fallback(
    client: genai.Client,
    contents: Any,
    config: Optional[Any] = None,
) -> Any:
    """
    A robust wrapper around client.models.generate_content that automatically
    rotates through configured Gemini models upon encountering quota limits (429),
    server spikes (503), or deprecated/unavailable models (404).
    """
    models = get_gemini_fallback_models()

    last_exception = None

    for i, model_name in enumerate(models):
        logger.info(f"Attempting Gemini model: {model_name}")

        try:
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=model_name,
                contents=contents,
                config=config,
            )

            if i > 0:
                logger.info(f"Gemini fallback succeeded with model: {model_name}")

            return response

        except Exception as e:
            last_exception = e
            if is_rotatable_model_error(e):
                logger.warning(f"Gemini model {model_name} unavailable or quota exceeded ({e}). Moving to next model.")
                continue
            else:
                # Non-rotatable fatal error, re-raise immediately
                raise e

    print("no quota")
    logger.error("no quota")
    raise RuntimeError("no quota")
