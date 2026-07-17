"""Demo data seeded only when the users table is empty.

Lets `docker compose up` give you a working login on a fresh DB with no manual step.
"""
import asyncpg

from app.core.security import hash_password

DEMO_USERS = [
    {"name": "Morgan Manager", "email": "manager@demo.com", "password": "password123", "role": "manager"},
    {"name": "Frankie FOH", "email": "foh@demo.com", "password": "password123", "role": "foh"},
    {"name": "Jordan FOH", "email": "jordan@demo.com", "password": "password123", "role": "foh"},
    {"name": "Taylor FOH", "email": "taylor@demo.com", "password": "password123", "role": "foh"},
    {"name": "Bailey BOH", "email": "boh@demo.com", "password": "password123", "role": "boh"},
    {"name": "Alex BOH", "email": "alex@demo.com", "password": "password123", "role": "boh"},
]

FULL_WEEK_AVAILABILITY = (
    '{"monday":[{"start":"00:00","end":"23:59"}],"tuesday":[{"start":"00:00","end":"23:59"}],'
    '"wednesday":[{"start":"00:00","end":"23:59"}],"thursday":[{"start":"00:00","end":"23:59"}],'
    '"friday":[{"start":"00:00","end":"23:59"}],"saturday":[{"start":"00:00","end":"23:59"}],'
    '"sunday":[{"start":"00:00","end":"23:59"}]}'
)

# Varied so Smart Fill's confidence gating has something real to filter on in the demo: Jordan
# and Bailey are "experienced" (qualify for high-confidence blocks), Taylor and Alex are not.
DEMO_CONFIDENCE = {
    "foh@demo.com": 3, "jordan@demo.com": 5, "taylor@demo.com": 2,
    "boh@demo.com": 4, "alex@demo.com": 2,
}


async def seed_demo_data(pool: asyncpg.Pool) -> None:
    if await pool.fetchval("SELECT count(*) FROM users"):
        # Existing demo databases predate scheduling profiles; keep the visible
        # demo usable without changing availability for real customer accounts.
        await pool.execute(
            """UPDATE users SET weekly_availability = $1::jsonb
               WHERE email LIKE '%@demo.com' AND weekly_availability = '[]'::jsonb""",
            FULL_WEEK_AVAILABILITY,
        )
        demo_restaurant = await pool.fetchval("SELECT id FROM restaurants WHERE name = 'Demo Restaurant'")
        if demo_restaurant:
            for demo_user in DEMO_USERS:
                role_id = await pool.fetchval("SELECT id FROM roles WHERE name = $1", demo_user["role"])
                await pool.execute(
                    """INSERT INTO users (restaurant_id, role_id, name, email, password_hash, weekly_availability)
                       VALUES ($1, $2, $3, $4, $5, $6::jsonb) ON CONFLICT (email) DO NOTHING""",
                    demo_restaurant, role_id, demo_user["name"], demo_user["email"], hash_password(demo_user["password"]),
                    FULL_WEEK_AVAILABILITY,
                )
        return

    restaurant_id = await pool.fetchval(
        "INSERT INTO restaurants (name) VALUES ($1) RETURNING id", "Demo Restaurant"
    )
    for u in DEMO_USERS:
        role_id = await pool.fetchval("SELECT id FROM roles WHERE name = $1", u["role"])
        await pool.execute(
            "INSERT INTO users (restaurant_id, role_id, name, email, password_hash, weekly_availability) "
            "VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
            restaurant_id, role_id, u["name"], u["email"], hash_password(u["password"]),
            FULL_WEEK_AVAILABILITY,
        )
    print("[db] Seeded demo restaurant + 3 dummy users (manager/foh/boh)")


async def seed_scheduling_defaults(pool: asyncpg.Pool) -> None:
    """Coverage-requirement + confidence demo data. Called separately from seed_demo_data (and
    after main.py's second init_db() pass), because departments only exist once the schema has
    run against the just-created demo restaurant -- see main.py's lifespan comment."""
    restaurant_id = await pool.fetchval("SELECT id FROM restaurants WHERE name = 'Demo Restaurant'")
    if not restaurant_id:
        return
    for email, confidence in DEMO_CONFIDENCE.items():
        await pool.execute("UPDATE users SET scheduling_confidence = $1 WHERE email = $2", confidence, email)
    if await pool.fetchval("SELECT 1 FROM coverage_requirements WHERE resto_id = $1", restaurant_id):
        return
    foh = await pool.fetchval("SELECT id FROM departments WHERE resto_id = $1 AND role_category = 'foh'", restaurant_id)
    boh = await pool.fetchval("SELECT id FROM departments WHERE resto_id = $1 AND role_category = 'boh'", restaurant_id)
    if not foh:
        return
    for day in range(5):  # Monday-Friday dinner rush
        await pool.execute(
            """INSERT INTO coverage_requirements (resto_id, department_id, day_of_week, start_time, end_time, count_required, min_confidence, notes)
               VALUES ($1, $2, $3, '17:00', '21:00', 2, 3, 'Weekday dinner rush')""",
            restaurant_id, foh, day,
        )
        if boh:
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
    print("[db] Seeded demo coverage requirements + confidence ratings")
