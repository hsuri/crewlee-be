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

    # Email is scoped per-restaurant (not globally unique), so the same email can match
    # more than one row here -- e.g. the same person with accounts at two restaurants.
    # Try the supplied password against every active, claimed row and log into whichever
    # one it matches; this needs no "which restaurant?" UI since the password disambiguates.
    # creds.email is already lowercased by UserLoginRequest's validator; LOWER() on the column
    # side matches it against any already-stored row regardless of the case it was saved in.
    rows = await db.pool.fetch(
        """
        SELECT users.id, users.name, users.email, users.password_hash, users.active, roles.name AS role
        FROM users JOIN roles ON roles.id = users.role_id
        WHERE LOWER(users.email) = $1
        """,
        creds.email,
    )
    if not rows:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    active_rows = [r for r in rows if r["active"]]
    if not active_rows:
        return JSONResponse(
            status_code=403,
            content={"detail": "This account has been deactivated. Contact your manager.", "deactivated": True},
        )

    for row in active_rows:
        if row["password_hash"] is not None and verify_password(creds.password, row["password_hash"]):
            return {
                "token": make_token(row["id"]),
                "user": {"id": row["id"], "name": row["name"], "email": row["email"], "role": row["role"]},
            }

    if any(row["password_hash"] is None for row in active_rows):
        return JSONResponse(
            status_code=403,
            content={"detail": "Choose a password to finish setting up your account.", "pending": True},
        )

    raise HTTPException(status_code=401, detail="Invalid email or password")


@router.post("/api/auth/set-password")
async def set_password(payload: SetPasswordRequest):
    if not db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    # Same email can now span multiple restaurant accounts -- resolve to the oldest still-pending
    # one. (Known limitation: if two restaurants both have an unclaimed pending invite for the
    # exact same email, the other one stays pending with no way to disambiguate here.)
    rows = await db.pool.fetch(
        """
        SELECT users.id, users.name, users.email, users.password_hash, users.active, roles.name AS role
        FROM users JOIN roles ON roles.id = users.role_id
        WHERE LOWER(users.email) = $1
        ORDER BY users.id
        """,
        payload.email,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="No pending account found for that email")

    row = next((r for r in rows if r["password_hash"] is None), None)
    if row is None:
        raise HTTPException(status_code=409, detail="This account already has a password. Use the login form instead.")
    if not row["active"]:
        raise HTTPException(status_code=403, detail="This account has been deactivated.")
    if len(payload.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    await db.pool.execute(
        "UPDATE users SET password_hash = $1 WHERE id = $2", hash_password(payload.password), row["id"]
    )
    return {
        "token": make_token(row["id"]),
        "user": {"id": row["id"], "name": row["name"], "email": row["email"], "role": row["role"]},
    }


@router.get("/api/auth/invite-status")
async def invite_status(email: str):
    """Read-only lookup so the landing-page signup flow can tell someone whether to set a
    password or log in, before they touch either field. `set_password` remains the
    authoritative check -- this just drives which UI step to show."""
    if not db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    # Not a Pydantic model field (this is a query param), so normalize by hand here.
    email = email.strip().lower()
    rows = await db.pool.fetch(
        "SELECT password_hash, active FROM users WHERE LOWER(email) = $1", email
    )
    if not rows:
        return {"status": "not_found"}
    if any(r["password_hash"] is None and r["active"] for r in rows):
        return {"status": "pending"}
    return {"status": "active"}


@router.get("/api/me")
async def me(user: dict = Depends(require_user)):
    return user
