# Cloud Run migration – 200DMA app

This directory contains a *pilot* replacement for the GitHub Actions schedule.
Do not disable the existing `.github/workflows/update-market-data.yml` before
the external job is deployed and several market sessions have been compared.

## Behavior
- Uses the existing Yahoo Finance ^GSPC / ^NDX daily close inputs.
- Adds **only new trading dates** and preserves existing observed closing levels.
- Recomputes 200-trading-day averages and writes existing `history.json` and
  `data.json` formats consumed by GitHub Pages.
- Publishes prices before trying FRED DFF; FRED failures do not block price output.
- **DRY_RUN=true by default**, so no GitHub writes occur until explicitly enabled.
- Runs idempotently: if no newer common index date is available, it skips price writes.
- Does not independently prove Yahoo's early after-close daily candle is final.
  Compare multiple days with official closing levels before disabling the old updater.

## Build and deploy
From repository root, with a Google Cloud project and billing enabled:

```sh
PROJECT_ID="your-gcp-project-id"
REGION="asia-northeast1"
gcloud config set project "$PROJECT_ID"
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com

gcloud artifacts repositories create seasonsoda-jobs --repository-format=docker --location="$REGION"
gcloud builds submit --config cloudrun/cloudbuild.yaml --substitutions "_REGION=$REGION" .
```

Create a **fine-grained GitHub token** restricted to
`seasonsoda-afk/market-200dma` with `Contents: Read and write`. Store it in
Google Secret Manager; never commit it to this repository.

Create a Cloud Run Job using the above image. Set:
- `GITHUB_REPO=seasonsoda-afk/market-200dma`
- `DRY_RUN=true` initially
- `GITHUB_TOKEN` from Secret Manager
- max retries: 0; single task
- Service account with only Secret Manager Secret Accessor on that secret.

Schedule via Cloud Scheduler (timezone `America/New_York`) at 16:15, 16:30,
16:45, 17:00, 17:15 and 17:30 on weekdays. This requires two cron schedules:
`15,30,45 16 * * 1-5` and `0,15,30 17 * * 1-5`.
Point both schedules at the same Cloud Run job with an OAuth-authenticated service
account that has `roles/run.invoker`.

## Rollout checklist
1. Deploy with DRY_RUN=true, check Cloud Logging for successful Yahoo dates.
2. Verify index levels / SMA against the existing app for several sessions.
3. Enable writes only after handling conflicts with the existing GitHub updater;
   do not leave two production writers active indefinitely.
4. Compare GitHub Pages visibility and data-file dates.
5. Disable the GitHub Actions `schedule` trigger after confirmed cutover, while
   retaining `workflow_dispatch` for rollback.
6. Confirm Google Cloud costs and scheduler alerts.

Caveat: during pilot, jobs may be triggered multiple times. Cloud Run and Cloud
Scheduler are not hard real-time services. Yahoo is an unofficial data feed.
