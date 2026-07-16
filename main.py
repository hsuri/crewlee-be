import base64
import json
import os
import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from typing import Optional

import asyncpg
import bcrypt
from fastapi import FastAPI, HTTPException, Header, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from scheduling_service import (
    parse_date, parse_time, release_shift_if_no_live_swaps, serialize_shift, validate_assignment, week_start,
)

import config as cfg

load_dotenv(".env.local")

# ── DB pool ───────────────────────────────────────────────────────────────────

_pool: Optional[asyncpg.Pool] = None


async def _create_pool() -> Optional[asyncpg.Pool]:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("[db] No DATABASE_URL — running without database")
        return None

    parsed = urlparse(db_url)
    qs = parse_qs(parsed.query)

    # DB_PASSWORD env var takes priority over whatever is in the URL —
    # avoids URL-encoding issues with special characters like !
    password = os.environ.get("DB_PASSWORD") or parsed.password or ""

    if "/cloudsql/" in db_url:
        socket_dir = qs.get("host", [None])[0]
        return await asyncpg.create_pool(
            host=socket_dir,
            user=parsed.username,
            password=password,
            database=parsed.path.lstrip("/"),
        )

    return await asyncpg.create_pool(
        host=parsed.hostname,
        port=parsed.port or 5432,
        user=parsed.username,
        password=password,
        database=parsed.path.lstrip("/"),
    )


async def _init_db(pool: asyncpg.Pool) -> None:
    col_defs = []
    for f in cfg.DB_FIELDS:
        not_null = "NOT NULL" if f["required"] else ""
        col_defs.append(f"  {f['name']} text {not_null}".strip())

    await pool.execute(f"""
        CREATE TABLE IF NOT EXISTS {cfg.DB_TABLE} (
            id         SERIAL PRIMARY KEY,
            {",\n            ".join(col_defs)},
            created_at timestamptz DEFAULT now()
        )
    """)
    print(f"[db] Table '{cfg.DB_TABLE}' ready")

    schema_path = Path(__file__).parent / "db" / "schema.sql"
    await pool.execute(schema_path.read_text())
    print("[db] Core schema (restaurants, roles, users) ready")


# One dummy account per role, seeded only when the users table is empty —
# lets `docker compose up` give you a working login on a fresh DB with no manual step.
DEMO_USERS = [
    {"name": "Morgan Manager", "email": "manager@demo.com", "password": "password123", "role": "manager"},
    {"name": "Frankie FOH", "email": "foh@demo.com", "password": "password123", "role": "foh"},
    {"name": "Jordan FOH", "email": "jordan@demo.com", "password": "password123", "role": "foh"},
    {"name": "Taylor FOH", "email": "taylor@demo.com", "password": "password123", "role": "foh"},
    {"name": "Bailey BOH", "email": "boh@demo.com", "password": "password123", "role": "boh"},
    {"name": "Alex BOH", "email": "alex@demo.com", "password": "password123", "role": "boh"},
]


async def _seed_demo_data(pool: asyncpg.Pool) -> None:
    if await pool.fetchval("SELECT count(*) FROM users"):
        # Existing demo databases predate scheduling profiles; keep the visible
        # demo usable without changing availability for real customer accounts.
        await pool.execute(
            """UPDATE users SET weekly_availability = $1::jsonb
               WHERE email LIKE '%@demo.com' AND weekly_availability = '[]'::jsonb""",
            '{"monday":[{"start":"00:00","end":"23:59"}],"tuesday":[{"start":"00:00","end":"23:59"}],"wednesday":[{"start":"00:00","end":"23:59"}],"thursday":[{"start":"00:00","end":"23:59"}],"friday":[{"start":"00:00","end":"23:59"}],"saturday":[{"start":"00:00","end":"23:59"}],"sunday":[{"start":"00:00","end":"23:59"}]}'
        )
        demo_restaurant = await pool.fetchval("SELECT id FROM restaurants WHERE name = 'Demo Restaurant'")
        if demo_restaurant:
            for demo_user in DEMO_USERS:
                role_id = await pool.fetchval("SELECT id FROM roles WHERE name = $1", demo_user["role"])
                await pool.execute(
                    """INSERT INTO users (restaurant_id, role_id, name, email, password_hash, weekly_availability)
                       VALUES ($1, $2, $3, $4, $5, $6::jsonb) ON CONFLICT (email) DO NOTHING""",
                    demo_restaurant, role_id, demo_user["name"], demo_user["email"], _hash_password(demo_user["password"]),
                    '{"monday":[{"start":"00:00","end":"23:59"}],"tuesday":[{"start":"00:00","end":"23:59"}],"wednesday":[{"start":"00:00","end":"23:59"}],"thursday":[{"start":"00:00","end":"23:59"}],"friday":[{"start":"00:00","end":"23:59"}],"saturday":[{"start":"00:00","end":"23:59"}],"sunday":[{"start":"00:00","end":"23:59"}]}'
                )
        return

    restaurant_id = await pool.fetchval(
        "INSERT INTO restaurants (name) VALUES ($1) RETURNING id", "Demo Restaurant"
    )
    for u in DEMO_USERS:
        role_id = await pool.fetchval("SELECT id FROM roles WHERE name = $1", u["role"])
        await pool.execute(
            "INSERT INTO users (restaurant_id, role_id, name, email, password_hash, weekly_availability) "
            "VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
            restaurant_id, role_id, u["name"], u["email"], _hash_password(u["password"]),
            '{"monday":[{"start":"00:00","end":"23:59"}],"tuesday":[{"start":"00:00","end":"23:59"}],"wednesday":[{"start":"00:00","end":"23:59"}],"thursday":[{"start":"00:00","end":"23:59"}],"friday":[{"start":"00:00","end":"23:59"}],"saturday":[{"start":"00:00","end":"23:59"}],"sunday":[{"start":"00:00","end":"23:59"}]}'
        )
    print("[db] Seeded demo restaurant + 3 dummy users (manager/foh/boh)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    try:
        _pool = await _create_pool()
        if _pool:
            await _init_db(_pool)
            await _seed_demo_data(_pool)
    except Exception as e:
        # DB failure must not prevent the container from starting —
        # Cloud Run kills the revision if the process dies before binding to PORT.
        print(f"[db] Startup error (running without DB): {e}")
    yield
    if _pool:
        await _pool.close()


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title=f"{cfg.PROJECT_NAME} API", version="1.0.0", lifespan=lifespan)

ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Authorization", "Content-Type"],
)

# ── Auth ──────────────────────────────────────────────────────────────────────


def _admin_password() -> str:
    return os.environ.get("ADMIN_PASSWORD", "admin123")


async def require_auth(authorization: Optional[str] = Header(None)) -> None:
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token or token != _admin_password():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


# User session tokens are intentionally minimal for this prototype: an unsigned
# base64 user id, not a real signed/expiring session. Fine for dummy demo users;
# revisit before this touches real credentials.
def _make_token(user_id: int) -> str:
    return base64.urlsafe_b64encode(str(user_id).encode()).decode()


def _decode_token(token: str) -> Optional[int]:
    try:
        return int(base64.urlsafe_b64decode(token).decode())
    except Exception:
        return None


async def require_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    user_id = _decode_token(authorization[7:])
    if user_id is None or not _pool:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    row = await _pool.fetchrow(
        """
        SELECT users.id, users.name, users.email, roles.name AS role
        FROM users JOIN roles ON roles.id = users.role_id
        WHERE users.id = $1
        """,
        user_id,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return dict(row)


# ── Models ────────────────────────────────────────────────────────────────────


class WaitlistEntry(BaseModel):
    name: str
    email: str
    restaurant: str
    role: str


class LoginRequest(BaseModel):
    password: str


class UserLoginRequest(BaseModel):
    email: str
    password: str


class AutoBuildRequest(BaseModel):
    weekStart: str


class ShiftUpdateRequest(BaseModel):
    employeeId: Optional[int] = None
    date: str
    startTime: str
    endTime: str


class ShiftCreateRequest(BaseModel):
    roleRequired: str
    date: str
    startTime: str
    endTime: str
    employeeId: Optional[int] = None


class AvailabilityRequest(BaseModel):
    weeklyAvailability: dict


class SwapDecisionRequest(BaseModel):
    approve: bool


# ── Helpers ───────────────────────────────────────────────────────────────────


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def _serialize_row(row: asyncpg.Record) -> dict:
    result = {}
    for key, value in dict(row).items():
        if isinstance(value, datetime.datetime):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/config/public")
async def public_config():
    return {
        "project": {"name": cfg.PROJECT_NAME, "slug": cfg.PROJECT_SLUG},
        "fields": cfg.DB_FIELDS,
    }


@app.post("/api/waitlist")
async def join_waitlist(entry: WaitlistEntry):
    data = entry.model_dump()

    for f in cfg.DB_FIELDS:
        if f["required"] and not data.get(f["name"]):
            raise HTTPException(status_code=400, detail=f"{f['label']} is required")

    if not _pool:
        return {"success": True, "id": None}

    col_names = [f["name"] for f in cfg.DB_FIELDS]
    values = [data[n] for n in col_names]
    placeholders = ", ".join(f"${i + 1}" for i in range(len(col_names)))

    try:
        row = await _pool.fetchrow(
            f"INSERT INTO {cfg.DB_TABLE} ({', '.join(col_names)}) "
            f"VALUES ({placeholders}) RETURNING id",
            *values,
        )
        return {"success": True, "id": row["id"]}
    except Exception as e:
        print(f"[db] Insert error: {e}")
        raise HTTPException(status_code=500, detail="Database error")


@app.post("/api/admin/login")
async def admin_login(req: LoginRequest):
    if req.password != _admin_password():
        raise HTTPException(status_code=401, detail="Invalid password")
    return {"token": _admin_password()}


@app.post("/api/auth/login")
async def user_login(creds: UserLoginRequest):
    if not _pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    row = await _pool.fetchrow(
        """
        SELECT users.id, users.name, users.email, users.password_hash, roles.name AS role
        FROM users JOIN roles ON roles.id = users.role_id
        WHERE users.email = $1
        """,
        creds.email,
    )
    if not row or not _verify_password(creds.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    return {
        "token": _make_token(row["id"]),
        "user": {"id": row["id"], "name": row["name"], "email": row["email"], "role": row["role"]},
    }


@app.get("/api/me")
async def me(user: dict = Depends(require_user)):
    return user


# ── Scheduling ───────────────────────────────────────────────────────────────

async def _restaurant_id_for(user_id: int) -> int:
    if not _pool:
        raise HTTPException(503, detail="Database unavailable")
    restaurant_id = await _pool.fetchval("SELECT restaurant_id FROM users WHERE id = $1", user_id)
    if not restaurant_id:
        raise HTTPException(404, detail="Restaurant membership not found")
    return restaurant_id


@app.get("/api/scheduling/shifts")
async def list_shifts(weekStart: Optional[str] = None, user: dict = Depends(require_user)):
    restaurant_id = await _restaurant_id_for(user["id"])
    is_manager = user["role"] == "manager"
    if weekStart:
        monday = week_start(parse_date(weekStart))
        query = "SELECT id FROM shifts WHERE resto_id = $1 AND shift_date BETWEEN $2 AND $3" + (
            "" if is_manager else " AND employee_id = $4"
        )
        args = (restaurant_id, monday, monday + datetime.timedelta(days=6)) + (() if is_manager else (user["id"],))
    else:
        query = "SELECT id FROM shifts WHERE resto_id = $1" + ("" if is_manager else " AND employee_id = $2")
        args = (restaurant_id,) + (() if is_manager else (user["id"],))
    query += " ORDER BY shift_date, start_time"
    rows = await _pool.fetch(query, *args)
    return [await serialize_shift(_pool, row) for row in rows]


@app.get("/api/scheduling/employees")
async def scheduling_employees(user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can view the team roster")
    restaurant_id = await _restaurant_id_for(user["id"])
    rows = await _pool.fetch("SELECT u.id, u.name, r.name AS role FROM users u JOIN roles r ON r.id = u.role_id WHERE u.restaurant_id = $1 AND r.name IN ('foh', 'boh') ORDER BY u.name", restaurant_id)
    return [{"id": row["id"], "name": row["name"], "role": row["role"]} for row in rows]


@app.post("/api/scheduling/shifts")
async def create_shift(payload: ShiftCreateRequest, user: dict = Depends(require_user)):
    """Create an open shift or validate and publish an assigned one."""
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can create shifts")
    if payload.roleRequired not in ("foh", "boh"):
        raise HTTPException(422, detail="roleRequired must be FOH or BOH")
    restaurant_id = await _restaurant_id_for(user["id"])
    shift_date, start_time, end_time = parse_date(payload.date), parse_time(payload.startTime), parse_time(payload.endTime)
    if payload.employeeId is not None:
        await validate_assignment(_pool, resto_id=restaurant_id, employee_id=payload.employeeId,
            role_required=payload.roleRequired, shift_date=shift_date, start_time=start_time, end_time=end_time)
    row = await _pool.fetchrow(
        """INSERT INTO shifts (resto_id, employee_id, role_required, shift_date, start_time, end_time, status)
           VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
        restaurant_id, payload.employeeId, payload.roleRequired, shift_date, start_time, end_time,
        "Scheduled" if payload.employeeId is not None else "Open",
    )
    return await serialize_shift(_pool, row)


@app.get("/api/scheduling/availability")
async def get_availability(user: dict = Depends(require_user)):
    availability = await _pool.fetchval("SELECT weekly_availability FROM users WHERE id = $1", user["id"])
    if isinstance(availability, str):
        availability = json.loads(availability)
    return {"weeklyAvailability": availability or {}}


@app.patch("/api/scheduling/availability")
async def update_availability(payload: AvailabilityRequest, user: dict = Depends(require_user)):
    valid_days = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
    if any(day.lower() not in valid_days for day in payload.weeklyAvailability):
        raise HTTPException(422, detail="Availability must be grouped by day of the week")
    for day, windows in payload.weeklyAvailability.items():
        if not isinstance(windows, list):
            raise HTTPException(422, detail=f"Availability for {day} must be a list of time windows")
        for window in windows:
            try:
                if parse_time(window["start"]) >= parse_time(window["end"]):
                    raise HTTPException(422, detail=f"Availability for {day} must end after it starts")
            except (KeyError, TypeError):
                raise HTTPException(422, detail=f"Availability for {day} needs start and end times")
    await _pool.execute("UPDATE users SET weekly_availability = $1::jsonb WHERE id = $2", json.dumps(payload.weeklyAvailability), user["id"])
    return {"weeklyAvailability": payload.weeklyAvailability}


@app.post("/api/scheduling/auto-build")
async def auto_build_schedule(payload: AutoBuildRequest, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can auto-build schedules")
    restaurant_id, monday = await _restaurant_id_for(user["id"]), week_start(parse_date(payload.weekStart))
    open_shifts = await _pool.fetch("SELECT * FROM shifts WHERE resto_id = $1 AND shift_date BETWEEN $2 AND $3 AND employee_id IS NULL AND status = 'Open' ORDER BY shift_date, start_time", restaurant_id, monday, monday + datetime.timedelta(days=6))
    assignments, unfilled = [], []
    for shift in open_shifts:
        candidates = await _pool.fetch("SELECT u.id FROM users u JOIN roles r ON r.id = u.role_id WHERE u.restaurant_id = $1 AND r.name = $2 ORDER BY u.id", restaurant_id, shift["role_required"])
        chosen = None
        for candidate in candidates:
            try:
                await validate_assignment(_pool, resto_id=restaurant_id, employee_id=candidate["id"], role_required=shift["role_required"], shift_date=shift["shift_date"], start_time=shift["start_time"], end_time=shift["end_time"])
                chosen = candidate["id"]
                break
            except HTTPException:
                continue
        if chosen:
            row = await _pool.fetchrow("UPDATE shifts SET employee_id = $1, status = 'Scheduled' WHERE id = $2 RETURNING id", chosen, shift["id"])
            assignments.append(await serialize_shift(_pool, row))
        else:
            unfilled.append(shift["id"])
    return {"assigned": assignments, "unfilledShiftIds": unfilled}


@app.patch("/api/scheduling/shifts/{shift_id}")
async def update_shift(shift_id: int, payload: ShiftUpdateRequest, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can edit shifts")
    restaurant_id = await _restaurant_id_for(user["id"])
    current = await _pool.fetchrow("SELECT * FROM shifts WHERE id = $1 AND resto_id = $2", shift_id, restaurant_id)
    if not current:
        raise HTTPException(404, detail="Shift not found")
    shift_date, start_time, end_time = parse_date(payload.date), parse_time(payload.startTime), parse_time(payload.endTime)
    if payload.employeeId is not None:
        await validate_assignment(_pool, resto_id=restaurant_id, employee_id=payload.employeeId, role_required=current["role_required"], shift_date=shift_date, start_time=start_time, end_time=end_time, exclude_shift_id=shift_id)
    row = await _pool.fetchrow("UPDATE shifts SET employee_id = $1, shift_date = $2, start_time = $3, end_time = $4, status = CASE WHEN $1 IS NULL THEN 'Open' ELSE 'Scheduled' END WHERE id = $5 RETURNING id", payload.employeeId, shift_date, start_time, end_time, shift_id)
    return await serialize_shift(_pool, row)


@app.post("/api/scheduling/drop-shift")
async def drop_shift(shiftId: int, user: dict = Depends(require_user)):
    restaurant_id = await _restaurant_id_for(user["id"])
    shift = await _pool.fetchrow("SELECT * FROM shifts WHERE id = $1 AND resto_id = $2 AND employee_id = $3", shiftId, restaurant_id, user["id"])
    if not shift:
        raise HTTPException(404, detail="Assigned shift not found")
    if shift["status"] == "Pending_Swap":
        raise HTTPException(409, detail="This shift is already in the swap queue")
    candidates = await _pool.fetch("SELECT u.id FROM users u JOIN roles r ON r.id = u.role_id WHERE u.restaurant_id = $1 AND r.name = $2 AND u.id <> $3", restaurant_id, shift["role_required"], user["id"])
    eligible = []
    for candidate in candidates:
        try:
            await validate_assignment(_pool, resto_id=restaurant_id, employee_id=candidate["id"], role_required=shift["role_required"], shift_date=shift["shift_date"], start_time=shift["start_time"], end_time=shift["end_time"], exclude_shift_id=shiftId)
        except HTTPException:
            continue
        eligible.append(candidate["id"])
    if not eligible:
        return {"shiftId": shiftId, "status": "Scheduled", "matches": []}
    matches = []
    async with _pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute("UPDATE shifts SET status = 'Pending_Swap' WHERE id = $1", shiftId)
            for employee_id in eligible:
                request = await connection.fetchrow("INSERT INTO swap_requests (resto_id, original_shift_id, requesting_employee_id, target_employee_id, status) VALUES ($1, $2, $3, $4, 'Pending_Match') ON CONFLICT (original_shift_id, target_employee_id) DO UPDATE SET status = 'Pending_Match' RETURNING id", restaurant_id, shiftId, user["id"], employee_id)
                matches.append({"swapRequestId": request["id"], "employeeId": employee_id})
    return {"shiftId": shiftId, "status": "Pending_Swap", "matches": matches}


@app.get("/api/scheduling/eligible-shifts")
async def eligible_shifts(user: dict = Depends(require_user)):
    rows = await _pool.fetch("SELECT sr.id AS swap_request_id, s.id FROM swap_requests sr JOIN shifts s ON s.id = sr.original_shift_id WHERE sr.target_employee_id = $1 AND sr.status = 'Pending_Match' AND s.status = 'Pending_Swap' ORDER BY s.shift_date, s.start_time", user["id"])
    result = []
    for row in rows:
        shift = await serialize_shift(_pool, row)
        shift["swapRequestId"] = row["swap_request_id"]
        result.append(shift)
    return result


@app.post("/api/scheduling/swap-requests/{request_id}/claim")
async def claim_swap(request_id: int, user: dict = Depends(require_user)):
    """A qualified employee explicitly claims a marketplace shift for manager approval."""
    async with _pool.acquire() as connection:
        async with connection.transaction():
            request = await connection.fetchrow(
                "SELECT * FROM swap_requests WHERE id = $1 AND target_employee_id = $2 FOR UPDATE",
                request_id, user["id"],
            )
            if not request or request["status"] != "Pending_Match":
                raise HTTPException(404, detail="Eligible shift is no longer available")
            shift = await connection.fetchrow(
                "SELECT * FROM shifts WHERE id = $1 AND status = 'Pending_Swap' FOR UPDATE",
                request["original_shift_id"],
            )
            if not shift:
                raise HTTPException(409, detail="This shift is no longer available")
            # Recheck at claim time: availability or weekly hours may have changed.
            await validate_assignment(connection, resto_id=request["resto_id"], employee_id=user["id"], role_required=shift["role_required"], shift_date=shift["shift_date"], start_time=shift["start_time"], end_time=shift["end_time"], exclude_shift_id=shift["id"])
            await connection.execute("UPDATE swap_requests SET status = CASE WHEN id = $1 THEN 'Pending_Approval' ELSE 'Rejected' END WHERE original_shift_id = $2 AND status = 'Pending_Match'", request_id, shift["id"])
    return {"id": request_id, "status": "Pending_Approval"}


@app.get("/api/scheduling/swap-requests")
async def swap_requests(user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can view the approval queue")
    restaurant_id = await _restaurant_id_for(user["id"])
    rows = await _pool.fetch("SELECT sr.id, sr.status, s.id AS shift_id, s.shift_date, s.start_time, s.end_time, requester.name AS requesting_name, target.name AS target_name FROM swap_requests sr JOIN shifts s ON s.id = sr.original_shift_id JOIN users requester ON requester.id = sr.requesting_employee_id LEFT JOIN users target ON target.id = sr.target_employee_id WHERE sr.resto_id = $1 AND sr.status = 'Pending_Approval' ORDER BY sr.created_at DESC", restaurant_id)
    return [{"id": r["id"], "status": r["status"], "shiftId": r["shift_id"], "date": r["shift_date"].isoformat(), "startTime": r["start_time"].isoformat(timespec="minutes"), "endTime": r["end_time"].isoformat(timespec="minutes"), "requestingEmployeeName": r["requesting_name"], "targetEmployeeName": r["target_name"]} for r in rows]


@app.post("/api/scheduling/swap-requests/{request_id}/decision")
async def decide_swap(request_id: int, payload: SwapDecisionRequest, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can decide swaps")
    restaurant_id = await _restaurant_id_for(user["id"])
    request = await _pool.fetchrow("SELECT * FROM swap_requests WHERE id = $1 AND resto_id = $2", request_id, restaurant_id)
    if not request or request["status"] != "Pending_Approval":
        raise HTTPException(404, detail="Active swap request not found")
    if not payload.approve:
        async with _pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute("UPDATE swap_requests SET status = 'Rejected' WHERE id = $1", request_id)
                await release_shift_if_no_live_swaps(connection, request["original_shift_id"])
        return {"id": request_id, "status": "Rejected"}
    shift = await _pool.fetchrow("SELECT * FROM shifts WHERE id = $1", request["original_shift_id"])
    await validate_assignment(_pool, resto_id=restaurant_id, employee_id=request["target_employee_id"], role_required=shift["role_required"], shift_date=shift["shift_date"], start_time=shift["start_time"], end_time=shift["end_time"], exclude_shift_id=shift["id"])
    async with _pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute("UPDATE shifts SET employee_id = $1, status = 'Scheduled' WHERE id = $2", request["target_employee_id"], shift["id"])
            await connection.execute("UPDATE swap_requests SET status = CASE WHEN id = $1 THEN 'Completed' ELSE 'Rejected' END WHERE original_shift_id = $2 AND status = 'Pending_Approval'", request_id, shift["id"])
    return {"id": request_id, "status": "Completed"}


@app.get("/api/waitlist")
async def get_waitlist(_=Depends(require_auth)):
    if not _pool:
        return []
    try:
        rows = await _pool.fetch(
            f"SELECT * FROM {cfg.DB_TABLE} ORDER BY created_at DESC"
        )
        return [_serialize_row(r) for r in rows]
    except Exception as e:
        print(f"[db] Query error: {e}")
        raise HTTPException(status_code=500, detail="Database error")
