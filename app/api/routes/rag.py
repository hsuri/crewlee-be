from fastapi import APIRouter, Depends, HTTPException

from app.core.config import RAG_TOP_K
from app.core.security import require_user
from app.db import session as db
from app.models.schemas import RagDocumentCreateRequest, RagDocumentUpdateRequest, RagQueryRequest
from app.services import rag

router = APIRouter()


async def _restaurant_id_for(user_id: int) -> int:
    if not db.pool:
        raise HTTPException(503, detail="Database unavailable")
    restaurant_id = await db.pool.fetchval("SELECT restaurant_id FROM users WHERE id = $1", user_id)
    if not restaurant_id:
        raise HTTPException(404, detail="Restaurant membership not found")
    return restaurant_id


async def _rechunk_and_embed(document_id: int, resto_id: int, content: str) -> None:
    chunks = rag.chunk_text(content)
    if not chunks:
        raise HTTPException(422, detail="Document content is empty after chunking")
    try:
        embeddings = rag.embed_documents(chunks)
    except Exception as e:
        raise HTTPException(502, detail=f"Embedding service unavailable: {e}")
    await db.pool.execute("DELETE FROM rag_chunks WHERE document_id = $1", document_id)
    for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        await db.pool.execute(
            """INSERT INTO rag_chunks (document_id, resto_id, chunk_index, content, embedding)
               VALUES ($1, $2, $3, $4, $5::vector)""",
            document_id, resto_id, i, chunk, rag.to_vector_literal(embedding),
        )


@router.get("/api/rag/documents")
async def list_documents(user: dict = Depends(require_user)):
    restaurant_id = await _restaurant_id_for(user["id"])
    rows = await db.pool.fetch(
        """SELECT d.id, d.title, d.doc_type, d.visibility, d.created_at, d.updated_at, u.name AS uploaded_by_name
           FROM rag_documents d JOIN users u ON u.id = d.uploaded_by
           WHERE d.resto_id = $1 ORDER BY d.updated_at DESC""",
        restaurant_id,
    )
    return [
        {"id": r["id"], "title": r["title"], "docType": r["doc_type"], "visibility": r["visibility"],
         "uploadedByName": r["uploaded_by_name"],
         "createdAt": r["created_at"].isoformat(), "updatedAt": r["updated_at"].isoformat()}
        for r in rows
    ]


@router.get("/api/rag/documents/{document_id}")
async def get_document(document_id: int, user: dict = Depends(require_user)):
    restaurant_id = await _restaurant_id_for(user["id"])
    row = await db.pool.fetchrow(
        """SELECT d.id, d.title, d.doc_type, d.visibility, d.content, d.created_at, d.updated_at, u.name AS uploaded_by_name
           FROM rag_documents d JOIN users u ON u.id = d.uploaded_by
           WHERE d.id = $1 AND d.resto_id = $2""",
        document_id, restaurant_id,
    )
    if not row:
        raise HTTPException(404, detail="Document not found")
    return {
        "id": row["id"], "title": row["title"], "docType": row["doc_type"], "visibility": row["visibility"],
        "content": row["content"], "uploadedByName": row["uploaded_by_name"],
        "createdAt": row["created_at"].isoformat(), "updatedAt": row["updated_at"].isoformat(),
    }


@router.post("/api/rag/documents")
async def create_document(payload: RagDocumentCreateRequest, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can add documents")
    if not payload.title.strip():
        raise HTTPException(422, detail="Title is required")
    if not payload.content.strip():
        raise HTTPException(422, detail="Document content is required")
    restaurant_id = await _restaurant_id_for(user["id"])
    row = await db.pool.fetchrow(
        """INSERT INTO rag_documents (resto_id, uploaded_by, title, doc_type, visibility, content)
           VALUES ($1, $2, $3, $4, $5, $6) RETURNING id, created_at, updated_at""",
        restaurant_id, user["id"], payload.title.strip(), payload.docType, payload.visibility, payload.content.strip(),
    )
    await _rechunk_and_embed(row["id"], restaurant_id, payload.content.strip())
    return {"id": row["id"], "createdAt": row["created_at"].isoformat(), "updatedAt": row["updated_at"].isoformat()}


@router.put("/api/rag/documents/{document_id}")
async def update_document(document_id: int, payload: RagDocumentUpdateRequest, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can modify documents")
    if not payload.title.strip():
        raise HTTPException(422, detail="Title is required")
    if not payload.content.strip():
        raise HTTPException(422, detail="Document content is required")
    restaurant_id = await _restaurant_id_for(user["id"])
    row = await db.pool.fetchrow(
        """UPDATE rag_documents SET title = $3, doc_type = $4, visibility = $5, content = $6, updated_at = now()
           WHERE id = $1 AND resto_id = $2 RETURNING id, updated_at""",
        document_id, restaurant_id, payload.title.strip(), payload.docType, payload.visibility, payload.content.strip(),
    )
    if not row:
        raise HTTPException(404, detail="Document not found")
    await _rechunk_and_embed(document_id, restaurant_id, payload.content.strip())
    return {"id": row["id"], "updatedAt": row["updated_at"].isoformat()}


@router.delete("/api/rag/documents/{document_id}")
async def delete_document(document_id: int, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can delete documents")
    restaurant_id = await _restaurant_id_for(user["id"])
    row = await db.pool.fetchrow(
        "DELETE FROM rag_documents WHERE id = $1 AND resto_id = $2 RETURNING id", document_id, restaurant_id,
    )
    if not row:
        raise HTTPException(404, detail="Document not found")
    return {"id": document_id, "deleted": True}


@router.post("/api/rag/query")
async def query_documents(payload: RagQueryRequest, user: dict = Depends(require_user)):
    if not payload.question.strip():
        raise HTTPException(422, detail="Question is required")
    restaurant_id = await _restaurant_id_for(user["id"])
    has_any = await db.pool.fetchval("SELECT EXISTS(SELECT 1 FROM rag_chunks WHERE resto_id = $1)", restaurant_id)
    if not has_any:
        return {"answer": "There's nothing in the knowledge base yet -- ask a manager to add some documents first.", "citations": []}
    try:
        question_embedding = rag.embed_query(payload.question.strip())
    except Exception as e:
        raise HTTPException(502, detail=f"Embedding service unavailable: {e}")
    rows = await db.pool.fetch(
        """SELECT c.content, d.title
           FROM rag_chunks c JOIN rag_documents d ON d.id = c.document_id
           WHERE c.resto_id = $1
           ORDER BY c.embedding <=> $2::vector
           LIMIT $3""",
        restaurant_id, rag.to_vector_literal(question_embedding), RAG_TOP_K,
    )
    matches = [{"title": r["title"], "content": r["content"]} for r in rows]
    try:
        return rag.answer_question(payload.question.strip(), matches)
    except Exception as e:
        raise HTTPException(502, detail=f"AI service unavailable: {e}")
