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
    # Try the supplied password against every active, claimed row. Usually it disambiguates
    # on its own (different password per job -> exactly one match, no picker needed). If the
    # person reuses the same password at both places, more than one row matches -- see below.
    rows = await db.pool.fetch(
        """
        SELECT users.id, users.name, users.email, users.password_hash, users.active, roles.name AS role,
               restaurants.id AS restaurant_id, restaurants.name AS restaurant_name
        FROM users
        JOIN roles ON roles.id = users.role_id
        JOIN restaurants ON restaurants.id = users.restaurant_id
        WHERE users.email = $1
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

    def _account(row, other_restaurants=None):
        user = {
            "id": row["id"],
            "name": row["name"],
            "email": row["email"],
            "role": row["role"],
            "restaurantId": row["restaurant_id"],
            "restaurantName": row["restaurant_name"],
        }
        if other_restaurants:
            user["otherRestaurants"] = other_restaurants
        return {"token": make_token(row["id"]), "user": user}

    matches = [
        row for row in active_rows
        if row["password_hash"] is not None and verify_password(creds.password, row["password_hash"])
    ]
    if len(matches) == 1:
        # Different password per restaurant already disambiguates fine (no picker needed), but
        # surface any other restaurant(s) this email has a row at -- purely informational, since
        # we have no valid token for a row we didn't just verify the password against. `status`
        # distinguishes "log in there" (already claimed, different password) from "sign up
        # there" (invited but no password set yet) so the hint doesn't tell someone to log in
        # somewhere they can't yet.
        others = [
            {"name": r["restaurant_name"], "status": "pending" if r["password_hash"] is None else "active"}
            for r in active_rows if r["id"] != matches[0]["id"]
        ]
        return _account(matches[0], others)
    if len(matches) > 1:
        # Same password matches more than one restaurant account for this email -- picking one
        # arbitrarily would silently log them into the wrong restaurant. The password already
        # proved who they are for every matching row, so hand back a real, usable token for
        # each one and let the frontend show a "which restaurant?" picker instead of guessing.
        return {"accounts": [_account(row) for row in matches]}

    pending_rows = [r for r in active_rows if r["password_hash"] is None]
    if pending_rows:
        return JSONResponse(
            status_code=403,
            content={
                "detail": "Choose a password to finish setting up your account.",
                "pending": True,
                # Only name it when unambiguous -- see invite_status's identical rule below.
                "restaurantName": pending_rows[0]["restaurant_name"] if len(pending_rows) == 1 else None,
            },
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
        SELECT users.id, users.name, users.email, users.password_hash, users.active, roles.name AS role,
               restaurants.id AS restaurant_id, restaurants.name AS restaurant_name
        FROM users
        JOIN roles ON roles.id = users.role_id
        JOIN restaurants ON restaurants.id = users.restaurant_id
        WHERE users.email = $1
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
        "user": {
            "id": row["id"],
            "name": row["name"],
            "email": row["email"],
            "role": row["role"],
            "restaurantId": row["restaurant_id"],
            "restaurantName": row["restaurant_name"],
        },
    }


@router.get("/api/auth/invite-status")
async def invite_status(email: str):
    """Read-only lookup so the landing-page signup flow can tell someone whether to set a
    password or log in, before they touch either field. `set_password` remains the
    authoritative check -- this just drives which UI step to show."""
    if not db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    rows = await db.pool.fetch(
        """
        SELECT users.password_hash, users.active, restaurants.name AS restaurant_name
        FROM users JOIN restaurants ON restaurants.id = users.restaurant_id
        WHERE users.email = $1
        """,
        email,
    )
    if not rows:
        return {"status": "not_found"}
    pending = [r for r in rows if r["password_hash"] is None and r["active"]]
    if pending:
        # Only name the restaurant when there's exactly one pending invite to claim -- with two
        # simultaneous pending invites for the same email (the known ambiguous-signup edge case),
        # set_password always resolves to the oldest one anyway, so naming a specific restaurant
        # here would be a guess dressed up as a fact.
        return {"status": "pending", "restaurantName": pending[0]["restaurant_name"] if len(pending) == 1 else None}
    return {"status": "active"}


@router.get("/api/me")
async def me(user: dict = Depends(require_user)):
    return user
