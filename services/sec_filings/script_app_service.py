"""Social Media Script documents for the Social Media portal's backend (app-to-app, see tools/sec_filings/script_app_router.py).

The portal creates a script's shared document here, reads its text back (to run its own checks), invites reviewers and
sends it on a person's behalf, and gets a short-lived token for the iframe that shows the shared editor. Every call names
the person it acts for by email (the two portals have separate user tables, and the same email is the link); nobody is
ever impersonated beyond that one call, and a person who isn't a user here is refused with a clear message.
"""

from __future__ import annotations

import datetime
import re
import secrets
import uuid
from typing import Any, Dict, List, Optional
from uuid import UUID

import jwt

from postgresql_db.database import get_pool
from services.sec_filings import script_review_flow as flow
from services.sec_filings import script_review_rules as rules
from services.sec_filings import script_review_service as review
from services.sec_filings.documents import sec_document_service as docs
from services.sec_filings.script_review_guard import save_document
from services.shared.auth.service_security_policy import JWT_ALGORITHM, JWT_ISSUER, JWT_SECRET

EMBED_TOKEN_MINUTES = 15
SCRIPT_HEADERS = ["Time", "Visual / footage", "Voiceover", "On-screen text"]
# The look of a script page (the navy header row, banded rows), as in the Social Media portal's script document.
HEADER_FILL = "#1F3A5F"
BAND_FILL = "#F1F5F9"
FONT_FAMILY = 'Arial, "Helvetica Neue", Helvetica, sans-serif'  # a script is set in Arial, as in the Social Media portal


class AppServiceError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


async def _ensure_standard_user_role(conn: Any, user_id: UUID) -> None:
    """Assign the Standard User (`USER`) role if the user has no active role yet."""
    has_role = await conn.fetchval(
        "SELECT 1 FROM user_roles WHERE user_id = $1 AND is_active = true LIMIT 1",
        user_id,
    )
    if has_role:
        return
    role = await conn.fetchrow(
        """
        SELECT id FROM roles
        WHERE UPPER(code) = 'USER' OR UPPER(name) = 'STANDARD USER'
        ORDER BY (UPPER(code) = 'USER') DESC
        LIMIT 1
        """
    )
    if not role:
        role = await conn.fetchrow(
            """
            INSERT INTO roles (code, name, description, is_system_role, is_active)
            VALUES ('USER', 'Standard User', 'Standard system user with basic dashboard access', false, true)
            ON CONFLICT (code) DO UPDATE SET is_active = true
            RETURNING id
            """
        )
    if role:
        await conn.execute(
            """
            INSERT INTO user_roles (user_id, role_id, assigned_at, is_active)
            VALUES ($1, $2, now(), true)
            ON CONFLICT (user_id, role_id) DO UPDATE SET is_active = true, assigned_at = now()
            """,
            user_id,
            role["id"],
        )


async def user_by_email(email: str) -> Dict[str, Any]:
    """Return an active Share Document user for `email`, auto-provisioning them with Standard User role if absent."""
    clean_email = (email or "").strip().lower()
    if not clean_email or "@" not in clean_email:
        raise AppServiceError("A valid email is required", 422)
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT id, email, coalesce(nullif(full_name, ''), email) AS name FROM users "
        "WHERE lower(email) = $1 AND deleted_at IS NULL AND is_active = true",
        clean_email,
    )
    if row:
        return dict(row)

    fallback_name = clean_email.split("@", 1)[0].replace(".", " ").replace("_", " ").strip().title() or clean_email
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO users (
                    email, full_name, auth_provider, sso_enabled,
                    is_active, is_super_admin, created_at
                ) VALUES ($1, $2, 'microsoft', true, true, false, now())
                ON CONFLICT (email) DO UPDATE SET
                    full_name = COALESCE(NULLIF(users.full_name, ''), EXCLUDED.full_name),
                    auth_provider = COALESCE(users.auth_provider, 'microsoft'),
                    sso_enabled = true,
                    is_active = true,
                    deleted_at = NULL,
                    deleted_by = NULL
                RETURNING id, email, coalesce(nullif(full_name, ''), email) AS name
                """,
                clean_email,
                fallback_name,
            )
            await _ensure_standard_user_role(conn, row["id"])
    return dict(row)


# --------------------------------------------------------------------------- the document <-> rows


def _row(row_id: str, kind: str, cells: List[str], **extra: Any) -> Dict[str, Any]:
    return {"id": row_id, "type": kind, "cells": cells, **extra}


def build_document(doc_id: str, owner: Dict[str, Any], title: str, details: Dict[str, str], rows: List[Dict[str, str]],
                   external_ref: Optional[Dict[str, Any]], number: int = 1) -> Dict[str, Any]:
    """A SecFilingDocument in the shape of the front's Social Media Script template (data/socialMediaScriptTemplate.ts): the
    page of the Social Media portal's script (numbered title, storyline, hook score | duration | lane, beats, footage note)."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    stamp = uuid.uuid4().hex[:8]
    # The table draws its own header row from `headers`: the rows are only the beats (a header row here would show twice).
    beats = [
        _row(f"sc-r{i + 1}", "data", [r.get("time", ""), r.get("visual", ""), r.get("voiceover", ""), r.get("onscreen", "")],
             **({"shading": BAND_FILL} if i % 2 else {}))
        for i, r in enumerate(rows or [{}] * 5)]
    facts = " | ".join(x for x in (f"Hook score {details.get('Hook score', '')}".strip(), details.get("Duration") or "about 35 seconds",
                                   details.get("Lane") or "Green lane") if x)

    def para(n: int, text: str, **extra: Any) -> Dict[str, Any]:
        return {"id": f"blk-{stamp}-{n}", "type": "paragraph", "section": "Script", "text": text, "alignment": "left",
                "spacingBottom": 4, "fontFamily": FONT_FAMILY, **extra}

    blocks = [
        {"id": f"blk-{stamp}-0", "type": "heading", "section": "Script", "level": 2, "text": f"{number}. {title}", "alignment": "left",
         "bold": True, "color": HEADER_FILL, "spacingBottom": 4, "fontFamily": FONT_FAMILY},
        para(1, f"Storyline: {details.get('Storyline', '')}", spacingBottom=4),
        para(2, facts, color="#6b7280", spacingBottom=8),
        {"id": f"blk-{stamp}-3", "type": "financial_table", "section": "Script", "title": "Script", "headers": SCRIPT_HEADERS,
         "headerShading": HEADER_FILL, "headerTextColor": "#FFFFFF", "boldColumns": [3], "columnAlignments": ["left"] * 4,
         "columnWidths": ["7%", "29%", "40%", "24%"], "spacingTop": 2, "spacingBottom": 8, "rows": beats},
    ]
    if (details.get("Description") or "").strip():  # the note line is there when the script has one, as in the document
        blocks.append(para(4, f"Footage and compliance note: {details['Description']}"))
    return {
        "id": doc_id, "title": title, "templateType": rules.TEMPLATE_ID, "formType": rules.FORM_TYPE, "period": "",
        "status": "draft", "version": "v1.0.0", "versionNumber": 1, "blocks": blocks, "createdAt": now, "updatedAt": now,
        "lastModifiedBy": owner["name"], "ownerId": str(owner["id"]), "ownerEmail": (owner["email"] or "").lower(),
        "externalRef": external_ref or None,
    }


_FACTS = re.compile(r"^hook score\s*([^|]*)\|\s*([^|]*)\|\s*(.*)$", re.IGNORECASE)


def read_script(document: Dict[str, Any]) -> Dict[str, Any]:
    """Details and beats of a script document, plus its text as the portal's checks read it (one line per beat part). Reads the
    page format (title, storyline, facts and note paragraphs, a table of beats) and the older details-table format."""
    details: Dict[str, str] = {}
    beats: List[Dict[str, str]] = []
    for block in document.get("blocks") or []:
        kind, text = block.get("type"), (block.get("text") or "").strip()
        facts = _FACTS.match(text) if kind == "paragraph" else None
        if kind == "paragraph" and text.lower().startswith("storyline:"):
            details["Storyline"] = text.split(":", 1)[1].strip()
        elif facts:
            details["Hook score"], details["Duration"], details["Lane"] = facts.group(1).strip(), facts.group(2).strip(), facts.group(3).strip()
        elif kind == "paragraph" and text.lower().startswith("footage and compliance note"):
            details["Description"] = text.split(":", 1)[1].strip()
        elif kind == "financial_table":
            data = [r["cells"] for r in block.get("rows") or [] if r.get("type") == "data"]
            if block.get("section") == "Script Details":
                details.update({c[0]: (c[1] if len(c) > 1 else "") for c in data if c})
            elif block.get("section") == "Script":
                beats = [dict(zip(("time", "visual", "voiceover", "onscreen"), [*c, "", "", "", ""][:4])) for c in data]
    lines = [f"{k}: {v}" for k, v in details.items() if v]
    for b in beats:
        if any(b.values()):
            lines.append(f"[{b['time']}] Visual: {b['visual']} | Voiceover: {b['voiceover']} | On-screen: {b['onscreen']}")
    return {"title": document.get("title"), "details": details, "beats": beats, "text": "\n".join(lines),
            "voiceover": " ".join(b["voiceover"] for b in beats if b["voiceover"]),
            "onscreen": " ".join(b["onscreen"] for b in beats if b["onscreen"])}


# --------------------------------------------------------------------------- what the portal calls


async def create_script(email: str, title: str, details: Dict[str, str], rows: List[Dict[str, str]],
                        external_ref: Optional[Dict[str, Any]], number: int = 1) -> Dict[str, Any]:
    # Every script, blank or attached, is made from the Social Media Script template (build_document): the same page, tables and
    # checks whoever creates it and however; its beats come from the attached file or are left empty.
    owner = await user_by_email(email)
    doc_id = f"doc-social-script-{uuid.uuid4().hex[:12]}"
    document = build_document(doc_id, owner, title, details, rows, external_ref, number)
    result = await save_document(owner["id"], doc_id, document, None, rules.TEMPLATE_ID, owner["name"])
    return {"document_id": doc_id, "revision": result["revision"]}


DELETABLE = ("DRAFT", "CHANGES_REQUESTED")


async def delete_scripts(actor_email: str, document_ids: List[str]) -> Dict[str, Any]:
    """Delete script documents (soft, like every delete here) as `actor_email`: their owner or a super admin. Only script
    documents with no review in progress go; the rest are listed in `skipped` with why, and the others still go."""
    user = await user_by_email(actor_email)
    pool = get_pool()
    deleted: List[str] = []
    skipped: List[Dict[str, str]] = []
    async with pool.acquire() as conn:
        _, is_super = await docs._actor(conn, user["id"])
        for doc_id in dict.fromkeys(document_ids):
            row = await conn.fetchrow(
                """SELECT d.owner_id, d.template_type, r.status FROM sec_documents d
                   LEFT JOIN sec_script_reviews r ON r.document_id = d.id WHERE d.id = $1 AND d.deleted_at IS NULL""", doc_id)
            if not row:
                continue  # already gone: nothing to do
            if row["template_type"] != rules.TEMPLATE_ID:
                skipped.append({"id": doc_id, "reason": "Not a script document"})
            elif not is_super and row["owner_id"] != user["id"]:
                skipped.append({"id": doc_id, "reason": "Only the owner can delete a script"})
            elif row["status"] and row["status"] not in DELETABLE:
                skipped.append({"id": doc_id, "reason": "It is in review"})
            else:
                await conn.execute("UPDATE sec_documents SET deleted_at = now(), deleted_by = $2 WHERE id = $1", doc_id, user["id"])
                deleted.append(doc_id)
    return {"deleted": deleted, "skipped": skipped}


async def get_script(document_id: str, email: str) -> Dict[str, Any]:
    user = await user_by_email(email)
    try:
        got = await docs.get_document(user["id"], document_id)
    except (docs.DocumentNotFound, docs.DocumentForbidden) as e:
        raise AppServiceError("That script isn't there, or this person can't open it", 404) from e
    return {**read_script(got["document"]), "revision": got["revision"], "version": got["document"].get("version")}


async def get_review(document_id: str, email: str) -> Dict[str, Any]:
    return await review.get_state((await user_by_email(email))["id"], document_id)


async def send_script(document_id: str, actor_email: str, invitees: List[Any], page_ids: List[str], note: Optional[str]) -> Dict[str, Any]:
    """Invite and send for review as the creator. `invitees` carry an email, a role and a capacity."""
    from models.sec_filings.script_review_model import ScriptInvitee

    actor = await user_by_email(actor_email)
    people = []
    for inv in invitees:
        try:
            person = await user_by_email(inv.email)
        except AppServiceError:
            if inv.optional:
                continue
            raise
        people.append(ScriptInvitee(user_id=str(person["id"]), role=inv.role, capacity=inv.capacity))
    return await review.send_for_review(actor["id"], document_id, people, page_ids, note)


async def act(document_id: str, actor_email: str, action: str, vote: Optional[str], route: Optional[str],
              decision: Optional[str], note: Optional[str]) -> Dict[str, Any]:
    """A move in the review on a person's behalf: check_in, withdraw, finalize, decide, create_post."""
    uid: UUID = (await user_by_email(actor_email))["id"]
    if action == "check_in":
        return await flow.check_in(uid, document_id, vote or "", note)
    if action == "withdraw":
        return await flow.withdraw_check_in(uid, document_id)
    if action == "finalize":
        return await flow.finalize(uid, document_id, route, note)
    if action == "decide":
        return await flow.decide(uid, document_id, decision or "", note)
    if action == "create_post":
        return await flow.create_post(uid, document_id, note)
    raise AppServiceError(f"Unknown action {action}", 400)


async def embed_token(document_id: str, email: str) -> Dict[str, Any]:
    """A short-lived sign-in for the iframe, for one person; it only works once the person may open the document."""
    user = await user_by_email(email)
    try:
        await docs.check_document_access(user["id"], document_id)
    except docs.DocumentForbidden as e:
        raise AppServiceError("This person can't open that script", 403) from e
    now = datetime.datetime.now(datetime.timezone.utc)
    claims = {"sub": str(user["id"]), "iss": JWT_ISSUER, "iat": now, "jti": secrets.token_urlsafe(16), "embed_doc": document_id,
              "exp": now + datetime.timedelta(minutes=EMBED_TOKEN_MINUTES)}
    return {"token": jwt.encode(claims, JWT_SECRET, algorithm=JWT_ALGORITHM), "expires_in": EMBED_TOKEN_MINUTES * 60}
