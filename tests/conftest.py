"""Test setup.

Every test runs against a real Postgres (the same one docker-compose/local dev uses -- this
app has no ORM to fake against, and the guest/tenant-isolation tests below are only meaningful
against real SQL). Each test gets its own transaction on a single connection that's rolled
back at the end (`db_conn` fixture), so tests never persist anything and never need a separate
test database -- `db.pool` is monkeypatched to a tiny shim (`_TxPool`) that forwards
fetch/fetchval/fetchrow/execute to that one connection, matching the subset of asyncpg.Pool's
interface the app actually calls.

Requires `DATABASE_URL` pointing at a reachable Postgres with the pgvector extension
available (e.g. `docker compose up db`, or `TEST_DATABASE_URL` to point elsewhere) --
see crewlee-be/CLAUDE.md's Testing section.
"""
import os

os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", "postgres://postgres:postgres@localhost:5432/crewlee"))
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("VOYAGE_API_KEY", "test-voyage-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("RAG_BUCKET_NAME", "test-bucket-unused")

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.security import make_token
from app.db import session as db
from app.main import app


class _TxPool:
    """See module docstring -- forwards the app's `db.pool` calls to one open transaction."""

    def __init__(self, conn: asyncpg.Connection):
        self._conn = conn

    async def fetch(self, query, *args):
        return await self._conn.fetch(query, *args)

    async def fetchrow(self, query, *args):
        return await self._conn.fetchrow(query, *args)

    async def fetchval(self, query, *args):
        return await self._conn.fetchval(query, *args)

    async def execute(self, query, *args):
        return await self._conn.execute(query, *args)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _schema_ready():
    """Runs the idempotent schema bootstrap once per test session, committed for real (not
    inside the per-test rollback) -- so table/index DDL and the roles seed only need to exist
    once, the same way a real deployment's first boot does it.
    """
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        await db.init_db(conn)
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The rate limiter's hit-counts are process-global module state (see
    app/core/rate_limit.py), not per-test -- clear it before every test so the guest-AI
    endpoint tests never depend on execution order or how many other tests already hit the
    same slug.
    """
    from app.core import rate_limit
    rate_limit._hits.clear()
    yield


@pytest_asyncio.fixture
async def db_conn():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    tx = conn.transaction()
    await tx.start()
    previous_pool = db.pool
    db.pool = _TxPool(conn)
    try:
        yield conn
    finally:
        db.pool = previous_pool
        await tx.rollback()
        await conn.close()


@pytest_asyncio.fixture
async def client(db_conn):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def auth_headers(user_id: int) -> dict:
    return {"Authorization": f"Bearer {make_token(user_id)}"}


async def make_restaurant(conn: asyncpg.Connection, slug: str, name: str = None) -> int:
    return await conn.fetchval(
        "INSERT INTO restaurants (name, slug) VALUES ($1, $2) RETURNING id",
        name or slug.replace("-", " ").title(), slug,
    )


async def make_user(conn: asyncpg.Connection, restaurant_id: int, role: str, email: str) -> int:
    role_id = await conn.fetchval("SELECT id FROM roles WHERE name = $1", role)
    return await conn.fetchval(
        """INSERT INTO users (restaurant_id, role_id, name, email, active)
           VALUES ($1, $2, $3, $4, true) RETURNING id""",
        restaurant_id, role_id, email.split("@")[0], email,
    )


def fake_embedding(seed: str) -> list[float]:
    # Deterministic, not semantically meaningful -- these tests check access control
    # (which chunks are even retrievable), not ranking quality.
    h = abs(hash(seed))
    return [((h >> (i % 32)) % 1000) / 1000.0 for i in range(512)]


async def make_document(
    conn: asyncpg.Connection, restaurant_id: int, uploaded_by: int, title: str, content: str,
    doc_type: str = "other", is_guest_visible: bool = False,
) -> int:
    doc_id = await conn.fetchval(
        """INSERT INTO rag_documents
               (resto_id, uploaded_by, title, doc_type, is_guest_visible, content, original_filename, file_type)
           VALUES ($1, $2, $3, $4, $5, $6, $7, 'txt') RETURNING id""",
        restaurant_id, uploaded_by, title, doc_type, is_guest_visible, content, f"{title}.txt",
    )
    from app.services.rag import to_vector_literal
    await conn.execute(
        """INSERT INTO rag_chunks (document_id, resto_id, chunk_index, content, embedding)
           VALUES ($1, $2, 0, $3, $4::vector)""",
        doc_id, restaurant_id, content, to_vector_literal(fake_embedding(content)),
    )
    return doc_id
