"""Whether the AI (Gemini) is working right now, for the banner every signed-in user sees when it isn't.

Read from the AI calls themselves: each one is traced with whether it worked and, when it didn't, what kind of failure
it was (ai_observability.classify_error). The AI counts as down when the latest call failed and nothing has worked for
a couple of minutes (one model failing and the next one working, as the fallback does all the time, is not "down").
Read from the database, so every server process agrees and a restart doesn't forget.
"""

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from postgresql_db.database import fetch_all

WINDOW = timedelta(minutes=30)
# The latest call failed and nothing has worked for this long: the AI is down.
QUIET_AFTER_OK = timedelta(minutes=2)
CACHE_SECONDS = 15

# The most telling kind of failure first.
_PRIORITY = ["KEY", "QUOTA", "MODEL", "BUSY", "OTHER"]

_TEXT = {
    "KEY": (
        "The AI key isn't working",
        # "It may have expired or been revoked. Ask an admin to renew the Gemini key."
        "It may have expired or been revoked. Ask an admin to renew the AWS Bedrock key.",
    ),
    "QUOTA": (
        "The AI limit has been reached",
        # "The Gemini quota is used up for now. It works again when the limit resets, or when an admin raises it."
        "The Bedrock limit is used up for now. It works again when the limit resets, or when an admin raises it.",
    ),
    "MODEL": (
        "The AI models aren't available",
        # "The configured Gemini models have been retired or renamed. Ask an admin to update them."
        "The configured Bedrock models have been retired or renamed. Ask an admin to update them.",
    ),
    "BUSY": (
        "The AI isn't responding",
        "The AI service is overloaded or timing out. It usually clears on its own.",
    ),
    "OTHER": (
        "The AI isn't working",
        "AI calls are failing. Ask an admin to check the logs.",
    ),
}
_EFFECT = (
    "Until it works: scores that need the AI are held back and posts can't be approved without one, and sentiment, "
    "captions and reading images, documents and video aren't updated."
)


class AiStatus(BaseModel):
    available: bool = True
    # KEY, QUOTA, MODEL, BUSY or OTHER while it isn't.
    reason: Optional[str] = None
    title: Optional[str] = None
    message: Optional[str] = None
    since: Optional[datetime] = None


def summarize(rows: List[Dict[str, Any]], now: datetime, key_set: bool = True) -> AiStatus:
    """rows: the AI calls in the window, newest first, each with status, kind (the failure kind) and started_at."""
    if not key_set:
        return AiStatus(available=False, reason="KEY", title="The AI key isn't set", # message="No Gemini key is configured. "
        message="No AWS Bedrock key is configured. " + _EFFECT)
    if not rows or rows[0]["status"] == "OK":
        return AiStatus()
    last_ok = next((r["started_at"] for r in rows if r["status"] == "OK"), None)
    if last_ok is not None and now - last_ok < QUIET_AFTER_OK:
        return AiStatus()  # one model failed just after another worked: the fallback is still going
    failed = [r for r in rows if r["status"] != "OK" and (last_ok is None or r["started_at"] > last_ok)]
    kinds = {r.get("kind") or "OTHER" for r in failed}
    reason = next((k for k in _PRIORITY if k in kinds), "OTHER")
    title, message = _TEXT[reason]
    return AiStatus(
        available=False, reason=reason, title=title, message=f"{message} {_EFFECT}",
        since=min(r["started_at"] for r in failed) if failed else None,
    )


_cache: Dict[str, Any] = {"at": 0.0, "status": AiStatus()}


async def status() -> AiStatus:
    if time.monotonic() - _cache["at"] < CACHE_SECONDS:
        return _cache["status"]
    rows = await fetch_all(
        """
        SELECT status, attributes->>'error_kind' AS kind, started_at
        FROM ai_trace_spans
        WHERE kind = 'model' AND (name = 'bedrock.generate_content' OR name = 'gemini.generate_content') AND started_at > $1
        ORDER BY started_at DESC LIMIT 300
        """,
        datetime.now(timezone.utc) - WINDOW,
    )
    from services.ai.core.bedrock_service import has_ai_key
    result = summarize([dict(r) for r in rows], datetime.now(timezone.utc), has_ai_key())
    _cache.update(at=time.monotonic(), status=result)
    return result
