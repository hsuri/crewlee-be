GCP_PROJECT_ID = "pambii-ai-inc"
GCP_REGION = "us-central1"
SERVICE_NAME = "crewlee-api"
CLOUD_SQL_INSTANCE = "pambii-ai-inc:us-central1:crewlee"
DB_NAME = "crewlee"
DB_TABLE = "waitlist"

PROJECT_NAME = "Crewlee"
PROJECT_SLUG = "crewlee"

# RAG knowledge base: embedding model (Voyage AI) + generation model (Claude), plus the
# retrieval/chunking knobs the service layer reads. RAG_EMBEDDING_DIM must match the
# `vector(N)` size on rag_chunks.embedding in db/schema.sql if the embedding model changes.
RAG_EMBEDDING_MODEL = "voyage-3-lite"
RAG_EMBEDDING_DIM = 512
RAG_GENERATION_MODEL = "claude-sonnet-5"
RAG_CHUNK_MAX_CHARS = 1500
RAG_TOP_K = 8

# Guest AI: same retrieval/generation knobs as the employee assistant, but its own top-k
# (kept equal today, split out because guest grounding is more conservative and may want a
# different k later) and a per-IP rate limit for the public, unauthenticated endpoint.
GUEST_RAG_TOP_K = 8
GUEST_RATE_LIMIT_MAX_REQUESTS = 20
GUEST_RATE_LIMIT_WINDOW_SECONDS = 300

DB_FIELDS = [
    {"name": "name",       "label": "Your Name",       "type": "text",   "required": True},
    {"name": "email",      "label": "Email",           "type": "email",  "required": True},
    {"name": "restaurant", "label": "Restaurant Name", "type": "text",   "required": True},
    {
        "name": "role", "label": "Your Role", "type": "select", "required": True,
        "selectOptions": ["Owner", "General Manager", "Operations Manager", "Other"],
    },
]
