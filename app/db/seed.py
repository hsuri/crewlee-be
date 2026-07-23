"""Demo data seeded only when the users table is empty.

Lets `docker compose up` give you a working login on a fresh DB with no manual step.
"""
import asyncpg

from app.core.security import hash_password

# confidence is varied so Smart Fill's confidence gating has something real to filter on in the
# demo: Jordan and Bailey are "experienced" (qualify for high-confidence blocks), Taylor and Alex
# are not.
DEMO_USERS = [
    {"name": "Morgan Manager", "email": "manager@demo.com", "password": "password123", "role": "manager", "confidence": 3},
    {"name": "Frankie FOH", "email": "foh@demo.com", "password": "password123", "role": "foh", "confidence": 3},
    {"name": "Jordan FOH", "email": "jordan@demo.com", "password": "password123", "role": "foh", "confidence": 5},
    {"name": "Taylor FOH", "email": "taylor@demo.com", "password": "password123", "role": "foh", "confidence": 2},
    {"name": "Bailey BOH", "email": "boh@demo.com", "password": "password123", "role": "boh", "confidence": 4},
    {"name": "Alex BOH", "email": "alex@demo.com", "password": "password123", "role": "boh", "confidence": 2},
]

FULL_WEEK_AVAILABILITY = (
    '{"monday":[{"start":"00:00","end":"23:59"}],"tuesday":[{"start":"00:00","end":"23:59"}],'
    '"wednesday":[{"start":"00:00","end":"23:59"}],"thursday":[{"start":"00:00","end":"23:59"}],'
    '"friday":[{"start":"00:00","end":"23:59"}],"saturday":[{"start":"00:00","end":"23:59"}],'
    '"sunday":[{"start":"00:00","end":"23:59"}]}'
)


async def seed_demo_data(pool: asyncpg.Pool) -> None:
    if await pool.fetchval("SELECT count(*) FROM users"):
        return

    restaurant_id = await pool.fetchval(
        "INSERT INTO restaurants (name, slug) VALUES ($1, $2) RETURNING id",
        "Demo Restaurant", "demo-restaurant",
    )
    foh_dept = await pool.fetchval(
        "INSERT INTO departments (resto_id, name, role_category) VALUES ($1, 'Front of House', 'foh') RETURNING id",
        restaurant_id,
    )
    boh_dept = await pool.fetchval(
        "INSERT INTO departments (resto_id, name, role_category) VALUES ($1, 'Back of House', 'boh') RETURNING id",
        restaurant_id,
    )
    department_by_role = {"foh": foh_dept, "boh": boh_dept}
    for u in DEMO_USERS:
        role_id = await pool.fetchval("SELECT id FROM roles WHERE name = $1", u["role"])
        await pool.execute(
            """INSERT INTO users (restaurant_id, role_id, department_id, name, email, password_hash, weekly_availability, scheduling_confidence)
               VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)""",
            restaurant_id, role_id, department_by_role.get(u["role"]), u["name"], u["email"],
            hash_password(u["password"]), FULL_WEEK_AVAILABILITY, u["confidence"],
        )
    print("[db] Seeded demo restaurant + departments + 6 dummy users")


async def seed_scheduling_defaults(pool: asyncpg.Pool) -> None:
    """Coverage-requirement demo data. Kept separate from seed_demo_data so it stays a no-op
    once coverage_requirements already exist for the demo restaurant, independent of whether
    users needed (re)seeding."""
    restaurant_id = await pool.fetchval("SELECT id FROM restaurants WHERE name = 'Demo Restaurant'")
    if not restaurant_id or await pool.fetchval("SELECT 1 FROM coverage_requirements WHERE resto_id = $1", restaurant_id):
        return
    foh = await pool.fetchval("SELECT id FROM departments WHERE resto_id = $1 AND role_category = 'foh'", restaurant_id)
    boh = await pool.fetchval("SELECT id FROM departments WHERE resto_id = $1 AND role_category = 'boh'", restaurant_id)
    if not foh or not boh:
        return
    for day in range(5):  # Monday-Friday dinner rush
        await pool.execute(
            """INSERT INTO coverage_requirements (resto_id, department_id, day_of_week, start_time, end_time, count_required, min_confidence, notes)
               VALUES ($1, $2, $3, '17:00', '21:00', 2, 3, 'Weekday dinner rush')""",
            restaurant_id, foh, day,
        )
        await pool.execute(
            """INSERT INTO coverage_requirements (resto_id, department_id, day_of_week, start_time, end_time, count_required, notes)
               VALUES ($1, $2, $3, '17:00', '21:00', 1, 'Weekday dinner rush')""",
            restaurant_id, boh, day,
        )
    await pool.execute(
        """INSERT INTO coverage_requirements (resto_id, department_id, day_of_week, start_time, end_time, count_required, min_confidence, notes)
           VALUES ($1, $2, 5, '17:00', '22:00', 2, 5, 'Saturday dinner rush -- experienced staff only')""",
        restaurant_id, foh,
    )
    print("[db] Seeded demo coverage requirements")
