import os
import datetime
from contextlib import asynccontextmanager
from urllib.parse import urlparse, parse_qs
from typing import Optional

import asyncpg
from fastapi import FastAPI, HTTPException, Header, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    try:
        _pool = await _create_pool()
        if _pool:
            await _init_db(_pool)
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
    allow_methods=["GET", "POST"],
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


# ── Models ────────────────────────────────────────────────────────────────────


class WaitlistEntry(BaseModel):
    name: str
    email: str
    restaurant: str
    role: str


class LoginRequest(BaseModel):
    password: str


# ── Helpers ───────────────────────────────────────────────────────────────────


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
