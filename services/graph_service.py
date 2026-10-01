"""Microsoft Graph API client for employee profile sync.

Reuses the existing Microsoft Entra App Registration (MICROSOFT_CLIENT_ID /
MICROSOFT_CLIENT_SECRET / MICROSOFT_TENANT_ID, see tools/auth_router.py) —
no separate app registration or login flow is introduced here.

Two acquisition paths are used:
  - Delegated: the access token already issued during the existing OAuth
    callback (scope already includes User.Read) is reused as-is to call
    GET /v1.0/me for the signed-in employee. No extra token round trip.
  - Application (client credentials): used only by the background sync
    (services/graph_sync_job.py) to read other users' profiles via
    GET /v1.0/users, which requires the User.Read.All application
    permission with admin consent. If that consent hasn't been granted,
    get_app_only_token() logs and returns None rather than raising, so the
    background job degrades to a no-op instead of crashing the app.
"""

import asyncio
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
_SELECT_FIELDS = "id,displayName,mail,userPrincipalName,department,jobTitle,accountEnabled"

_MAX_RETRIES = 1
_REQUEST_TIMEOUT = 2.0


def _normalize_profile(raw: dict) -> dict:
    return {
        "object_id": raw.get("id"),
        "display_name": raw.get("displayName"),
        "email": (raw.get("mail") or raw.get("userPrincipalName") or "").lower() or None,
        "user_principal_name": raw.get("userPrincipalName"),
        "department": raw.get("department"),
        "job_title": raw.get("jobTitle"),
        "account_enabled": raw.get("accountEnabled"),
    }


async def fetch_me_profile(access_token: str) -> Optional[dict]:
    """Fetch the signed-in employee's own profile via GET /v1.0/me.

    Best-effort: returns None on any failure so callers (the OAuth callback)
    can fall back to ID-token claims and never block login on Graph.
    """
    url = f"{GRAPH_BASE_URL}/me"
    params = {"$select": _SELECT_FIELDS}
    headers = {"Authorization": f"Bearer {access_token}"}

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                response = await client.get(url, params=params, headers=headers)
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", "1"))
                logger.warning(
                    "Graph /me throttled",
                    extra={"event": "graph_me_throttled", "retry_after": retry_after},
                )
                await asyncio.sleep(retry_after)
                continue
            response.raise_for_status()
            return _normalize_profile(response.json())
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Graph /me request rejected",
                extra={"event": "graph_me_failed", "status_code": exc.response.status_code},
            )
            return None
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt < _MAX_RETRIES:
                logger.warning(
                    "Graph /me request errored, retrying",
                    extra={"event": "graph_me_retry", "attempt": attempt, "error_type": type(exc).__name__},
                )
                await asyncio.sleep(0.5 * attempt)
            else:
                logger.warning(
                    "Graph /me request errored, giving up",
                    extra={"event": "graph_me_error", "error_type": type(exc).__name__},
                )
        except Exception:
            logger.exception(
                "Unexpected error calling Graph /me",
                extra={"event": "graph_me_unexpected_error"},
            )
            return None
    return None


async def get_app_only_token() -> Optional[str]:
    """Acquire an application-only Graph token via client credentials, using
    the same Entra App Registration as the delegated login flow.

    Requires the User.Read.All application permission to have admin consent.
    Returns None (logged) if that hasn't happened yet or credentials are
    missing, so the background sync can skip a run instead of crashing.
    """
    tenant_id = os.getenv("MICROSOFT_TENANT_ID", "common")
    client_id = os.getenv("MICROSOFT_CLIENT_ID", "")
    client_secret = os.getenv("MICROSOFT_CLIENT_SECRET", "")
    if not client_id or not client_secret or tenant_id in ("common", "organizations", "consumers"):
        logger.warning(
            "Graph app-only token not configured (missing client credentials or a tenant-specific MICROSOFT_TENANT_ID)",
            extra={"event": "graph_app_token_unconfigured"},
        )
        return None

    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                response = await client.post(token_url, data=data)
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", "1"))
                await asyncio.sleep(retry_after)
                continue
            response.raise_for_status()
            return response.json().get("access_token")
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Graph app-only token request rejected — check admin consent for User.Read.All",
                extra={"event": "graph_app_token_rejected", "status_code": exc.response.status_code},
            )
            return None
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(0.5 * attempt)
            else:
                logger.error(
                    "Graph app-only token request failed",
                    extra={"event": "graph_app_token_error", "error_type": type(exc).__name__},
                )
        except Exception:
            logger.exception(
                "Unexpected error acquiring Graph app-only token",
                extra={"event": "graph_app_token_unexpected_error"},
            )
            return None
    return None


async def search_users(query: str = "") -> list:
    """Search Entra users by display name or email using Graph $search.
    Falls back to searching local registered users if Graph is unavailable or lacks tenant admin consent.
    """
    q = (query or "").strip().replace("'", "")
    results_map = {}

    # 1. Search local organization users
    try:
        from postgresql_db.database import fetch_all
        if q:
            like_q = f"%{q}%"
            rows = await fetch_all(
                """
                SELECT id, microsoft_object_id, email, full_name, job_title, department
                FROM users
                WHERE is_active = true
                  AND (
                      email ILIKE $1
                      OR full_name ILIKE $1
                      OR department ILIKE $1
                      OR job_title ILIKE $1
                  )
                ORDER BY full_name, email
                LIMIT 50
                """,
                like_q,
            )
        else:
            rows = await fetch_all(
                """
                SELECT id, microsoft_object_id, email, full_name, job_title, department
                FROM users
                WHERE is_active = true
                ORDER BY full_name, email
                LIMIT 50
                """
            )
        for r in rows:
            email = (r["email"] or "").lower().strip()
            key = email or str(r["id"])
            results_map[key] = {
                "object_id": r["microsoft_object_id"] or str(r["id"]),
                "display_name": r["full_name"] or "",
                "email": email,
                "job_title": r["job_title"] or "",
                "department": r["department"] or "",
                "user_principal_name": email,
            }
    except Exception as exc:
        logger.debug(f"Local user search note: {exc}")

    # 2. Query Microsoft Graph if query length >= 2
    if q and len(q) >= 2:
        token = await get_app_only_token()
        if token:
            url = f"{GRAPH_BASE_URL}/users"
            headers = {
                "Authorization": f"Bearer {token}",
                "ConsistencyLevel": "eventual",
            }
            params = {
                "$search": f'"displayName:{q}" OR "mail:{q}" OR "userPrincipalName:{q}"',
                "$select": _SELECT_FIELDS,
                "$top": 25,
            }
            try:
                async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                    resp = await client.get(url, params=params, headers=headers)
                    if resp.status_code == 200:
                        for u in resp.json().get("value", []):
                            prof = _normalize_profile(u)
                            if prof["email"] and prof["email"] not in results_map:
                                results_map[prof["email"]] = prof
            except Exception as exc:
                logger.debug(f"Graph $search note: {exc}")

    return list(results_map.values())
async def fetch_users_page(access_token: str, next_link: Optional[str] = None) -> Optional[dict]:
    """Fetch one page of /v1.0/users (or follow a previous @odata.nextLink).

    Returns {"value": [normalized profile, ...], "next_link": str | None},
    or None if this page could not be fetched after retries.
    """
    url = next_link or f"{GRAPH_BASE_URL}/users"
    params = None if next_link else {"$select": _SELECT_FIELDS, "$top": 999}
    headers = {"Authorization": f"Bearer {access_token}"}

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                response = await client.get(url, params=params, headers=headers)
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", "2"))
                logger.warning(
                    "Graph /users throttled",
                    extra={"event": "graph_users_throttled", "retry_after": retry_after},
                )
                await asyncio.sleep(retry_after)
                continue
            response.raise_for_status()
            body = response.json()
            return {
                "value": [_normalize_profile(u) for u in body.get("value", [])],
                "next_link": body.get("@odata.nextLink"),
            }
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Graph /users page request failed",
                extra={"event": "graph_users_failed", "status_code": exc.response.status_code},
            )
            return None
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(0.5 * attempt)
            else:
                logger.error(
                    "Graph /users page request errored",
                    extra={"event": "graph_users_error", "error_type": type(exc).__name__},
                )
        except Exception:
            logger.exception(
                "Unexpected error calling Graph /users",
                extra={"event": "graph_users_unexpected_error"},
            )
            return None
    return None


# ────────────────────────────────────────────────────────────
# Microsoft Graph Organizational Hierarchy & Manager Chain
# ────────────────────────────────────────────────────────────

_hierarchy_cache: dict = {}
_HIERARCHY_CACHE_TTL_SECONDS = int(os.getenv("GRAPH_HIERARCHY_CACHE_TTL", "300"))


def clear_graph_hierarchy_cache():
    global _hierarchy_cache
    _hierarchy_cache.clear()


def normalize_department(dept: Optional[str]) -> str:
    """Normalize department names: trim whitespace and lowercase."""
    return (dept or "").strip().lower()


def _extract_nested_managers(manager_obj: Optional[dict]) -> list:
    """Recursively extract nested manager objects if expanded with $levels."""
    chain = []
    curr = manager_obj
    visited = set()
    while curr and isinstance(curr, dict):
        oid = curr.get("id") or curr.get("object_id")
        if not oid or oid in visited:
            break
        visited.add(oid)
        norm = _normalize_profile(curr) if "displayName" in curr or "mail" in curr else curr
        chain.append(norm)
        curr = curr.get("manager")
    return chain


def flatten_manager_chain(manager_obj: Optional[dict]) -> list:
    """Convert a nested Microsoft Graph manager response into an ordered list.
    Order:
      Index 0: Immediate manager
      ...
      Index -1: Root manager / CEO
    """
    return _extract_nested_managers(manager_obj)


async def fetch_user_manager_chain(user_oid: str, access_token: Optional[str] = None) -> tuple:
    """Fetch requester profile and ordered manager chain from Microsoft Graph.

    Returns (requester_profile, [immediate_manager, ..., company_root_manager]).
    Order of chain:
      Index 0: Immediate manager
      ...
      Index -1: Highest person in company hierarchy (CEO)
    """
    if not user_oid:
        return None, []

    token = access_token or await get_app_only_token()
    if not token:
        logger.warning("No Graph token available for manager chain lookup")
        return None, []

    headers = {
        "Authorization": f"Bearer {token}",
        "ConsistencyLevel": "eventual",
    }
    url = f"{GRAPH_BASE_URL}/users/{user_oid}"
    select_fields = "id,displayName,mail,userPrincipalName,department,jobTitle,accountEnabled"
    params = {
        "$select": select_fields,
        "$expand": f"manager($levels=max;$select={select_fields})",
    }

    requester_profile = None
    manager_chain = []

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code == 200:
                body = resp.json()
                requester_profile = _normalize_profile(body)
                manager_chain = _extract_nested_managers(body.get("manager"))
            elif resp.status_code == 404:
                return None, []
    except Exception as exc:
        logger.warning(f"Graph manager expand request failed: {exc}")

    # Fallback loop: if $expand=manager($levels=max) returned only 1 level or is unsupported
    if requester_profile and (not manager_chain or len(manager_chain) <= 1):
        try:
            curr_id = manager_chain[0]["object_id"] if manager_chain else user_oid
            visited_ids = {user_oid}
            if manager_chain:
                visited_ids.add(curr_id)

            max_depth = 15
            for _ in range(max_depth):
                mgr_url = f"{GRAPH_BASE_URL}/users/{curr_id}/manager"
                params = {"$select": select_fields}
                async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                    mgr_resp = await client.get(mgr_url, params=params, headers=headers)
                    if mgr_resp.status_code != 200:
                        break
                    mgr_data = mgr_resp.json()
                    mgr_profile = _normalize_profile(mgr_data)
                    mgr_oid = mgr_profile.get("object_id")
                    if not mgr_oid or mgr_oid in visited_ids:
                        break
                    visited_ids.add(mgr_oid)
                    if not manager_chain or manager_chain[-1]["object_id"] != mgr_oid:
                        manager_chain.append(mgr_profile)
                    curr_id = mgr_oid
        except Exception as exc:
            logger.debug(f"Sequential manager walk failed: {exc}")

    return requester_profile, manager_chain


async def get_cached_or_live_hierarchy(user_oid: str, access_token: Optional[str] = None) -> tuple:
    """Retrieve hierarchy from cache or live Graph API with TTL."""
    import time
    global _hierarchy_cache
    now = time.monotonic()

    cached = _hierarchy_cache.get(user_oid)
    if cached:
        exp_time, req_prof, chain = cached
        if now < exp_time:
            return req_prof, chain

    req_prof, chain = await fetch_user_manager_chain(user_oid, access_token=access_token)
    if req_prof is not None:
        _hierarchy_cache[user_oid] = (now + _HIERARCHY_CACHE_TTL_SECONDS, req_prof, chain)
    return req_prof, chain
