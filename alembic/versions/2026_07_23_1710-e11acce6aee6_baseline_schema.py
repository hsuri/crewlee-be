"""baseline schema

Frozen snapshot of db/schema.sql as of this migration's creation date. This is the
whole current schema (restaurants through rag_chunks) expressed as CREATE statements
only -- no ALTERs anywhere, on purpose (see crewlee-be/CLAUDE.md): the app has no real
customer data yet, so every schema change up to this point was folded directly into
its CREATE TABLE rather than layered on with ALTER. Alembic takes over from here --
new schema changes should be new migrations (ALTER TABLE etc.), not edits to this file
or to db/schema.sql's CREATE statements.

db/schema.sql itself is left in place and keeps running from app/main.py's lifespan on
every boot (CREATE ... IF NOT EXISTS, so it's a no-op once Alembic has already created
these tables) -- a deliberate, lower-risk choice for now rather than cutting local dev
/ Docker / Cloud Run over to `alembic upgrade head` in the same pass that introduces
Alembic. If schema.sql and this baseline (plus future migrations) ever drift apart,
Alembic's history is the one to trust.

Revision ID: e11acce6aee6
Revises:
Create Date: 2026-07-23 17:10:15.388040

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e11acce6aee6'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS restaurants (
            id         SERIAL PRIMARY KEY,
            name       text NOT NULL,
            slug       text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
            created_at timestamptz DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS roles (
            id   SERIAL PRIMARY KEY,
            name text NOT NULL UNIQUE
        )
    """)

    op.execute("""
        INSERT INTO roles (name) VALUES ('manager'), ('foh'), ('boh')
        ON CONFLICT (name) DO NOTHING
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS departments (
            id            SERIAL PRIMARY KEY,
            resto_id      integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
            name          text NOT NULL,
            role_category text NOT NULL CHECK (role_category IN ('foh', 'boh')),
            created_at    timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS departments_resto_idx ON departments (resto_id)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id                       SERIAL PRIMARY KEY,
            restaurant_id            integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
            role_id                  integer NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
            department_id            integer REFERENCES departments(id) ON DELETE SET NULL,
            name                     text NOT NULL,
            email                    text NOT NULL UNIQUE,
            password_hash            text,
            active                   boolean NOT NULL DEFAULT true,
            weekly_availability      jsonb NOT NULL DEFAULT '[]'::jsonb,
            max_hours_per_week       numeric(5,2) NOT NULL DEFAULT 40,
            min_hours_per_week       numeric(5,2) NOT NULL DEFAULT 0,
            preferred_hours_per_week numeric(5,2),
            scheduling_confidence    smallint NOT NULL DEFAULT 3 CHECK (scheduling_confidence BETWEEN 1 AND 5),
            scheduling_notes         text NOT NULL DEFAULT '',
            auto_schedule_opt_out    boolean NOT NULL DEFAULT false,
            created_at               timestamptz DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS coverage_requirements (
            id                  SERIAL PRIMARY KEY,
            resto_id            integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
            department_id       integer NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
            day_of_week         smallint NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
            week_start_override date,
            start_time          time NOT NULL,
            end_time            time NOT NULL,
            count_required      smallint NOT NULL CHECK (count_required > 0),
            min_confidence      smallint CHECK (min_confidence BETWEEN 1 AND 5),
            notes               text NOT NULL DEFAULT '',
            created_at          timestamptz NOT NULL DEFAULT now(),
            CHECK (start_time <> end_time)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS coverage_requirements_resto_day_idx ON coverage_requirements (resto_id, day_of_week)")
    op.execute("CREATE INDEX IF NOT EXISTS coverage_requirements_override_idx ON coverage_requirements (resto_id, week_start_override) WHERE week_start_override IS NOT NULL")

    op.execute("""
        CREATE TABLE IF NOT EXISTS shifts (
            id             SERIAL PRIMARY KEY,
            resto_id       integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
            employee_id    integer REFERENCES users(id) ON DELETE SET NULL,
            department_id  integer REFERENCES departments(id) ON DELETE SET NULL,
            requirement_id integer REFERENCES coverage_requirements(id) ON DELETE SET NULL,
            role_required  text NOT NULL CHECK (role_required IN ('foh', 'boh')),
            shift_date     date NOT NULL,
            start_time     time NOT NULL,
            end_time       time NOT NULL,
            status         text NOT NULL DEFAULT 'Open' CHECK (status IN ('Scheduled', 'Open', 'Pending_Swap')),
            is_draft       boolean NOT NULL DEFAULT false,
            created_at     timestamptz NOT NULL DEFAULT now(),
            CHECK (start_time <> end_time)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS shifts_resto_week_idx ON shifts (resto_id, shift_date)")
    op.execute("CREATE INDEX IF NOT EXISTS shifts_employee_week_idx ON shifts (employee_id, shift_date)")
    op.execute("CREATE INDEX IF NOT EXISTS shifts_requirement_idx ON shifts (requirement_id)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS swap_requests (
            id                     SERIAL PRIMARY KEY,
            resto_id               integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
            original_shift_id      integer NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
            requesting_employee_id integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            target_employee_id     integer REFERENCES users(id) ON DELETE CASCADE,
            status                 text NOT NULL DEFAULT 'Pending_Match'
                                   CHECK (status IN ('Pending_Match', 'Pending_Approval', 'Completed', 'Rejected')),
            created_at             timestamptz NOT NULL DEFAULT now(),
            UNIQUE (original_shift_id, target_employee_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS swap_requests_target_status_idx ON swap_requests (target_employee_id, status)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS schedule_templates (
            id         SERIAL PRIMARY KEY,
            resto_id   integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
            name       text NOT NULL,
            shifts     jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS schedule_templates_resto_idx ON schedule_templates (resto_id)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS announcements (
            id         SERIAL PRIMARY KEY,
            resto_id   integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
            author_id  integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title      text NOT NULL,
            body       text NOT NULL,
            pinned     boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS announcements_resto_idx ON announcements (resto_id, pinned DESC, created_at DESC)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS announcement_reads (
            id              SERIAL PRIMARY KEY,
            announcement_id integer NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
            employee_id     integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            read_at         timestamptz NOT NULL DEFAULT now(),
            UNIQUE (announcement_id, employee_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS announcement_reads_announcement_idx ON announcement_reads (announcement_id)")

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute("""
        CREATE TABLE IF NOT EXISTS rag_documents (
            id                SERIAL PRIMARY KEY,
            resto_id          integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
            uploaded_by       integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title             text NOT NULL,
            doc_type          text NOT NULL DEFAULT 'other' CHECK (doc_type IN ('recipe', 'sop', 'training', 'license', 'other')),
            visibility        text NOT NULL DEFAULT 'employee' CHECK (visibility IN ('owner', 'manager', 'employee')),
            content           text NOT NULL,
            original_filename text NOT NULL,
            file_type         text NOT NULL CHECK (file_type IN ('pdf', 'docx', 'txt')),
            gcs_path          text,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS rag_documents_resto_idx ON rag_documents (resto_id)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS rag_chunks (
            id          SERIAL PRIMARY KEY,
            document_id integer NOT NULL REFERENCES rag_documents(id) ON DELETE CASCADE,
            resto_id    integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
            chunk_index smallint NOT NULL,
            content     text NOT NULL,
            embedding   vector(512) NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS rag_chunks_document_idx ON rag_chunks (document_id)")
    op.execute("CREATE INDEX IF NOT EXISTS rag_chunks_resto_idx ON rag_chunks (resto_id)")
    op.execute("CREATE INDEX IF NOT EXISTS rag_chunks_embedding_idx ON rag_chunks USING hnsw (embedding vector_cosine_ops)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS rag_chunks CASCADE")
    op.execute("DROP TABLE IF EXISTS rag_documents CASCADE")
    op.execute("DROP TABLE IF EXISTS announcement_reads CASCADE")
    op.execute("DROP TABLE IF EXISTS announcements CASCADE")
    op.execute("DROP TABLE IF EXISTS schedule_templates CASCADE")
    op.execute("DROP TABLE IF EXISTS swap_requests CASCADE")
    op.execute("DROP TABLE IF EXISTS shifts CASCADE")
    op.execute("DROP TABLE IF EXISTS coverage_requirements CASCADE")
    op.execute("DROP TABLE IF EXISTS users CASCADE")
    op.execute("DROP TABLE IF EXISTS departments CASCADE")
    op.execute("DROP TABLE IF EXISTS roles CASCADE")
    op.execute("DROP TABLE IF EXISTS restaurants CASCADE")
