import datetime

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from app.core.config import DB_FIELDS, DB_TABLE
from app.core.security import admin_password, require_auth
from app.db import session as db
from app.models.schemas import LoginRequest, WaitlistEntry

router = APIRouter()


def _serialize_row(row: asyncpg.Record) -> dict:
    result = {}
    for key, value in dict(row).items():
        if isinstance(value, datetime.datetime):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result


@router.post("/api/waitlist")
async def join_waitlist(entry: WaitlistEntry):
    data = entry.model_dump()

    for f in DB_FIELDS:
        if f["required"] and not data.get(f["name"]):
            raise HTTPException(status_code=400, detail=f"{f['label']} is required")

    if not db.pool:
        return {"success": True, "id": None}

    col_names = [f["name"] for f in DB_FIELDS]
    values = [data[n] for n in col_names]
    placeholders = ", ".join(f"${i + 1}" for i in range(len(col_names)))

    try:
        row = await db.pool.fetchrow(
            f"INSERT INTO {DB_TABLE} ({', '.join(col_names)}) "
            f"VALUES ({placeholders}) RETURNING id",
            *values,
        )
        return {"success": True, "id": row["id"]}
    except Exception as e:
        print(f"[db] Insert error: {e}")
        raise HTTPException(status_code=500, detail="Database error")


@router.post("/api/admin/login")
async def admin_login(req: LoginRequest):
    if req.password != admin_password():
        raise HTTPException(status_code=401, detail="Invalid password")
    return {"token": admin_password()}


@router.get("/api/waitlist")
async def get_waitlist(_=Depends(require_auth)):
    if not db.pool:
        return []
    try:
        rows = await db.pool.fetch(
            f"SELECT * FROM {DB_TABLE} ORDER BY created_at DESC"
        )
        return [_serialize_row(r) for r in rows]
    except Exception as e:
        print(f"[db] Query error: {e}")
        raise HTTPException(status_code=500, detail="Database error")
