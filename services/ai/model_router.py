"""Model routing: picks which Gemini models a request tries first, by feature and by how demanding the request is.

Three tiers, each an ordered list of models (override with AI_ROUTE_TIER_<TIER>=model-a,model-b):

    LIGHT     short, cheap requests (assistant chat, short prompts)      flash-lite models first
    STANDARD  ordinary analysis                                          flash models first
    HEAVY     long or high-stakes requests                               a stronger model first, when configured

Each feature has a base tier (AI_ROUTE_<FEATURE>=LIGHT|STANDARD|HEAVY overrides it), and the request can move up:
attachments or a long prompt lift LIGHT to STANDARD; a very long prompt or high stakes lift it to HEAVY. The rest
of the configured models (GEMINI_AVAILABLE_MODELS) follow the tier's models, so a model that is out of quota or
retired still falls back as before. AI_ROUTING_ENABLED=false turns routing off (plain fallback order for everything).
"""

import os
from dataclasses import dataclass
from typing import Dict, List

TIERS = ("LIGHT", "STANDARD", "HEAVY")
# HEAVY defaults to flash because Pro models have no free-tier allowance; on a paid key, put a Pro model first.
_TIER_DEFAULTS: Dict[str, str] = {
    "LIGHT": "gemini-3.1-flash-lite,gemini-2.5-flash-lite",
    "STANDARD": "gemini-2.5-flash,gemini-3-flash-preview",
    "HEAVY": "gemini-2.5-flash,gemini-3-flash-preview",
}
_FEATURE_TIERS: Dict[str, str] = {
    "CHAT": "LIGHT",
}
LONG_PROMPT_CHARS = 8_000
VERY_LONG_PROMPT_CHARS = 60_000

_DEFAULT_MODELS = "gemini-2.5-flash,gemini-3.1-flash-lite,gemini-2.5-flash-lite,gemini-3.1-pro-preview,gemini-3-flash-preview"
_RETIRED_MODELS = {
    "gemini-2.0-flash", "gemini-2.0-pro", "gemini-2.0-flash-lite", "gemini-2-flash", "gemini-2-flash-lite",
    "gemini-2", "gemini-1.5-flash", "gemini-1.5-pro", "gemini-1.0-pro", "gemini-2.5-pro",
}
_ROTATABLE_ERROR_HINTS = (
    "404", "not found", "no longer available", "not available", "429", "503", "resource_exhausted",
    "quota exceeded", "too many requests", "unavailable", "high demand", "is not supported", "invalid model",
    "deprecated",
)


@dataclass
class Route:
    tier: str
    models: List[str]
    reason: str


def fallback_models() -> List[str]:
    """Every configured Gemini model (GEMINI_AVAILABLE_MODELS), retired ones left out, in the configured order."""
    raw = os.getenv("GEMINI_AVAILABLE_MODELS", _DEFAULT_MODELS)
    models = [m.strip().lower().replace(" ", "-") for m in raw.split(",") if m.strip()]
    models = [m for m in dict.fromkeys(models) if m not in _RETIRED_MODELS]
    return models or ["gemini-2.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"]


def is_rotatable_model_error(e: Exception) -> bool:
    """A rate limit, exhausted quota, overloaded or retired model: worth trying the next model."""
    code = getattr(e, "code", getattr(e, "status", None))
    if code in (400, 404, 429, 503):
        return True
    msg = str(e).lower()
    return any(k in msg for k in _ROTATABLE_ERROR_HINTS)


def enabled() -> bool:
    return os.getenv("AI_ROUTING_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def tier_models(tier: str) -> List[str]:
    raw = os.getenv(f"AI_ROUTE_TIER_{tier}", _TIER_DEFAULTS[tier])
    return [m.strip().lower() for m in raw.split(",") if m.strip()]


def base_tier(feature: str) -> str:
    tier = os.getenv(f"AI_ROUTE_{feature}", _FEATURE_TIERS.get(feature, "STANDARD")).strip().upper()
    return tier if tier in TIERS else "STANDARD"


def route(feature: str, *, prompt_chars: int = 0, attachments: int = 0, high_stakes: bool = False) -> Route:
    """The models to try, in order, for this request, and why that tier was chosen."""
    available = fallback_models()
    if not enabled():
        return Route("FALLBACK", available, "routing disabled")
    tier = base_tier(feature)
    reasons = [f"{feature.lower()} defaults to {tier.lower()}"]
    if high_stakes or prompt_chars > VERY_LONG_PROMPT_CHARS:
        if tier != "HEAVY":
            tier = "HEAVY"
            reasons.append("high stakes" if high_stakes else f"prompt over {VERY_LONG_PROMPT_CHARS:,} characters")
    elif tier == "LIGHT" and (attachments or prompt_chars > LONG_PROMPT_CHARS):
        tier = "STANDARD"
        reasons.append("has attachments" if attachments else f"prompt over {LONG_PROMPT_CHARS:,} characters")
    preferred = [m for m in tier_models(tier) if m in available]
    return Route(tier, preferred + [m for m in available if m not in preferred], "; ".join(reasons))
