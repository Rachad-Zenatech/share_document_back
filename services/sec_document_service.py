"""DB logic for filing documents, their change proposals and version snapshots.

Documents used to live only in each browser's localStorage. They are stored here
now so they are shared between users and devices. The frontend keeps the same
document/proposal/snapshot JSON shapes; this module stores them as JSONB and adds
access control and optimistic concurrency (`revision`).

Access to a document: super admins, its owner, anyone in its invited emails / user
ids, and authors of a proposal on it. Only the owner or a super admin may delete it.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from uuid import UUID

from postgresql_db.database import get_pool
from postgresql_db.sec_filing_schema import ensure_schema


class DocumentNotFound(Exception):
    pass


class DocumentForbidden(Exception):
    pass


class DocumentConflict(Exception):
    """The save was based on an older revision than the one stored."""

    def __init__(self, revision: int, document: dict[str, Any]):
        super().__init__("document was changed by someone else")
        self.revision = revision
        self.document = document


# $1 = user id (uuid), $2 = lower-cased email, $3 = is super admin
_ACCESS_SQL = """(
    $3::boolean
    OR d.owner_id = $1::uuid
    OR d.invited_user_ids ? ($1::uuid)::text
    OR d.invited_emails ? $2::text
    OR EXISTS (
        SELECT 1 FROM sec_document_proposals p
        WHERE p.document_id = d.id AND p.author_email = $2::text
    )
)"""

_PREVIEW_BLOCKS = 15


def _load(value: Any) -> Any:
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def _lower_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return sorted({str(v).strip().lower() for v in values if v and str(v).strip()})


def _as_uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _clean_content(document: dict[str, Any], document_id: str) -> dict[str, Any]:
    content = dict(document)
    content["id"] = document_id
    # The linked workbook is resolved from the spreadsheet registry when the document
    # is opened; storing a copy here would only go stale.
    content.pop("attachedSpreadsheet", None)
    return content


async def _actor(conn, user_id: UUID) -> tuple[str, bool]:
    row = await conn.fetchrow(
        """
        SELECT lower(u.email) AS email,
               (u.is_super_admin OR EXISTS (
                   SELECT 1 FROM user_roles ur JOIN roles r ON r.id = ur.role_id
                   WHERE ur.user_id = u.id AND ur.is_active AND r.code = 'SUPER_ADMIN'
               )) AS is_super
        FROM users u
        WHERE u.id = $1
        """,
        user_id,
    )
    if not row:
        raise DocumentForbidden("unknown user")
    return row["email"] or "", bool(row["is_super"])


async def _check_access(conn, user_id: UUID, email: str, is_super: bool, document_id: str) -> dict[str, Any]:
    row = await conn.fetchrow(
        f"""
        SELECT d.id, d.owner_id, d.revision, d.content, {_ACCESS_SQL} AS allowed
        FROM sec_documents d
        WHERE d.id = $4 AND d.deleted_at IS NULL
        """,
        user_id,
        email,
        is_super,
        document_id,
    )
    if not row:
        raise DocumentNotFound(document_id)
    if not row["allowed"]:
        raise DocumentForbidden(document_id)
    return dict(row)


def _summary(row: Any) -> dict[str, Any]:
    content = _load(row["content"]) or {}
    invited_emails = _load(row["invited_emails"]) or []
    invited_ids = _load(row["invited_user_ids"]) or []
    return {
        "id": row["id"],
        "title": row["title"],
        "formType": row["form_type"] or content.get("formType") or "SEC Filing",
        "period": row["period"],
        "templateType": row["template_type"],
        "owner": row["owner_name"] or row["owner_email"] or "",
        "ownerId": str(row["owner_id"]) if row["owner_id"] else None,
        "ownerEmail": row["owner_email"],
        "invitedEmails": invited_emails,
        "invitedUserIds": invited_ids,
        "isShared": bool(invited_emails or invited_ids),
        "version": content.get("version") or "v1.0",
        "versionNumber": content.get("versionNumber"),
        "blocksCount": row["blocks_count"] or 0,
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat(),
        "revision": row["revision"],
        "previewBlocks": _load(row["preview_blocks"]) or [],
    }


async def list_documents(user_id: UUID) -> list[dict[str, Any]]:
    await ensure_schema()
    pool = get_pool()
    async with pool.acquire() as conn:
        email, is_super = await _actor(conn, user_id)
        rows = await conn.fetch(
            f"""
            SELECT d.id, d.title, d.form_type, d.period, d.template_type, d.owner_id,
                   d.owner_email, d.owner_name, d.invited_emails, d.invited_user_ids,
                   d.revision, d.created_at, d.updated_at,
                   d.content - 'blocks' AS content,
                   jsonb_array_length(COALESCE(d.content->'blocks', '[]'::jsonb)) AS blocks_count,
                   jsonb_path_query_array(d.content, '$.blocks[0 to {_PREVIEW_BLOCKS - 1}]') AS preview_blocks
            FROM sec_documents d
            WHERE d.deleted_at IS NULL AND {_ACCESS_SQL}
            ORDER BY d.updated_at DESC
            """,
            user_id,
            email,
            is_super,
        )
    return [_summary(r) for r in rows]


async def get_document(user_id: UUID, document_id: str) -> dict[str, Any]:
    await ensure_schema()
    pool = get_pool()
    async with pool.acquire() as conn:
        email, is_super = await _actor(conn, user_id)
        row = await _check_access(conn, user_id, email, is_super, document_id)
    return {"document": _load(row["content"]), "revision": row["revision"]}


async def save_document(
    user_id: UUID,
    document_id: str,
    document: dict[str, Any],
    base_revision: int | None,
    template_type: str | None = None,
    owner_name: str | None = None,
) -> dict[str, Any]:
    """Create the document, or update it if `base_revision` still matches."""
    await ensure_schema()
    content = _clean_content(document, document_id)
    invited_emails = _lower_list(content.get("invitedEmails"))
    invited_ids = _lower_list(content.get("invitedUserIds"))
    content_json = json.dumps(content)

    pool = get_pool()
    async with pool.acquire() as conn:
        email, is_super = await _actor(conn, user_id)
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT id, deleted_at FROM sec_documents WHERE id = $1 FOR UPDATE", document_id
            )
            if existing and existing["deleted_at"] is not None:
                raise DocumentNotFound(document_id)

            if not existing:
                owner_id = _as_uuid(content.get("ownerId"))
                if owner_id and not await conn.fetchval("SELECT 1 FROM users WHERE id = $1", owner_id):
                    owner_id = None
                row = await conn.fetchrow(
                    """
                    INSERT INTO sec_documents
                        (id, title, form_type, period, template_type, owner_id, owner_email, owner_name,
                         invited_emails, invited_user_ids, content, created_by, updated_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11::jsonb, $12, $12)
                    RETURNING revision
                    """,
                    document_id,
                    content.get("title") or "Untitled Document",
                    content.get("formType"),
                    content.get("period"),
                    template_type,
                    owner_id or user_id,
                    (content.get("ownerEmail") or email or None),
                    owner_name,
                    json.dumps(invited_emails),
                    json.dumps(invited_ids),
                    content_json,
                    user_id,
                )
                return {"revision": row["revision"], "created": True}

            current = await _check_access(conn, user_id, email, is_super, document_id)
            if base_revision is not None and base_revision != current["revision"]:
                raise DocumentConflict(current["revision"], _load(current["content"]))

            row = await conn.fetchrow(
                """
                UPDATE sec_documents
                SET content = $2::jsonb,
                    title = COALESCE($3, title),
                    form_type = COALESCE($4, form_type),
                    period = COALESCE($5, period),
                    template_type = COALESCE($6, template_type),
                    invited_emails = $7::jsonb,
                    invited_user_ids = $8::jsonb,
                    revision = revision + 1,
                    updated_at = now(),
                    updated_by = $9
                WHERE id = $1
                RETURNING revision
                """,
                document_id,
                content_json,
                content.get("title"),
                content.get("formType"),
                content.get("period"),
                template_type,
                json.dumps(invited_emails),
                json.dumps(invited_ids),
                user_id,
            )
    return {"revision": row["revision"], "created": False}


async def delete_document(user_id: UUID, document_id: str) -> bool:
    await ensure_schema()
    pool = get_pool()
    async with pool.acquire() as conn:
        email, is_super = await _actor(conn, user_id)
        row = await _check_access(conn, user_id, email, is_super, document_id)
        if not is_super and row["owner_id"] != user_id:
            raise DocumentForbidden("only the owner can delete a document")
        await conn.execute(
            "UPDATE sec_documents SET deleted_at = now(), deleted_by = $2 WHERE id = $1",
            document_id,
            user_id,
        )
    return True


# --------------------------------------------------------------------------- proposals


async def list_proposals(user_id: UUID, document_id: str) -> list[dict[str, Any]]:
    await ensure_schema()
    pool = get_pool()
    async with pool.acquire() as conn:
        email, is_super = await _actor(conn, user_id)
        await _check_access(conn, user_id, email, is_super, document_id)
        rows = await conn.fetch(
            "SELECT data FROM sec_document_proposals WHERE document_id = $1 ORDER BY updated_at DESC",
            document_id,
        )
    return [_load(r["data"]) for r in rows]


async def get_proposal(user_id: UUID, proposal_id: str) -> dict[str, Any]:
    await ensure_schema()
    pool = get_pool()
    async with pool.acquire() as conn:
        email, is_super = await _actor(conn, user_id)
        row = await conn.fetchrow(
            "SELECT document_id, data FROM sec_document_proposals WHERE id = $1", proposal_id
        )
        if not row:
            raise DocumentNotFound(proposal_id)
        await _check_access(conn, user_id, email, is_super, row["document_id"])
    return {"documentId": row["document_id"], "proposal": _load(row["data"])}


async def save_proposal(user_id: UUID, proposal_id: str, document_id: str, proposal: dict[str, Any]) -> bool:
    await ensure_schema()
    data = dict(proposal)
    data["id"] = proposal_id
    data["documentId"] = document_id
    author_email = str((data.get("author") or {}).get("email") or "").strip().lower() or None

    pool = get_pool()
    async with pool.acquire() as conn:
        email, is_super = await _actor(conn, user_id)
        await _check_access(conn, user_id, email, is_super, document_id)
        moved = await conn.fetchval(
            "SELECT document_id <> $2 FROM sec_document_proposals WHERE id = $1", proposal_id, document_id
        )
        if moved:
            raise DocumentForbidden("a proposal cannot be moved to another document")
        await conn.execute(
            """
            INSERT INTO sec_document_proposals (id, document_id, status, author_email, data, created_by, updated_by)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $6)
            ON CONFLICT (id) DO UPDATE
            SET status = EXCLUDED.status,
                author_email = EXCLUDED.author_email,
                data = EXCLUDED.data,
                updated_at = now(),
                updated_by = EXCLUDED.updated_by
            """,
            proposal_id,
            document_id,
            data.get("status") or "draft",
            author_email,
            json.dumps(data),
            user_id,
        )
    return True


# --------------------------------------------------------------------------- versions


async def list_versions(user_id: UUID, document_id: str) -> list[dict[str, Any]]:
    await ensure_schema()
    pool = get_pool()
    async with pool.acquire() as conn:
        email, is_super = await _actor(conn, user_id)
        await _check_access(conn, user_id, email, is_super, document_id)
        rows = await conn.fetch(
            "SELECT data FROM sec_document_versions WHERE document_id = $1 ORDER BY created_at",
            document_id,
        )
    return [_load(r["data"]) for r in rows]


async def add_version(user_id: UUID, document_id: str, snapshot: dict[str, Any]) -> bool:
    await ensure_schema()
    data = dict(snapshot)
    data["documentId"] = document_id
    snapshot_id = str(data.get("id") or f"snap-{uuid.uuid4().hex[:12]}")
    data["id"] = snapshot_id

    pool = get_pool()
    async with pool.acquire() as conn:
        email, is_super = await _actor(conn, user_id)
        await _check_access(conn, user_id, email, is_super, document_id)
        # Snapshots are immutable history; re-sending one is a no-op.
        await conn.execute(
            """
            INSERT INTO sec_document_versions (id, document_id, version_number, data, created_by)
            VALUES ($1, $2, $3, $4::jsonb, $5)
            ON CONFLICT (id) DO NOTHING
            """,
            snapshot_id,
            document_id,
            data.get("versionNumber") if isinstance(data.get("versionNumber"), int) else None,
            json.dumps(data),
            user_id,
        )
    return True


# --------------------------------------------------------------------------- one-time import


async def import_from_browser(
    user_id: UUID,
    documents: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    versions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bulk-load what a browser had in localStorage. Existing ids are never overwritten,
    so a second browser with an older copy of the same document cannot clobber it."""
    await ensure_schema()
    docs = [d for d in documents if isinstance(d, dict) and d.get("id")]
    doc_ids = [str(d["id"]) for d in docs]
    doc_contents = [json.dumps(_clean_content(d, str(d["id"]))) for d in docs]
    doc_emails = [json.dumps(_lower_list(d.get("invitedEmails"))) for d in docs]
    doc_uids = [json.dumps(_lower_list(d.get("invitedUserIds"))) for d in docs]
    doc_templates = [d.get("templateType") for d in docs]

    pool = get_pool()
    async with pool.acquire() as conn:
        email, is_super = await _actor(conn, user_id)
        async with conn.transaction():
            inserted = await conn.fetch(
                """
                INSERT INTO sec_documents
                    (id, title, form_type, period, template_type, owner_id, owner_email,
                     invited_emails, invited_user_ids, content, created_by, updated_by)
                SELECT t.id, COALESCE(t.content::jsonb->>'title', 'Untitled Document'),
                       t.content::jsonb->>'formType', t.content::jsonb->>'period', t.template_type,
                       $6, $7, t.emails::jsonb, t.uids::jsonb, t.content::jsonb, $6, $6
                FROM unnest($1::varchar[], $2::text[], $3::text[], $4::text[], $5::varchar[])
                     AS t(id, content, emails, uids, template_type)
                ON CONFLICT (id) DO NOTHING
                RETURNING id
                """,
                doc_ids,
                doc_contents,
                doc_emails,
                doc_uids,
                doc_templates,
                user_id,
                email or None,
            )

            # Child rows only for documents this user can now see.
            accessible = {
                r["id"]
                for r in await conn.fetch(
                    f"SELECT d.id FROM sec_documents d WHERE d.deleted_at IS NULL AND {_ACCESS_SQL}",
                    user_id,
                    email,
                    is_super,
                )
            }

            props = [
                p for p in proposals
                if isinstance(p, dict) and p.get("id") and p.get("documentId") in accessible
            ]
            proposals_inserted = await conn.fetch(
                """
                INSERT INTO sec_document_proposals (id, document_id, status, author_email, data, created_by, updated_by)
                SELECT t.id, t.data::jsonb->>'documentId', COALESCE(t.data::jsonb->>'status', 'draft'),
                       lower(t.data::jsonb->'author'->>'email'), t.data::jsonb, $3, $3
                FROM unnest($1::varchar[], $2::text[]) AS t(id, data)
                ON CONFLICT (id) DO NOTHING
                RETURNING id
                """,
                [str(p["id"]) for p in props],
                [json.dumps(p) for p in props],
                user_id,
            )

            snaps = [
                v for v in versions
                if isinstance(v, dict) and v.get("id") and v.get("documentId") in accessible
            ]
            versions_inserted = await conn.fetch(
                """
                INSERT INTO sec_document_versions (id, document_id, version_number, data, created_by)
                SELECT t.id, t.data::jsonb->>'documentId',
                       CASE WHEN (t.data::jsonb->>'versionNumber') ~ '^[0-9]+$'
                            THEN (t.data::jsonb->>'versionNumber')::int END,
                       t.data::jsonb, $3
                FROM unnest($1::varchar[], $2::text[]) AS t(id, data)
                ON CONFLICT (id) DO NOTHING
                RETURNING id
                """,
                [str(v["id"]) for v in snaps],
                [json.dumps(v) for v in snaps],
                user_id,
            )

    return {
        "documentsImported": [r["id"] for r in inserted],
        "documentsSkipped": [i for i in doc_ids if i not in {r["id"] for r in inserted}],
        "proposalsImported": len(proposals_inserted),
        "versionsImported": len(versions_inserted),
    }
