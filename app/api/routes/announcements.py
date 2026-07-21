from fastapi import APIRouter, Depends, HTTPException

from app.core.security import require_user
from app.db import session as db
from app.models.schemas import AnnouncementCreateRequest

router = APIRouter()


async def _restaurant_id_for(user_id: int) -> int:
    if not db.pool:
        raise HTTPException(503, detail="Database unavailable")
    restaurant_id = await db.pool.fetchval("SELECT restaurant_id FROM users WHERE id = $1", user_id)
    if not restaurant_id:
        raise HTTPException(404, detail="Restaurant membership not found")
    return restaurant_id


@router.get("/api/announcements")
async def list_announcements(user: dict = Depends(require_user)):
    restaurant_id = await _restaurant_id_for(user["id"])
    is_manager = user["role"] == "manager"
    rows = await db.pool.fetch(
        """SELECT a.id, a.title, a.body, a.pinned, a.created_at, a.author_id, author.name AS author_name,
                  ar.read_at AS my_read_at,
                  (SELECT count(*) FROM announcement_reads r WHERE r.announcement_id = a.id) AS read_count
           FROM announcements a
           JOIN users author ON author.id = a.author_id
           LEFT JOIN announcement_reads ar ON ar.announcement_id = a.id AND ar.employee_id = $2
           WHERE a.resto_id = $1
           ORDER BY a.pinned DESC, a.created_at DESC""",
        restaurant_id, user["id"],
    )
    total_recipients = None
    if is_manager:
        total_recipients = await db.pool.fetchval("SELECT count(*) FROM users WHERE restaurant_id = $1", restaurant_id)
    result = []
    for row in rows:
        entry = {
            "id": row["id"], "title": row["title"], "body": row["body"], "pinned": row["pinned"],
            "createdAt": row["created_at"].isoformat(), "authorName": row["author_name"],
            "readByMe": row["my_read_at"] is not None,
            "readAt": row["my_read_at"].isoformat() if row["my_read_at"] else None,
        }
        if is_manager:
            entry["readCount"] = row["read_count"]
            entry["totalRecipients"] = max(total_recipients - 1, 0)  # excludes this announcement's own author
        result.append(entry)
    return result


@router.post("/api/announcements")
async def create_announcement(payload: AnnouncementCreateRequest, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can post announcements")
    if not payload.title.strip():
        raise HTTPException(422, detail="Title is required")
    if not payload.body.strip():
        raise HTTPException(422, detail="Announcement body is required")
    restaurant_id = await _restaurant_id_for(user["id"])
    row = await db.pool.fetchrow(
        "INSERT INTO announcements (resto_id, author_id, title, body, pinned) VALUES ($1, $2, $3, $4, $5) RETURNING id, created_at",
        restaurant_id, user["id"], payload.title.strip(), payload.body.strip(), payload.pinned,
    )
    return {"id": row["id"], "createdAt": row["created_at"].isoformat()}


@router.post("/api/announcements/{announcement_id}/read")
async def acknowledge_announcement(announcement_id: int, user: dict = Depends(require_user)):
    restaurant_id = await _restaurant_id_for(user["id"])
    announcement = await db.pool.fetchrow("SELECT id FROM announcements WHERE id = $1 AND resto_id = $2", announcement_id, restaurant_id)
    if not announcement:
        raise HTTPException(404, detail="Announcement not found")
    row = await db.pool.fetchrow(
        """INSERT INTO announcement_reads (announcement_id, employee_id) VALUES ($1, $2)
           ON CONFLICT (announcement_id, employee_id) DO NOTHING
           RETURNING read_at""",
        announcement_id, user["id"],
    )
    if not row:
        row = await db.pool.fetchrow(
            "SELECT read_at FROM announcement_reads WHERE announcement_id = $1 AND employee_id = $2", announcement_id, user["id"],
        )
    return {"id": announcement_id, "readAt": row["read_at"].isoformat()}


@router.get("/api/announcements/{announcement_id}/reads")
async def announcement_reads(announcement_id: int, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can view read receipts")
    restaurant_id = await _restaurant_id_for(user["id"])
    announcement = await db.pool.fetchrow("SELECT id, author_id FROM announcements WHERE id = $1 AND resto_id = $2", announcement_id, restaurant_id)
    if not announcement:
        raise HTTPException(404, detail="Announcement not found")
    rows = await db.pool.fetch(
        """SELECT u.id, u.name, ar.read_at FROM users u
           LEFT JOIN announcement_reads ar ON ar.announcement_id = $1 AND ar.employee_id = u.id
           WHERE u.restaurant_id = $2 AND u.id <> $3
           ORDER BY ar.read_at IS NULL DESC, u.name""",
        announcement_id, restaurant_id, announcement["author_id"],
    )
    return [
        {"employeeId": row["id"], "name": row["name"], "read": row["read_at"] is not None,
         "readAt": row["read_at"].isoformat() if row["read_at"] else None}
        for row in rows
    ]


@router.delete("/api/announcements/{announcement_id}")
async def delete_announcement(announcement_id: int, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can delete announcements")
    restaurant_id = await _restaurant_id_for(user["id"])
    row = await db.pool.fetchrow("DELETE FROM announcements WHERE id = $1 AND resto_id = $2 RETURNING id", announcement_id, restaurant_id)
    if not row:
        raise HTTPException(404, detail="Announcement not found")
    return {"id": announcement_id, "deleted": True}
