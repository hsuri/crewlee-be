from fastapi import APIRouter

from app.core.config import DB_FIELDS, PROJECT_NAME, PROJECT_SLUG

router = APIRouter()


@router.get("/api/config/public")
async def public_config():
    return {
        "project": {"name": PROJECT_NAME, "slug": PROJECT_SLUG},
        "fields": DB_FIELDS,
    }
