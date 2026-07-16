"""DB pool lifecycle and idempotent schema bootstrap.

`pool` is a module-level singleton, set once in `app.main`'s lifespan and read
via `db.pool` (module attribute access, not `from ... import pool`) everywhere
else — that indirection matters, since importing the name directly would bind
to whatever `pool` was at import time (i.e. permanently `None`).
"""
import os
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import asyncpg

from app.core.config import DB_FIELDS, DB_TABLE

pool: Optional[asyncpg.Pool] = None


async def create_pool() -> Optional[asyncpg.Pool]:
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


async def init_db(active_pool: asyncpg.Pool) -> None:
    col_defs = []
    for f in DB_FIELDS:
        not_null = "NOT NULL" if f["required"] else ""
        col_defs.append(f"  {f['name']} text {not_null}".strip())

    await active_pool.execute(f"""
        CREATE TABLE IF NOT EXISTS {DB_TABLE} (
            id         SERIAL PRIMARY KEY,
            {",\n            ".join(col_defs)},
            created_at timestamptz DEFAULT now()
        )
    """)
    print(f"[db] Table '{DB_TABLE}' ready")

    # crewlee-be/db/schema.sql, three levels up from this file (app/db/session.py).
    schema_path = Path(__file__).resolve().parents[2] / "db" / "schema.sql"
    await active_pool.execute(schema_path.read_text())
    print("[db] Core schema (restaurants, roles, users) ready")
