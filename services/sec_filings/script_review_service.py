"""Social Media Script review: the state of a script's review, who may do what (script_review_rules), and the moves
through it: invite, send, check in, finalize, decide, create the post. Each move is recorded in the script's history
and published on the event bus (script_review_events).

A script is an ordinary document in `sec_documents` (made from the "social-media-script" template); this module adds
its review on top. Invited people are also put on the document's own invite lists, so the existing document access
(opening it, proposing changes, the merge review) lets them in; what they may then *change* is decided here.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from uuid import UUID

from postgresql_db.database import get_pool
from postgresql_db.schemas.sec_script_review_schema import ensure_schema
from services.sec_filings import script_review_events as events
from services.sec_filings import script_review_rules as rules


class ScriptReviewError(Exception):
    """A move that isn't allowed or doesn't fit the review's state; `status_code` is the HTTP status to answer with."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# --------------------------------------------------------------------------- reading


async def _actor(user_id: UUID) -> tuple[str, bool]:
    from services.sec_filings.documents.sec_document_service import _actor as actor
    async with get_pool().acquire() as conn:
        return await actor(conn, user_id)


async def _review(document_id: str) -> Optional[Dict[str, Any]]:
    await ensure_schema()
    row = await get_pool().fetchrow(
        """SELECT r.*, d.title, d.owner_id FROM sec_script_reviews r JOIN sec_documents d ON d.id = r.document_id
           WHERE r.document_id = $1 AND d.deleted_at IS NULL""", document_id)
    return dict(row) if row else None


async def _collaborators(review: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = await get_pool().fetch(
        """SELECT c.user_id, c.role, c.capacity, c.position, lower(u.email) AS email,
                  coalesce(nullif(u.full_name, ''), u.email) AS name, k.vote, k.note, k.checked_at
           FROM sec_script_collaborators c JOIN users u ON u.id = c.user_id
           LEFT JOIN sec_script_checkins k ON k.document_id = c.document_id AND k.user_id = c.user_id AND k.round = $2
           WHERE c.document_id = $1 ORDER BY c.position, name""", review["document_id"], review["round"])
    return [dict(r) for r in rows]


async def _context(user_id: UUID, document_id: str):
    """(review, collaborators, access) for a person; the review's missing or the person may not see it -> an error."""
    await ensure_schema()
    review = await _review(document_id)
    if not review:
        raise ScriptReviewError("This document has no script review", 404)
    collabs = await _collaborators(review)
    _, is_super = await _actor(user_id)
    mine = next((c for c in collabs if c["user_id"] == user_id), None)
    access = rules.compute_access(review["status"], is_creator=review["creator_id"] == user_id, is_super=is_super, mine=mine)
    if access is None:
        raise ScriptReviewError("You don't have access to this script", 403)
    return review, collabs, access


def _view(review: Dict[str, Any], collabs: List[Dict[str, Any]], access: rules.ScriptAccess) -> Dict[str, Any]:
    return {
        "document_id": review["document_id"], "title": review["title"], "status": review["status"], "round": review["round"],
        "creator_id": str(review["creator_id"]), "page_ids": review["page_ids"] if isinstance(review["page_ids"], list)
        else json.loads(review["page_ids"] or "[]"),
        "all_checked": rules.all_checked(review["status"], collabs), "decision_note": review["decision_note"],
        "post_requested": review["post_requested_at"] is not None, "access": access.as_dict(),
        "collaborators": [{"user_id": str(c["user_id"]), "name": c["name"], "email": c["email"], "role": c["role"],
                           "capacity": c["capacity"], "vote": c["vote"], "note": c["note"]} for c in collabs],
    }


async def get_state(user_id: UUID, document_id: str) -> Dict[str, Any]:
    review, collabs, access = await _context(user_id, document_id)
    return {**_view(review, collabs, access), "you": str(user_id)}


async def _record(document_id: str, round_: int, user_id: Optional[UUID], kind: str, **detail: Any) -> None:
    await get_pool().execute(
        "INSERT INTO sec_script_events (document_id, round, user_id, kind, detail) VALUES ($1, $2, $3, $4, $5::jsonb)",
        document_id, round_, user_id, kind, json.dumps(detail, default=str))


async def history(user_id: UUID, document_id: str) -> List[Dict[str, Any]]:
    await _context(user_id, document_id)
    rows = await get_pool().fetch(
        """SELECT e.kind, e.round, e.detail, e.created_at, coalesce(nullif(u.full_name, ''), u.email) AS name
           FROM sec_script_events e LEFT JOIN users u ON u.id = e.user_id WHERE e.document_id = $1 ORDER BY e.id""", document_id)
    return [{"kind": r["kind"], "round": r["round"], "name": r["name"], "at": r["created_at"].isoformat(),
             "detail": json.loads(r["detail"]) if isinstance(r["detail"], str) else r["detail"]} for r in rows]


# --------------------------------------------------------------------------- creating, guarding writes


async def init_review(user_id: UUID, document_id: str) -> None:
    """Start the review of a script document just made from the template (the creator is its owner). Idempotent."""
    await ensure_schema()
    row = await get_pool().fetchrow(
        """INSERT INTO sec_script_reviews (document_id, creator_id)
           SELECT d.id, coalesce(d.owner_id, $2) FROM sec_documents d WHERE d.id = $1
           ON CONFLICT (document_id) DO NOTHING RETURNING creator_id""", document_id, user_id)
    if row:
        await _record(document_id, 1, user_id, "CREATED")
        events.publish("template.created", document_id, user_id, status="DRAFT", template=rules.TEMPLATE_ID)


async def guard_write(user_id: UUID, document_id: str) -> None:
    """Called before a script document or one of its change proposals is saved: only people who may edit it (or a manager
    merging) can. Documents without a script review aren't affected."""
    review = await _review(document_id)
    if not review:
        return
    from services.sec_filings.documents.sec_document_service import DocumentForbidden
    try:
        _, _, access = await _context(user_id, document_id)
    except ScriptReviewError as e:
        raise DocumentForbidden(str(e)) from e
    if not (access.can_edit or access.can_decide):
        raise DocumentForbidden("You can view this script and add notes, but not change it right now")


async def note_document_changed(user_id: UUID, document_id: str) -> None:
    """A change during review clears the check-ins, so everyone looks again."""
    review = await _review(document_id)
    if not review or review["status"] != "IN_REVIEW":
        return
    cleared = await get_pool().fetch(
        "DELETE FROM sec_script_checkins WHERE document_id = $1 AND round = $2 RETURNING user_id", document_id, review["round"])
    if cleared:
        await _record(document_id, review["round"], user_id, "CHECKINS_CLEARED", count=len(cleared))


# --------------------------------------------------------------------------- inviting and sending


async def _set_invitees(review: Dict[str, Any], by: UUID, invitees: List[Any]) -> List[Dict[str, Any]]:
    try:
        ids = [UUID(str(i.user_id)) for i in invitees]
    except ValueError as e:
        raise ScriptReviewError("An invited person's id isn't valid", 400) from e
    if len(set(ids)) != len(ids):
        raise ScriptReviewError("Each person can be invited once", 400)
    if review["creator_id"] in ids:
        raise ScriptReviewError("The creator can't also be a reviewer", 400)
    found = await get_pool().fetch("SELECT id, lower(email) AS email FROM users WHERE id = ANY($1::uuid[])", ids)
    if len(found) != len(ids):
        raise ScriptReviewError("Some invited people aren't users of this portal", 400)
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM sec_script_collaborators WHERE document_id = $1", review["document_id"])
            for pos, inv in enumerate(invitees):
                capacity = "EDIT" if inv.role == "MANAGER" else inv.capacity  # a manager always reviews with edit rights
                await conn.execute(
                    """INSERT INTO sec_script_collaborators (document_id, user_id, role, capacity, position, invited_by)
                       VALUES ($1, $2, $3, $4, $5, $6)""", review["document_id"], UUID(str(inv.user_id)), inv.role, capacity, pos, by)
            # So the document's own access (opening it, proposing, merging) lets them in.
            await conn.execute(
                """UPDATE sec_documents SET
                       invited_user_ids = (SELECT coalesce(jsonb_agg(DISTINCT v), '[]'::jsonb) FROM jsonb_array_elements_text(invited_user_ids || $2::jsonb) v),
                       invited_emails = (SELECT coalesce(jsonb_agg(DISTINCT v), '[]'::jsonb) FROM jsonb_array_elements_text(invited_emails || $3::jsonb) v),
                       revision = revision + 1, updated_at = now()
                   WHERE id = $1""", review["document_id"], json.dumps([str(i) for i in ids]),
                json.dumps([f["email"] for f in found if f["email"]]))
            await conn.execute(
                """UPDATE sec_documents SET content = content || jsonb_build_object(
                       'invitedUserIds', invited_user_ids, 'invitedEmails', invited_emails) WHERE id = $1""", review["document_id"])
    return [{"user_id": str(i.user_id), "role": i.role, "capacity": "EDIT" if i.role == "MANAGER" else i.capacity} for i in invitees]


async def invite(user_id: UUID, document_id: str, invitees: List[Any]) -> Dict[str, Any]:
    """Set who is invited (only the creator, while the script is theirs to edit)."""
    review, _, access = await _context(user_id, document_id)
    if not access.can_submit:
        raise ScriptReviewError("Only the creator can invite reviewers, before sending the script", 403)
    saved = await _set_invitees(review, user_id, invitees)
    await _record(document_id, review["round"], user_id, "INVITED", invitees=saved)
    events.publish("reviewer.invited", document_id, user_id, invitees=saved, round=review["round"])
    return await get_state(user_id, document_id)


async def send_for_review(user_id: UUID, document_id: str, invitees: List[Any], page_ids: List[str], note: Optional[str]) -> Dict[str, Any]:
    review, _, access = await _context(user_id, document_id)
    if not access.can_submit:
        raise ScriptReviewError("Only the creator can send the script for review, while it is a draft or sent back", 403)
    if invitees:
        saved = await _set_invitees(review, user_id, invitees)
        events.publish("reviewer.invited", document_id, user_id, invitees=saved, round=review["round"])
    collabs = await _collaborators(review)
    if not any(rules.is_required(c) for c in collabs):
        raise ScriptReviewError("Invite at least one reviewer or suggesting reviewer who can edit", 400)
    round_ = review["round"] + (1 if review["status"] == "CHANGES_REQUESTED" else 0)
    await get_pool().execute(
        """UPDATE sec_script_reviews SET status = 'IN_REVIEW', round = $2, page_ids = $3::jsonb, decision_note = NULL,
               decided_by = NULL, decided_at = NULL, updated_at = now() WHERE document_id = $1""",
        document_id, round_, json.dumps(page_ids or []))
    await _record(document_id, round_, user_id, "SUBMITTED", note=note, page_ids=page_ids)
    events.publish("review.submitted", document_id, user_id, status="IN_REVIEW", round=round_, pageIds=page_ids or [], note=note)
    return await get_state(user_id, document_id)
