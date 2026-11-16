"""Who may call the Social Media portal's script API (tools/sec_filings/script_app_router.py).

The call is signed with a secret of its own, SCRIPT_SERVICE_SECRET, shared only by the two backends that make and receive it
(falling back to SESSION_SECRET when it isn't set, for setups where those are the same). It is not the session secret, so
the two portals don't need the same session secret, and this one can't sign anybody in. A token must name the calling
service ("social-media") and carry the scope the route needs.
"""

import os
from typing import Any, Dict, Optional

import jwt
from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from services.shared.auth.service_security_policy import JWT_ALGORITHM, JWT_SECRET

CALLER = "social-media"
_bearer = HTTPBearer(auto_error=False)


def _secret() -> str:
    return os.getenv("SCRIPT_SERVICE_SECRET") or JWT_SECRET


def require_script_service(scope: str):
    """Dependency: the caller is the Social Media portal's backend with `scope`."""
    async def guard(credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer)) -> Dict[str, Any]:
        if not credentials:
            raise HTTPException(status_code=401, detail="Missing authentication credentials")
        try:
            claims = jwt.decode(credentials.credentials, _secret(), algorithms=[JWT_ALGORITHM], options={"verify_exp": True})
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Service token has expired")
        except jwt.PyJWTError as e:
            raise HTTPException(status_code=401, detail=f"Invalid service token: {e}")
        if claims.get("service") != CALLER or not claims.get("is_service_token"):
            raise HTTPException(status_code=403, detail="Only the Social Media portal's backend may call this")
        if scope not in (claims.get("scopes") or []):
            raise HTTPException(status_code=403, detail=f"The token doesn't carry the scope {scope}")
        return {"caller_service": CALLER, "granted_scope": scope}
    return guard
