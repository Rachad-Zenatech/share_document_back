"""No-login guest access for external contributors: one change proposal, nothing else.

A guest invite link carries a high-entropy token in its URL fragment, so it is never
sent to a server or written to access logs as part of the URL. The browser sends it in
the X-Invite-Token header instead. A valid token lets the holder:

  - read that one proposal, plus the document it branches from (minus sharing and
    ownership fields) and the document's linked workbook, to show the diff;
  - save that proposal's editable fields and submit it for review, while it is a draft.

It grants nothing else: no other proposals, documents, versions, spreadsheet writes,
users or endpoints. Authorship, permissions, the assigned section, review fields and
every status change other than draft -> pending_review always come from the stored
copy, never from the guest. With an assigned section, blocks outside it are restored
from the stored copy on every save.

Tokens are registered by `sync_guest_invite` when a signed-in user with access to the
document saves the proposal (sec_document_service.save_proposal). Rotating the token
on the proposal revokes the old link.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from postgresql_db.database import get_pool
from postgresql_db.schemas.sec_filing_schema import ensure_schema
from services.sec_filings.sec_filing_spreadsheet_service import get_document_spreadsheet

GUEST_INVITE_TTL_DAYS = int(os.getenv("SEC_GUEST_INVITE_TTL_DAYS", "30"))

# "g1_" + at least 32 random bytes, base64url. Older links carried guessable
# "inv-<timestamp>" tokens; those never grant guest access.
_GUEST_TOKEN_RE = re.compile(r"^g1_[A-Za-z0-9_-]{43,128}$")

_MAX_BLOCKS = 5000
_MAX_PROPOSAL_BYTES = 10 * 1024 * 1024

# Sharing and ownership details a guest has no need to see.
_HIDDEN_DOCUMENT_FIELDS = ("invitedEmails", "invitedUserIds", "owner", "ownerId", "ownerEmail", "lockedBy")


class GuestInviteInvalid(Exception):
    """Unknown, revoked or expired token, or the proposal/document is gone."""


class GuestProposalLocked(Exception):
    """The proposal is no longer a draft (submitted, merged or rejected)."""


class GuestEditRejected(Exception):
    pass


def is_guest_token(token: Any) -> bool:
    return isinstance(token, str) and bool(_GUEST_TOKEN_RE.match(token))


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load(value: Any) -> Any:
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def sync_guest_invite(conn, proposal_id: str, token: Any, user_id: UUID) -> None:
    """Make the proposal's guest link match its `inviteToken`. Called inside save_proposal,
    after the caller's access to the document was checked."""
    if not is_guest_token(token):
        await conn.execute("DELETE FROM sec_proposal_guest_invites WHERE proposal_id = $1", proposal_id)
        return
    # Re-saving the same token keeps its expiry; a new token restarts it.
    await conn.execute(
        """
        INSERT INTO sec_proposal_guest_invites (proposal_id, token_hash, expires_at, created_by)
        VALUES ($1, $2, now() + make_interval(days => $3), $4)
        ON CONFLICT (proposal_id) DO UPDATE
        SET token_hash = EXCLUDED.token_hash,
            expires_at = EXCLUDED.expires_at,
            created_at = now(),
            created_by = EXCLUDED.created_by,
            last_used_at = NULL
        WHERE sec_proposal_guest_invites.token_hash <> EXCLUDED.token_hash
        """,
        proposal_id,
        _hash(token),
        GUEST_INVITE_TTL_DAYS,
        user_id,
    )


async def _resolve(conn, token: str | None, *, lock: bool = False):
    if not is_guest_token(token):
        raise GuestInviteInvalid()
    row = await conn.fetchrow(
        f"""
        SELECT p.id, p.document_id, p.status, p.data, d.content AS document, i.expires_at
        FROM sec_proposal_guest_invites i
        JOIN sec_document_proposals p ON p.id = i.proposal_id
        JOIN sec_documents d ON d.id = p.document_id AND d.deleted_at IS NULL
        WHERE i.token_hash = $1 AND i.expires_at > now()
        {"FOR UPDATE OF p" if lock else ""}
        """,
        _hash(token),
    )
    if not row:
        raise GuestInviteInvalid()
    return row


async def get_guest_session(token: str | None) -> dict[str, Any]:
    await ensure_schema()
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await _resolve(conn, token)
        await conn.execute(
            "UPDATE sec_proposal_guest_invites SET last_used_at = now() WHERE proposal_id = $1", row["id"]
        )

    document = dict(_load(row["document"]) or {})
    for field in _HIDDEN_DOCUMENT_FIELDS:
        document.pop(field, None)
    document["id"] = row["document_id"]

    return {
        "documentId": row["document_id"],
        "document": document,
        "proposal": _load(row["data"]),
        "spreadsheet": await get_document_spreadsheet(row["document_id"]),
        "expiresAt": row["expires_at"].isoformat(),
    }


def _scope_to_section(stored_blocks: list, incoming_blocks: list, section: str) -> list:
    """Keep every block outside `section` exactly as stored; take the guest's blocks
    for the section, placed where the section starts in the stored copy."""
    mine = [b for b in incoming_blocks if b.get("section") == section]
    result: list = []
    placed = False
    for block in stored_blocks:
        if isinstance(block, dict) and block.get("section") == section:
            if not placed:
                result.extend(mine)
                placed = True
            continue
        result.append(block)
    if not placed:
        result.extend(mine)
    return result


def _apply_guest_edits(stored: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    perms = stored.get("permissions") or {}
    merged = dict(stored)

    blocks = incoming.get("blocks")
    if blocks is not None and blocks != stored.get("blocks"):
        if perms.get("canEditDocument") is False:
            raise GuestEditRejected("This invite does not allow editing the document.")
        if not isinstance(blocks, list) or len(blocks) > _MAX_BLOCKS or not all(isinstance(b, dict) for b in blocks):
            raise GuestEditRejected("Invalid document blocks.")
        section = stored.get("assignedSection")
        if section and section != "ALL":
            blocks = _scope_to_section(stored.get("blocks") or [], blocks, section)
        merged["blocks"] = blocks

    # Only the proposal's own copy of the workbook; the document's workbook is never written.
    if perms.get("canEditSpreadsheet") is True:
        sheet = incoming.get("attachedSpreadsheet", stored.get("attachedSpreadsheet"))
        diffs = incoming.get("spreadsheetDiffs", stored.get("spreadsheetDiffs"))
        if sheet is not None and not isinstance(sheet, dict):
            raise GuestEditRejected("Invalid spreadsheet.")
        if diffs is not None and not isinstance(diffs, list):
            raise GuestEditRejected("Invalid spreadsheet changes.")
        merged["attachedSpreadsheet"] = sheet
        merged["spreadsheetDiffs"] = diffs

    summary = incoming.get("changeSummary")
    if isinstance(summary, dict):
        stored_summary = stored.get("changeSummary") or {}
        merged["changeSummary"] = {
            "addedCount": _count(summary.get("addedCount")),
            "modifiedCount": _count(summary.get("modifiedCount")),
            "deletedCount": _count(summary.get("deletedCount")),
            "description": str(summary.get("description") or stored_summary.get("description") or "")[:2000],
        }

    if isinstance(incoming.get("submissionNotes"), str):
        merged["submissionNotes"] = incoming["submissionNotes"][:5000]

    now = _now_iso()
    merged["updatedAt"] = now
    if incoming.get("status") == "pending_review":
        merged["status"] = "pending_review"
        merged["submittedAt"] = now
    else:
        merged["status"] = "draft"

    if len(json.dumps(merged)) > _MAX_PROPOSAL_BYTES:
        raise GuestEditRejected("These changes are too large to save.")
    return merged


async def save_guest_proposal(token: str | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """Apply a guest's edits (and optional submission) to their proposal. Returns the
    stored proposal so the client can adopt exactly what was kept."""
    await ensure_schema()
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await _resolve(conn, token, lock=True)
            if row["status"] != "draft":
                raise GuestProposalLocked()
            stored = _load(row["data"]) or {}
            merged = _apply_guest_edits(stored, incoming)
            merged["id"] = row["id"]
            merged["documentId"] = row["document_id"]
            await conn.execute(
                """
                UPDATE sec_document_proposals
                SET status = $2, data = $3::jsonb, updated_at = now(), updated_by = NULL
                WHERE id = $1
                """,
                row["id"],
                merged["status"],
                json.dumps(merged),
            )
            await conn.execute(
                "UPDATE sec_proposal_guest_invites SET last_used_at = now() WHERE proposal_id = $1", row["id"]
            )
    return merged
