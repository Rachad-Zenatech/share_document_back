from fastapi import APIRouter, Cookie, Depends, HTTPException, Header
from pydantic import BaseModel
from typing import List, Optional
from uuid import UUID

from models.rbac_model import (
    UserCreate, UserUpdate, UserBulkDelete, RoleCreate, RoleUpdate, RoleNavigationPermissionCreate, RoleMcpToolPermissionCreate,
    PermissionModule, RolePermissionGroupsUpdate
)
from services.rbac_service import (
    get_all_users, create_user, update_user, delete_user, bulk_delete_users,
    get_all_roles, get_role_tree, create_role, update_role, delete_role,
    get_user_roles, set_user_roles,
    get_role_navigation_permissions, update_role_navigation_permissions,
    get_role_mcp_tool_permissions, update_role_mcp_tool_permissions,
    get_login_activity_logs,
    get_permission_modules, get_role_permission_groups, update_role_permission_groups
)
from services.auth_service import (
    AUTH_COOKIE_NAME,
    get_current_user_id_dependency,
    get_my_permissions,
    require_permission,
    user_can_access_page,
)
from postgresql_db.database import fetch_all

router = APIRouter()

# --- AUTHENTICATION DEPENDENCY ---
async def get_current_user_id(
    authorization: Optional[str] = Header(None),
    access_token: Optional[str] = Cookie(None, alias=AUTH_COOKIE_NAME),
) -> UUID:
    return await get_current_user_id_dependency(authorization, access_token)

@router.get("/me/permissions")
async def read_my_permissions(user_id: UUID = Depends(get_current_user_id)):
    perms = await get_my_permissions(user_id)
    if not perms:
        raise HTTPException(status_code=404, detail="User not found")

    # If the user just refreshed the page, the beforeunload event might have set logout_at.
    # We clear it here if it was set within the last 15 seconds.
    from postgresql_db.database import get_pool
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE login_activity_logs
            SET logout_at = NULL
            WHERE id = (
                SELECT id FROM login_activity_logs
                WHERE user_id = $1 AND success = true
                ORDER BY created_at DESC
                LIMIT 1
            ) AND logout_at IS NOT NULL AND logout_at > now() - interval '15 seconds'
        """, user_id)

    return perms



# --- AUDIT LOGS ---
@router.get("/audit-logs")
async def list_audit_logs(user_id: UUID = Depends(require_permission("AUDIT_LOG_READ"))):
    from services.rbac_service import get_audit_logs
    return await get_audit_logs()

@router.get("/login-activities")
async def list_login_activities(user_id: UUID = Depends(require_permission("AUDIT_LOG_READ"))):
    return await get_login_activity_logs()

# --- USERS ---
@router.get("/configuration/users")
async def list_users(is_active: Optional[bool] = None, user_id: UUID = Depends(get_current_user_id_dependency)):
    return await get_all_users(is_active=is_active)

@router.post("/configuration/users")
async def add_user(user: UserCreate, user_id: UUID = Depends(require_permission("CONFIG_USERS_CREATE"))):
    try:
        return await create_user(user, actor_id=user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.put("/configuration/users/{id}")
async def edit_user(id: UUID, user: UserUpdate, user_id: UUID = Depends(require_permission("CONFIG_USERS_UPDATE"))):
    try:
        result = await update_user(id, user, actor_id=user_id)
        if not result:
            raise HTTPException(status_code=404, detail="User not found")
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.delete("/configuration/users/{id}")
async def remove_user(id: UUID, user_id: UUID = Depends(require_permission("CONFIG_USERS_DELETE"))):
    try:
        success = await delete_user(id, actor_id=user_id)
        if not success:
            raise HTTPException(status_code=404, detail="User not found")
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/configuration/users/bulk-delete")
async def remove_users_bulk(payload: UserBulkDelete, user_id: UUID = Depends(require_permission("CONFIG_USERS_DELETE"))):
    try:
        result = await bulk_delete_users(payload.user_ids, actor_id=user_id)
        return {"success": True, **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

# --- ROLES ---
@router.get("/configuration/roles")
async def list_roles(user_id: UUID = Depends(get_current_user_id_dependency)):
    return await get_all_roles()

@router.get("/configuration/roles/tree")
async def list_role_tree(user_id: UUID = Depends(require_permission("CONFIG_ROLES_READ"))):
    return await get_role_tree()

@router.post("/configuration/roles")
async def add_role(role: RoleCreate, user_id: UUID = Depends(require_permission("CONFIG_ROLES_CREATE"))):
    return await create_role(role, actor_id=user_id)

@router.put("/configuration/roles/{id}")
async def edit_role(id: UUID, role: RoleUpdate, user_id: UUID = Depends(require_permission("CONFIG_ROLES_UPDATE"))):
    result = await update_role(id, role, actor_id=user_id)
    if not result:
        raise HTTPException(status_code=404, detail="Role not found")
    return result

@router.delete("/configuration/roles/{id}")
async def remove_role(id: UUID, user_id: UUID = Depends(require_permission("CONFIG_ROLES_DELETE"))):
    try:
        success = await delete_role(id, actor_id=user_id)
        if not success:
            raise HTTPException(status_code=404, detail="Role not found")
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

# --- USER ROLE ASSIGNMENT ---
@router.get("/configuration/users/{id}/roles")
async def list_user_roles(id: UUID, user_id: UUID = Depends(require_permission("CONFIG_USER_ROLE_ASSIGNMENT_READ"))):
    return await get_user_roles(id)

class SetRolesRequest(BaseModel):
    role_ids: List[UUID]

@router.put("/configuration/users/{id}/roles")
async def update_user_roles(id: UUID, req: SetRolesRequest, user_id: UUID = Depends(require_permission("CONFIG_USER_ROLE_ASSIGNMENT_UPDATE"))):
    return await set_user_roles(id, req.role_ids, actor_id=user_id)

# --- ROLE NAVIGATION PERMISSIONS ---
@router.get("/configuration/navigation-items")
async def list_navigation_items(user_id: UUID = Depends(require_permission("CONFIG_ROLE_NAVIGATION_PERMISSIONS_READ"))):
    return await fetch_all("SELECT * FROM navigation_items ORDER BY parent_code, display_order")

@router.get("/configuration/actions")
async def list_actions(user_id: UUID = Depends(require_permission("CONFIG_ROLE_NAVIGATION_PERMISSIONS_READ"))):
    return await fetch_all("SELECT * FROM permission_actions ORDER BY code")

@router.get("/configuration/roles/{id}/navigation-permissions")
async def list_role_navigation_permissions(id: UUID, user_id: UUID = Depends(require_permission("CONFIG_ROLE_NAVIGATION_PERMISSIONS_READ"))):
    return await get_role_navigation_permissions(id)

@router.put("/configuration/roles/{id}/navigation-permissions")
async def edit_role_navigation_permissions(id: UUID, req: List[RoleNavigationPermissionCreate], user_id: UUID = Depends(require_permission("CONFIG_ROLE_NAVIGATION_PERMISSIONS_UPDATE"))):
    return await update_role_navigation_permissions(id, req, actor_id=user_id)

# --- ROLE PBAC PERMISSION GROUPS ---
@router.get("/configuration/permission-modules", response_model=List[PermissionModule])
async def get_permission_modules_route(user_id: UUID = Depends(require_permission("CONFIG_ROLES_READ"))):
    """Get all available permission modules and their groups."""
    return await get_permission_modules()

@router.get("/configuration/roles/{role_id}/permission-groups", response_model=List[int])
async def get_role_permission_groups_route(role_id: UUID, user_id: UUID = Depends(require_permission("CONFIG_ROLES_READ"))):
    """Get the assigned permission group IDs for a role."""
    return await get_role_permission_groups(role_id)

@router.put("/configuration/roles/{role_id}/permission-groups", response_model=List[int])
async def update_role_permission_groups_route(
    role_id: UUID,
    update_data: RolePermissionGroupsUpdate,
    user_id: UUID = Depends(require_permission("CONFIG_ROLES_UPDATE"))
):
    """Update the assigned permission group IDs for a role."""
    return await update_role_permission_groups(role_id, update_data.permission_group_ids, user_id)

# --- ROLE MCP TOOL PERMISSIONS ---
@router.get("/configuration/mcp-tools")
async def list_mcp_tools(user_id: UUID = Depends(require_permission("CONFIG_ROLE_MCP_TOOL_PERMISSIONS_READ"))):
    return await fetch_all("SELECT * FROM mcp_tools ORDER BY code")

@router.get("/configuration/roles/{id}/mcp-tool-permissions")
async def list_role_mcp_tool_permissions(id: UUID, user_id: UUID = Depends(require_permission("CONFIG_ROLE_MCP_TOOL_PERMISSIONS_READ"))):
    return await get_role_mcp_tool_permissions(id)

@router.put("/configuration/roles/{id}/mcp-tool-permissions")
async def edit_role_mcp_tool_permissions(id: UUID, req: List[RoleMcpToolPermissionCreate], user_id: UUID = Depends(require_permission("CONFIG_ROLE_MCP_TOOL_PERMISSIONS_UPDATE"))):
    return await update_role_mcp_tool_permissions(id, req, actor_id=user_id)
