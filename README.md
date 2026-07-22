# crewlee-be

Backend API for Crewlee — a restaurant knowledge-sharing, scheduling, and announcements platform. FastAPI + Postgres (`asyncpg`), owns all business logic and the database. The [crewlee-fe](../crewlee-fe) frontend is a thin proxy — it has no DB access of its own.

For the full data model, business rules, and architectural rationale, see **`CLAUDE.md`** in this repo — it's kept detailed and current; this README is just the practical quick-start.

## Stack

Python 3.12, FastAPI, `asyncpg`, Postgres 15. Standard `app/` package layout — see `CLAUDE.md` → Architecture for the file-by-file breakdown.

## API

| Router | Routes | Auth |
|---|---|---|
| `health` | `GET /health` | — |
| `public_config` | `GET /api/config/public` | — |
| `waitlist` | `POST /api/waitlist`, `POST /api/admin/login`, `GET /api/waitlist` | last one: Bearer `ADMIN_PASSWORD` |
| `auth` | `POST /api/auth/login`, `GET /api/me` | second: Bearer user token |
| `scheduling` | `/api/scheduling/*` — departments, employees, coverage requirements, shifts, availability, auto-build, templates, publish, swap requests | Bearer user token; manager-only routes 403 non-managers |
| `announcements` | `/api/announcements*` — list, create, acknowledge, read receipts, delete | Bearer user token; post/delete/read-receipts are manager-only |

Scheduling is the largest and most detailed router — see `CLAUDE.md` → "Scheduling business rules" for the validation rules every shift-assignment path runs through (`validate_assignment`: role match, availability, Quebec 40h overtime cap, overlap/rest-period, overnight-shift handling).

## Database

Schema lives in [`db/schema.sql`](db/schema.sql), executed verbatim on every boot (`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` only — **no `ALTER`**, by design, so the file stays a clean target for a future migration tool). Core tables:

- **`restaurants`** — tenant root
- **`roles`** — seeded once: `manager`, `foh`, `boh`
- **`departments`** — sits between restaurant and employee, categorized `foh`/`boh`
- **`users`** — restaurant staff, plus a full scheduling profile (availability, hour limits, Smart Fill confidence)
- **`coverage_requirements`** → **`shifts`** → **`swap_requests`** — the three-layer scheduling model (staffing need → generated shift → assignment/swap)
- **`schedule_templates`**, **`announcements`** / **`announcement_reads`**
- **`waitlist`** — unrelated pre-launch signups, schema driven by `app/core/config.py`'s `DB_FIELDS`

**Important local-dev gotcha:** because schema changes are never `ALTER`ed in, an existing local Postgres volume with an older table shape will silently keep the old columns — `CREATE TABLE IF NOT EXISTS` is a no-op if the table already exists, even if its shape has since changed upstream. If you pull changes and something references a column that "doesn't exist," reset your local DB:

```bash
docker compose down -v   # drops the local Postgres volume
docker compose up -d --build
```

Demo data (restaurant, departments, 6 users, coverage requirements) is only (re-)seeded when `users` is empty, so a full reset is also how you get fresh demo data after a schema change.

## Demo accounts

Auto-seeded on first boot against an empty `users` table (`app/db/seed.py`), all password `password123`:

| Email | Role | Notes |
|---|---|---|
| `manager@demo.com` | manager | |
| `foh@demo.com`, `jordan@demo.com`, `taylor@demo.com` | foh | varied Smart Fill confidence for demo purposes |
| `boh@demo.com`, `alex@demo.com` | boh | varied Smart Fill confidence for demo purposes |

## Auth

Intentionally minimal prototype scheme — not signed, not expiring: the user session token is an unsigned base64-encoded user id, and the waitlist admin token is just the raw `ADMIN_PASSWORD`. Fine for demo/internal use; do not treat as production-grade session security. See `CLAUDE.md` → Auth.

If the database is unreachable at startup, the API still boots rather than crashing (Cloud Run kills a revision that doesn't bind `$PORT` in time) — routes needing the DB 503 individually instead.

## Local dev

**Docker (recommended)** — spins up the API + a Postgres 15 container:

```bash
npm run dev     # docker compose up --quiet-pull
npm run logs    # tail API logs
npm run stop    # docker compose down
```

API on `http://localhost:8001`, Postgres on `localhost:5432` (`postgres`/`postgres`/`crewlee`). **If you rebuilt the image or changed `db/schema.sql`, rebuild explicitly** (`docker compose up -d --build`) — `npm run dev` alone won't pick up a stale image.

**Without Docker:**

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env.local   # then edit as needed
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
# or: ./scripts/dev.sh
```

## Environment variables

See `.env.example`:

| Var | Purpose |
|---|---|
| `PORT` | Port to listen on (default `8001` locally, `8080` in the container) |
| `DATABASE_URL` | Postgres connection string |
| `ADMIN_PASSWORD` | Waitlist admin panel password |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins (the frontend's URL) |

## Known limitations

See `CLAUDE.md` → Known limitations for the full list (no tests/CI, no migration tool, `PATCH /api/scheduling/shifts/{id}` field requirements, announcements have no edit, "Ask (RAG)" not started, auth scheme). Highlights:

- No RAG knowledge base yet — not started, backend or frontend.
- No automated tests or CI — verify scheduling/announcement changes manually via `curl` against the demo accounts.

## Deployment

GCP Cloud Run + Cloud SQL, via `scripts/`:

- `scripts/setup.sh` — one-time GCP setup (IAM, Cloud SQL client role). Cloud SQL instance/DB are shared with `crewlee-fe` — run its `setup.sh` first.
- `scripts/deploy.sh` — deploys this service to Cloud Run.
