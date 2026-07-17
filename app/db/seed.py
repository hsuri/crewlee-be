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
