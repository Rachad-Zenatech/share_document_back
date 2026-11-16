"""Microsoft Entra SSO claims parsing, user provisioning, and department resolution."""

import datetime
from typing import Optional
from uuid import UUID

from services.shared.core.cache_service import emit_cache_invalidation


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


def getCurrentUserFromMicrosoftClaims(token_info: dict) -> dict:
    """Normalize Microsoft Entra ID token claims into oid, tid, sub, email, and name."""
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


async def _upsert_microsoft_user_row(conn, claims: dict, graph_profile: Optional[dict]):
    """Find or insert the local user record for an authenticated Microsoft identity."""
    department = (graph_profile.get("department") if graph_profile else None) or None
    job_title = (graph_profile.get("job_title") if graph_profile else None) or None
    account_enabled = graph_profile.get("account_enabled") if graph_profile else None
    graph_synced_at = datetime.datetime.now(datetime.timezone.utc) if graph_profile else None
    clean_email = (claims.get('email') or '').lower().strip()
    clean_name = (claims.get('name') or (graph_profile.get("display_name") if graph_profile else '') or '').strip()
    oid = claims.get('oid') or (graph_profile.get("object_id") if graph_profile else None)

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
        return user, was_deleted

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
    return user, was_deleted


async def _ensure_requester_role_assignment(conn, user_id: UUID, was_deleted: bool) -> None:
    """Ensure the REQUESTER role exists and is assigned when the user has no active role or was restored."""
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

    if was_deleted:
        # If previously deleted, revoke all old roles and reset solely to REQUESTER
        await conn.execute("UPDATE user_roles SET is_active = false WHERE user_id = $1", user_id)
        if requester_role:
            await conn.execute(
                """
                INSERT INTO user_roles (user_id, role_id, assigned_at, is_active)
                VALUES ($1, $2, now(), true)
                ON CONFLICT (user_id, role_id)
                DO UPDATE SET is_active = true, assigned_at = now()
                """,
                user_id, requester_role["id"],
            )
        return

    # For new or unassigned users, ensure REQUESTER role is active
    has_role = await conn.fetchval(
        "SELECT 1 FROM user_roles WHERE user_id = $1 AND is_active = true LIMIT 1",
        user_id,
    )
    if not has_role and requester_role:
        await conn.execute(
            """
            INSERT INTO user_roles (user_id, role_id, assigned_at, is_active)
            VALUES ($1, $2, now(), true)
            ON CONFLICT (user_id, role_id)
            DO UPDATE SET is_active = true, assigned_at = now()
            """,
            user_id, requester_role["id"],
        )


async def upsertMicrosoftUser(claims: dict, graph_profile: Optional[dict] = None):
    """Create or update the local user record for an authenticated Microsoft
    Entra identity. Automatically activates @zenatech.com users and assigns
    the default REQUESTER role.
    """
    from postgresql_db.database import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        user, was_deleted = await _upsert_microsoft_user_row(conn, claims, graph_profile)
        if not user:
            return None
        await _ensure_requester_role_assignment(conn, user["id"], was_deleted)
        await emit_cache_invalidation(scope="permissions", key=f"perms:{user['id']}")
        return dict(user)
