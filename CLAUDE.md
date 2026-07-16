# crewlee-be

Backend for Crewlee, a restaurant operations platform. FastAPI + asyncpg + Postgres. This service owns all business logic and the database; the sibling `crewlee-fe` repo is a thin static server that only proxies `/api/*` here.

## Architecture

Two files carry the application:

- **`main.py`** — the FastAPI app: DB pool lifecycle, CORS, auth, Pydantic request models, and every route.
- **`scheduling_service.py`** — pure business-rule module for scheduling: date/time parsing, availability/overtime/rest-period validation, and shift serialization. It exists so that every code path that can change a shift's assignment (manual create, drag-and-drop edit, auto-build, swap claim, swap approval) runs through the *same* `validate_assignment` check. If you add a new way to assign a shift to an employee, route it through `validate_assignment` rather than re-deriving the rules — that's the whole reason this module is split out.

**No migration framework.** `db/schema.sql` is executed verbatim on every process boot (`_init_db`, called from the FastAPI `lifespan` context in `main.py`). Every statement in it must stay idempotent — `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`. This is how dev/demo databases stay in sync without Alembic or similar; if you add a column, add it as an idempotent `ALTER` at the bottom of the relevant block, don't rewrite the `CREATE TABLE`.

If the DB is unreachable at startup, the app still boots (`lifespan` swallows the exception) — required for Cloud Run, which kills a revision that doesn't bind to `$PORT` in time. Routes that need `_pool` will 503 individually instead.

## Data model

- **`restaurants`** — tenant root. Everything else scopes to `resto_id`/`restaurant_id`.
- **`roles`** — seeded once: `manager`, `foh`, `boh`.
- **`users`** — `restaurant_id`, `role_id`, `email`/`password_hash` (bcrypt), plus two scheduling-profile columns added via idempotent `ALTER`: `weekly_availability` (jsonb, default `'[]'`) and `max_hours_per_week` (numeric, default 40).
- **`shifts`** — `employee_id` nullable (`NULL` = open/unassigned shift, not a separate boolean). `status` lifecycle: `Open → Scheduled ↔ Pending_Swap`. A shift only enters `Pending_Swap` when at least one coworker has actually passed validation for it — `drop_shift` checks eligibility *before* flipping status, specifically so `Pending_Swap` always means "something real is pending" and never gets stuck with no way back to `Scheduled`.
- **`swap_requests`** — one row per (shift, candidate) pairing offered during a drop. Status lifecycle: `Pending_Match → Pending_Approval → Completed | Rejected`. `UNIQUE(original_shift_id, target_employee_id)` prevents duplicate offers to the same person; the `ON CONFLICT ... DO UPDATE` in `drop_shift` re-uses that constraint to re-open a stale row rather than erroring.
- Indexes: `shifts(resto_id, shift_date)`, `shifts(employee_id, shift_date)`, `swap_requests(target_employee_id, status)` — all sized for "shifts in a given week" and "my pending swaps" lookups, which are the hot paths.

## Auth

`require_user` decodes an **unsigned base64-encoded user id** as the bearer token (`_make_token`/`_decode_token`, `main.py`). This is explicitly a prototype scheme — not signed, not expiring, trivially forgeable by anyone who can guess/enumerate a user id. It is fine for the current demo-only deployment and must not be treated as a real session mechanism if this app ever handles real credentials or real restaurant data — replace with signed JWTs or server-side sessions first. Admin auth (`require_auth`, gates `GET /api/waitlist`) is separately just the raw `ADMIN_PASSWORD` env var used as a bearer token — same caveat.

## Scheduling business rules (`scheduling_service.py`)

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
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```
or via Docker: `npm run dev` (`docker compose up`) — API on `:8001`, Postgres 15 on `:5432` (`postgres`/`postgres`/`crewlee`).

Demo accounts (auto-seeded on first boot against an empty `users` table, and idempotently backfilled with full-week availability on subsequent boots against pre-scheduling dev DBs): `manager@demo.com`, `foh@demo.com`, `jordan@demo.com`, `taylor@demo.com`, `boh@demo.com`, `alex@demo.com` — all password `password123`.

## Known limitations

- **No automated tests, no CI.** Verify scheduling changes manually via `curl` against the demo accounts (log in, exercise the endpoint, check `GET /api/scheduling/shifts` for the resulting state) — there is no test suite to run instead.
- **No migration tool** — see Architecture above; schema changes must stay idempotent `ALTER`/`CREATE ... IF NOT EXISTS` statements appended to `db/schema.sql`.
- **`PATCH /api/scheduling/shifts/{id}` requires `date`/`startTime`/`endTime` on every call**, even a pure reassignment — `ShiftUpdateRequest` only makes `employeeId` optional. Works today because the only caller (the frontend's drag-and-drop handler) always resends the full current shift; a partial-update contract would need `ShiftUpdateRequest`'s other fields made optional and merged with the current row.
- **No `DELETE /api/scheduling/shifts/{id}`** endpoint, and CORS `allow_methods` doesn't include `DELETE`. The only way to "remove" an assignment today is `PATCH` with `employeeId: null`, which reverts the shift to `Open` rather than deleting the row.
- **`auto_build_schedule`** is a naive greedy matcher — for each open shift in the week, iterates candidates `ORDER BY u.id` and takes the first one that validates. No fairness/load-balancing (always prefers the lowest user id first) and no transaction wrapping around the whole run (a failure partway through leaves a partially-built schedule, though each individual assignment is itself consistent).
- Auth scheme — see the Auth section above.
