"""RAG knowledge-base service: chunking, embedding, and answer generation.

Chunking is structure-aware (splits on blank lines and header-like lines) rather than a
fixed-size sliding window -- recipes and SOPs read as ordered steps, so slicing mid-step
would hand the model a fragment that doesn't make sense pulled out of context.
"""
import os
import re
import uuid
from io import BytesIO
from typing import Optional

import anthropic
import docx
import pypdf
import voyageai
from google.cloud import storage

from app.core.config import GCP_PROJECT_ID, RAG_CHUNK_MAX_CHARS, RAG_EMBEDDING_MODEL, RAG_GENERATION_MODEL

_HEADER_RE = re.compile(r"^(#{1,6}\s+.+|[A-Z][A-Z0-9 /&'-]{2,60}:?)$")

FILE_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
}

_voyage_client: Optional[voyageai.Client] = None
_anthropic_client: Optional[anthropic.Anthropic] = None
_storage_client: Optional[storage.Client] = None


def chunk_text(content: str, max_chars: int = RAG_CHUNK_MAX_CHARS) -> list[str]:
    """Merge blank-line-separated paragraphs into chunks up to max_chars. A line that looks
    like a header (markdown `#...` or a short all-caps label) always starts a new chunk. A
    single paragraph longer than max_chars is hard-split on sentence boundaries as a fallback.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", content.strip()) if p.strip()]
    chunks: list[str] = []
    current = ""

    def flush():
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for para in paragraphs:
        is_header = bool(_HEADER_RE.match(para.splitlines()[0]))
        if is_header and current:
            flush()
        if len(para) > max_chars:
            flush()
            piece = ""
            for sentence in re.split(r"(?<=[.!?])\s+", para):
                if piece and len(piece) + len(sentence) + 1 > max_chars:
                    chunks.append(piece.strip())
                    piece = ""
                piece = f"{piece} {sentence}".strip()
            if piece:
                chunks.append(piece.strip())
            continue
        candidate = f"{current}\n\n{para}".strip() if current else para
        if len(candidate) > max_chars:
            flush()
            current = para
        else:
            current = candidate
    flush()
    return chunks


def _voyage() -> voyageai.Client:
    global _voyage_client
    if _voyage_client is None:
        _voyage_client = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])
    return _voyage_client


def embed_documents(texts: list[str]) -> list[list[float]]:
    return _voyage().embed(texts, model=RAG_EMBEDDING_MODEL, input_type="document").embeddings


def embed_query(text: str) -> list[float]:
    return _voyage().embed([text], model=RAG_EMBEDDING_MODEL, input_type="query").embeddings[0]


def _anthropic() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic()
    return _anthropic_client


def answer_question(question: str, matches: list[dict]) -> dict:
    """matches: top-k retrieved chunks as {"title", "content"}. Each becomes its own citable
    `document` content block, so a citation always traces back to one specific chunk/source.
    """
    document_blocks = [
        {
            "type": "document",
            "source": {"type": "text", "media_type": "text/plain", "data": m["content"]},
            "title": m["title"],
            "citations": {"enabled": True},
        }
        for m in matches
    ]
    response = _anthropic().messages.create(
        model=RAG_GENERATION_MODEL,
        max_tokens=2048,
        system=(
            "You are a helpful assistant answering a restaurant employee's question using only "
            "the provided documents (recipes, SOPs, training material, licenses). If the answer "
            "isn't in the documents, say so plainly rather than guessing."
        ),
        messages=[{
            "role": "user",
            "content": [*document_blocks, {"type": "text", "text": question}],
        }],
    )
    answer_parts = []
    citations = []
    for block in response.content:
        if block.type != "text":
            continue
        answer_parts.append(block.text)
        for c in (block.citations or []):
            citations.append({"documentTitle": c.document_title, "citedText": c.cited_text})
    return {"answer": "".join(answer_parts), "citations": citations}


def to_vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"


def extract_text(filename: str, data: bytes) -> tuple[str, str]:
    """Returns (extracted_text, file_type). file_type drives both the DB's file_type column
    and the GCS object's content-type -- raises ValueError for anything but pdf/docx/txt.

    The FE's "paste text" option isn't a separate code path -- it wraps the pasted string as a
    client-side File named *.txt and posts it through the same multipart upload, so it flows
    through exactly the same extract/chunk/embed/GCS-store pipeline as a real file (and gets a
    real downloadable original, same as PDF/DOCX).
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "pdf":
        reader = pypdf.PdfReader(BytesIO(data))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages).strip(), "pdf"
    if ext == "docx":
        document = docx.Document(BytesIO(data))
        return "\n\n".join(p.text for p in document.paragraphs if p.text.strip()).strip(), "docx"
    if ext == "txt":
        return data.decode("utf-8", errors="replace").strip(), "txt"
    raise ValueError(f"Unsupported file type: .{ext or 'unknown'} (only .pdf, .docx, and .txt are supported)")


def _storage() -> storage.Client:
    global _storage_client
    if _storage_client is None:
        _storage_client = storage.Client(project=GCP_PROJECT_ID)
    return _storage_client


def _bucket() -> storage.Bucket:
    return _storage().bucket(os.environ.get("RAG_BUCKET_NAME", "crewlee-rag-docs-local"))


def upload_file(resto_id: int, filename: str, data: bytes, file_type: str) -> str:
    # Keyed by a fresh uuid rather than the document's id, so a create/update can upload the
    # new blob and confirm it succeeded *before* touching Postgres at all or deleting whatever
    # blob (if any) it's replacing -- see _prepare_chunks in app/api/routes/rag.py for why.
    gcs_path = f"{resto_id}/{uuid.uuid4()}/{filename}"
    _bucket().blob(gcs_path).upload_from_string(data, content_type=FILE_CONTENT_TYPES[file_type])
    return gcs_path


def download_file(gcs_path: str) -> bytes:
    return _bucket().blob(gcs_path).download_as_bytes()


def delete_file(gcs_path: str) -> None:
    _bucket().blob(gcs_path).delete()
