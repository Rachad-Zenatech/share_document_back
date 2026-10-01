"""
Graph User Search Router
Exposes a search endpoint that proxies Microsoft Graph /v1.0/users?$search=...
Used by the CEO Dashboard autocomplete to find Entra users to assign as approvers.
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from services.auth_service import get_current_user_id_dependency
from services.graph_service import search_users

router = APIRouter()


class GraphUserResult(BaseModel):
    object_id: Optional[str] = None
    display_name: Optional[str] = None
    email: Optional[str] = None
    job_title: Optional[str] = None
    department: Optional[str] = None
    user_principal_name: Optional[str] = None


@router.get(
    "/graph/users/search",
    response_model=List[GraphUserResult],
    dependencies=[Depends(get_current_user_id_dependency)],
    summary="Search Entra users via Microsoft Graph",
    description="Searches active Entra/Azure AD users by display name or email. Used for approver role assignment autocomplete.",
)
async def graph_user_search(q: Optional[str] = Query(None, description="Search query (name or email)")):
    results = await search_users(q or "")
    return [
        GraphUserResult(
            object_id=u.get("object_id"),
            display_name=u.get("display_name"),
            email=u.get("email"),
            job_title=u.get("job_title"),
            department=u.get("department"),
            user_principal_name=u.get("user_principal_name"),
        )
        for u in results
    ]
