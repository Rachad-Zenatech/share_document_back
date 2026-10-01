from uuid import UUID
from postgresql_db.database import fetch_all, fetch_one
from services.cache_service import permission_cache, emit_cache_invalidation
import jwt
import asyncio
import contextvars
import datetime
import logging
import os
import secrets
from fastapi import Cookie, Depends, Header, HTTPException, Response, Query
from typing import Optional

logger = logging.getLogger(__name__)

JWT_SECRET = os.environ.get("JWT_SECRET") or os.environ.get("SESSION_SECRET")
if not JWT_SECRET or len(JWT_SECRET) < 32:
    raise RuntimeError("JWT_SECRET or SESSION_SECRET must be configured with at least 32 characters")
JWT_ALGORITHM = "HS256"
JWT_ISSUER = os.getenv("JWT_ISSUER", "zenatech-internal-portal")
ALLOWED_JWT_ISSUERS = {"zenatech-internal-portal", "app-template-auth", JWT_ISSUER}
AUTH_COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "access_token")
AUTH_COOKIE_MAX_AGE = max(300, int(os.getenv("AUTH_TOKEN_TTL_SECONDS", "7200")))

current_user_id_ctx = contextvars.ContextVar("current_user_id", default=None)

_user_auth_cache = {}
_last_activity_update = {}
_super_admin_cache = {}

async def _update_last_activity_async(user_id: UUID):
    try:
        from postgresql_db.database import execute
        await execute("UPDATE users SET last_activity_at = now() WHERE id = $1", user_id)
    except Exception:
        pass



# Create token
def create_access_token(user_id: UUID, is_super_admin: bool):
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "sub": str(user_id),
        "is_super_admin": is_super_admin,
        "iss": JWT_ISSUER,
        "iat": now,
        "jti": secrets.token_urlsafe(16),
        "exp": now + datetime.timedelta(seconds=AUTH_COOKIE_MAX_AGE),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

# Helper to verify token and extract user_id
def verify_token(token: str):
    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "exp", "iat"], "verify_iss": False},
        )
        iss = payload.get("iss")
        if iss and iss not in ALLOWED_JWT_ISSUERS:
            logger.debug(f"Token issuer {iss} not in allowed list, but accepting matching secret")
        return payload
    except jwt.PyJWTError:
        return None

async def get_my_permissions(user_id: UUID):
    cache_key = f"perms:{user_id}"
    cached = await permission_cache.get(cache_key)
    if cached is not None:
        return cached

    # Fetch user
    user = await fetch_one(
        """
        SELECT id, email, full_name, is_active, is_super_admin,
               department, job_title,
               last_login_at, created_at, updated_at
        FROM users
        WHERE id = $1 AND deleted_at IS NULL AND is_active = true
        """,
        user_id,
    )
    if not user:
        return None

    # Fetch roles
    roles_sql = """
        SELECT r.*
        FROM roles r
        JOIN user_roles ur ON ur.role_id = r.id
        WHERE ur.user_id = $1 AND ur.is_active = true AND r.deleted_at IS NULL AND r.is_active = true
    """
    roles = await fetch_all(roles_sql, user_id)
    is_super_admin = user["is_super_admin"] or any(r["code"] == "SUPER_ADMIN" for r in roles)

    # Fetch page permissions
    page_perms = {}
    mcp_perms = []

    if is_super_admin:
        # Get all pages and actions
        pages = await fetch_all("SELECT code FROM navigation_items WHERE is_active = true")
        actions = await fetch_all("SELECT code FROM permission_actions")
        action_codes = [a["code"] for a in actions]
        for p in pages:
            page_perms[p["code"]] = action_codes

        # Get all MCP tools
        tools = await fetch_all("SELECT code FROM mcp_tools WHERE is_active = true")
        mcp_perms = [t["code"] for t in tools]
    else:
        # Get assigned page permissions
        if roles:
            role_ids = [r["id"] for r in roles]
            placeholders = ", ".join(f"${i+1}" for i in range(len(role_ids)))

            page_sql = f"""
                SELECT p.code as page_code, a.code as action_code
                FROM role_navigation_permissions rpp
                JOIN navigation_items p ON rpp.navigation_item_id = p.id
                JOIN permission_actions a ON rpp.action_id = a.id
                WHERE rpp.role_id IN ({placeholders}) AND rpp.is_allowed = true
            """
            assigned_page_perms = await fetch_all(page_sql, *role_ids)
            for perm in assigned_page_perms:
                pc = perm["page_code"]
                ac = perm["action_code"]
                if pc not in page_perms:
                    page_perms[pc] = []
                if ac not in page_perms[pc]:
                    page_perms[pc].append(ac)

            mcp_sql = f"""
                SELECT t.code as tool_code
                FROM role_mcp_tool_permissions rm
                JOIN mcp_tools t ON rm.mcp_tool_id = t.id
                WHERE rm.role_id IN ({placeholders}) AND rm.is_allowed = true
            """
            assigned_mcp_perms = await fetch_all(mcp_sql, *role_ids)
            mcp_perms = list(set([t["tool_code"] for t in assigned_mcp_perms]))

            # --- ADD PBAC PERMISSION GROUPS ---
            pbac_sql = f"""
                SELECT a.api_module_code, a.action
                FROM role_permission_groups rpg
                JOIN permission_group_actions a ON rpg.permission_group_id = a.permission_group_id
                WHERE rpg.role_id IN ({placeholders})
            """
            pbac_perms = await fetch_all(pbac_sql, *role_ids)
            for perm in pbac_perms:
                pc = perm["api_module_code"]
                ac = perm["action"]
                if pc not in page_perms:
                    page_perms[pc] = []
                if ac not in page_perms[pc]:
                    page_perms[pc].append(ac)
    # Fetch active workflow roles for this user
    wf_roles = []
    try:
        wf_roles_sql = """
            SELECT DISTINCT role
            FROM workflow_assignments
            WHERE (user_id = $1 OR (user_ids IS NOT NULL AND $1 = ANY(user_ids)))
            AND active = true
        """
        wf_rows = await fetch_all(wf_roles_sql, user_id)
        wf_roles = [w["role"] for w in wf_rows if w.get("role") and not w["role"].startswith("DELETED_")]
    except Exception:
        wf_roles = []

    result = {
        "user": dict(user),
        "roles": [dict(r) for r in roles],
        "workflow_roles": wf_roles,
        "navigation_permissions": page_perms,
        "mcp_tool_permissions": mcp_perms
    }
    await permission_cache.set(cache_key, result, ttl_seconds=60)
    return result

async def user_can_access_page(user_id: UUID, page_code: str, action_code: str = "VIEW"):
    perms = await get_my_permissions(user_id)
    if not perms:
        return False

    user = perms["user"]
    if user.get("is_super_admin"):
        return True

    if any(r.get("code") == "SUPER_ADMIN" for r in perms["roles"]):
        return True

    allowed_actions = perms["navigation_permissions"].get(page_code, [])
    return action_code in allowed_actions

async def user_can_use_mcp_tool(user_id: UUID, tool_code: str):
    perms = await get_my_permissions(user_id)
    if not perms:
        return False

    user = perms["user"]
    if user.get("is_super_admin"):
        return True

    if any(r.get("code") == "SUPER_ADMIN" for r in perms["roles"]):
        return True

    return tool_code in perms["mcp_tool_permissions"]

async def resolve_user_department(user_id: UUID) -> str:
    """
    Resolves user department matching the frontend logic:
    1. Direct users.department
    2. Direct role.department for assigned active roles
    3. Parent role hierarchy (e.g. Requester -> CEO -> Executive)
    4. Super Admin -> Executive
    5. Fallback -> General
    """
    from postgresql_db.database import get_pool
    pool = get_pool()
    async with pool.acquire() as conn:
        u = await conn.fetchrow("SELECT department, is_super_admin FROM users WHERE id = $1", user_id)
        if not u:
            return "General"
        if u["department"] and u["department"].strip():
            return u["department"].strip()
        if u["is_super_admin"]:
            return "Executive"

        roles = await conn.fetch("""
            SELECT r.id, r.name, r.code, r.department, r.parent_role_id
            FROM roles r
            JOIN user_roles ur ON ur.role_id = r.id
            WHERE ur.user_id = $1 AND ur.is_active = true AND r.is_active = true AND r.deleted_at IS NULL
        """, user_id)

        all_roles = await conn.fetch("SELECT id, name, code, department, parent_role_id FROM roles WHERE is_active = true AND deleted_at IS NULL")
        role_map = {r["id"]: dict(r) for r in all_roles}

        for r in roles:
            if r["department"] and r["department"].strip():
                return r["department"].strip()
            curr = dict(r)
            visited = set()
            while curr.get("parent_role_id") and curr["parent_role_id"] not in visited:
                visited.add(curr["parent_role_id"])
                parent = role_map.get(curr["parent_role_id"])
                if not parent:
                    break
                if parent.get("department") and parent["department"].strip():
                    return parent["department"].strip()
                curr = parent

        return "General"

def _is_cookie_secure() -> bool:
    env_val = os.getenv("AUTH_COOKIE_SECURE")
    if env_val is not None:
        return env_val.strip().lower() in {"1", "true", "yes", "on"}
    return os.getenv("APP_ENV", "development").lower() == "production"

def set_access_token_cookie(response: Response, token: str) -> None:
    secure = _is_cookie_secure()
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        max_age=AUTH_COOKIE_MAX_AGE,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_access_token_cookie(response: Response) -> None:
    secure = _is_cookie_secure()
    response.delete_cookie(
        key=AUTH_COOKIE_NAME,
        path="/",
        samesite="lax",
        httponly=True,
        secure=secure,
    )
    if AUTH_COOKIE_NAME != "zenatech_access_token":
        response.delete_cookie(
            key="zenatech_access_token",
            path="/",
            samesite="lax",
            httponly=True,
            secure=secure,
        )

def getCurrentUserFromMicrosoftClaims(token_info: dict) -> dict:
    if not token_info:
        return {'oid': None, 'tid': None, 'sub': None, 'email': '', 'name': ''}
    email = (
        token_info.get('email')
        or token_info.get('preferred_username')
        or token_info.get('upn')
        or token_info.get('unique_name')
        or ''
    ).lower().strip()
    name = (
        token_info.get('name')
        or f"{token_info.get('given_name', '')} {token_info.get('family_name', '')}".strip()
        or token_info.get('displayName')
        or ''
    ).strip()
    return {
        'oid': token_info.get('oid') or token_info.get('sub'),
        'tid': token_info.get('tid'),
        'sub': token_info.get('sub'),
        'email': email,
        'name': name,
    }

async def upsertMicrosoftUser(claims: dict, graph_profile: Optional[dict] = None):
    """Create or update the local user record for an authenticated Microsoft
    Entra identity. Automatically activates @zenatech.com users and assigns
    the default REQUESTER role.
    """
    from postgresql_db.database import get_pool

    department = (graph_profile.get("department") if graph_profile else None) or None
    job_title = (graph_profile.get("job_title") if graph_profile else None) or None
    account_enabled = graph_profile.get("account_enabled") if graph_profile else None
    graph_synced_at = datetime.datetime.now(datetime.timezone.utc) if graph_profile else None
    clean_email = (claims.get('email') or '').lower().strip()
    clean_name = (claims.get('name') or (graph_profile.get("display_name") if graph_profile else '') or '').strip()
    oid = claims.get('oid') or (graph_profile.get("object_id") if graph_profile else None)

    pool = get_pool()
    async with pool.acquire() as conn:
        # Find existing user by microsoft_object_id OR email
        user = await conn.fetchrow("""
            SELECT * FROM users
            WHERE (microsoft_object_id IS NOT NULL AND microsoft_object_id = $1)
               OR LOWER(TRIM(email)) = $2
            ORDER BY (microsoft_object_id = $1) DESC
            LIMIT 1
        """, oid, clean_email)

        was_deleted = bool(user and user.get("deleted_at") is not None)

        if user:
            await conn.execute("""
                UPDATE users SET
                    email = $1,
                    full_name = COALESCE(NULLIF($2, ''), full_name),
                    microsoft_object_id = COALESCE($3, microsoft_object_id),
                    microsoft_tenant_id = COALESCE($4, microsoft_tenant_id),
                    microsoft_subject_id = COALESCE($5, microsoft_subject_id),
                    auth_provider = 'microsoft',
                    sso_enabled = true,
                    department = COALESCE($6, department),
                    job_title = COALESCE($7, job_title),
                    graph_account_enabled = COALESCE($8, graph_account_enabled),
                    last_graph_sync = COALESCE($9, last_graph_sync),
                    is_active = CASE
                        WHEN $8::boolean IS FALSE THEN false
                        ELSE true
                    END,
                    is_super_admin = CASE
                        WHEN deleted_at IS NOT NULL THEN false
                        ELSE is_super_admin
                    END,
                    deleted_at = NULL,
                    deleted_by = NULL,
                    last_login_at = now(),
                    last_sso_login_at = now(),
                    last_activity_at = now(),
                    last_logout_at = NULL
                WHERE id = $10
            """, clean_email, clean_name, oid, claims.get('tid'), claims.get('sub'),
                 department, job_title, account_enabled, graph_synced_at, user['id'])
            user = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user['id'])
        else:
            # Brand-new identity: auto-create as active regular user (is_super_admin = false)
            user = await conn.fetchrow("""
                INSERT INTO users (
                    microsoft_object_id, microsoft_tenant_id, microsoft_subject_id,
                    email, full_name, auth_provider, sso_enabled, is_active, is_super_admin,
                    department, job_title, graph_account_enabled, last_graph_sync,
                    last_login_at, last_sso_login_at, last_activity_at, created_at
                ) VALUES ($1, $2, $3, $4, $5, 'microsoft', true, true, false, $6, $7, $8, $9, now(), now(), now(), now())
                ON CONFLICT (email) DO UPDATE SET
                    full_name = COALESCE(NULLIF(EXCLUDED.full_name, ''), users.full_name),
                    microsoft_object_id = COALESCE(EXCLUDED.microsoft_object_id, users.microsoft_object_id),
                    microsoft_tenant_id = COALESCE(EXCLUDED.microsoft_tenant_id, users.microsoft_tenant_id),
                    microsoft_subject_id = COALESCE(EXCLUDED.microsoft_subject_id, users.microsoft_subject_id),
                    department = COALESCE(EXCLUDED.department, users.department),
                    job_title = COALESCE(EXCLUDED.job_title, users.job_title),
                    auth_provider = 'microsoft',
                    sso_enabled = true,
                    is_active = true,
                    is_super_admin = CASE WHEN users.deleted_at IS NOT NULL THEN false ELSE users.is_super_admin END,
                    deleted_at = NULL,
                    deleted_by = NULL,
                    last_login_at = now(),
                    last_sso_login_at = now(),
                    last_activity_at = now(),
                    last_logout_at = NULL
                RETURNING *
            """, oid, claims.get('tid'), claims.get('sub'), clean_email, clean_name,
                 department, job_title, account_enabled, graph_synced_at)

        # Ensure REQUESTER role is available in roles table
        requester_role = await conn.fetchrow("""
            SELECT id FROM roles
            WHERE UPPER(code) = 'REQUESTER' OR UPPER(name) = 'REQUESTER'
            ORDER BY (UPPER(code) = 'REQUESTER') DESC
            LIMIT 1
        """)
        if not requester_role:
            requester_role = await conn.fetchrow("""
                INSERT INTO roles (code, name, is_system_role, is_active)
                VALUES ('REQUESTER', 'Requester', true, true)
                ON CONFLICT (code) DO UPDATE SET is_active = true
                RETURNING id
            """)

        if user:
            if was_deleted:
                # If previously deleted, revoke all old roles and reset solely to REQUESTER
                await conn.execute("UPDATE user_roles SET is_active = false WHERE user_id = $1", user["id"])
                if requester_role:
                    await conn.execute(
                        """
                        INSERT INTO user_roles (user_id, role_id, assigned_at, is_active)
                        VALUES ($1, $2, now(), true)
                        ON CONFLICT (user_id, role_id)
                        DO UPDATE SET is_active = true, assigned_at = now()
                        """,
                        user["id"], requester_role["id"],
                    )
            else:
                # For new or unassigned users, ensure REQUESTER role is active
                has_role = await conn.fetchval(
                    "SELECT 1 FROM user_roles WHERE user_id = $1 AND is_active = true LIMIT 1",
                    user["id"]
                )
                if not has_role and requester_role:
                    await conn.execute(
                        """
                        INSERT INTO user_roles (user_id, role_id, assigned_at, is_active)
                        VALUES ($1, $2, now(), true)
                        ON CONFLICT (user_id, role_id)
                        DO UPDATE SET is_active = true, assigned_at = now()
                        """,
                        user["id"], requester_role["id"],
                    )
            await emit_cache_invalidation(scope="permissions", key=f"perms:{user['id']}")
            return dict(user)

        return None

async def getUserRoles(user_id: UUID):
    from postgresql_db.database import fetch_all
    roles_sql = """
        SELECT r.id, r.code, r.name
        FROM roles r
        JOIN user_roles ur ON ur.role_id = r.id
        WHERE ur.user_id = $1 AND ur.is_active = true AND r.deleted_at IS NULL AND r.is_active = true
    """
    roles = await fetch_all(roles_sql, user_id)
    return [dict(r) for r in roles]

async def getUserNavigationPermissions(role_ids: list):
    from postgresql_db.database import fetch_all
    if not role_ids:
        return []
    placeholders = ', '.join(f'${i+1}' for i in range(len(role_ids)))
    nav_sql = f"""
        SELECT DISTINCT n.id, n.code, n.name, n.route_path as "routePath",
               n.parent_code as "parentCode", n.display_order as "displayOrder", n.icon
        FROM role_navigation_permissions rpp
        JOIN navigation_items n ON rpp.navigation_item_id = n.id
        WHERE rpp.role_id IN ({placeholders}) AND rpp.is_allowed = true AND n.is_active = true
        ORDER BY n.display_order
    """
    nav_items = await fetch_all(nav_sql, *role_ids)
    return [dict(n) for n in nav_items]

async def getUserToolPermissions(role_ids: list):
    from postgresql_db.database import fetch_all
    if not role_ids:
        return []
    placeholders = ', '.join(f'${i+1}' for i in range(len(role_ids)))
    tool_sql = f"""
        SELECT t.id, t.code, t.name, rmp.access_level as "accessLevel", rmp.conditions
        FROM role_mcp_tool_permissions rmp
        JOIN mcp_tools t ON rmp.mcp_tool_id = t.id
        WHERE rmp.role_id IN ({placeholders}) AND rmp.is_allowed = true AND t.is_active = true
    """
    tools = await fetch_all(tool_sql, *role_ids)
    return [dict(t) for t in tools]

async def get_current_user_id_dependency(
    authorization: Optional[str] = Header(None),
    access_token: Optional[str] = Cookie(None, alias=AUTH_COOKIE_NAME),
    zenatech_token: Optional[str] = Cookie(None, alias="zenatech_access_token"),
    query_token: Optional[str] = Query(None, alias='token'),
) -> UUID:
    bearer_token = None
    if authorization and authorization.startswith("Bearer "):
        bearer_token = authorization.removeprefix("Bearer ").strip()
    token = bearer_token or access_token or zenatech_token or query_token
    if not token:
        raise HTTPException(status_code=401, detail='Not authenticated')
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail='Invalid token')
    user_id = None
    sub_raw = payload.get('sub')
    if sub_raw:
        try:
            user_id = UUID(str(sub_raw))
        except (KeyError, TypeError, ValueError):
            user_id = None

    import time
    now_ts = time.time()
    user = None
    if user_id:
        cached = _user_auth_cache.get(user_id)
        if cached and (now_ts - cached["time"]) < 30.0:
            user = cached["user"]
        else:
            user = await fetch_one(
                "SELECT id, last_activity_at, last_logout_at, is_super_admin FROM users WHERE id = $1 AND deleted_at IS NULL AND is_active = true",
                user_id,
            )

    if not user and (payload.get("is_service_token") or payload.get("email")):
        if payload.get("email"):
            user = await fetch_one(
                "SELECT id, last_activity_at, last_logout_at, is_super_admin FROM users WHERE LOWER(email) = LOWER($1) AND deleted_at IS NULL AND is_active = true",
                payload["email"],
            )
        if not user and payload.get("is_service_token"):
            user = await fetch_one(
                "SELECT id, last_activity_at, last_logout_at, is_super_admin FROM users WHERE is_super_admin = true AND deleted_at IS NULL AND is_active = true ORDER BY created_at ASC LIMIT 1",
            )
            if not user:
                user = await fetch_one(
                    "SELECT id, last_activity_at, last_logout_at, is_super_admin FROM users WHERE deleted_at IS NULL AND is_active = true ORDER BY created_at ASC LIMIT 1",
                )

    if not user:
        raise HTTPException(status_code=401, detail="User is inactive or no longer exists")

    user_id = user["id"]
    _user_auth_cache[user_id] = {"user": user, "time": now_ts}

    now_dt = datetime.datetime.now(datetime.timezone.utc)

    if user.get("last_logout_at"):
        token_iat = datetime.datetime.fromtimestamp(payload['iat'], tz=datetime.timezone.utc)
        if token_iat < user["last_logout_at"]:
            raise HTTPException(status_code=401, detail="Session expired")

    if user.get("last_activity_at"):
        if now_dt - user["last_activity_at"] > datetime.timedelta(minutes=30):
            from postgresql_db.database import execute
            if payload.get("is_super_admin") or payload.get("is_service_token"):
                await execute("UPDATE users SET last_activity_at = now(), last_logout_at = NULL WHERE id = $1", user_id)
            else:
                await execute("UPDATE users SET last_logout_at = now() WHERE id = $1", user_id)
                raise HTTPException(status_code=401, detail="Logged out due to inactivity")

    last_upd = _last_activity_update.get(user_id, 0.0)
    if now_ts - last_upd > 60.0:
        _last_activity_update[user_id] = now_ts
        asyncio.create_task(_update_last_activity_async(user_id))

    current_user_id_ctx.set(user_id)
    return user_id

def requireNavigationAccess(navigationCode: str, actionCode: str = 'VIEW'):
    async def dependency(user_id: UUID = Depends(get_current_user_id_dependency)):
        from postgresql_db.database import fetch_one
        user = await fetch_one("SELECT is_super_admin FROM users WHERE id = $1", user_id)
        if user and user['is_super_admin']:
            return user_id

        has_access = await user_can_access_page(user_id, navigationCode, actionCode)
        if not has_access:
            from services.rbac_service import log_audit_action
            await log_audit_action('PERMISSION_DENIED', user_id, f'Denied {actionCode} on {navigationCode}')
            raise HTTPException(status_code=403, detail='Permission denied')
        return user_id
    return dependency

def require_permission(permission_code: str):
    """
    New API Permission Guard based on PBAC proposal.
    Expects format: MODULE_ACTION (e.g. GENERAL_LEDGER_CREATE)
    """
    async def dependency(user_id: UUID = Depends(get_current_user_id_dependency)):
        from postgresql_db.database import fetch_one
        user = await fetch_one("SELECT is_super_admin FROM users WHERE id = $1", user_id)
        if user and user['is_super_admin']:
            return user_id

        parts = permission_code.rsplit('_', 1)
        if len(parts) == 2:
            navigationCode, newActionCode = parts
        else:
            navigationCode = permission_code
            newActionCode = 'PAGE_ACCESS'

        # Backward compatibility map for existing database action codes
        action_map = {
            "READ": "VIEW",
            "UPDATE": "UPDATE",
            "CREATE": "CREATE",
            "DELETE": "DELETE",
            "IMPORT": "CREATE",  # Map import to create initially
            "EXPORT": "VIEW",    # Map export to view initially
            "PROCESS": "UPDATE", # Map process to update initially
            "PAGE_ACCESS": "VIEW"
        }

        # Fallback to the new code if not mapped, allowing future DB migration
        db_action_code = action_map.get(newActionCode, newActionCode)

        has_access = await user_can_access_page(user_id, navigationCode, db_action_code)
        if not has_access:
            from services.rbac_service import log_audit_action
            await log_audit_action('PERMISSION_DENIED', user_id, f'Denied {permission_code}')
            raise HTTPException(status_code=403, detail='Permission denied')
        return user_id
    return dependency

def requireToolAccess(toolCode: str, requiredAccessLevel: str = 'execute'):
    async def dependency(user_id: UUID = Depends(get_current_user_id_dependency)):
        from postgresql_db.database import fetch_one
        user = await fetch_one("SELECT is_super_admin FROM users WHERE id = $1", user_id)
        if user and user['is_super_admin']:
            return user_id

        has_access = await user_can_use_mcp_tool(user_id, toolCode)
        if not has_access:
            from services.rbac_service import log_audit_action
            await log_audit_action('PERMISSION_DENIED', user_id, f'Denied tool access on {toolCode}')
            raise HTTPException(status_code=403, detail='Permission denied')
        return user_id
    return dependency



