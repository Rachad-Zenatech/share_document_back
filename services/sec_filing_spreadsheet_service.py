"""Service for managing attached dynamic spreadsheets per SEC document."""

import json
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from postgresql_db.database import get_pool
from postgresql_db.sec_filing_schema import ensure_schema


async def get_document_spreadsheet(document_id: str) -> dict[str, Any] | None:
    await ensure_schema()
    pool = await get_pool()

    query = """
        SELECT id, document_id, name, sheet_name, total_rows, total_columns, columns, cells, tabs, active_tab_id, updated_at
        FROM sec_attached_spreadsheets
        WHERE document_id = $1;
    """

    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, document_id)
        if not row:
            return None

        columns = row["columns"]
        if isinstance(columns, str):
            columns = json.loads(columns)

        cells = row["cells"]
        if isinstance(cells, str):
            cells = json.loads(cells)

        tabs = row["tabs"] if "tabs" in row and row["tabs"] is not None else []
        if isinstance(tabs, str):
            tabs = json.loads(tabs)

        return {
            "id": str(row["id"]),
            "documentId": row["document_id"],
            "name": row["name"],
            "sheetName": row["sheet_name"],
            "totalRows": row["total_rows"],
            "totalColumns": row["total_columns"],
            "columns": columns,
            "cells": cells,
            "tabs": tabs,
            "activeTabId": row["active_tab_id"] if "active_tab_id" in row else None,
            "updatedAt": row["updated_at"].isoformat() if row["updated_at"] else None,
        }


async def save_document_spreadsheet(
    document_id: str,
    data: dict[str, Any],
    user_id: Optional[UUID] = None,
) -> dict[str, Any]:
    await ensure_schema()
    pool = await get_pool()

    name = data.get("name") or "Spreadsheet"
    sheet_name = data.get("sheetName") or data.get("sheet_name") or "Sheet1"
    total_rows = int(data.get("totalRows") or data.get("total_rows") or 20)
    total_columns = int(data.get("totalColumns") or data.get("total_columns") or 10)
    columns = data.get("columns") or []
    cells = data.get("cells") or {}
    tabs = data.get("tabs") or []
    active_tab_id = data.get("activeTabId") or data.get("active_tab_id")
    now = datetime.now(timezone.utc)

    columns_json = json.dumps(columns) if not isinstance(columns, str) else columns
    cells_json = json.dumps(cells) if not isinstance(cells, str) else cells
    tabs_json = json.dumps(tabs) if not isinstance(tabs, str) else tabs

    query = """
        INSERT INTO sec_attached_spreadsheets (
            document_id, name, sheet_name, total_rows, total_columns, columns, cells, tabs, active_tab_id, updated_at, updated_by
        )
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8::jsonb, $9, $10, $11)
        ON CONFLICT (document_id) DO UPDATE SET
            name = EXCLUDED.name,
            sheet_name = EXCLUDED.sheet_name,
            total_rows = EXCLUDED.total_rows,
            total_columns = EXCLUDED.total_columns,
            columns = EXCLUDED.columns,
            cells = EXCLUDED.cells,
            tabs = EXCLUDED.tabs,
            active_tab_id = EXCLUDED.active_tab_id,
            updated_at = EXCLUDED.updated_at,
            updated_by = EXCLUDED.updated_by
        RETURNING id, document_id, name, sheet_name, total_rows, total_columns, columns, cells, tabs, active_tab_id, updated_at;
    """

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            query,
            document_id,
            name,
            sheet_name,
            total_rows,
            total_columns,
            columns_json,
            cells_json,
            tabs_json,
            active_tab_id,
            now,
            user_id,
        )

        return {
            "id": str(row["id"]),
            "documentId": row["document_id"],
            "name": row["name"],
            "sheetName": row["sheet_name"],
            "totalRows": row["total_rows"],
            "totalColumns": row["total_columns"],
            "columns": json.loads(row["columns"]) if isinstance(row["columns"], str) else row["columns"],
            "cells": json.loads(row["cells"]) if isinstance(row["cells"], str) else row["cells"],
            "tabs": json.loads(row["tabs"]) if isinstance(row["tabs"], str) else (row["tabs"] or []),
            "activeTabId": row["active_tab_id"] if "active_tab_id" in row else None,
            "updatedAt": row["updated_at"].isoformat() if row["updated_at"] else None,
        }


async def update_spreadsheet_cell(
    document_id: str,
    cell_ref: str,
    value: Any,
    user_id: Optional[UUID] = None,
) -> dict[str, Any]:
    await ensure_schema()
    pool = await get_pool()
    now = datetime.now(timezone.utc)
    clean_ref = cell_ref.strip().upper()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT cells, tabs, active_tab_id FROM sec_attached_spreadsheets WHERE document_id = $1;",
            document_id,
        )
        if not row:
            initial_cells = {clean_ref: value}
            insert_q = """
                INSERT INTO sec_attached_spreadsheets (
                    document_id, name, sheet_name, total_rows, total_columns, columns, cells, tabs, updated_at, updated_by
                )
                VALUES ($1, 'Spreadsheet', 'Sheet1', 20, 10, '[]'::jsonb, $2::jsonb, '[]'::jsonb, $3, $4)
                RETURNING id;
            """
            await conn.fetchrow(insert_q, document_id, json.dumps(initial_cells), now, user_id)
            return {"documentId": document_id, "cellRef": clean_ref, "value": value}

        current_cells = row["cells"]
        if isinstance(current_cells, str):
            current_cells = json.loads(current_cells)
        current_cells[clean_ref] = value

        # Also update cell inside active tab if tabs exist
        current_tabs = row["tabs"] if "tabs" in row and row["tabs"] is not None else []
        if isinstance(current_tabs, str):
            current_tabs = json.loads(current_tabs)
        active_tab_id = row["active_tab_id"] if "active_tab_id" in row else None

        if isinstance(current_tabs, list) and len(current_tabs) > 0:
            for tab in current_tabs:
                if tab.get("id") == active_tab_id or active_tab_id is None:
                    tab_cells = tab.get("cells") or {}
                    tab_cells[clean_ref] = value
                    tab["cells"] = tab_cells
                    break

        await conn.execute(
            """
            UPDATE sec_attached_spreadsheets
            SET cells = $1::jsonb,
                tabs = $2::jsonb,
                updated_at = $3,
                updated_by = $4
            WHERE document_id = $5;
            """,
            json.dumps(current_cells),
            json.dumps(current_tabs),
            now,
            user_id,
            document_id,
        )

        return {"documentId": document_id, "cellRef": clean_ref, "value": value}
