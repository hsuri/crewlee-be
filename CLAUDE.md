# crewlee-be

Backend for Crewlee, a restaurant operations platform. FastAPI + asyncpg + Postgres. This service owns all business logic and the database; the sibling `crewlee-fe` repo is a thin static server that only proxies `/api/*` here.

## Architecture

Standard FastAPI package layout under `app/`:

- **`app/main.py`** — app factory: DB pool lifecycle (`lifespan`), CORS, and `include_router` wiring for every router in `app/api/routes/`.
- **`app/api/routes/`** — one router module per resource: `health.py`, `public_config.py`, `waitlist.py` (waitlist signups + admin login/list), `auth.py` (staff login + `/api/me`), `scheduling.py` (everything under `/api/scheduling/*`, still the bulk of the route count — it's one cohesive domain, not a sign it needs re-splitting), `announcements.py` (`/api/announcements/*` — team announcements + read receipts, the first resource after scheduling to get its own file, per the one-module-per-resource rule).
- **`app/models/schemas.py`** — every Pydantic request model, used across routers.
- **`app/services/scheduling.py`** (formerly `scheduling_service.py`) — pure business-rule module for scheduling: date/time parsing, availability/overtime/rest-period validation, and shift serialization. It exists so that every code path that can change a shift's assignment (manual create, drag-and-drop edit, auto-build, swap claim, swap approval) runs through the *same* `validate_assignment` check. If you add a new way to assign a shift to an employee, route it through `validate_assignment` rather than re-deriving the rules — that's the whole reason this module is split out.
- **`app/core/config.py`** — project + GCP/deploy constants (`DB_FIELDS`, `PROJECT_NAME`, `GCP_PROJECT_ID`, etc). `scripts/setup.sh`/`scripts/deploy.sh` read these via `python3 -c "from app.core import config; ..."`, so keep it import-safe (no side effects at module scope).
- **`app/core/security.py`** — `require_auth`/`require_user` FastAPI dependencies, prototype bearer-token encode/decode, and bcrypt hash/verify helpers.
- **`app/db/session.py`** — pool creation (`create_pool`) and idempotent schema bootstrap (`init_db`). Holds the pool as a module-level attribute, `db.pool` — code reads it via `from app.db import session as db; db.pool`, never `from app.db.session import pool` (that would bind at import time, before `lifespan` sets it, and stay `None` forever).
- **`app/db/seed.py`** — demo restaurant/users seeding (`DEMO_USERS`, `seed_demo_data`), run from `lifespan` on an empty `users` table.

Entrypoint is `app.main:app` (see `Dockerfile` / `scripts/dev.sh`), run from the repo root so `db/schema.sql` (still a top-level directory, not under `app/`) resolves correctly relative to `app/db/session.py`.

**No migration framework yet.** `db/schema.sql` is executed verbatim on every process boot (`init_db`, called from the FastAPI `lifespan` context in `app/main.py`). It is plain `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` statements only — deliberately **not** `ALTER TABLE ... ADD COLUMN`, and deliberately no data-backfill `UPDATE`/`INSERT ... WHERE NOT EXISTS` logic either (that kind of default-provisioning now lives in `app/db/seed.py`, next to the restaurant/user creation it belongs with). When you add a column, add it directly to the `CREATE TABLE` — don't reach for `ALTER`. This keeps the file a clean target for an eventual Alembic migration (or an equivalent) to take over: schema definition only, no embedded business logic, no accumulated migration history to translate. The practical cost today is that this file only ever produces the *current* shape of a fresh database — it does not know how to evolve an existing one, so a schema change against a database that already has the old table shape requires dropping and recreating it (fine for local/demo Postgres; would need a real migration tool before this app has real customer data to preserve across a schema change).

If the DB is unreachable at startup, the app still boots (`lifespan` swallows the exception) — required for Cloud Run, which kills a revision that doesn't bind to `$PORT` in time. Routes that need `db.pool` will 503 individually instead.

## Data model

- **`restaurants`** — tenant root. Everything else scopes to `resto_id`/`restaurant_id`.
- **`roles`** — seeded once: `manager`, `foh`, `boh`.
- **`departments`** — sits between restaurant and employee (`role_category` is `foh`/`boh`, so `validate_assignment`'s role-match logic keeps working unchanged even though a restaurant can have multiple departments per category). `users.department_id` and `shifts.department_id` both reference it `ON DELETE SET NULL` (deleting a department just unassigns its employees/shifts); `coverage_requirements.department_id` is `ON DELETE CASCADE` instead, since a requirement block can't exist without a department — `DELETE /api/scheduling/departments/{id}` relies on both FK behaviors and does no manual cleanup itself.
- **`users`** — `restaurant_id`, `role_id`, `email`/`password_hash` (bcrypt), `department_id`, plus scheduling-profile columns added via idempotent `ALTER`: `weekly_availability` (jsonb, default `'[]'`), `max_hours_per_week` (numeric, default 40), and the Smart Fill candidate-ranking fields — `scheduling_confidence` (1-5, default 3), `min_hours_per_week`, `preferred_hours_per_week`, `scheduling_notes`, `auto_schedule_opt_out`. The confidence/opt-out fields are candidate-*selection* concerns for the auto paths only (`auto_build_schedule`, `generate_shifts_from_requirements`) — manual assignment ignores them entirely, so a manager overriding the algorithm by hand always still works.
- **`coverage_requirements`** — layer 1 of the scheduling workflow: staffing needs per day-of-week/time-block/department, independent of any actual shift or employee. `week_start_override IS NULL` rows are the recurring weekly default; a row with it set to a Monday applies only to that ISO week, replacing the default for that `(day_of_week, department_id)` pair. `min_confidence` (nullable) is an explicit, manager-set gate — not an inferred "busy day" heuristic — that Smart Fill enforces when filling shifts generated from that block. Resolved via `resolve_effective_requirements` (`app/services/scheduling.py`).
- **`shifts`** — `employee_id` nullable (`NULL` = open/unassigned shift, not a separate boolean). `status` lifecycle: `Open → Scheduled ↔ Pending_Swap`. A shift only enters `Pending_Swap` when at least one coworker has actually passed validation for it — `drop_shift` checks eligibility *before* flipping status, specifically so `Pending_Swap` always means "something real is pending" and never gets stuck with no way back to `Scheduled`. `requirement_id` (nullable) traces a generated shift back to the `coverage_requirements` block that produced it — what makes `POST /api/scheduling/requirements/generate-shifts` idempotent (re-running it tops up the gap rather than duplicating). `is_draft` gates manager-only visibility until `POST /api/scheduling/publish`.
- **`swap_requests`** — one row per (shift, candidate) pairing offered during a drop. Status lifecycle: `Pending_Match → Pending_Approval → Completed | Rejected`. `UNIQUE(original_shift_id, target_employee_id)` prevents duplicate offers to the same person; the `ON CONFLICT ... DO UPDATE` in `drop_shift` re-uses that constraint to re-open a stale row rather than erroring.
- **`schedule_templates`** — a week's shifts snapshotted as a single jsonb blob rather than normalized rows (read-mostly, small, no join benefit).
- **`announcements`** / **`announcement_reads`** — team announcements (`resto_id`, `author_id`, `title`, `body`, `pinned`) and one read-receipt row per employee who has explicitly confirmed reading one (`UNIQUE(announcement_id, employee_id)`, `ON CONFLICT DO NOTHING` on re-acknowledge so `read_at` always reflects the *first* confirmation). Acknowledgment is an explicit `POST .../read` call, not inferred from viewing the list.
- Indexes: `shifts(resto_id, shift_date)`, `shifts(employee_id, shift_date)`, `swap_requests(target_employee_id, status)` — all sized for "shifts in a given week" and "my pending swaps" lookups, which are the hot paths.

## Auth

`require_user` decodes an **unsigned base64-encoded user id** as the bearer token (`make_token`/`decode_token`, `app/core/security.py`). This is explicitly a prototype scheme — not signed, not expiring, trivially forgeable by anyone who can guess/enumerate a user id. It is fine for the current demo-only deployment and must not be treated as a real session mechanism if this app ever handles real credentials or real restaurant data — replace with signed JWTs or server-side sessions first. Admin auth (`require_auth`, gates `GET /api/waitlist`) is separately just the raw `ADMIN_PASSWORD` env var used as a bearer token — same caveat.

## Scheduling business rules (`app/services/scheduling.py`)

All enforced inside `validate_assignment`, called by every assignment path:

- **Role match** — employee's role must equal the shift's `role_required`.
- **Availability** — `availability_allows` requires the shift to fall *entirely inside* one of the employee's declared windows for that weekday (not just overlap it). Accepts **two different JSON shapes** for `weekly_availability` — `{"monday": [{"start","end"}], ...}` (what the current frontend writes) and a flat `[{"day","start","end"}]` list — normalize defensively if you touch this code, don't assume one shape.
- **Quebec 40h/week overtime cap** — `QUEBEC_WEEKLY_LIMIT = 40`; actual cap per employee is `min(profile.max_hours_per_week, 40)`. Summed over `Scheduled`/`Pending_Swap` shifts in the shift's ISO week (Monday–Sunday via `week_start`).
- **Overlap detection** and **11-hour minimum rest** (`MIN_REST_HOURS`) between shifts in the same week, including on the excluded-shift-id path used when editing a shift in place.
- **Overnight shifts** — `shift_bounds` treats `end_time <= start_time` as crossing midnight (adds a day), so both duration math and rest-period math handle a 22:00–06:00 shift correctly. Anything that computes shift duration/overlap directly instead of going through `shift_bounds`/`shift_hours` will get this wrong.

## API conventions

- **camelCase JSON over snake_case Postgres columns.** `serialize_shift` is the single translation point for shifts — always return through it rather than hand-rolling a shift response shape.
- **Per-route manager gating**, not middleware: every manager-only route starts with `if user["role"] != "manager": raise HTTPException(403, ...)`. This is a deliberate, repeated pattern in this codebase, not an oversight — keep it consistent if you add routes rather than introducing a decorator/dependency that only some routes use.
- **jsonb defensive decoding**: asyncpg can return a jsonb column as an already-decoded Python object or as a raw JSON string, depending on connection codec state. The existing pattern (`availability_allows`, `get_availability`) is `if isinstance(x, str): x = json.loads(x)` — follow it for any new jsonb reads.

## Local dev

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env.local   # already present in a working checkout
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
```
or via Docker: `npm run dev` (`docker compose up`) — API on `:8001`, Postgres 15 on `:5432` (`postgres`/`postgres`/`crewlee`).

Demo accounts (auto-seeded on first boot against an empty `users` table — see `app/db/seed.py`; a database that already has users is left alone): `manager@demo.com`, `foh@demo.com`, `jordan@demo.com`, `taylor@demo.com`, `boh@demo.com`, `alex@demo.com` — all password `password123`.

## Known limitations

- **No automated tests, no CI.** Verify scheduling changes manually via `curl` against the demo accounts (log in, exercise the endpoint, check `GET /api/scheduling/shifts` for the resulting state) — there is no test suite to run instead.
- **No migration tool** — see Architecture above; schema changes must stay idempotent `ALTER`/`CREATE ... IF NOT EXISTS` statements appended to `db/schema.sql`.
- **`PATCH /api/scheduling/shifts/{id}` requires `date`/`startTime`/`endTime` on every call**, even a pure reassignment — `ShiftUpdateRequest` only makes `employeeId` optional. Works today because the only caller (the frontend's drag-and-drop handler) always resends the full current shift; a partial-update contract would need `ShiftUpdateRequest`'s other fields made optional and merged with the current row.
- **`auto_build_schedule`** ranks candidates by (1) anyone still under their own `min_hours_per_week`, (2) fewest hours already assigned this week (fairness), (3) higher `scheduling_confidence` as a tiebreak, but only on blocks whose `coverage_requirements.min_confidence` is set — confidence never overrides fairness on an ordinary shift. Still no transaction wrapping around the *whole* run (a failure partway through leaves a partially-built schedule, though each individual assignment is itself consistent).
- **Announcements have no edit, only delete** — once posted, a broadcast can't be revised (editing after people have acknowledged it would make read receipts misleading); retracting via `DELETE` is fine since it cascades `announcement_reads` with it.
- Auth scheme — see the Auth section above.
