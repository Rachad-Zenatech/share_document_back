"""Periodic Microsoft Graph sync for already-known employees.

Runs as a background asyncio task started from server.py's lifespan,
mirroring the existing MCP-tool-sync background task there. This job only
UPDATES users that already exist locally, matched by microsoft_object_id —
user creation stays login-driven (services/auth_service.py::upsertMicrosoftUser),
so a stale/incomplete Graph directory can never fabricate a local account.

Disabled by default via GRAPH_BACKGROUND_SYNC_ENABLED until the tenant admin
grants consent for the User.Read.All application permission this needs
(see services/graph_service.py::get_app_only_token).
"""

import asyncio
import datetime
import logging
import os
from typing import Optional

from postgresql_db.database import get_pool
from services.graph_service import fetch_users_page, get_app_only_token

logger = logging.getLogger(__name__)

DEFAULT_SYNC_INTERVAL_SECONDS = 6 * 60 * 60  # 6 hours


def _sync_interval_seconds() -> int:
    try:
        return max(300, int(os.getenv("GRAPH_SYNC_INTERVAL_SECONDS", str(DEFAULT_SYNC_INTERVAL_SECONDS))))
    except (TypeError, ValueError):
        return DEFAULT_SYNC_INTERVAL_SECONDS


async def _apply_profile(conn, profile: dict) -> bool:
    """Update one local user from a normalized Graph profile. Returns True if
    a row was actually matched/updated, False if no local user has this
    microsoft_object_id (not an error — most Graph directory users may not
    have ever logged into this app).
    """
    object_id = profile.get("object_id")
    if not object_id:
        return False
    account_enabled = profile.get("account_enabled")
    result = await conn.execute(
        """
        UPDATE users SET
            department = $1,
            job_title = $2,
            graph_account_enabled = $3,
            last_graph_sync = $4,
            is_active = CASE
                WHEN is_super_admin THEN is_active
                WHEN $3::boolean IS NOT NULL THEN $3
                ELSE is_active
            END
        WHERE microsoft_object_id = $5
        """,
        profile.get("department"),
        profile.get("job_title"),
        account_enabled,
        datetime.datetime.now(datetime.timezone.utc),
        object_id,
    )
    return result != "UPDATE 0"


async def run_graph_sync_once() -> dict:
    """Run a single sync pass. Returns a summary dict for logging/tests."""
    summary = {"pages": 0, "matched": 0, "skipped": 0, "aborted": False}

    access_token = await get_app_only_token()
    if not access_token:
        logger.warning(
            "Skipping Graph background sync — no app-only token available",
            extra={"event": "graph_sync_skipped_no_token"},
        )
        summary["aborted"] = True
        return summary

    pool = get_pool()
    next_link: Optional[str] = None
    while True:
        page = await fetch_users_page(access_token, next_link)
        if page is None:
            logger.error(
                "Graph background sync stopped early due to a page fetch failure",
                extra={"event": "graph_sync_page_failed", "pages_completed": summary["pages"]},
            )
            summary["aborted"] = True
            break

        summary["pages"] += 1
        async with pool.acquire() as conn:
            for profile in page["value"]:
                try:
                    updated = await _apply_profile(conn, profile)
                    if updated:
                        summary["matched"] += 1
                    else:
                        summary["skipped"] += 1
                except Exception:
                    logger.exception(
                        "Failed to apply Graph profile to local user; skipping this record",
                        extra={"event": "graph_sync_row_failed", "object_id": profile.get("object_id")},
                    )
                    summary["skipped"] += 1

        next_link = page.get("next_link")
        if not next_link:
            break

    logger.info("Graph background sync completed", extra={"event": "graph_sync_completed", **summary})
    return summary


async def graph_sync_loop(interval_seconds: Optional[int] = None) -> None:
    """Run run_graph_sync_once() forever on a fixed interval. A crashed
    iteration is logged and retried on the next interval rather than killing
    the task, matching how the existing mcp-tool-sync task is supervised.
    """
    interval = interval_seconds or _sync_interval_seconds()
    while True:
        try:
            await run_graph_sync_once()
        except Exception:
            logger.exception(
                "Graph background sync iteration crashed; will retry next interval",
                extra={"event": "graph_sync_iteration_crashed"},
            )
        await asyncio.sleep(interval)
