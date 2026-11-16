"""The Social Media portal's people, for a script's invite screen.

A script is written and reviewed with the Social Media portal's people, so its invite screen lists them rather than this
portal's own directory. This asks the Social Media portal's backend (app-to-app, signed with SCRIPT_SERVICE_SECRET, or
SESSION_SECRET when that isn't set, as the service "share-document"). The people are matched here by email, as everywhere
in the script flow; one who isn't a user here yet is marked (`has_account` false) and can't be invited until they are.
Env: SOCIAL_MEDIA_URL (default http://localhost:8009).
"""

import datetime
import os
import secrets
from typing import Any, Dict, List
from uuid import UUID

import httpx
import jwt

from postgresql_db.database import get_pool
from services.shared.auth.service_security_policy import JWT_ALGORITHM, JWT_SECRET

SERVICE = "share-document"
SCOPE = "script_users:read"
TIMEOUT_SEC = 15


class DirectoryError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _token() -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    claims = {"sub": f"service:{SERVICE}", "service": SERVICE, "is_service_token": True, "scopes": [SCOPE],
              "iat": int(now.timestamp()), "exp": int((now + datetime.timedelta(minutes=5)).timestamp()), "jti": secrets.token_urlsafe(12)}
    return jwt.encode(claims, os.getenv("SCRIPT_SERVICE_SECRET") or JWT_SECRET, algorithm=JWT_ALGORITHM)


async def script_users(user_id: UUID) -> List[Dict[str, Any]]:
    """The Social Media portal's people `user_id` could invite to a script."""
    pool = get_pool()
    me = await pool.fetchrow("SELECT email FROM users WHERE id = $1 AND deleted_at IS NULL", user_id)
    if not me:
        raise DirectoryError("Unknown user", 403)
    base = os.getenv("SOCIAL_MEDIA_URL", "http://localhost:8009").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as client:
            r = await client.get(f"{base}/api/script-docs/service/script-users", params={"email": me["email"]},
                                 headers={"Authorization": f"Bearer {_token()}"})
    except httpx.HTTPError as e:
        raise DirectoryError(f"The Social Media portal couldn't be reached: {e}") from e
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = None
        raise DirectoryError(str(detail or r.text[:200]), r.status_code if 400 <= r.status_code < 500 else 502)
    people = r.json()
    have = {row["email"].lower() for row in await pool.fetch(
        "SELECT email FROM users WHERE deleted_at IS NULL AND is_active = true AND lower(email) = ANY($1::text[])",
        [p["email"].lower() for p in people])}
    return [{**p, "has_account": p["email"].lower() in have} for p in people]
