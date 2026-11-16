"""Social Media Script review: the moves during review (check in, finalize, decide, create the post) and the events for
proposals and merges. Who may do each is decided by script_review_rules; the state and the invites are in
script_review_service."""

from __future__ import annotations

import time
from typing import Any, Dict, Optional
from uuid import UUID

from postgresql_db.database import get_pool
from services.sec_filings import script_review_events as events
from services.sec_filings import script_review_rules as rules
from services.sec_filings.script_review_service import (
    ScriptReviewError, _context, _record, get_state, note_document_changed,
)


async def _close(review: Dict[str, Any], status: str, user_id: UUID, note: Optional[str], kind: str) -> None:
    await get_pool().execute(
        """UPDATE sec_script_reviews SET status = $2, decision_note = $3, decided_by = $4, decided_at = now(), updated_at = now()
           WHERE document_id = $1""", review["document_id"], status, (note or "").strip() or None, user_id)
    await _record(review["document_id"], review["round"], user_id, kind, status=status, note=note)
    events.publish("review.finalized" if kind in ("ROUTED", "CHANGES_REQUESTED_BY_REVIEWERS") else "review.decided",
                   review["document_id"], user_id, status=status, round=review["round"], note=note,
                   creatorId=str(review["creator_id"]))


async def check_in(user_id: UUID, document_id: str, vote: str, note: Optional[str]) -> Dict[str, Any]:
    review, _, access = await _context(user_id, document_id)
    if not access.can_check_in:
        raise ScriptReviewError("You can't check in on this script", 403)
    problem = rules.check_vote(access.role, vote, note)
    if problem:
        raise ScriptReviewError(problem, 400)
    await get_pool().execute(
        """INSERT INTO sec_script_checkins (document_id, round, user_id, vote, note) VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (document_id, round, user_id) DO UPDATE SET vote = EXCLUDED.vote, note = EXCLUDED.note, checked_at = now()""",
        document_id, review["round"], user_id, vote, (note or "").strip() or None)
    await _record(document_id, review["round"], user_id, "CHECKIN", vote=vote, note=note)
    events.publish("review.checked_in", document_id, user_id, vote=vote, round=review["round"])
    return await get_state(user_id, document_id)


async def withdraw_check_in(user_id: UUID, document_id: str) -> Dict[str, Any]:
    review, _, _ = await _context(user_id, document_id)
    if review["status"] != "IN_REVIEW":
        raise ScriptReviewError("The script isn't in review", 409)
    await get_pool().execute("DELETE FROM sec_script_checkins WHERE document_id = $1 AND round = $2 AND user_id = $3",
                             document_id, review["round"], user_id)
    events.publish("review.checked_in", document_id, user_id, vote=None, round=review["round"])
    return await get_state(user_id, document_id)


async def finalize(user_id: UUID, document_id: str, route: Optional[str], note: Optional[str]) -> Dict[str, Any]:
    """Everyone has checked in: send it to a manager (level 2), approve it, or - if anyone asked for changes - send it back
    to the creator."""
    review, collabs, access = await _context(user_id, document_id)
    if review["status"] != "IN_REVIEW":
        raise ScriptReviewError("The script isn't in review", 409)
    if not access.can_finalize:
        raise ScriptReviewError("Only a reviewer who checked in, or a manager, can decide where the script goes", 403)
    if not rules.all_checked(review["status"], collabs):
        raise ScriptReviewError("Every reviewer has to check in first", 409)
    target = rules.finalize_target(collabs, route)
    if target == "CHANGES_REQUESTED":
        notes = "; ".join(f"{c['name']}: {c['note']}" for c in collabs if c.get("vote") == "CHANGES" and c.get("note"))
        await _close(review, target, user_id, note or notes, "CHANGES_REQUESTED_BY_REVIEWERS")
    else:
        await _close(review, target, user_id, note, "ROUTED" if target == "LEVEL2_REVIEW" else "APPROVED")
    return await get_state(user_id, document_id)


async def decide(user_id: UUID, document_id: str, decision: str, note: Optional[str]) -> Dict[str, Any]:
    """The manager's final decision (level 2, or earlier: a manager may decide at any point in review)."""
    review, _, access = await _context(user_id, document_id)
    if not access.can_decide:
        raise ScriptReviewError("Only a manager decides here", 403)
    if decision == "SEND_BACK" and not (note or "").strip():
        raise ScriptReviewError("Say what needs to change", 400)
    await _close(review, "APPROVED" if decision == "APPROVE" else "CHANGES_REQUESTED", user_id, note,
                 "APPROVED" if decision == "APPROVE" else "SENT_BACK")
    return await get_state(user_id, document_id)


async def create_post(user_id: UUID, document_id: str, note: Optional[str]) -> Dict[str, Any]:
    """Ask for a post to be made from the approved script. Only the creator or a manager may. The post itself is made by
    the Social Media portal, which listens for `post.requested`."""
    review, _, access = await _context(user_id, document_id)
    if not access.can_create_post:
        raise ScriptReviewError("Only the creator or a manager can create a post, and only from an approved script", 403)
    done = await get_pool().fetchval(
        """UPDATE sec_script_reviews SET post_requested_at = now(), post_requested_by = $2, updated_at = now()
           WHERE document_id = $1 AND post_requested_at IS NULL RETURNING true""", document_id, user_id)
    if not done:
        raise ScriptReviewError("A post was already requested from this script", 409)
    await _record(document_id, review["round"], user_id, "POST_REQUESTED", note=note)
    events.publish("post.requested", document_id, user_id, status="APPROVED", round=review["round"], title=review["title"],
                   creatorId=str(review["creator_id"]), note=note)
    return await get_state(user_id, document_id)


# ------------------------------------------------------------ proposals and merges (called from the document service)


async def after_proposal_saved(user_id: UUID, document_id: str, proposal_id: str, status: Optional[str]) -> None:
    review = await get_pool().fetchrow("SELECT round FROM sec_script_reviews WHERE document_id = $1", document_id)
    if not review:
        return
    await note_document_changed(user_id, document_id)
    await _record(document_id, review["round"], user_id, "PROPOSAL", proposalId=proposal_id, status=status)
    events.publish("change.proposed", document_id, user_id, proposalId=proposal_id, proposalStatus=status, round=review["round"])


async def after_merge(user_id: UUID, document_id: str, version_number: Optional[int]) -> None:
    review = await get_pool().fetchrow("SELECT round FROM sec_script_reviews WHERE document_id = $1", document_id)
    if not review:
        return
    await note_document_changed(user_id, document_id)
    await _record(document_id, review["round"], user_id, "MERGED", versionNumber=version_number)
    events.publish("merge.completed", document_id, user_id, versionNumber=version_number, round=review["round"])


_SAVED_EVENT_GAP_SEC = 5
_last_saved_event: Dict[str, float] = {}


async def after_document_saved(user_id: UUID, document_id: str) -> None:
    """A save of the script itself: check-ins are cleared, and the Social Media portal is told (at most every few seconds per
    script, so typing doesn't flood the bus) to read the text again."""
    await note_document_changed(user_id, document_id)
    review = await get_pool().fetchrow("SELECT round FROM sec_script_reviews WHERE document_id = $1", document_id)
    now = time.monotonic()
    if review and now - _last_saved_event.get(document_id, -1e9) > _SAVED_EVENT_GAP_SEC:
        _last_saved_event[document_id] = now
        events.publish("content.saved", document_id, user_id, round=review["round"])
