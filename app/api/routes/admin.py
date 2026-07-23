import re

from fastapi import APIRouter, Depends, HTTPException

from app.core.security import require_auth
from app.db import session as db
from app.models.schemas import RestaurantCreateRequest

router = APIRouter()

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


@router.post("/api/admin/restaurants", dependencies=[Depends(require_auth)])
async def create_restaurant(payload: RestaurantCreateRequest):
    if not db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    name = payload.name.strip()
    slug = payload.slug.strip().lower()
    manager_name = payload.managerName.strip()
    manager_email = payload.managerEmail.strip()

    if not name or not manager_name or not manager_email:
        raise HTTPException(status_code=422, detail="Name, manager name, and manager email are required")
    if not SLUG_RE.match(slug):
        raise HTTPException(status_code=422, detail="Slug must be lowercase letters, numbers, and hyphens only")
    if "@" not in manager_email:
        raise HTTPException(status_code=422, detail="Manager email is invalid")

    if await db.pool.fetchval("SELECT 1 FROM restaurants WHERE slug = $1", slug):
        raise HTTPException(status_code=409, detail="Slug already in use")

    async with db.pool.acquire() as connection:
        async with connection.transaction():
            restaurant = await connection.fetchrow(
                "INSERT INTO restaurants (name, slug) VALUES ($1, $2) RETURNING id, name, slug, created_at",
                name, slug,
            )
            role_id = await connection.fetchval("SELECT id FROM roles WHERE name = 'manager'")
            manager = await connection.fetchrow(
                """INSERT INTO users (restaurant_id, role_id, name, email, password_hash)
                   VALUES ($1, $2, $3, $4, NULL) RETURNING id, name, email""",
                restaurant["id"], role_id, manager_name, manager_email,
            )

    return {
        "id": restaurant["id"],
        "name": restaurant["name"],
        "slug": restaurant["slug"],
        "createdAt": restaurant["created_at"].isoformat(),
        "manager": {"id": manager["id"], "name": manager["name"], "email": manager["email"]},
    }


@router.get("/api/admin/restaurants", dependencies=[Depends(require_auth)])
async def list_restaurants():
    if not db.pool:
        return []

    rows = await db.pool.fetch(
        """
        SELECT r.id, r.name, r.slug, r.created_at, m.name AS manager_name, m.email AS manager_email,
               (SELECT count(*) FROM users u2 WHERE u2.restaurant_id = r.id AND u2.active) AS employee_count
        FROM restaurants r
        LEFT JOIN LATERAL (
            SELECT u.name, u.email FROM users u
            JOIN roles ro ON ro.id = u.role_id
            WHERE u.restaurant_id = r.id AND ro.name = 'manager'
            ORDER BY u.id LIMIT 1
        ) m ON true
        ORDER BY r.created_at DESC
        """
    )
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "slug": row["slug"],
            "managerName": row["manager_name"],
            "managerEmail": row["manager_email"],
            "employeeCount": row["employee_count"],
            "createdAt": row["created_at"].isoformat(),
        }
        for row in rows
    ]
