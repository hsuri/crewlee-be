"""Auth helpers: admin-password gate, prototype bearer tokens, and password hashing.

Both `require_auth` and `require_user` are prototype-grade — see module docstrings
at call sites / CLAUDE.md for the caveats before this touches real credentials.
"""
import base64
import os
from typing import Optional

import bcrypt
from fastapi import Header, HTTPException, status

from app.db import session as db


def admin_password() -> str:
    return os.environ.get("ADMIN_PASSWORD", "admin123")


async def require_auth(authorization: Optional[str] = Header(None)) -> None:
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token or token != admin_password():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


# User session tokens are intentionally minimal for this prototype: an unsigned
# base64 user id, not a real signed/expiring session. Fine for dummy demo users;
# revisit before this touches real credentials.
def make_token(user_id: int) -> str:
    return base64.urlsafe_b64encode(str(user_id).encode()).decode()


def decode_token(token: str) -> Optional[int]:
    try:
        return int(base64.urlsafe_b64decode(token).decode())
    except Exception:
        return None


async def require_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    user_id = decode_token(authorization[7:])
    if user_id is None or not db.pool:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    row = await db.pool.fetchrow(
        """
        SELECT users.id, users.name, users.email, users.active, roles.name AS role
        FROM users JOIN roles ON roles.id = users.role_id
        WHERE users.id = $1
        """,
        user_id,
    )
    # active is enforced here (not just at login) because tokens never expire -- this is the
    # only checkpoint that actually stops a deactivated user's existing token from working.
    if not row or not row["active"]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return dict(row)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())
