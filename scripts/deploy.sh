#!/usr/bin/env bash
# ── Deploy crewlee-api to GCP Cloud Run ───────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PROJECT_ID=$(python3   -c "from app.core import config; print(config.GCP_PROJECT_ID)")
REGION=$(python3       -c "from app.core import config; print(config.GCP_REGION)")
SERVICE_NAME=$(python3 -c "from app.core import config; print(config.SERVICE_NAME)")
CLOUD_SQL=$(python3    -c "from app.core import config; print(config.CLOUD_SQL_INSTANCE)")
DB_NAME=$(python3      -c "from app.core import config; print(config.DB_NAME)")

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Deploy: $SERVICE_NAME"
echo "  Project: $PROJECT_ID  |  Region: $REGION"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Load secrets from .env.production via Python (handles special chars safely) ─
# Using a heredoc means bash never interpolates the file contents,
# so characters like ! are passed through as-is.
if [ -f .env.production ]; then
  eval "$(python3 << 'PYEOF'
import shlex

vals = {}
with open('.env.production') as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' in line:
            k, v = line.split('=', 1)
            vals[k.strip()] = v.strip()

for key in ['DB_PASSWORD', 'ADMIN_PASSWORD', 'ALLOWED_ORIGINS', 'VOYAGE_API_KEY', 'ANTHROPIC_API_KEY', 'RAG_BUCKET_NAME']:
    if key in vals:
        print(f'{key}={shlex.quote(vals[key])}')
PYEOF
  )"
  echo "  Loaded secrets from .env.production"
fi

# ── Fall back to prompts for anything still missing ────────────────────────────
if [ -z "${DB_PASSWORD:-}" ]; then
  read -rsp "DB password (postgres user): " DB_PASSWORD; echo ""
fi
if [ -z "${ADMIN_PASSWORD:-}" ]; then
  read -rsp "Admin panel password:        " ADMIN_PASSWORD; echo ""
fi
if [ -z "${VOYAGE_API_KEY:-}" ]; then
  read -rsp "Voyage AI API key:           " VOYAGE_API_KEY; echo ""
fi
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  read -rsp "Anthropic API key:           " ANTHROPIC_API_KEY; echo ""
fi
RAG_BUCKET_NAME="${RAG_BUCKET_NAME:-crewlee-rag-docs}"

# ── ALLOWED_ORIGINS ────────────────────────────────────────────────────────────
if [ -z "${ALLOWED_ORIGINS:-}" ]; then
  FE_URL=$(gcloud run services describe crewlee \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format="value(status.url)" 2>/dev/null || echo "")

  if [ -n "$FE_URL" ]; then
    echo "  Detected frontend URL: $FE_URL"
    ALLOWED_ORIGINS="$FE_URL"
  else
    read -rp "Frontend URL (for CORS): " ALLOWED_ORIGINS
  fi
fi

# ── DATABASE_URL (no password in URL — passed separately as DB_PASSWORD) ───────
DATABASE_URL="postgres://postgres@/${DB_NAME}?host=/cloudsql/${CLOUD_SQL}"

echo ""
echo "  CORS origins: $ALLOWED_ORIGINS"
echo ""
echo "Submitting Cloud Build..."

gcloud builds submit \
  --project="$PROJECT_ID" \
  --config=cloudbuild.yaml \
  --substitutions=\
"_SERVICE_NAME=$SERVICE_NAME,\
_REGION=$REGION,\
_CLOUD_SQL_INSTANCE=$CLOUD_SQL,\
_DATABASE_URL=$DATABASE_URL,\
_DB_PASSWORD=$DB_PASSWORD,\
_ADMIN_PASSWORD=$ADMIN_PASSWORD,\
_ALLOWED_ORIGINS=$ALLOWED_ORIGINS,\
_VOYAGE_API_KEY=$VOYAGE_API_KEY,\
_ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY,\
_RAG_BUCKET_NAME=$RAG_BUCKET_NAME" \
  .

echo ""
echo "Making service publicly accessible..."
gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --member=allUsers \
  --role=roles/run.invoker \
  --quiet 2>/dev/null || echo "  (IAM binding skipped — may already be set)"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
API_URL=$(gcloud run services describe "$SERVICE_NAME" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format="value(status.url)" 2>/dev/null || echo "(check gcloud run services)")
echo "  Deployed: $API_URL"
echo ""
echo "  Use this URL as API_URL when deploying crewlee-fe:"
echo "  API_URL=$API_URL"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
