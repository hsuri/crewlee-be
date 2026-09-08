"""guest ai

Adds the schema for the Guest AI feature: a hard, enforced `is_guest_visible` flag on
rag_documents (the security boundary the public guest endpoint filters on), a one-row-per-
restaurant `guest_ai_settings` table, and a `guest_ai_queries` usage log for the manager
analytics view.

This is a real ALTER (not folded into a CREATE), so it only does anything useful once
`alembic upgrade head` is actually run against a database. As of this migration, nothing in
this repo's deploy/dev scripts does that yet -- `db/schema.sql` (executed on every boot by
app/main.py's lifespan) is still what's live everywhere, and it carries the identical
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS` statements below so
the feature works today without this migration ever being run. Both are idempotent, so running
this migration too (now or later, once alembic upgrade head is wired into deploy) is harmless.
See crewlee-be/CLAUDE.md's Known limitations for the full explanation of this gap.

Revision ID: a2ea4e521b6d
Revises: e11acce6aee6
Create Date: 2026-09-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a2ea4e521b6d'
down_revision: Union[str, Sequence[str], None] = 'e11acce6aee6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS is_guest_visible boolean NOT NULL DEFAULT false")
    op.execute(
        "CREATE INDEX IF NOT EXISTS rag_documents_guest_visible_idx "
        "ON rag_documents (resto_id, is_guest_visible) WHERE is_guest_visible"
    )
    op.execute("""
        CREATE TABLE IF NOT EXISTS guest_ai_settings (
            resto_id   integer PRIMARY KEY REFERENCES restaurants(id) ON DELETE CASCADE,
            enabled    boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS guest_ai_queries (
            id         SERIAL PRIMARY KEY,
            resto_id   integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
            question   text NOT NULL,
            answered   boolean NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS guest_ai_queries_resto_created_idx "
        "ON guest_ai_queries (resto_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS guest_ai_queries")
    op.execute("DROP TABLE IF EXISTS guest_ai_settings")
    op.execute("DROP INDEX IF EXISTS rag_documents_guest_visible_idx")
    op.execute("ALTER TABLE rag_documents DROP COLUMN IF EXISTS is_guest_visible")
