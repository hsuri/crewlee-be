"""Guest AI: a public, unauthenticated food/allergen chat for restaurant customers, and the
authenticated manager endpoints that configure it.

Security model (see crewlee-be/CLAUDE.md for the full writeup):
- The public routes never accept a restaurant id/tenant id from the client -- only a slug,
  resolved server-side to a restaurant row via `_restaurant_for_slug`. There is no path from a
  guest request to any other restaurant's data.
- The single hard boundary between "internal" and "guest-safe" knowledge is the
  `rag_documents.is_guest_visible` column, enforced in `_retrieve_guest_matches`'s SQL WHERE
  clause. A guest's question, however crafted, can only ever cause a retrieval against
  documents a manager explicitly flagged -- prompt injection in the question text can't widen
  that set, because the query embedding/search happens before the LLM ever sees the question.
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.config import GUEST_RAG_TOP_K, GUEST_RATE_LIMIT_MAX_REQUESTS, GUEST_RATE_LIMIT_WINDOW_SECONDS
from app.core.rate_limit import check as rate_limit_check
from app.core.security import require_user
from app.db import session as db
from app.models.schemas import GuestAiSettingsUpdateRequest, GuestKnowledgeVisibilityUpdateRequest, GuestQueryRequest
from app.services import rag

router = APIRouter()

MAX_QUESTION_LENGTH = 500
NOT_ENOUGH_INFO_ANSWER = (
    "I don't have enough information to answer that reliably. Please ask your server -- "
    "they'll be able to help."
)


async def _restaurant_id_for(user_id: int) -> int:
    if not db.pool:
        raise HTTPException(503, detail="Database unavailable")
    restaurant_id = await db.pool.fetchval("SELECT restaurant_id FROM users WHERE id = $1", user_id)
    if not restaurant_id:
        raise HTTPException(404, detail="Restaurant membership not found")
    return restaurant_id


def _require_manager(user: dict) -> None:
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can manage Guest AI")


async def _restaurant_for_slug(slug: str):
    if not db.pool:
        raise HTTPException(503, detail="Database unavailable")
    row = await db.pool.fetchrow("SELECT id, name FROM restaurants WHERE slug = $1", slug)
    if not row:
        raise HTTPException(404, detail="Restaurant not found")
    return row


async def _guest_ai_enabled(restaurant_id: int) -> bool:
    return bool(await db.pool.fetchval(
        "SELECT enabled FROM guest_ai_settings WHERE resto_id = $1", restaurant_id,
    ))


def _client_ip(request: Request) -> str:
    # Best-effort only -- see app/core/rate_limit.py. crewlee-fe's proxy is expected to set
    # X-Forwarded-For (xfwd: true in server.js); we trust its first hop and fall back to the
    # raw connecting socket otherwise. Anyone who can reach this service directly (bypassing
    # the frontend proxy) can spoof this header and dodge the limit entirely -- a real gateway/
    # WAF-level limiter would be needed to close that gap, which is out of scope for this pass.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _retrieve_guest_matches(restaurant_id: int, question: str) -> list[dict]:
    has_any = await db.pool.fetchval(
        """SELECT EXISTS(
               SELECT 1 FROM rag_chunks c JOIN rag_documents d ON d.id = c.document_id
               WHERE c.resto_id = $1 AND d.is_guest_visible = true
           )""",
        restaurant_id,
    )
    if not has_any:
        return []
    try:
        question_embedding = rag.embed_query(question)
    except Exception as e:
        raise HTTPException(502, detail=f"Embedding service unavailable: {e}")
    rows = await db.pool.fetch(
        """SELECT c.content, d.title
           FROM rag_chunks c JOIN rag_documents d ON d.id = c.document_id
           WHERE c.resto_id = $1 AND d.is_guest_visible = true
           ORDER BY c.embedding <=> $2::vector
           LIMIT $3""",
        restaurant_id, rag.to_vector_literal(question_embedding), GUEST_RAG_TOP_K,
    )
    return [{"title": r["title"], "content": r["content"]} for r in rows]


async def _answer_for_guest(restaurant_id: int, question: str) -> dict:
    """Shared by the public chat endpoint and the manager's Test Guest AI -- both must see
    exactly the same retrieval scope and grounding rules, or a test wouldn't mean anything.
    """
    matches = await _retrieve_guest_matches(restaurant_id, question)
    if not matches:
        return {"answer": NOT_ENOUGH_INFO_ANSWER, "citations": [], "hasEnoughInfo": False}
    try:
        result = rag.answer_guest_question(question, matches)
    except Exception as e:
        raise HTTPException(502, detail=f"AI service unavailable: {e}")
    return {"answer": result["answer"], "citations": result["citations"], "hasEnoughInfo": True}


async def _log_guest_query(restaurant_id: int, question: str, answered: bool) -> None:
    if not db.pool:
        return
    await db.pool.execute(
        "INSERT INTO guest_ai_queries (resto_id, question, answered) VALUES ($1, $2, $3)",
        restaurant_id, question, answered,
    )


# ── Public, unauthenticated routes ──────────────────────────────────────────────────────────

@router.get("/api/guest/{slug}")
async def get_guest_info(slug: str):
    restaurant = await _restaurant_for_slug(slug)
    enabled = await _guest_ai_enabled(restaurant["id"])
    return {"restaurantName": restaurant["name"], "enabled": enabled}


@router.post("/api/guest/{slug}/query")
async def query_guest_ai(slug: str, payload: GuestQueryRequest, request: Request):
    question = payload.question.strip()
    if not question:
        raise HTTPException(422, detail="Question is required")
    if len(question) > MAX_QUESTION_LENGTH:
        raise HTTPException(422, detail=f"Question is too long (max {MAX_QUESTION_LENGTH} characters)")

    restaurant = await _restaurant_for_slug(slug)
    if not await _guest_ai_enabled(restaurant["id"]):
        raise HTTPException(403, detail="Guest AI is not currently available at this restaurant")

    rate_key = f"{slug}:{_client_ip(request)}"
    if not rate_limit_check(rate_key, GUEST_RATE_LIMIT_MAX_REQUESTS, GUEST_RATE_LIMIT_WINDOW_SECONDS):
        raise HTTPException(429, detail="Too many questions -- please wait a moment and try again")

    result = await _answer_for_guest(restaurant["id"], question)
    await _log_guest_query(restaurant["id"], question, result["hasEnoughInfo"])
    # Citations are a debugging/trust aid for managers (see the test endpoint below), not
    # something a guest needs to see -- keep their response to just the answer text.
    return {"answer": result["answer"]}


# ── Manager routes (authenticated) ──────────────────────────────────────────────────────────

@router.get("/api/guest-ai/settings")
async def get_guest_ai_settings(user: dict = Depends(require_user)):
    restaurant_id = await _restaurant_id_for(user["id"])
    row = await db.pool.fetchrow(
        "SELECT r.slug, r.name, COALESCE(g.enabled, false) AS enabled "
        "FROM restaurants r LEFT JOIN guest_ai_settings g ON g.resto_id = r.id WHERE r.id = $1",
        restaurant_id,
    )
    return {"enabled": row["enabled"], "slug": row["slug"], "restaurantName": row["name"]}


@router.patch("/api/guest-ai/settings")
async def update_guest_ai_settings(payload: GuestAiSettingsUpdateRequest, user: dict = Depends(require_user)):
    _require_manager(user)
    restaurant_id = await _restaurant_id_for(user["id"])
    await db.pool.execute(
        """INSERT INTO guest_ai_settings (resto_id, enabled, updated_at) VALUES ($1, $2, now())
           ON CONFLICT (resto_id) DO UPDATE SET enabled = $2, updated_at = now()""",
        restaurant_id, payload.enabled,
    )
    return {"enabled": payload.enabled}


@router.get("/api/guest-ai/knowledge")
async def list_guest_knowledge(user: dict = Depends(require_user)):
    _require_manager(user)
    restaurant_id = await _restaurant_id_for(user["id"])
    rows = await db.pool.fetch(
        """SELECT id, title, doc_type, is_guest_visible, updated_at
           FROM rag_documents WHERE resto_id = $1 ORDER BY is_guest_visible DESC, updated_at DESC""",
        restaurant_id,
    )
    return [
        {"id": r["id"], "title": r["title"], "docType": r["doc_type"],
         "isGuestVisible": r["is_guest_visible"], "updatedAt": r["updated_at"].isoformat()}
        for r in rows
    ]


@router.patch("/api/guest-ai/knowledge/{document_id}")
async def update_guest_knowledge_visibility(
    document_id: int, payload: GuestKnowledgeVisibilityUpdateRequest, user: dict = Depends(require_user),
):
    _require_manager(user)
    restaurant_id = await _restaurant_id_for(user["id"])
    row = await db.pool.fetchrow(
        """UPDATE rag_documents SET is_guest_visible = $3, updated_at = now()
           WHERE id = $1 AND resto_id = $2 RETURNING id""",
        document_id, restaurant_id, payload.isGuestVisible,
    )
    if not row:
        raise HTTPException(404, detail="Document not found")
    return {"id": row["id"], "isGuestVisible": payload.isGuestVisible}


@router.post("/api/guest-ai/test-query")
async def test_guest_ai(payload: GuestQueryRequest, user: dict = Depends(require_user)):
    _require_manager(user)
    question = payload.question.strip()
    if not question:
        raise HTTPException(422, detail="Question is required")
    restaurant_id = await _restaurant_id_for(user["id"])
    # Deliberately ignores the enabled flag and rate limit -- a manager needs to be able to
    # test/tune guest-visible knowledge before flipping Guest AI on for real customers.
    return await _answer_for_guest(restaurant_id, question)


@router.get("/api/guest-ai/analytics")
async def get_guest_ai_analytics(user: dict = Depends(require_user)):
    _require_manager(user)
    restaurant_id = await _restaurant_id_for(user["id"])
    today_count = await db.pool.fetchval(
        "SELECT COUNT(*) FROM guest_ai_queries WHERE resto_id = $1 AND created_at >= date_trunc('day', now())",
        restaurant_id,
    )
    week_count = await db.pool.fetchval(
        "SELECT COUNT(*) FROM guest_ai_queries WHERE resto_id = $1 AND created_at >= now() - interval '7 days'",
        restaurant_id,
    )
    unanswered_count = await db.pool.fetchval(
        """SELECT COUNT(*) FROM guest_ai_queries
           WHERE resto_id = $1 AND answered = false AND created_at >= now() - interval '7 days'""",
        restaurant_id,
    )
    top_rows = await db.pool.fetch(
        """SELECT question, COUNT(*) AS n FROM guest_ai_queries
           WHERE resto_id = $1 AND created_at >= now() - interval '7 days'
           GROUP BY question ORDER BY n DESC, MAX(created_at) DESC LIMIT 5""",
        restaurant_id,
    )
    return {
        "questionsToday": today_count,
        "questionsThisWeek": week_count,
        "unansweredThisWeek": unanswered_count,
        "topQuestions": [{"question": r["question"], "count": r["n"]} for r in top_rows],
    }
