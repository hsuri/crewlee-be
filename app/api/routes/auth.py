from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.core.security import hash_password, make_token, require_user, verify_password
from app.db import session as db
from app.models.schemas import SetPasswordRequest, UserLoginRequest

router = APIRouter()


@router.post("/api/auth/login")
async def user_login(creds: UserLoginRequest):
    if not db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    row = await db.pool.fetchrow(
        """
        SELECT users.id, users.name, users.email, users.password_hash, users.active, roles.name AS role
        FROM users JOIN roles ON roles.id = users.role_id
        WHERE users.email = $1
        """,
        creds.email,
    )
    if not row:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not row["active"]:
        return JSONResponse(
            status_code=403,
            content={"detail": "This account has been deactivated. Contact your manager.", "deactivated": True},
        )
    if row["password_hash"] is None:
        return JSONResponse(
            status_code=403,
            content={"detail": "Choose a password to finish setting up your account.", "pending": True},
        )
    if not verify_password(creds.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    return {
        "token": make_token(row["id"]),
        "user": {"id": row["id"], "name": row["name"], "email": row["email"], "role": row["role"]},
    }


@router.post("/api/auth/set-password")
async def set_password(payload: SetPasswordRequest):
    if not db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    row = await db.pool.fetchrow(
        """
        SELECT users.id, users.name, users.email, users.password_hash, users.active, roles.name AS role
        FROM users JOIN roles ON roles.id = users.role_id
        WHERE users.email = $1
        """,
        payload.email,
    )
    if not row:
        raise HTTPException(status_code=404, detail="No pending account found for that email")
    if not row["active"]:
        raise HTTPException(status_code=403, detail="This account has been deactivated.")
    if row["password_hash"] is not None:
        raise HTTPException(status_code=409, detail="This account already has a password. Use the login form instead.")
    if len(payload.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    await db.pool.execute(
        "UPDATE users SET password_hash = $1 WHERE id = $2", hash_password(payload.password), row["id"]
    )
    return {
        "token": make_token(row["id"]),
        "user": {"id": row["id"], "name": row["name"], "email": row["email"], "role": row["role"]},
    }


@router.get("/api/me")
async def me(user: dict = Depends(require_user)):
    return user
