"""DB logic for SEC reporting workflows and their assignment to documents.

A workflow is an ordered list of steps ("Update Consolidated TB", "Write
footnotes", ...). Each document can have one workflow assigned; the contributor
invite modal offers that workflow's steps as the task / branch title.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from postgresql_db.database import fetch_all, fetch_one, get_pool
from postgresql_db.sec_filing_schema import ensure_schema
from services.cache_service import emit_cache_invalidation, master_data_cache

_LIST_CACHE_KEY = "master_data:sec_workflows"

_COLUMNS = "w.id, w.name, w.description, w.steps, w.created_at, w.updated_at, w.updated_by"


def _row_to_workflow(row: Any) -> dict[str, Any]:
    workflow = dict(row)
    # asyncpg returns JSONB as a string unless a codec is registered.
    steps = workflow.get("steps")
    if isinstance(steps, (str, bytes)):
        workflow["steps"] = json.loads(steps)
    return workflow


async def list_workflows() -> dict[str, Any]:
    cached = await master_data_cache.get(_LIST_CACHE_KEY)
    if cached is not None:
        return cached

    await ensure_schema()
    rows = await fetch_all(
        f"""
        SELECT {_COLUMNS},
               COALESCE(array_agg(a.document_id) FILTER (WHERE a.document_id IS NOT NULL), '{{}}') AS document_ids
        FROM sec_workflows w
        LEFT JOIN sec_document_workflow_assignments a ON a.workflow_id = w.id
        WHERE w.deleted_at IS NULL
        GROUP BY w.id
        ORDER BY w.name
        """
    )
    workflows = [_row_to_workflow(row) for row in rows]
    result = {"workflows": workflows, "total": len(workflows)}
    await master_data_cache.set(_LIST_CACHE_KEY, result, ttl_seconds=300)
    return result


async def get_workflow(workflow_id: str | UUID) -> dict[str, Any] | None:
    await ensure_schema()
    row = await fetch_one(
        f"""
        SELECT {_COLUMNS},
               COALESCE(array_agg(a.document_id) FILTER (WHERE a.document_id IS NOT NULL), '{{}}') AS document_ids
        FROM sec_workflows w
        LEFT JOIN sec_document_workflow_assignments a ON a.workflow_id = w.id
        WHERE w.id::text = $1 AND w.deleted_at IS NULL
        GROUP BY w.id
        """,
        str(workflow_id),
    )
    return _row_to_workflow(row) if row else None


async def create_workflow(payload: dict[str, Any], user_id: UUID) -> dict[str, Any]:
    await ensure_schema()
    row = await fetch_one(
        """
        INSERT INTO sec_workflows (name, description, steps, created_by, updated_by)
        VALUES ($1, $2, $3::jsonb, $4, $4)
        RETURNING id
        """,
        payload["name"],
        payload.get("description"),
        json.dumps(payload.get("steps") or []),
        user_id,
    )
    await emit_cache_invalidation(scope="master_data", key=_LIST_CACHE_KEY)
    return await get_workflow(row["id"])


async def update_workflow(workflow_id: str | UUID, payload: dict[str, Any], user_id: UUID) -> dict[str, Any]:
    await ensure_schema()

    # Only write the fields the caller actually supplied.
    sets: list[str] = []
    args: list[Any] = []
    for field in ("name", "description"):
        if field in payload and payload[field] is not None:
            args.append(payload[field])
            sets.append(f"{field} = ${len(args)}")
    if payload.get("steps") is not None:
        args.append(json.dumps(payload["steps"]))
        sets.append(f"steps = ${len(args)}::jsonb")

    args.append(user_id)
    sets.append(f"updated_by = ${len(args)}")
    sets.append("updated_at = now()")
    args.append(str(workflow_id))

    row = await fetch_one(
        f"""
        UPDATE sec_workflows
        SET {", ".join(sets)}
        WHERE id::text = ${len(args)} AND deleted_at IS NULL
        RETURNING id
        """,
        *args,
    )
    if not row:
        raise ValueError("workflow not found")
    await emit_cache_invalidation(scope="master_data", key=_LIST_CACHE_KEY)
    return await get_workflow(row["id"])


async def delete_workflow(workflow_id: str | UUID, user_id: UUID) -> dict[str, Any]:
    await ensure_schema()
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            deleted_id = await conn.fetchval(
                """
                UPDATE sec_workflows
                SET deleted_at = now(), deleted_by = $1
                WHERE id::text = $2 AND deleted_at IS NULL
                RETURNING id
                """,
                user_id,
                str(workflow_id),
            )
            if not deleted_id:
                raise ValueError("workflow not found")
            # A soft-deleted workflow must not keep driving document task titles.
            await conn.execute(
                "DELETE FROM sec_document_workflow_assignments WHERE workflow_id = $1",
                deleted_id,
            )
    await emit_cache_invalidation(scope="master_data", key=_LIST_CACHE_KEY)
    return {"id": str(deleted_id), "deleted": True}


async def set_workflow_documents(
    workflow_id: str | UUID, document_ids: list[str], user_id: UUID
) -> dict[str, Any]:
    """Make `document_ids` exactly the documents assigned to this workflow.

    A document can only follow one workflow, so assigning it here moves it off
    whichever workflow it was on before.
    """
    await ensure_schema()
    document_ids = list(dict.fromkeys(d for d in document_ids if d))
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            target_id = await conn.fetchval(
                "SELECT id FROM sec_workflows WHERE id::text = $1 AND deleted_at IS NULL",
                str(workflow_id),
            )
            if not target_id:
                raise ValueError("workflow not found")
            await conn.execute(
                """
                DELETE FROM sec_document_workflow_assignments
                WHERE workflow_id = $1 AND NOT (document_id = ANY($2::varchar[]))
                """,
                target_id,
                document_ids,
            )
            await conn.execute(
                """
                INSERT INTO sec_document_workflow_assignments (document_id, workflow_id, assigned_by)
                SELECT unnest($1::varchar[]), $2, $3
                ON CONFLICT (document_id)
                DO UPDATE SET workflow_id = EXCLUDED.workflow_id,
                              assigned_by = EXCLUDED.assigned_by,
                              assigned_at = now()
                WHERE sec_document_workflow_assignments.workflow_id IS DISTINCT FROM EXCLUDED.workflow_id
                """,
                document_ids,
                target_id,
                user_id,
            )
    await emit_cache_invalidation(scope="master_data", key=_LIST_CACHE_KEY)
    return await get_workflow(target_id)


async def get_document_workflow(document_id: str) -> dict[str, Any] | None:
    await ensure_schema()
    row = await fetch_one(
        f"""
        SELECT {_COLUMNS}, a.assigned_at
        FROM sec_document_workflow_assignments a
        JOIN sec_workflows w ON w.id = a.workflow_id AND w.deleted_at IS NULL
        WHERE a.document_id = $1
        """,
        document_id,
    )
    return _row_to_workflow(row) if row else None


async def assign_document_workflow(document_id: str, workflow_id: str | UUID, user_id: UUID) -> dict[str, Any]:
    await ensure_schema()
    row = await fetch_one(
        """
        INSERT INTO sec_document_workflow_assignments (document_id, workflow_id, assigned_by)
        SELECT $1, w.id, $3 FROM sec_workflows w
        WHERE w.id::text = $2 AND w.deleted_at IS NULL
        ON CONFLICT (document_id)
        DO UPDATE SET workflow_id = EXCLUDED.workflow_id,
                      assigned_by = EXCLUDED.assigned_by,
                      assigned_at = now()
        RETURNING document_id
        """,
        document_id,
        str(workflow_id),
        user_id,
    )
    if not row:
        raise ValueError("workflow not found")
    await emit_cache_invalidation(scope="master_data", key=_LIST_CACHE_KEY)
    return await get_document_workflow(document_id)


async def unassign_document_workflow(document_id: str) -> bool:
    await ensure_schema()
    row = await fetch_one(
        "DELETE FROM sec_document_workflow_assignments WHERE document_id = $1 RETURNING document_id",
        document_id,
    )
    if row:
        await emit_cache_invalidation(scope="master_data", key=_LIST_CACHE_KEY)
    return row is not None
