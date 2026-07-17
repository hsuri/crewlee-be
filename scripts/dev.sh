#!/usr/bin/env bash
# Start the API locally with hot-reload.
# Prerequisites: pip install -r requirements.txt (or use a venv)
#
# Usage: ./scripts/dev.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Load .env if present
if [ -f .env.local ]; then
  set -a; source .env.local; set +a
fi

# Activate venv if present
if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
elif [ -f venv/bin/activate ]; then
  source venv/bin/activate
fi

PORT="${PORT:-8001}"
echo "[dev] Starting Crewlee API on port $PORT..."
echo "[dev] DB: ${DATABASE_URL:-not set}"
echo "[dev] CORS: ${ALLOWED_ORIGINS:-http://localhost:3000}"
echo ""

exec uvicorn app.main:app --reload --host 0.0.0.0 --port "$PORT"
