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

from services.ai.core.gemini_service import (
    DEPRECATED_MODELS,
    get_gemini_fallback_models,
    is_rotatable_model_error,
    without_retired,
)

TIERS = ("LIGHT", "STANDARD", "HEAVY")
# HEAVY defaults to flash because Pro models have no free-tier allowance; on a paid key, put a Pro model first.
_TIER_DEFAULTS: Dict[str, str] = {
    "LIGHT": "gemini-3.1-flash-lite,gemini-3.5-flash-lite",
    "STANDARD": "gemini-3-flash-preview,gemini-3.1-flash-lite,gemini-3.5-flash-lite",
    "HEAVY": "gemini-3-flash-preview,gemini-3.1-flash-lite",
}
_FEATURE_TIERS: Dict[str, str] = {
    "CHAT": "LIGHT",
}
LONG_PROMPT_CHARS = 8_000
VERY_LONG_PROMPT_CHARS = 60_000

_DEFAULT_MODELS = "gemini-3-flash-preview,gemini-3.1-flash-lite,gemini-3.5-flash-lite"
_RETIRED_MODELS = DEPRECATED_MODELS


@dataclass
class Route:
    tier: str
    models: List[str]
    reason: str


def fallback_models() -> List[str]:
    """Every configured Gemini model (GEMINI_AVAILABLE_MODELS), retired ones left out, in priority order."""
    return get_gemini_fallback_models()


def enabled() -> bool:
    return os.getenv("AI_ROUTING_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def tier_models(tier: str) -> List[str]:
    raw = os.getenv(f"AI_ROUTE_TIER_{tier}", _TIER_DEFAULTS[tier])
    return without_retired([m.strip().lower() for m in raw.split(",") if m.strip()])


def base_tier(feature: str) -> str:
    tier = os.getenv(f"AI_ROUTE_{feature}", _FEATURE_TIERS.get(feature, "STANDARD")).strip().upper()
    return tier if tier in TIERS else "STANDARD"


def route(feature: str, *, prompt_chars: int = 0, attachments: int = 0, high_stakes: bool = False) -> Route:
    """The models to try, in order, for this request, and why that tier was chosen."""
    from services.ai.core.bedrock_service import get_bedrock_profile_id

    profile = get_bedrock_profile_id()
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
    if profile:
        preferred = [profile] + [m for m in preferred if m != profile]
    return Route(tier, preferred + [m for m in available if m not in preferred], "; ".join(reasons))
