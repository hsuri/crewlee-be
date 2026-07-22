from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from app.core.config import RAG_TOP_K
from app.core.security import require_user
from app.db import session as db
from app.models.schemas import RagQueryRequest
from app.services import rag

router = APIRouter()

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB -- generous for a text-based SOP/recipe/training doc


async def _restaurant_id_for(user_id: int) -> int:
    if not db.pool:
        raise HTTPException(503, detail="Database unavailable")
    restaurant_id = await db.pool.fetchval("SELECT restaurant_id FROM users WHERE id = $1", user_id)
    if not restaurant_id:
        raise HTTPException(404, detail="Restaurant membership not found")
    return restaurant_id


async def _read_and_extract(file: UploadFile) -> tuple[bytes, str, str]:
    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(422, detail="File is too large (max 20MB)")
    try:
        content, file_type = rag.extract_text(file.filename or "", data)
    except ValueError as e:
        raise HTTPException(422, detail=str(e))
    if not content:
        raise HTTPException(422, detail="Couldn't find any text in this file -- scanned/image-only PDFs aren't supported yet")
    return data, content, file_type


async def _prepare_chunks(content: str) -> tuple[list[str], list[list[float]]]:
    """Chunks + embeds up front, before any GCS/DB write for this document -- so a rate-limited
    or unreachable embedding service aborts cleanly (nothing written) rather than leaving a
    document's metadata/file updated while its chunks (and citations) still reflect the old
    content, which is what happened here before this was split out.
    """
    chunks = rag.chunk_text(content)
    if not chunks:
        raise HTTPException(422, detail="Document content is empty after chunking")
    try:
        embeddings = rag.embed_documents(chunks)
    except Exception as e:
        raise HTTPException(502, detail=f"Embedding service unavailable: {e}")
    return chunks, embeddings


async def _write_chunks(document_id: int, resto_id: int, chunks: list[str], embeddings: list[list[float]]) -> None:
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
        """SELECT d.id, d.title, d.doc_type, d.visibility, d.original_filename, d.file_type,
                  d.created_at, d.updated_at, u.name AS uploaded_by_name
           FROM rag_documents d JOIN users u ON u.id = d.uploaded_by
           WHERE d.resto_id = $1 ORDER BY d.updated_at DESC""",
        restaurant_id,
    )
    return [
        {"id": r["id"], "title": r["title"], "docType": r["doc_type"], "visibility": r["visibility"],
         "originalFilename": r["original_filename"], "fileType": r["file_type"],
         "uploadedByName": r["uploaded_by_name"],
         "createdAt": r["created_at"].isoformat(), "updatedAt": r["updated_at"].isoformat()}
        for r in rows
    ]


@router.get("/api/rag/documents/{document_id}")
async def get_document(document_id: int, user: dict = Depends(require_user)):
    restaurant_id = await _restaurant_id_for(user["id"])
    row = await db.pool.fetchrow(
        """SELECT d.id, d.title, d.doc_type, d.visibility, d.content, d.original_filename, d.file_type,
                  d.created_at, d.updated_at, u.name AS uploaded_by_name
           FROM rag_documents d JOIN users u ON u.id = d.uploaded_by
           WHERE d.id = $1 AND d.resto_id = $2""",
        document_id, restaurant_id,
    )
    if not row:
        raise HTTPException(404, detail="Document not found")
    return {
        "id": row["id"], "title": row["title"], "docType": row["doc_type"], "visibility": row["visibility"],
        "content": row["content"], "originalFilename": row["original_filename"], "fileType": row["file_type"],
        "uploadedByName": row["uploaded_by_name"],
        "createdAt": row["created_at"].isoformat(), "updatedAt": row["updated_at"].isoformat(),
    }


@router.get("/api/rag/documents/{document_id}/file")
async def download_document_file(document_id: int, user: dict = Depends(require_user)):
    restaurant_id = await _restaurant_id_for(user["id"])
    row = await db.pool.fetchrow(
        "SELECT gcs_path, original_filename, file_type FROM rag_documents WHERE id = $1 AND resto_id = $2",
        document_id, restaurant_id,
    )
    if not row or not row["gcs_path"]:
        raise HTTPException(404, detail="Document not found")
    data = rag.download_file(row["gcs_path"])
    return Response(
        content=data,
        media_type=rag.FILE_CONTENT_TYPES[row["file_type"]],
        headers={"Content-Disposition": f'attachment; filename="{row["original_filename"]}"'},
    )


@router.post("/api/rag/documents")
async def create_document(
    title: str = Form(...),
    docType: str = Form("other"),
    file: UploadFile = File(...),
    user: dict = Depends(require_user),
):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can add documents")
    if not title.strip():
        raise HTTPException(422, detail="Title is required")
    data, content, file_type = await _read_and_extract(file)
    chunks, embeddings = await _prepare_chunks(content)  # fail fast before any GCS/DB write
    restaurant_id = await _restaurant_id_for(user["id"])
    gcs_path = rag.upload_file(restaurant_id, file.filename, data, file_type)
    row = await db.pool.fetchrow(
        """INSERT INTO rag_documents (resto_id, uploaded_by, title, doc_type, content, original_filename, file_type, gcs_path)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id, created_at, updated_at""",
        restaurant_id, user["id"], title.strip(), docType, content, file.filename, file_type, gcs_path,
    )
    await _write_chunks(row["id"], restaurant_id, chunks, embeddings)
    return {"id": row["id"], "createdAt": row["created_at"].isoformat(), "updatedAt": row["updated_at"].isoformat()}


@router.put("/api/rag/documents/{document_id}")
async def update_document(
    document_id: int,
    title: str = Form(...),
    docType: str = Form("other"),
    file: UploadFile = File(None),
    user: dict = Depends(require_user),
):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can modify documents")
    if not title.strip():
        raise HTTPException(422, detail="Title is required")
    restaurant_id = await _restaurant_id_for(user["id"])
    existing = await db.pool.fetchrow(
        "SELECT gcs_path FROM rag_documents WHERE id = $1 AND resto_id = $2", document_id, restaurant_id,
    )
    if not existing:
        raise HTTPException(404, detail="Document not found")

    # A new file is optional here: without one, this just renames/retypes the document in
    # place -- no re-extraction, re-upload, or re-chunk/embed needed for a pure metadata edit.
    if file is None:
        row = await db.pool.fetchrow(
            """UPDATE rag_documents SET title = $3, doc_type = $4, updated_at = now()
               WHERE id = $1 AND resto_id = $2 RETURNING id, updated_at""",
            document_id, restaurant_id, title.strip(), docType,
        )
        return {"id": row["id"], "updatedAt": row["updated_at"].isoformat()}

    data, content, file_type = await _read_and_extract(file)
    chunks, embeddings = await _prepare_chunks(content)  # fail fast before touching GCS/DB
    gcs_path = rag.upload_file(restaurant_id, file.filename, data, file_type)
    row = await db.pool.fetchrow(
        """UPDATE rag_documents SET title = $3, doc_type = $4, content = $5, original_filename = $6,
                  file_type = $7, gcs_path = $8, updated_at = now()
           WHERE id = $1 AND resto_id = $2 RETURNING id, updated_at""",
        document_id, restaurant_id, title.strip(), docType, content, file.filename, file_type, gcs_path,
    )
    await _write_chunks(document_id, restaurant_id, chunks, embeddings)
    # Only delete the old blob once the new one is fully committed in Postgres too -- so a
    # failure anywhere above never leaves the document with zero valid backing files.
    if existing["gcs_path"] and existing["gcs_path"] != gcs_path:
        rag.delete_file(existing["gcs_path"])
    return {"id": row["id"], "updatedAt": row["updated_at"].isoformat()}


@router.delete("/api/rag/documents/{document_id}")
async def delete_document(document_id: int, user: dict = Depends(require_user)):
    if user["role"] != "manager":
        raise HTTPException(403, detail="Only managers can delete documents")
    restaurant_id = await _restaurant_id_for(user["id"])
    row = await db.pool.fetchrow(
        "DELETE FROM rag_documents WHERE id = $1 AND resto_id = $2 RETURNING id, gcs_path", document_id, restaurant_id,
    )
    if not row:
        raise HTTPException(404, detail="Document not found")
    if row["gcs_path"]:
        rag.delete_file(row["gcs_path"])
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
