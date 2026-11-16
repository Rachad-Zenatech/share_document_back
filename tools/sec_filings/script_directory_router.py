"""The people a script's invite screen offers: the Social Media portal's (see services/sec_filings/script_directory_service.py)."""

from typing import Any, Dict, List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from services.sec_filings import script_directory_service as directory
from services.shared.auth.auth_service import get_current_user_id_dependency

router = APIRouter()


@router.get("/script-users")
async def script_users(user_id: UUID = Depends(get_current_user_id_dependency)) -> List[Dict[str, Any]]:
    try:
        return await directory.script_users(user_id)
    except directory.DirectoryError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e
