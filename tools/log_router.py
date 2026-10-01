import json
from typing import Optional, Any
from fastapi import APIRouter, Depends, HTTPException, Query
from services.auth_service import get_current_user_id_dependency
from services.audit_service import get_audit_logs
from services.auth_service import require_permission

router = APIRouter()

def _safe_parse_json(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return val
    return val

@router.get("/logs", dependencies=[Depends(get_current_user_id_dependency), Depends(require_permission("AUDIT_LOGS_VIEW"))])
async def list_audit_logs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    action: Optional[str] = None,
    search: Optional[str] = None,
    user_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
):
    try:
        logs, total = await get_audit_logs(
            limit=limit,
            offset=offset,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            search=search,
            user_id=user_id,
            date_from=date_from,
            date_to=date_to
        )
        return {
            "items": [
                {
                    "id": log["id"],
                    "entity_type": log["entity_type"],
                    "entity_id": str(log["entity_id"]),
                    "entity_title": log.get("entity_title"),
                    "action": log["action"],
                    "user_id": str(log.get("actor_user_id") or log.get("user_id") or ""),
                    "actor_name": log.get("actor_name") or "System",
                    "actor_email": log.get("actor_email"),
                    "actor_department": log.get("actor_department"),
                    "actor_job_title": log.get("actor_job_title"),
                    "old_value": _safe_parse_json(log.get("old_value")),
                    "new_value": _safe_parse_json(log.get("new_value")),
                    "changes": _safe_parse_json(log.get("new_value")),
                    "ip_address": str(log["ip_address"]) if log.get("ip_address") else None,
                    "user_agent": log.get("user_agent"),
                    "created_at": log["created_at"].isoformat() if log.get("created_at") else None
                }
                for log in logs
            ],
            "total": total,
            "limit": limit,
            "offset": offset
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
