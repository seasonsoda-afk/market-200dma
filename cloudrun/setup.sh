#!/usr/bin/env bash
# Provision a DRY_RUN Cloud Run market updater in a user-owned GCP project.
# Run in Google Cloud Shell after selecting a project with billing enabled.
set -euo pipefail

PROJECT_ID="${1:-}"
REGION="${REGION:-asia-northeast1}"
JOB="market-200dma"
REPOSITORY="seasonsoda-jobs"
SECRET="market-200dma-github-token"
RUNTIME_SA="market-200dma-runtime"
SCHEDULER_SA="market-200dma-scheduler"

if [[ -z "$PROJECT_ID" ]]; then
  echo "Usage: bash cloudrun/setup.sh YOUR_PROJECT_ID" >&2
  exit 2
fi
for cmd in gcloud git; do command -v "$cmd" >/dev/null || { echo "Missing $cmd" >&2; exit 2; }; done

gcloud config set project "$PROJECT_ID"
gcloud services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
RUNTIME_EMAIL="$RUNTIME_SA@$PROJECT_ID.iam.gserviceaccount.com"
SCHEDULER_EMAIL="$SCHEDULER_SA@$PROJECT_ID.iam.gserviceaccount.com"
IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/$JOB:pilot"

if ! gcloud artifacts repositories describe "$REPOSITORY" --location="$REGION" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$REPOSITORY" --repository-format=docker --location="$REGION" --description="Market updater images"
fi

if ! gcloud iam service-accounts describe "$RUNTIME_EMAIL" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$RUNTIME_SA" --display-name="200DMA Cloud Run updater"
fi
if ! gcloud iam service-accounts describe "$SCHEDULER_EMAIL" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SCHEDULER_SA" --display-name="200DMA Cloud Scheduler invoker"
fi

if ! gcloud secrets describe "$SECRET" >/dev/null 2>&1; then
  gcloud secrets create "$SECRET" --replication-policy=automatic
fi
echo
echo "Create a GitHub fine-grained personal access token:"
echo "  Repository: seasonsoda-afk/market-200dma only"
echo "  Permission: Contents = Read and write"
echo "  Keep the token secret; input will not be printed."
read -r -s -p "Paste GitHub token (input hidden): " GITHUB_PAT
echo
if [[ -z "$GITHUB_PAT" ]]; then echo "Token must not be empty" >&2; exit 2; fi
printf '%s' "$GITHUB_PAT" | gcloud secrets versions add "$SECRET" --data-file=-
unset GITHUB_PAT
gcloud secrets add-iam-policy-binding "$SECRET" \
  --member="serviceAccount:$RUNTIME_EMAIL" \
  --role="roles/secretmanager.secretAccessor" >/dev/null

# Build under Cloud Build and push to private Artifact Registry.
# A project may require Cloud Build service-account permissions to be configured;
# the resulting error tells which principal needs Artifact Registry Writer.
gcloud builds submit --config cloudrun/cloudbuild.yaml --substitutions "_REGION=$REGION" .

gcloud run jobs deploy "$JOB" \
  --image="$IMAGE" \
  --region="$REGION" \
  --service-account="$RUNTIME_EMAIL" \
  --set-env-vars="GITHUB_REPO=seasonsoda-afk/market-200dma,DRY_RUN=true" \
  --set-secrets="GITHUB_TOKEN=$SECRET:latest" \
  --tasks=1 --max-retries=0 --task-timeout=5m

gcloud run jobs add-iam-policy-binding "$JOB" \
  --region="$REGION" \
  --member="serviceAccount:$SCHEDULER_EMAIL" \
  --role="roles/run.invoker" >/dev/null

# Cloud Scheduler accesses *.googleapis.com with OAuth.
RUN_URI="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_NUMBER/jobs/$JOB:run"
for PART in "evening-16|15,30,45 16 * * 1-5" "evening-17|0,15,30 17 * * 1-5"; do
  NAME="market-200dma-${PART%%|*}"
  CRON="${PART#*|}"
  gcloud scheduler jobs create http "$NAME" \
    --location="$REGION" \
    --schedule="$CRON" \
    --time-zone="America/New_York" \
    --uri="$RUN_URI" \
    --http-method=POST \
    --headers="Content-Type=application/json" \
    --message-body='{}' \
    --oauth-service-account-email="$SCHEDULER_EMAIL" \
    --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform" \
    --max-retry-attempts=0
done

echo
echo "Pilot configured successfully (DRY_RUN=true)."
echo "Manual test: gcloud run jobs execute $JOB --region $REGION --wait"
echo "After successful comparison, enable writes ONLY when the old writer is disabled:"
echo "  gcloud run jobs update $JOB --region $REGION --update-env-vars DRY_RUN=false"
echo "Do not enable writes or disable the old workflow until verification is complete."
