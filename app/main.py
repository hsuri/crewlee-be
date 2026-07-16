import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import auth, health, public_config, scheduling, waitlist
from app.core.config import PROJECT_NAME
from app.db import seed
from app.db import session as db

load_dotenv(".env.local")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        db.pool = await db.create_pool()
        if db.pool:
            await db.init_db(db.pool)
            await seed.seed_demo_data(db.pool)
            # Re-run the idempotent schema so department defaults backfill onto any
            # restaurant/users just created by seed_demo_data on a fresh DB, without
            # requiring a second process restart.
            await db.init_db(db.pool)
    except Exception as e:
        # DB failure must not prevent the container from starting —
        # Cloud Run kills the revision if the process dies before binding to PORT.
        print(f"[db] Startup error (running without DB): {e}")
    yield
    if db.pool:
        await db.pool.close()


app = FastAPI(title=f"{PROJECT_NAME} API", version="1.0.0", lifespan=lifespan)

ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(health.router)
app.include_router(public_config.router)
app.include_router(waitlist.router)
app.include_router(auth.router)
app.include_router(scheduling.router)
