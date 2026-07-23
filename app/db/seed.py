"""Demo data seeded only when the users table is empty.

Lets `docker compose up` give you a working login on a fresh DB with no manual step.
"""
from datetime import date, time, timedelta

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


async def seed_scheduling_shifts(pool: asyncpg.Pool) -> None:
    """Actual demo shifts (not just coverage_requirements) for the current week, so Month/Week/Day
    all have something real to show on a fresh boot instead of an empty grid. Idempotent: skips
    once the demo restaurant already has any shifts at all. Dates are computed relative to
    today rather than hardcoded, so the demo stays "this week" no matter when the container boots."""
    restaurant_id = await pool.fetchval("SELECT id FROM restaurants WHERE name = 'Demo Restaurant'")
    if not restaurant_id or await pool.fetchval("SELECT 1 FROM shifts WHERE resto_id = $1", restaurant_id):
        return
    foh = await pool.fetchval("SELECT id FROM departments WHERE resto_id = $1 AND role_category = 'foh'", restaurant_id)
    boh = await pool.fetchval("SELECT id FROM departments WHERE resto_id = $1 AND role_category = 'boh'", restaurant_id)
    if not foh or not boh:
        return
    employees = {row["email"]: row["id"] for row in await pool.fetch(
        "SELECT id, email FROM users WHERE restaurant_id = $1", restaurant_id
    )}
    frankie, jordan, taylor = employees.get("foh@demo.com"), employees.get("jordan@demo.com"), employees.get("taylor@demo.com")
    bailey, alex = employees.get("boh@demo.com"), employees.get("alex@demo.com")
    if not all([frankie, jordan, taylor, bailey, alex]):
        return

    # (day offset from Monday, employee or None for an open shift, department, start, end).
    # Kept each employee comfortably under the 40h/week cap so nothing shows a bogus overtime
    # warning on first login; a handful of shifts are left open/unassigned on purpose so
    # Week/Day/Month all show a mix of fully-staffed, partial, and open days.
    PLAN = [
        (0, frankie, foh, "09:00", "17:00"), (0, bailey, boh, "08:00", "16:00"), (0, None, foh, "17:00", "23:00"),
        (1, jordan, foh, "09:00", "17:00"), (1, taylor, foh, "17:00", "23:00"), (1, alex, boh, "08:00", "16:00"),
        (2, frankie, foh, "09:00", "17:00"), (2, bailey, boh, "08:00", "16:00"), (2, alex, boh, "16:00", "23:59"),
        (3, jordan, foh, "09:00", "17:00"), (3, taylor, foh, "09:00", "17:00"), (3, bailey, boh, "08:00", "16:00"), (3, None, boh, "16:00", "23:59"),
        (4, frankie, foh, "09:00", "17:00"), (4, jordan, foh, "17:00", "23:00"), (4, taylor, foh, "17:00", "23:00"), (4, bailey, boh, "08:00", "16:00"), (4, alex, boh, "16:00", "23:59"),
        (5, frankie, foh, "09:00", "17:00"), (5, jordan, foh, "09:00", "17:00"), (5, taylor, foh, "17:00", "23:00"), (5, None, boh, "08:00", "16:00"), (5, alex, boh, "16:00", "23:59"),
        (6, None, foh, "09:00", "17:00"), (6, None, boh, "08:00", "16:00"),
    ]
    employee_role = {frankie: "foh", jordan: "foh", taylor: "foh", bailey: "boh", alex: "boh"}
    dept_role = {foh: "foh", boh: "boh"}
    monday = date.today() - timedelta(days=date.today().weekday())

    def _time(s: str) -> time:
        h, m = s.split(":")
        return time(int(h), int(m))

    for offset, employee_id, department_id, start_str, end_str in PLAN:
        role_required = employee_role[employee_id] if employee_id else dept_role[department_id]
        status = "Scheduled" if employee_id else "Open"
        await pool.execute(
            """INSERT INTO shifts (resto_id, employee_id, department_id, role_required, shift_date, start_time, end_time, status, is_draft)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, false)""",
            restaurant_id, employee_id, department_id, role_required, monday + timedelta(days=offset), _time(start_str), _time(end_str), status,
        )
    print(f"[db] Seeded {len(PLAN)} demo shifts for the week of {monday}")


async def seed_announcements(pool: asyncpg.Pool) -> None:
    """Demo announcements plus a spread of read receipts, so the Announcements board -- and the
    manager's read-progress tooltips -- have something real to show on a fresh boot rather than
    an empty board with every count at 0."""
    restaurant_id = await pool.fetchval("SELECT id FROM restaurants WHERE name = 'Demo Restaurant'")
    if not restaurant_id or await pool.fetchval("SELECT 1 FROM announcements WHERE resto_id = $1", restaurant_id):
        return
    manager_id = await pool.fetchval(
        "SELECT id FROM users WHERE restaurant_id = $1 AND email = 'manager@demo.com'", restaurant_id
    )
    if not manager_id:
        return
    employees = {row["email"]: row["id"] for row in await pool.fetch(
        "SELECT id, email FROM users WHERE restaurant_id = $1 AND email != 'manager@demo.com'", restaurant_id
    )}
    frankie, jordan, taylor, bailey, alex = (
        employees.get("foh@demo.com"), employees.get("jordan@demo.com"), employees.get("taylor@demo.com"),
        employees.get("boh@demo.com"), employees.get("alex@demo.com"),
    )

    DEMO_ANNOUNCEMENTS = [
        {
            "title": "New POS terminals go live Monday",
            "body": "We're switching to the new terminals at every station starting Monday's lunch shift. Training video is on the tablet in the office -- watch it before your next shift. Ping Morgan with questions.",
            "pinned": True, "age_days": 1, "readers": [jordan, bailey],
        },
        {
            "title": "Allergy menu insert starts today",
            "body": "The laminated allergy insert goes in every menu as of this shift -- make sure your section has one before you sit your first table.",
            "pinned": False, "age_days": 0, "readers": [frankie, jordan, bailey, alex],
        },
        {
            "title": "Walk-in cooler repair complete",
            "body": "Repair crew finished the walk-in compressor this morning -- back to normal temps. Thanks for your patience covering with the reach-in this week.",
            "pinned": False, "age_days": 2, "readers": [frankie, jordan, taylor, bailey, alex],
        },
        {
            "title": "Great job this weekend",
            "body": "We had our best Saturday night of the quarter -- great work from every station, kitchen, floor, and bar. Keep it up.",
            "pinned": False, "age_days": 4, "readers": [frankie, jordan, taylor, alex],
        },
    ]
    for a in DEMO_ANNOUNCEMENTS:
        announcement_id = await pool.fetchval(
            """INSERT INTO announcements (resto_id, author_id, title, body, pinned, created_at)
               VALUES ($1, $2, $3, $4, $5, now() - $6::interval) RETURNING id""",
            restaurant_id, manager_id, a["title"], a["body"], a["pinned"], timedelta(days=a["age_days"]),
        )
        read_age = timedelta(days=max(a["age_days"] - 1, 0))
        for reader_id in a["readers"]:
            if not reader_id:
                continue
            await pool.execute(
                """INSERT INTO announcement_reads (announcement_id, employee_id, read_at)
                   VALUES ($1, $2, now() - $3::interval)
                   ON CONFLICT (announcement_id, employee_id) DO NOTHING""",
                announcement_id, reader_id, read_age,
            )
    print(f"[db] Seeded {len(DEMO_ANNOUNCEMENTS)} demo announcements")
