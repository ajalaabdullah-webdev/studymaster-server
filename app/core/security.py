"""Authentication primitives.

Deliberately mirrors the legacy Node backend so existing users are unaffected:

- JWT: HS256, payload ``{"id": <user _id string>}`` + ``exp``/``iat``,
  signed with the same ``JWT_SECRET`` and a 30-day expiry. Tokens already in
  users' localStorage remain valid; new tokens are accepted by nothing but this
  service once Node is retired.
- Passwords: bcrypt. bcryptjs ($2a$/$2b$) hashes verify natively with the
  Python ``bcrypt`` library, so no password resets are required.
"""

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import settings


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(plain: str) -> str:
    """Hash a password with bcrypt (cost 10, matching the legacy backend)."""
    salt = bcrypt.gensalt(rounds=10)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# JWT (Node jsonwebtoken compatible)
# --------------------------------------------------------------------------- #
def create_access_token(user_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "id": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=settings.jwt_expire_days)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT. Raises ``jwt.PyJWTError`` on failure."""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def get_user_id_from_token(token: str) -> str | None:
    try:
        payload = decode_access_token(token)
        return payload.get("id")
    except jwt.PyJWTError:
        return None
