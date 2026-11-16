"""Social Media Script reviews: the three document writes, guarded.

A script document (template "social-media-script") has a review (script_review_service) that decides who may change it.
These wrap the document service's writes: refuse the people who may only view, then tell the review what happened
(clearing check-ins, publishing events). Documents without a script review behave exactly as before. The routes import
these instead of the plain ones.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from services.sec_filings import document_live_sync as live
from services.sec_filings import script_review_flow as flow
from services.sec_filings import script_review_rules as rules
from services.sec_filings import script_review_service as review
from services.sec_filings.documents import sec_document_service as docs


async def save_document(
    user_id: UUID,
    document_id: str,
    document: dict[str, Any],
    base_revision: int | None,
    template_type: str | None = None,
    owner_name: str | None = None,
) -> dict[str, Any]:
    await review.guard_write(user_id, document_id)
    result = await docs.save_document(user_id, document_id, document, base_revision, template_type, owner_name)
    if result.get("created") and rules.is_script_document(template_type, document.get("formType")):
        await review.init_review(user_id, document_id)
    elif not result.get("created"):
        await flow.after_document_saved(user_id, document_id)
        await live.announce_change(document_id)
    return result


async def save_proposal(user_id: UUID, proposal_id: str, document_id: str, proposal: dict[str, Any]) -> bool:
    await review.guard_write(user_id, document_id)
    saved = await docs.save_proposal(user_id, proposal_id, document_id, proposal)
    await flow.after_proposal_saved(user_id, document_id, proposal_id, proposal.get("status"))
    await live.announce_change(document_id)
    return saved


async def add_version(user_id: UUID, document_id: str, snapshot: dict[str, Any]) -> bool:
    await review.guard_write(user_id, document_id)
    saved = await docs.add_version(user_id, document_id, snapshot)
    version = snapshot.get("versionNumber")
    await flow.after_merge(user_id, document_id, version if isinstance(version, int) else None)
    await live.announce_change(document_id)
    return saved
