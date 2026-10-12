"""Request dependencies — authentication.

Mirrors the legacy Express ``protect`` middleware: accepts a JWT either as a
``Authorization: Bearer <token>`` header or as a ``?token=`` query parameter
(the latter is needed for SSE / EventSource, which cannot set headers).
"""

from typing import Optional

from beanie import PydanticObjectId
from fastapi import Depends, HTTPException, Query, Request, status

from app.core.security import decode_access_token
from app.models.user import User
import jwt as pyjwt


def _extract_token(request: Request, token_q: Optional[str]) -> Optional[str]:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return token_q


async def get_current_user(
    request: Request,
    token: Optional[str] = Query(default=None),
) -> User:
    raw = _extract_token(request, token)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authorized to access this route",
        )
    try:
        payload = decode_access_token(raw)
    except pyjwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authorized to access this route",
        )

    user_id = payload.get("id")
    if not user_id or not PydanticObjectId.is_valid(user_id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        )

    user = await User.get(PydanticObjectId(user_id))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer exists"
        )
    return user


async def get_current_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user
