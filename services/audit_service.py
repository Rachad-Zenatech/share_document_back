import json
from datetime import datetime
from uuid import UUID
from typing import Optional, Dict, Any, List, Tuple
from postgresql_db.database import get_pool

async def log_audit_event(
    entity_type: str,
    entity_id: str,
    action: str,
    user_id: UUID,
    changes: Optional[Dict[str, Any]] = None
):
    """
    Records an event in the audit_logs table.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO audit_logs (entity_type, entity_id, action, actor_user_id, new_value)
            VALUES ($1, $2, $3, $4, $5)
            """,
            entity_type,
            entity_id,
            action,
            user_id,
            json.dumps(changes, default=str) if changes else None
        )

async def get_audit_logs(
    limit: int = 50,
    offset: int = 0,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    action: Optional[str] = None,
    search: Optional[str] = None,
    user_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], int]:
    pool = get_pool()
    async with pool.acquire() as conn:
        query = """
        SELECT al.*,
               COALESCE(u.full_name, 'System') as actor_name,
               u.email as actor_email,
               u.department as actor_department,
               u.job_title as actor_job_title,
               CASE
                   WHEN al.entity_type = 'PurchaseRequest' THEN (SELECT product_name FROM tasks WHERE CAST(id AS TEXT) = al.entity_id LIMIT 1)
                   WHEN al.entity_type = 'User' THEN (SELECT full_name FROM users WHERE CAST(id AS TEXT) = al.entity_id LIMIT 1)
                   WHEN al.entity_type = 'Role' THEN (SELECT name FROM roles WHERE CAST(id AS TEXT) = al.entity_id LIMIT 1)
                   ELSE NULL
               END as entity_title
        FROM audit_logs al
        LEFT JOIN users u ON al.actor_user_id = u.id
        WHERE 1=1
        """
        count_query = """
        SELECT count(*)
        FROM audit_logs al
        LEFT JOIN users u ON al.actor_user_id = u.id
        WHERE 1=1
        """
        args = []

        if entity_type and entity_type.upper() != "ALL":
            args.append(entity_type)
            clause = f" AND al.entity_type = ${len(args)}"
            query += clause
            count_query += clause

        if entity_id:
            args.append(str(entity_id))
            clause = f" AND al.entity_id = ${len(args)}"
            query += clause
            count_query += clause

        if action and action.upper() != "ALL":
            args.append(action)
            clause = f" AND al.action = ${len(args)}"
            query += clause
            count_query += clause

        if user_id and user_id.upper() != "ALL":
            args.append(user_id)
            clause = f" AND CAST(al.actor_user_id AS TEXT) = ${len(args)}"
            query += clause
            count_query += clause

        if date_from:
            args.append(date_from)
            clause = f" AND al.created_at >= ${len(args)}::timestamp"
            query += clause
            count_query += clause

        if date_to:
            args.append(date_to)
            clause = f" AND al.created_at <= ${len(args)}::timestamp"
            query += clause
            count_query += clause

        if search and search.strip():
            search_param = f"%{search.strip()}%"
            args.append(search_param)
            idx = len(args)
            clause = f""" AND (
                al.action ILIKE ${idx}
                OR al.entity_type ILIKE ${idx}
                OR al.entity_id ILIKE ${idx}
                OR u.full_name ILIKE ${idx}
                OR u.email ILIKE ${idx}
                OR (al.entity_type = 'PurchaseRequest' AND EXISTS (
                    SELECT 1 FROM tasks WHERE CAST(id AS TEXT) = al.entity_id AND product_name ILIKE ${idx}
                ))
            )"""
            query += clause
            count_query += clause

        query += f" ORDER BY al.created_at DESC LIMIT {limit} OFFSET {offset}"

        records = await conn.fetch(query, *args)
        total = await conn.fetchval(count_query, *args)

        return [dict(r) for r in records], total or 0
