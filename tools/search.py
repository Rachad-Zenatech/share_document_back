import re
from fastapi import APIRouter, Query, Depends
from typing import List, Optional, Any, Dict
from pydantic import BaseModel
from postgresql_db.database import fetch_all
from uuid import UUID
from services.auth_service import get_current_user_id_dependency, get_my_permissions

router = APIRouter()

class SearchResult(BaseModel):
    type: str
    id: Optional[str] = None
    title: str
    subtitle: Optional[str] = None
    url: Optional[str] = None

# Static system pages for quick navigation search with explicit permission codes
SYSTEM_PAGES = [
    {"perm_code": "DASHBOARD", "title": "Dashboard", "subtitle": "System Overview & Metric Cards", "url": "/dashboard", "keywords": ["dashboard", "home", "kpi", "overview", "analytics"]},
    {"perm_code": "CONFIG_USERS", "title": "Users Management", "subtitle": "Manage User Accounts & Roles", "url": "/configuration/users", "keywords": ["users", "accounts", "employees", "members", "staff"]},
    {"perm_code": "CONFIG_ROLES", "title": "Roles & Permissions", "subtitle": "Role Hierarchy & Navigation Access", "url": "/configuration/roles", "keywords": ["roles", "permissions", "rbac", "access", "groups"]},
    {"perm_code": "CONFIG_ROLE_API_PERMISSIONS", "title": "Role API Permissions", "subtitle": "Configure Backend Endpoint Access", "url": "/configuration/role-api-permissions", "keywords": ["api", "permissions", "endpoints", "security"]},
    {"perm_code": "CONFIG_ROLE_MCP_TOOL_PERMISSIONS", "title": "Role MCP Tools", "subtitle": "Configure MCP Tool Access", "url": "/configuration/role-mcp-tool-permissions", "keywords": ["mcp", "tools", "ai", "permissions"]},
    {"perm_code": "CONFIG_USER_ROLE_ASSIGNMENT", "title": "User Role Assignments", "subtitle": "Assign Roles and Permissions to Users", "url": "/configuration/user-role-assignment", "keywords": ["assignment", "roles", "users", "assign"]},
    {"perm_code": "AUDIT_LOG", "title": "Audit Logs", "subtitle": "System Audit Trail & Login History", "url": "/log/audit", "keywords": ["audit", "logs", "history", "security", "activity", "trail"]},
]

@router.get("")
async def global_search(
    q: str = Query(..., min_length=1, max_length=100),
    user_id: UUID = Depends(get_current_user_id_dependency)
) -> List[SearchResult]:
    query_str = q.strip()
    if not query_str:
        return []

    search_term = f"%{query_str}%"
    lower_query = query_str.lower()
    results: List[SearchResult] = []

    # 1. Fetch user permissions
    perms = await get_my_permissions(user_id)
    if not perms:
        return []

    is_super_admin = perms["user"]["is_super_admin"] or any(r.get("code") == "SUPER_ADMIN" for r in perms.get("roles", []))
    nav_perms: Dict[str, Any] = perms.get("navigation_permissions", {})

    def has_perm(code: str) -> bool:
        if is_super_admin:
            return True
        if not nav_perms:
            return False
        if code in nav_perms:
            return True
        aliases = {
            "AUDIT_LOG": ["AUDIT_LOGS", "SYSTEM_LOGS"],
        }
        for alias in aliases.get(code, []):
            if alias in nav_perms:
                return True
        return False

    # 2. Search Navigation Pages
    for page in SYSTEM_PAGES:
        if not has_perm(page["perm_code"]):
            continue

        matches_title = lower_query in page["title"].lower()
        matches_sub = lower_query in page["subtitle"].lower()
        matches_kw = any(lower_query in kw for kw in page["keywords"])
        if matches_title or matches_sub or matches_kw:
            results.append(
                SearchResult(
                    type="page",
                    id=page["perm_code"],
                    title=f"Page: {page['title']}",
                    subtitle=page["subtitle"],
                    url=page["url"],
                )
            )

    # 3. Search Users
    if has_perm("CONFIG_USERS"):
        try:
            users_sql = """
                SELECT id, full_name, email, job_title, department
                FROM users
                WHERE deleted_at IS NULL AND is_active = true
                  AND (
                      full_name ILIKE $1
                      OR email ILIKE $1
                      OR job_title ILIKE $1
                      OR department ILIKE $1
                  )
                ORDER BY full_name
                LIMIT 5
            """
            users = await fetch_all(users_sql, search_term)
            for u in users:
                u_name = u["full_name"] or u["email"]
                u_sub = f"{u['email']} • {u['job_title'] or u['department'] or 'User'}"
                results.append(
                    SearchResult(
                        type="user",
                        id=str(u["id"]),
                        title=f"User: {u_name}",
                        subtitle=u_sub,
                        url="/configuration/users",
                    )
                )
        except Exception:
            pass

    # 4. Search Roles
    if has_perm("CONFIG_ROLES"):
        try:
            roles_sql = """
                SELECT id, name, code, description, department
                FROM roles
                WHERE deleted_at IS NULL AND is_active = true
                  AND (
                      name ILIKE $1
                      OR code ILIKE $1
                      OR description ILIKE $1
                      OR department ILIKE $1
                  )
                ORDER BY name
                LIMIT 5
            """
            roles = await fetch_all(roles_sql, search_term)
            for r in roles:
                results.append(
                    SearchResult(
                        type="role",
                        id=str(r["id"]),
                        title=f"Role: {r['name']}",
                        subtitle=r["description"] or r["code"] or "Role Configuration",
                        url="/configuration/roles",
                    )
                )
        except Exception:
            pass

    # 5. Search Audit Logs
    if has_perm("AUDIT_LOG"):
        try:
            audit_sql = """
                SELECT a.id, a.action, a.entity_type, a.entity_id, a.created_at, u.full_name as actor_name
                FROM audit_logs a
                LEFT JOIN users u ON a.actor_user_id = u.id
                WHERE (
                    a.action ILIKE $1
                    OR a.entity_type ILIKE $1
                    OR u.full_name ILIKE $1
                    OR CAST(a.entity_id AS TEXT) ILIKE $1
                )
                ORDER BY a.created_at DESC
                LIMIT 5
            """
            audits = await fetch_all(audit_sql, search_term)
            for a in audits:
                results.append(
                    SearchResult(
                        type="audit",
                        id=str(a["id"]),
                        title=f"Audit: {a['action']} ({a['entity_type']})",
                        subtitle=f"By {a['actor_name'] or 'System'} on {a['created_at'].strftime('%b %d, %Y') if a.get('created_at') else 'Recent'}",
                        url="/log/audit",
                    )
                )
        except Exception:
            pass

    return results
