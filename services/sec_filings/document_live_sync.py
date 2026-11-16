"""Telling open editors that a document changed, so they show it without a reload.

After a save, a proposal or a merge on a document of a kind with `live_sync` (see document_types), the portal's notification
socket gets a WORKFLOW_SYNC for that document; every editor that has it open reads it again (the front's
useDocumentLiveSync). Saves come in bursts while someone types, so a document is announced at most once a second.
"""

import asyncio
import logging
import time
from typing import Dict, Set

from postgresql_db.database import fetch_one
from services.sec_filings.document_types import config_of

logger = logging.getLogger(__name__)

MIN_GAP_SEC = 1.0
_last: Dict[str, float] = {}
_trailing: Set[str] = set()


def _announce_later(document_id: str, delay: float) -> None:
    """One more announcement once the gap is over, so the last change of a burst is never left unannounced."""
    if document_id in _trailing:
        return
    _trailing.add(document_id)

    async def later() -> None:
        await asyncio.sleep(delay)
        _trailing.discard(document_id)
        await announce_change(document_id)

    asyncio.get_running_loop().create_task(later())


async def announce_change(document_id: str) -> None:
    """Announce that this document changed, when its kind asks for live updates. Never raises."""
    now = time.monotonic()
    since = now - _last.get(document_id, -1e9)
    if since < MIN_GAP_SEC:
        _announce_later(document_id, MIN_GAP_SEC - since + 0.05)
        return
    try:
        row = await fetch_one("SELECT template_type, form_type FROM sec_documents WHERE id = $1 AND deleted_at IS NULL", document_id)
        if not row or not config_of(row["template_type"], row["form_type"]).live_sync:
            return
        _last[document_id] = now
        from services.notifications.notification_service import broadcast_workflow_sync, broadcaster
        broadcast_workflow_sync("SecFiling", document_id)
        logger.info("[LiveSync] announced %s to %d open notification socket(s)", document_id, len(broadcaster.listeners))
    except Exception as e:
        logger.info("Live update of %s skipped: %s", document_id, e)
