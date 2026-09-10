CREATE TABLE IF NOT EXISTS restaurants (
    id         SERIAL PRIMARY KEY,
    name       text NOT NULL,
    slug       text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS roles (
    id   SERIAL PRIMARY KEY,
    name text NOT NULL UNIQUE
);

INSERT INTO roles (name) VALUES ('manager'), ('foh'), ('boh')
ON CONFLICT (name) DO NOTHING;

-- Departments sit between the restaurant and its roles/employees (Location -> Department ->
-- Role -> Employee). Each department binds to one of the existing foh/boh role categories
-- rather than introducing a separate custom-role taxonomy, so validate_assignment's role-match
-- logic keeps working unchanged; a restaurant can still have multiple departments per category
-- (e.g. "Kitchen" + "Prep", both boh) if it wants a finer split.
CREATE TABLE IF NOT EXISTS departments (
    id            SERIAL PRIMARY KEY,
    resto_id      integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    name          text NOT NULL,
    role_category text NOT NULL CHECK (role_category IN ('foh', 'boh')),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS departments_resto_idx ON departments (resto_id);

CREATE TABLE IF NOT EXISTS users (
    id                       SERIAL PRIMARY KEY,
    restaurant_id            integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    role_id                  integer NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
    department_id            integer REFERENCES departments(id) ON DELETE SET NULL,
    name                     text NOT NULL,
    -- Globally unique, not scoped per-restaurant -- one email is one person, one restaurant.
    -- Login (auth.py) has no restaurant filter, so if the same email+password ever existed at
    -- two restaurants it would silently log in to whichever row the query happened to return
    -- first; a global constraint makes that impossible instead of just unhandled. Always
    -- stored lowercase (see EmployeeCreateRequest/RestaurantCreateRequest/UserLoginRequest
    -- validators in app/models/schemas.py) -- auth.py's queries match on LOWER(email) too, so
    -- this stays correct even against any row that predates that normalization.
    email                    text NOT NULL UNIQUE,
    -- NULL = invited but hasn't chosen a password yet (POST /api/auth/set-password fills this
    -- in on first login). No email-invite link exists -- the person "signs up" themselves.
    password_hash            text,
    -- Soft-delete flag for "remove employee": false blocks login (require_user) and new shift
    -- assignment (validate_assignment), but leaves past shifts/announcements attributed intact.
    active                   boolean NOT NULL DEFAULT true,
    -- weekly_availability is JSON so it can represent multiple availability windows in a day
    -- without a separate employee-availability table.
    weekly_availability      jsonb NOT NULL DEFAULT '[]'::jsonb,
    max_hours_per_week       numeric(5,2) NOT NULL DEFAULT 40,
    -- Smart-fill profile fields: how auto_build_schedule ranks and filters candidates beyond the
    -- hard rules in validate_assignment (role/availability/overtime/rest). All candidate-
    -- selection concerns for the auto paths only -- manual assignment ignores them, so a manager
    -- overriding the algorithm by hand always still works.
    min_hours_per_week       numeric(5,2) NOT NULL DEFAULT 0,
    preferred_hours_per_week numeric(5,2),
    scheduling_confidence    smallint NOT NULL DEFAULT 3 CHECK (scheduling_confidence BETWEEN 1 AND 5),
    scheduling_notes         text NOT NULL DEFAULT '',
    auto_schedule_opt_out    boolean NOT NULL DEFAULT false,
    created_at               timestamptz DEFAULT now()
);

-- Layer 1 of the scheduling workflow: staffing needs per day-of-week/time-block/department,
-- decoupled from any actual shift or employee (layers 2 and 3). A row with week_start_override
-- NULL is the recurring weekly default; a row with it set to a Monday applies only to that ISO
-- week, replacing the default for that (day_of_week, department_id) pair -- this is how a
-- manager overrides a single week (e.g. a holiday) without editing the template every other week
-- reuses.
CREATE TABLE IF NOT EXISTS coverage_requirements (
    id                  SERIAL PRIMARY KEY,
    resto_id            integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    department_id       integer NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    day_of_week         smallint NOT NULL CHECK (day_of_week BETWEEN 0 AND 6), -- Monday=0..Sunday=6, matches date.weekday()/week_start()
    week_start_override date,
    start_time          time NOT NULL,
    end_time            time NOT NULL,
    count_required      smallint NOT NULL CHECK (count_required > 0),
    min_confidence      smallint CHECK (min_confidence BETWEEN 1 AND 5),
    notes               text NOT NULL DEFAULT '',
    created_at          timestamptz NOT NULL DEFAULT now(),
    CHECK (start_time <> end_time)
);

CREATE INDEX IF NOT EXISTS coverage_requirements_resto_day_idx ON coverage_requirements (resto_id, day_of_week);
CREATE INDEX IF NOT EXISTS coverage_requirements_override_idx ON coverage_requirements (resto_id, week_start_override) WHERE week_start_override IS NOT NULL;

CREATE TABLE IF NOT EXISTS shifts (
    id             SERIAL PRIMARY KEY,
    resto_id       integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    employee_id    integer REFERENCES users(id) ON DELETE SET NULL, -- NULL = open/unassigned shift, not a separate boolean
    department_id  integer REFERENCES departments(id) ON DELETE SET NULL,
    -- Traces a generated shift back to the requirement block that produced it -- what makes
    -- "Generate Shifts" idempotent (re-running it tops up the gap rather than duplicating).
    requirement_id integer REFERENCES coverage_requirements(id) ON DELETE SET NULL,
    role_required  text NOT NULL CHECK (role_required IN ('foh', 'boh')),
    shift_date     date NOT NULL,
    start_time     time NOT NULL,
    end_time       time NOT NULL,
    -- Lifecycle: Open -> Scheduled <-> Pending_Swap. A shift only enters Pending_Swap when at
    -- least one coworker has actually passed validation for it (see drop_shift), so
    -- Pending_Swap always means "something real is pending."
    status         text NOT NULL DEFAULT 'Open' CHECK (status IN ('Scheduled', 'Open', 'Pending_Swap')),
    -- Draft/publish is orthogonal to the status lifecycle above: a manager can build or edit a
    -- shift in draft (invisible to employees) and publish it later.
    is_draft       boolean NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CHECK (start_time <> end_time)
);

CREATE INDEX IF NOT EXISTS shifts_resto_week_idx ON shifts (resto_id, shift_date);
CREATE INDEX IF NOT EXISTS shifts_employee_week_idx ON shifts (employee_id, shift_date);
CREATE INDEX IF NOT EXISTS shifts_requirement_idx ON shifts (requirement_id);

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
);

CREATE INDEX IF NOT EXISTS swap_requests_target_status_idx
ON swap_requests (target_employee_id, status);

-- A template snapshots a week's shifts as a single jsonb blob (dayOffset/departmentId/
-- employeeId/startTime/endTime per entry) rather than a join table — templates are read-mostly
-- and small, so there's no benefit to normalizing them into their own shift-like rows.
CREATE TABLE IF NOT EXISTS schedule_templates (
    id         SERIAL PRIMARY KEY,
    resto_id   integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    name       text NOT NULL,
    shifts     jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS schedule_templates_resto_idx ON schedule_templates (resto_id);

CREATE TABLE IF NOT EXISTS announcements (
    id         SERIAL PRIMARY KEY,
    resto_id   integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    author_id  integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      text NOT NULL,
    body       text NOT NULL,
    pinned     boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS announcements_resto_idx ON announcements (resto_id, pinned DESC, created_at DESC);

-- One row per employee who has explicitly confirmed reading an announcement -- the read
-- receipt. UNIQUE means a second "mark as read" call is a harmless no-op (ON CONFLICT DO
-- NOTHING), so read_at always reflects the first confirmation, not the latest click.
CREATE TABLE IF NOT EXISTS announcement_reads (
    id              SERIAL PRIMARY KEY,
    announcement_id integer NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
    employee_id     integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    read_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (announcement_id, employee_id)
);

CREATE INDEX IF NOT EXISTS announcement_reads_announcement_idx ON announcement_reads (announcement_id);

CREATE EXTENSION IF NOT EXISTS vector;

-- RAG knowledge base: managers upload documents (recipes, SOPs, training docs, licenses)
-- and employees query them conversationally. `visibility` is a forward-looking tier
-- (owner/manager/employee) for a possible future document-level access model -- it is not
-- enforced anywhere yet, every restaurant member can currently read every document
-- regardless of this value. No version history: PUT replaces a document's content and
-- chunks in place rather than keeping prior revisions. `content` is text extracted at
-- upload time from the original PDF/DOCX/pasted-text-as-.txt (app/services/rag.py's
-- extract_text) -- the original file itself lives in GCS at `gcs_path`, not in Postgres.
-- is_guest_visible is a hard, enforced security boundary (unlike `visibility` above): the
-- public Guest AI endpoint's retrieval query filters on it directly, so a document only ever
-- reaches an unauthenticated guest if a manager has explicitly flagged it. Defaults to false
-- so every existing/new document is internal-only until a manager opts it in. Orthogonal to
-- `visibility` (which is about internal role tiers, unenforced) rather than layered onto it,
-- since "guest" isn't another rung on the owner/manager/employee ladder.
CREATE TABLE IF NOT EXISTS rag_documents (
    id                SERIAL PRIMARY KEY,
    resto_id          integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    uploaded_by       integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title             text NOT NULL,
    doc_type          text NOT NULL DEFAULT 'other' CHECK (doc_type IN ('recipe', 'sop', 'training', 'license', 'other')),
    visibility        text NOT NULL DEFAULT 'employee' CHECK (visibility IN ('owner', 'manager', 'employee')),
    is_guest_visible  boolean NOT NULL DEFAULT false,
    content           text NOT NULL,
    original_filename text NOT NULL,
    file_type         text NOT NULL CHECK (file_type IN ('pdf', 'docx', 'txt')),
    -- Nullable because it's filled in right after the GCS upload (which needs to succeed
    -- first -- see _prepare_chunks in app/api/routes/rag.py) rather than in this same insert.
    gcs_path          text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- schema.sql only ever CREATEs (see file header / CLAUDE.md) so this column would silently
-- never reach a database that already has rag_documents (any deployed environment, including
-- Baton Rouge's). ADD COLUMN IF NOT EXISTS is idempotent like everything else here, so it's
-- safe to run on every boot alongside the CREATE TABLE above -- this is the one exception to
-- "only CREATE", made deliberately because the alternative (Alembic) isn't wired into any
-- deploy path yet. See alembic/versions/*_guest_ai.py, which carries the same statement for
-- when that changes, and crewlee-be/CLAUDE.md's Known limitations for the full explanation.
ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS is_guest_visible boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS rag_documents_resto_idx ON rag_documents (resto_id);
CREATE INDEX IF NOT EXISTS rag_documents_guest_visible_idx ON rag_documents (resto_id, is_guest_visible) WHERE is_guest_visible;

-- One row per chunk of a document's content, embedded independently for retrieval.
-- resto_id is denormalized from rag_documents so a query can filter/index on it directly
-- without a join. embedding is sized for voyage-3-lite (512 dims) -- change RAG_EMBEDDING_DIM
-- in app/core/config.py and this column together if the embedding model ever changes.
CREATE TABLE IF NOT EXISTS rag_chunks (
    id          SERIAL PRIMARY KEY,
    document_id integer NOT NULL REFERENCES rag_documents(id) ON DELETE CASCADE,
    resto_id    integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    chunk_index smallint NOT NULL,
    content     text NOT NULL,
    embedding   vector(512) NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS rag_chunks_document_idx ON rag_chunks (document_id);
CREATE INDEX IF NOT EXISTS rag_chunks_resto_idx ON rag_chunks (resto_id);
CREATE INDEX IF NOT EXISTS rag_chunks_embedding_idx ON rag_chunks USING hnsw (embedding vector_cosine_ops);

-- One row per restaurant, created on first access to the Guest AI settings endpoint (see
-- app/api/routes/guest_ai.py's _get_or_create_settings) rather than at restaurant-creation
-- time, so a restaurant that never touches Guest AI never gets a row. `enabled` gates the
-- public endpoint entirely -- a disabled restaurant's guest chat 404s rather than answering
-- from an empty/stale knowledge set.
CREATE TABLE IF NOT EXISTS guest_ai_settings (
    resto_id   integer PRIMARY KEY REFERENCES restaurants(id) ON DELETE CASCADE,
    enabled    boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Lightweight usage log for the manager-facing "Questions today/this week" metrics --
-- deliberately holds no guest identity (no IP, no session id, no cookie): a guest is never
-- identifiable across questions or across visits. `answered` is false when retrieval found
-- nothing guest-visible to ground an answer in (see rag.answer_guest_question's
-- has_enough_info), which is what powers the "unanswered questions" metric.
CREATE TABLE IF NOT EXISTS guest_ai_queries (
    id         SERIAL PRIMARY KEY,
    resto_id   integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    question   text NOT NULL,
    answered   boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS guest_ai_queries_resto_created_idx ON guest_ai_queries (resto_id, created_at DESC);
