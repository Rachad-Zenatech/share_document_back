"""DB logic for SEC filing financial statement table templates.

Templates are global (no company scoping) and soft deleted. Built-in templates
are seeded from `postgresql_db/seeds/sec_financial_table_templates.json`; they
can be edited but not deleted, so the picker always has a usable starting set.
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from postgresql_db.database import fetch_all, fetch_one, get_pool

_COLUMNS = """
    id, template_key, name, description, badge, icon, color, sort_order,
    is_builtin, block, created_at, updated_at, updated_by
"""


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return slug or "template"


def _row_to_template(row: Any) -> dict[str, Any]:
    template = dict(row)
    # asyncpg returns JSONB as a string unless a codec is registered.
    block = template.get("block")
    if isinstance(block, (str, bytes)):
        template["block"] = json.loads(block)
    return template


async def list_table_templates(include_deleted: bool = False) -> dict[str, Any]:
    where = "" if include_deleted else "WHERE deleted_at IS NULL"
    rows = await fetch_all(
        f"""
        SELECT {_COLUMNS}
        FROM sec_financial_table_templates
        {where}
        ORDER BY sort_order, name
        """
    )
    templates = [_row_to_template(row) for row in rows]
    return {"templates": templates, "total": len(templates)}


async def get_table_template(template_identifier: str | UUID) -> dict[str, Any] | None:
    identifier_str = str(template_identifier)
    row = await fetch_one(
        f"""
        SELECT {_COLUMNS}
        FROM sec_financial_table_templates
        WHERE (id::text = $1 OR template_key = $1) AND deleted_at IS NULL
        """,
        identifier_str,
    )
    return _row_to_template(row) if row else None


async def create_table_template(payload: dict[str, Any], user_id: UUID) -> dict[str, Any]:
    requested_key = payload.get("template_key") or _slugify(payload["name"])

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # The unique index only covers live rows, so find a free key among those.
            key = requested_key
            suffix = 2
            while await conn.fetchval(
                "SELECT 1 FROM sec_financial_table_templates WHERE template_key = $1 AND deleted_at IS NULL",
                key,
            ):
                key = f"{requested_key}_{suffix}"
                suffix += 1

            row = await conn.fetchrow(
                f"""
                INSERT INTO sec_financial_table_templates
                    (template_key, name, description, badge, icon, color, sort_order,
                     is_builtin, block, created_by, updated_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, false, $8::jsonb, $9, $9)
                RETURNING {_COLUMNS}
                """,
                key,
                payload["name"],
                payload.get("description"),
                payload.get("badge"),
                payload.get("icon") or "table",
                payload.get("color"),
                payload.get("sort_order") or 0,
                json.dumps(payload["block"]),
                user_id,
            )
    return _row_to_template(row)


async def update_table_template(
    template_identifier: str | UUID, payload: dict[str, Any], user_id: UUID
) -> dict[str, Any]:
    identifier_str = str(template_identifier)

    existing = await fetch_one(
        f"""
        SELECT {_COLUMNS}
        FROM sec_financial_table_templates
        WHERE (id::text = $1 OR template_key = $1) AND deleted_at IS NULL
        """,
        identifier_str,
    )

    if not existing:
        # If not present in the DB yet, create/upsert it so updates never fail with 'not found'
        name = payload.get("name") or identifier_str.replace("_", " ").title()
        create_payload = {
            "template_key": identifier_str if not re.match(r"^[0-9a-fA-F-]{36}$", identifier_str) else _slugify(name),
            "name": name,
            "description": payload.get("description"),
            "badge": payload.get("badge"),
            "icon": payload.get("icon") or "table",
            "color": payload.get("color"),
            "sort_order": payload.get("sort_order") or 0,
            "block": payload.get("block") or {
                "title": name,
                "headers": ["Item", "Value"],
                "columnAlignments": ["left", "right"],
                "rows": []
            }
        }
        return await create_table_template(create_payload, user_id)

    target_id = existing["id"]

    # Only write the fields the caller actually supplied.
    sets: list[str] = []
    args: list[Any] = []

    simple_fields = ("name", "description", "badge", "icon", "color", "sort_order")
    for field in simple_fields:
        if field in payload and payload[field] is not None:
            args.append(payload[field])
            sets.append(f"{field} = ${len(args)}")

    if payload.get("block") is not None:
        args.append(json.dumps(payload["block"]))
        sets.append(f"block = ${len(args)}::jsonb")

    if not sets:
        return _row_to_template(existing)

    args.append(user_id)
    sets.append(f"updated_by = ${len(args)}")
    sets.append("updated_at = now()")

    args.append(target_id)
    row = await fetch_one(
        f"""
        UPDATE sec_financial_table_templates
        SET {", ".join(sets)}
        WHERE id = ${len(args)} AND deleted_at IS NULL
        RETURNING {_COLUMNS}
        """,
        *args,
    )
    if not row:
        raise ValueError("template not found")
    return _row_to_template(row)


async def delete_table_template(template_identifier: str | UUID, user_id: UUID) -> dict[str, Any]:
    existing = await fetch_one(
        """
        SELECT id, is_builtin FROM sec_financial_table_templates
        WHERE (id::text = $1 OR template_key = $1) AND deleted_at IS NULL
        """,
        str(template_identifier),
    )
    if not existing:
        raise ValueError("template not found")
    if existing["is_builtin"]:
        raise ValueError("built-in templates cannot be deleted")

    await fetch_one(
        """
        UPDATE sec_financial_table_templates
        SET deleted_at = now(), deleted_by = $1
        WHERE id = $2
        RETURNING id
        """,
        user_id,
        existing["id"],
    )
    return {"id": str(existing["id"]), "deleted": True}


async def bootstrap_sec_filing_table_templates() -> None:
    """Ensure the table exists.

    Runs on application startup.
    """
    from postgresql_db.sec_filing_schema import ensure_schema

    await ensure_schema()
