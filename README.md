# crewlee-be

Backend API for Crewlee — a restaurant knowledge-sharing, scheduling, and announcements platform. FastAPI + Postgres (`asyncpg`), owns all business logic and the database. The [crewlee-fe](../crewlee-fe) frontend just proxies `/api/*` here.

## Stack

Python 3.12, FastAPI, `asyncpg`, Postgres 15.

## API

| Route | Auth | Purpose |
|---|---|---|
| `GET /health` | — | Liveness check |
| `GET /api/config/public` | — | Project name/slug + waitlist field schema |
| `POST /api/waitlist` | — | Insert a waitlist signup |
| `POST /api/admin/login` | — | Admin password login, returns a token (waitlist admin panel) |
| `GET /api/waitlist` | Bearer `ADMIN_PASSWORD` | List waitlist signups |
| `POST /api/auth/login` | — | Staff login (`email` + `password`), returns `{token, user}` |
| `GET /api/me` | Bearer user token | Returns the current logged-in user |

## Database

Schema lives in [`db/schema.sql`](db/schema.sql) and runs automatically on startup (`CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` — safe to re-run):

- **`restaurants`** — id, name
- **`roles`** — id, name — seeded with `manager`, `foh`, `boh`
- **`users`** — restaurant staff: name, email, password_hash, `restaurant_id` → restaurants, `role_id` → roles
- **`waitlist`** — pre-launch signups (schema driven by `config.py`'s `DB_FIELDS`, separate from the tables above)

On first boot with an empty `users` table, a demo restaurant and one dummy user per role are seeded automatically (see `DEMO_USERS` in `main.py`):

| Email | Password | Role |
|---|---|---|
| `manager@demo.com` | `password123` | manager |
| `foh@demo.com` | `password123` | foh |
| `boh@demo.com` | `password123` | boh |

Auth is intentionally minimal for this stage: the user session token is an unsigned base64-encoded user id, and the admin token is just the admin password. Fine for internal/demo use — don't assume it's production-hardened.

If the database is unreachable at startup, the API still comes up (logs the error and runs without a DB) rather than crashing — Cloud Run kills a revision that doesn't bind to `$PORT` in time.

## Local dev

**Docker (recommended)** — spins up the API + a Postgres 15 container:

```bash
npm run dev     # docker compose up --quiet-pull
npm run logs    # tail API logs
npm run stop    # docker compose down
```

API on `http://localhost:8001`, Postgres on `localhost:5432` (`postgres`/`postgres`/`crewlee`).

**Without Docker:**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env.local   # then edit as needed
./scripts/dev.sh              # uvicorn --reload
```

## Environment variables

See `.env.example`:

| Var | Purpose |
|---|---|
| `PORT` | Port to listen on (default `8001` locally, `8080` in the container) |
| `DATABASE_URL` | Postgres connection string |
| `ADMIN_PASSWORD` | Waitlist admin panel password |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins (the frontend's URL) |

## Deployment

GCP Cloud Run + Cloud SQL, via `scripts/`:

- `scripts/setup.sh` — one-time GCP setup (IAM, Cloud SQL client role). Cloud SQL instance/DB are shared with `crewlee-fe` — run its `setup.sh` first.
- `scripts/deploy.sh` — deploys this service to Cloud Run.
