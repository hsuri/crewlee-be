from fastapi import APIRouter, Depends, HTTPException

from app.core.security import make_token, require_user, verify_password
from app.db import session as db
from app.models.schemas import UserLoginRequest

router = APIRouter()


@router.post("/api/auth/login")
async def user_login(creds: UserLoginRequest):
    if not db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    row = await db.pool.fetchrow(
        """
        SELECT users.id, users.name, users.email, users.password_hash, roles.name AS role
        FROM users JOIN roles ON roles.id = users.role_id
        WHERE users.email = $1
        """,
        creds.email,
    )
    if not row or not verify_password(creds.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    return {
        "token": make_token(row["id"]),
        "user": {"id": row["id"], "name": row["name"], "email": row["email"], "role": row["role"]},
    }


@router.get("/api/me")
async def me(user: dict = Depends(require_user)):
    return user
