"""Adopt Create Post scripts once; retries never overwrite shared edits or decisions."""

import json
from uuid import UUID
from models.sec_filings.script_review_model import AppImportSubmission, ScriptInvitee
from postgresql_db.database import get_pool
from services.sec_filings import script_app_service as app, script_review_service as review


async def _people(payload: AppImportSubmission, owner):
    people = {}
    for inv in payload.invitees:
        try:
            person = await app.user_by_email(inv.email)
        except app.AppServiceError:
            if inv.optional:
                continue
            raise
        if person["id"] != owner["id"]:
            people[person["id"]] = ScriptInvitee(user_id=str(person["id"]), role=inv.role, capacity=inv.capacity)
    if payload.status == "IN_REVIEW" and not any(p.capacity == "EDIT" and p.role != "MANAGER" for p in people.values()):
        raise app.AppServiceError("Pick a reviewer other than the creator for shared-document review", 422)
    return list(people.values())


async def _document(conn, payload, owner):
    row = await conn.fetchrow("SELECT owner_id, content, deleted_at FROM sec_documents WHERE id = $1 FOR UPDATE", payload.document_id)
    if row:
        content = json.loads(row["content"]) if isinstance(row["content"], str) else dict(row["content"])
        ref = content.get("externalRef") or {}
        if row["deleted_at"] or row["owner_id"] != owner["id"] or ref.get("sourceHash") != payload.source_hash:
            raise app.AppServiceError("This upload already has a shared document. Open that document to continue its review.", 409)
        return
    ref = {**(payload.external_ref or {}), "sourceHash": payload.source_hash}
    document = app.build_document(payload.document_id, owner, payload.title,
                                  {str(k): str(v) for k, v in payload.details.items()},
                                  [b.model_dump() for b in payload.rows], ref, payload.number)
    if not any(payload.details.get(k) for k in ("Hook score", "Duration", "Lane")):
        document["blocks"] = [b for b in document["blocks"] if not (b.get("type") == "paragraph" and b.get("text", "").startswith("Hook score"))]
    # New documents only. Existing shared content is never overwritten by an import retry.
    await conn.execute(
        "INSERT INTO sec_documents (id, title, form_type, template_type, owner_id, owner_email, owner_name, content, created_by, updated_by) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $5, $5)", payload.document_id, payload.title,
        app.rules.FORM_TYPE, app.rules.TEMPLATE_ID, owner["id"], owner["email"], owner["name"], json.dumps(document))


async def _initialize(conn, payload, owner, people):
    await conn.execute(
        "INSERT INTO sec_script_reviews (document_id, creator_id, status, round) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (document_id) DO NOTHING", payload.document_id, owner["id"], payload.status, payload.round)
    initialized = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM sec_script_events WHERE document_id = $1 AND kind = 'IMPORTED')", payload.document_id)
    if initialized:
        return
    await conn.execute("DELETE FROM sec_script_collaborators WHERE document_id = $1", payload.document_id)
    ids = [p.user_id for p in people]
    emails = await conn.fetch("SELECT lower(email) AS email FROM users WHERE id = ANY($1::uuid[])", [UUID(i) for i in ids])
    await conn.executemany(
        "INSERT INTO sec_script_collaborators (document_id, user_id, role, capacity, position, invited_by) VALUES ($1, $2, $3, $4, $5, $6)",
        [(payload.document_id, UUID(p.user_id), p.role, "EDIT" if p.role == "MANAGER" else p.capacity, pos, owner["id"])
         for pos, p in enumerate(people)])
    await conn.execute(
        "UPDATE sec_documents SET invited_user_ids = $2::jsonb, invited_emails = $3::jsonb, "
        "content = content || jsonb_build_object('invitedUserIds', $2::jsonb, 'invitedEmails', $3::jsonb), revision = revision + 1 WHERE id = $1",
        payload.document_id, json.dumps(ids), json.dumps([r["email"] for r in emails]))
    await conn.execute(
        "UPDATE sec_script_reviews SET status = $2, round = $3, updated_at = now() WHERE document_id = $1",
        payload.document_id, payload.status, payload.round)
    await conn.execute(
        "INSERT INTO sec_script_events (document_id, round, user_id, kind) VALUES ($1, $2, $3, 'IMPORTED')",
        payload.document_id, payload.round, owner["id"])


async def import_submission(payload: AppImportSubmission):
    owner = await app.user_by_email(payload.user_email)
    people = await _people(payload, owner)
    await review.ensure_schema()
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # One connection and transaction: retries cannot duplicate records or leave a partially imported review.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", payload.document_id)
            await _document(conn, payload, owner)
            await _initialize(conn, payload, owner, people)
            state = await conn.fetchrow("SELECT status, round FROM sec_script_reviews WHERE document_id = $1", payload.document_id)
    result = await app.get_script(payload.document_id, payload.user_email)
    return {**result, "document_id": payload.document_id, **dict(state)}
