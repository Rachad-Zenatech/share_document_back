import logging
import os
from fastapi import APIRouter, Request, HTTPException, Depends, Response
from fastapi.responses import JSONResponse, RedirectResponse

from authlib.integrations.starlette_client import OAuth
from starlette.config import Config
from services.auth_service import (
    create_access_token,
    getCurrentUserFromMicrosoftClaims,
    upsertMicrosoftUser,
    getUserRoles,
    getUserNavigationPermissions,
    getUserToolPermissions,
    clear_access_token_cookie,
    get_current_user_id_dependency,
    set_access_token_cookie,
    AUTH_COOKIE_NAME,
)
from services.graph_service import fetch_me_profile
from tools.rbac_router import get_current_user_id
from services.rbac_service import log_login_activity
from postgresql_db.database import get_pool
from uuid import UUID

router = APIRouter()
logger = logging.getLogger(__name__)

# Try to load from environment
MICROSOFT_CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID", "")
MICROSOFT_CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET", "")
MICROSOFT_TENANT_ID = os.getenv("MICROSOFT_TENANT_ID", "common")

config_dict = {
    "MICROSOFT_CLIENT_ID": MICROSOFT_CLIENT_ID,
    "MICROSOFT_CLIENT_SECRET": MICROSOFT_CLIENT_SECRET
}
config = Config(environ=config_dict)
oauth = OAuth(config)

CONF_URL = f"https://login.microsoftonline.com/{MICROSOFT_TENANT_ID}/v2.0/.well-known/openid-configuration"

try:
    oauth.register(
        name='microsoft',
        server_metadata_url=CONF_URL,
        client_kwargs={
            'scope': 'openid email profile User.Read'
        }
    )
except Exception:
    logger.exception(
        "Could not register Microsoft OAuth client",
        extra={"event": "microsoft_oauth_registration_failed"},
    )

@router.get("/auth/microsoft/login")
async def microsoft_login(request: Request):
    redirect_uri = os.getenv("MICROSOFT_REDIRECT_URI")
    if not redirect_uri:
        logger.error(
            "MICROSOFT_REDIRECT_URI is not configured",
            extra={"event": "microsoft_oauth_misconfigured"},
        )
        raise HTTPException(status_code=500, detail="Microsoft sign-in is not configured")
    # oauth.microsoft is only present when registration succeeded (valid client id/secret).
    client = getattr(oauth, "microsoft", None)
    if client is None:
        logger.error(
            "Microsoft OAuth client is not registered",
            extra={"event": "microsoft_oauth_not_registered"},
        )
        raise HTTPException(status_code=500, detail="Microsoft sign-in is not configured")
    try:
        return await client.authorize_redirect(request, redirect_uri, prompt="select_account")
    except Exception as exc:
        logger.exception(
            "Failed to start Microsoft OAuth redirect",
            extra={"event": "microsoft_oauth_login_failed"},
        )
        raise HTTPException(status_code=502, detail="Could not reach Microsoft sign-in") from exc

@router.get("/auth/microsoft/callback")
async def microsoft_callback(request: Request):
    try:
        token = await oauth.microsoft.authorize_access_token(request)
        user_info = token.get('userinfo') or {}
    except Exception as exc:
        logger.warning(
            "Microsoft OAuth callback failed",
            extra={
                "event": "microsoft_oauth_callback_failed",
                "error_type": type(exc).__name__,
            },
        )
        import traceback
        with open("ms_error.log", "w") as f:
            f.write(traceback.format_exc())
        raise HTTPException(status_code=400, detail=f"Microsoft sign-in failed: {str(exc)}") from exc

    # Reuse the access token to fetch the employee's Graph profile
    graph_profile = None
    graph_access_token = token.get('access_token')
    if graph_access_token:
        try:
            graph_profile = await fetch_me_profile(graph_access_token)
        except Exception:
            logger.exception(
                "Microsoft Graph profile fetch failed during login; continuing with ID token claims only",
                extra={"event": "graph_me_fetch_exception"},
            )
            graph_profile = None

    claims = getCurrentUserFromMicrosoftClaims(user_info)
    if graph_profile:
        if not claims.get('email'):
            claims['email'] = (graph_profile.get('email') or graph_profile.get('user_principal_name') or '').lower().strip()
        if not claims.get('name'):
            claims['name'] = (graph_profile.get('display_name') or '').strip()
        if not claims.get('oid'):
            claims['oid'] = graph_profile.get('object_id')

    email = (claims.get('email') or '').lower().strip()
    claims['email'] = email

    if not email:
        raise HTTPException(status_code=403, detail="Email not provided by Microsoft")

    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    # Check allowed email domains if configured
    allowed_domains_str = os.getenv("ALLOWED_EMAIL_DOMAINS", "").strip()
    if allowed_domains_str and allowed_domains_str != "*":
        allowed_domains = [d.strip().lower().lstrip("@") for d in allowed_domains_str.split(",") if d.strip()]
        user_domain = email.split("@")[-1].lower() if "@" in email else ""
        if user_domain not in allowed_domains:
            await log_login_activity(email, None, False, f"Domain @{user_domain} is not authorized", ip_address, user_agent)
            raise HTTPException(status_code=403, detail=f"Email domain @{user_domain} is not authorized for this application")

    # Connect Microsoft Entra identity to local RBAC
    user = await upsertMicrosoftUser(claims, graph_profile)

    frontend_url = (os.getenv("FRONTEND_URL") or "http://localhost:6000").rstrip("/")

    if not user.get("is_active"):
        await log_login_activity(email, user["id"], False, "Account pending/inactive", ip_address, user_agent)
        return RedirectResponse(f"{frontend_url}/login?status=ACCESS_PENDING&error=account_pending")

    await log_login_activity(email, user["id"], True, None, ip_address, user_agent)

    access_token = create_access_token(user["id"], user["is_super_admin"])
    redirect_url = f"{frontend_url}/login?status=success&token={access_token}"
    response = RedirectResponse(redirect_url)
    set_access_token_cookie(response, access_token)
    return response

@router.get("/auth/bootstrap")
async def get_auth_bootstrap(user_id: UUID = Depends(get_current_user_id_dependency)):
    pool = get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT id, email, full_name, is_active, is_super_admin, auth_provider FROM users WHERE id = $1", user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        user_dict = {
            "id": str(user["id"]),
            "email": user["email"],
            "fullName": user["full_name"],
            "isActive": user["is_active"],
            "isSuperAdmin": user["is_super_admin"],
            "authProvider": user["auth_provider"]
        }

        roles = await getUserRoles(user_id)
        role_ids = [r["id"] for r in roles]

        navigation = await getUserNavigationPermissions(role_ids) if not user["is_super_admin"] else await get_all_navigation_items()
        tools = await getUserToolPermissions(role_ids) if not user["is_super_admin"] else await get_all_mcp_tools()

        return {
            "user": user_dict,
            "roles": roles,
            "navigation": navigation,
            "tools": tools
        }

async def get_all_navigation_items():
    pool = get_pool()
    async with pool.acquire() as conn:
        items = await conn.fetch("SELECT id, code, name, route_path as \"routePath\", parent_code as \"parentCode\", display_order as \"displayOrder\", icon FROM navigation_items WHERE is_active = true ORDER BY display_order")
        return [dict(i) for i in items]

async def get_all_mcp_tools():
    pool = get_pool()
    async with pool.acquire() as conn:
        tools = await conn.fetch("SELECT id, code, name, 'execute' as \"accessLevel\", '{}'::jsonb as conditions FROM mcp_tools WHERE is_active = true")
        return [dict(t) for t in tools]

@router.post("/auth/heartbeat")
async def heartbeat(
    response: Response,
    user_id: UUID = Depends(get_current_user_id_dependency),
):
    pool = get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            """
            UPDATE users
            SET last_activity_at = now()
            WHERE id = $1
              AND deleted_at IS NULL
              AND is_active = TRUE
            RETURNING is_super_admin
            """,
            user_id,
        )
    if not user:
        raise HTTPException(status_code=401, detail="User is inactive or no longer exists")
    refreshed_token = create_access_token(user_id, bool(user["is_super_admin"]))
    set_access_token_cookie(response, refreshed_token)
    return {"status": "ok"}

@router.post("/auth/logout")
async def logout(request: Request):
    auth_header = request.headers.get("Authorization")
    token = None
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
    else:
        token = request.cookies.get(AUTH_COOKIE_NAME)

    if not token:
        try:
            form = await request.form()
            token = form.get("token")
        except:
            pass

    response = JSONResponse({"status": "success"})
    clear_access_token_cookie(response)
    request.session.clear()

    if token:
        import jwt
        from services.auth_service import JWT_SECRET, JWT_ALGORITHM, JWT_ISSUER
        from services.cache_service import emit_cache_invalidation
        try:
            payload = jwt.decode(
                token,
                JWT_SECRET,
                algorithms=[JWT_ALGORITHM],
                options={"verify_exp": False, "require": ["sub", "iat"], "verify_iss": False}
            )
        except jwt.PyJWTError:
            payload = None

        if payload:
            try:
                user_id = UUID(payload["sub"])
                await emit_cache_invalidation(scope="permissions", key=f"perms:{user_id}")
                pool = get_pool()
                async with pool.acquire() as conn:
                    # Get user's last_login_at
                    user = await conn.fetchrow("SELECT id FROM users WHERE id = $1", user_id)
                    if user:
                        # Update the corresponding login activity log to set logout_at
                        await conn.execute("""
                            UPDATE login_activity_logs
                            SET logout_at = now()
                            WHERE id = (
                                SELECT id FROM login_activity_logs
                                WHERE user_id = $1 AND success = true AND logout_at IS NULL
                                ORDER BY created_at DESC
                                LIMIT 1
                            )
                        """, user_id)
            except Exception:
                pass

    return response





@router.get("/auth/dev-login")
async def dev_login(email: str):
    if os.getenv("APP_ENV", "development").lower() == "production":
        raise HTTPException(status_code=404, detail="Endpoint not available in production")

    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:6000").rstrip("/")

    pool = get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT id, is_super_admin FROM users WHERE email = $1 AND is_active = true", email)
        if not user:
            user = await conn.fetchrow("SELECT id, is_super_admin FROM users WHERE is_active = true ORDER BY is_super_admin DESC LIMIT 1")
            if not user:
                url = f"{frontend_url}/login?error=account_not_found"
                return RedirectResponse(url=url)

        await conn.execute("UPDATE users SET last_activity_at = now(), last_logout_at = NULL WHERE id = $1", user['id'])
        token = create_access_token(user['id'], bool(user['is_super_admin']))

        url = f"{frontend_url}/login?status=success&token={token}&ms_id_token=dev"
        response = RedirectResponse(url=url)
        set_access_token_cookie(response, token)
        return response
