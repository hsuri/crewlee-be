#!/usr/bin/env bash
# ── One-time GCP setup for crewlee-api (backend) ──────────────────────────────
#
# Usage:  ./scripts/setup.sh
#
# What this does:
#   1. Sets gcloud to the GCP project
#   2. Enables required APIs (idempotent — safe to re-run)
#   3. Grants Cloud SQL client role to the Cloud Run service account
#   4. Grants Cloud Build the permissions needed to deploy Cloud Run
#
# The Cloud SQL instance and database are shared with crewlee-fe.
# Run crewlee-fe/scripts/setup.sh first if you haven't already.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PROJECT_ID=$(python3 -c "import config; print(config.GCP_PROJECT_ID)")
REGION=$(python3    -c "import config; print(config.GCP_REGION)")
SERVICE_NAME=$(python3 -c "import config; print(config.SERVICE_NAME)")

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Setup: $SERVICE_NAME"
echo "  GCP Project: $PROJECT_ID  |  Region: $REGION"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── 1. Set project ─────────────────────────────────────────────────────────────
echo "[1/4] Setting gcloud project to $PROJECT_ID..."
gcloud config set project "$PROJECT_ID"

# ── 2. Enable APIs ─────────────────────────────────────────────────────────────
echo "[2/4] Enabling required APIs..."
gcloud services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  cloudbuild.googleapis.com \
  containerregistry.googleapis.com \
  --project="$PROJECT_ID"
echo "      APIs enabled."

# ── 3. IAM for Cloud Run + Cloud Build ─────────────────────────────────────────
echo "[3/4] Setting IAM permissions..."

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
CLOUD_RUN_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
CLOUD_BUILD_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

for SA in "$CLOUD_RUN_SA" "$CLOUD_BUILD_SA"; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA}" \
    --role="roles/cloudsql.client" \
    --quiet 2>/dev/null || true
done

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${CLOUD_BUILD_SA}" \
  --role="roles/run.admin" \
  --quiet 2>/dev/null || true

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${CLOUD_BUILD_SA}" \
  --role="roles/iam.serviceAccountUser" \
  --quiet 2>/dev/null || true

echo "      IAM configured."

# ── 4. Done ────────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "[4/4] Setup complete!"
echo ""
echo "  NEXT STEP: run deploy.sh"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
