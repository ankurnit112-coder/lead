# Autonomous Daily Lead Prospector & Cold Outreach Pipeline

Fully autonomous daily engine that scrapes global B2B prospects via **Apify**, scores them with
**Llama 3.1 8B Instruct via Hugging Face** (`meta-llama/Meta-Llama-3.1-8B-Instruct`), drafts bespoke <110-word cold emails,
deduplicates against history, and logs strictly the **top 10** leads/day into Google Sheets.
Runs at **08:00 UTC daily** via GitHub Actions.

## How it works

1. **Dedup memory (Sheets first):** reads Column D (`Website URL`) from the worksheet
   `Outreach Pipeline - Daily Top 10` into an in-memory domain set.
2. **Multi-region scraping (Apify):** triggers `compass/crawler-google-places`
   (overridable via `APIFY_ACTOR_ID`) across 5 rotated niche × market combos per day
   (US, UK, AU, CA, DE, FR, NL, IE), targeting 40–50 candidates. Drops entries without a
   valid website/email; backfills missing emails by scraping the root domain homepage +
   `/contact` with regex. Drops anything matching the dedup set.
3. **Fit evaluation (HF JSON mode):** each candidate → `offer_angle`
   (`Authority Website` | `AI Growth Website`), `quality_score` 0–100, one-sentence
   `pain_point`. Sorted descending, strictly top 10 kept.
4. **Bespoke drafting (HF JSON mode):** per top-10 lead, peer-to-peer note
   (<110 words, banned clichés removed, specific bottleneck → commercial cost → soft
   Loom-preview CTA) + 3–6 word lowercase/sentence-case subject.
5. **Sheets sync:** appends 10 rows with columns
   `Date Added | Company Name | Country/Region | Website URL | Contact Email | Offer Angle | Quality Score | Core Pain Point | Subject Line | Custom Email Pitch | Status`
   with `Status = "Pending Review"`.

One bad record never kills the batch: per-prospect try/except, exponential-backoff retries
on Hugging Face + Apify, and strict typing/logging throughout.

## Repo layout

```text
pipeline.py                         # full pipeline (Steps 3.1–3.5)
requirements.txt                    # pinned deps
.github/workflows/daily_pipeline.yml  # cron 0 8 * * * + secret injection
README.md
```

## Prerequisites

- Python 3.11+
- Apify account + token ([console.apify.com](https://console.apify.com) → Settings → Integrations → API tokens)
- Hugging Face account + token ([huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) → Create token with Inference permission) + access to the gated `meta-llama/Meta-Llama-3.1-8B-Instruct` model (open the model page while logged in and accept the license)
- Google Cloud project + Service Account + a Google Sheet
- GitHub repo to host this code

## 1. Google Cloud Service Account setup

1. Go to [Google Cloud Console](https://console.cloud.google.com) → create/select a project.
2. **APIs & Services → Library:** enable **Google Sheets API** and **Google Drive API**.
3. **IAM & Admin → Service Accounts → Create Service Account:**
   - Name e.g. `lead-prospector`, role: none needed (least privilege).
   - Open the account → **Keys → Add Key → Create new key → JSON** → downloads
     `service-account.json`.
4. Copy the `client_email` from that JSON (looks like `lead-prospector@<project>.iam.gserviceaccount.com`).
5. Create your Google Sheet (e.g. `Lead Pipeline`), and **Share → add the `client_email`
   as Editor**. Without this, the API gets 403/404.
6. Get the **Spreadsheet ID** from the sheet URL:
   `https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit`.
   The pipeline auto-creates the worksheet `Outreach Pipeline - Daily Top 10` with the
   correct headers on first run (Column D = Website URL is the dedup key — do not reorder).

## 2. Local setup

```powershell
# Windows PowerShell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Set env vars (PowerShell — current session only)
$env:APIFY_TOKEN = "apify_api_..."
$env:HF_TOKEN = "hf_..."
$env:GOOGLE_SHEET_ID = "<SPREADSHEET_ID>"
# Entire JSON file content as a single-line string:
$env:GOOGLE_SERVICE_ACCOUNT_JSON = (Get-Content -Raw service-account.json)

# Optional overrides
$env:GOOGLE_WORKSHEET_NAME = "Outreach Pipeline - Daily Top 10"
$env:APIFY_ACTOR_ID = "compass/crawler-google-places"
$env:HF_MODEL = "meta-llama/Meta-Llama-3.1-8B-Instruct"
$env:MAX_CANDIDATES = "50"

python pipeline.py
```

Dry run (no Sheets write, prints rows to stdout — useful before sharing the sheet):

```powershell
$env:DRY_RUN = "true"
python pipeline.py
```

Bash equivalent:

```bash
export APIFY_TOKEN="apify_api_..." HF_TOKEN="hf_..." GOOGLE_SHEET_ID="<ID>"
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat service-account.json)"
python pipeline.py
```

## 3. Deploy to GitHub (scheduled daily run)

1. Push these files to a GitHub repo.
2. **Repo → Settings → Secrets and variables → Actions → New repository secret**, add:
   | Secret | Value |
   |---|---|
   | `APIFY_TOKEN` | your Apify token |
   | `HF_TOKEN` | your Hugging Face token (with Inference access) |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | full contents of `service-account.json` (raw JSON, not a path) |
   | `GOOGLE_SHEET_ID` | spreadsheet ID from the URL |
   | `GOOGLE_WORKSHEET_NAME` _(optional)_ | defaults to `Outreach Pipeline - Daily Top 10` |
   | `APIFY_ACTOR_ID` _(optional)_ | defaults to `compass/crawler-google-places` |
3. The workflow `.github/workflows/daily_pipeline.yml` is already configured with
   `schedule: cron '0 8 * * *'` (08:00 UTC daily) plus `workflow_dispatch` for manual runs.
4. Test: **Actions tab → Daily Lead Prospector → Run workflow**, then check the new
   10 rows in your sheet.
5. Note: GitHub disables scheduled workflows after 60 days of repo inactivity — trigger a
   manual run or push to re-enable.

## Environment variable reference

| Var | Required | Default | Purpose |
|---|---|---|---|
| `APIFY_TOKEN` | yes | — | Apify API auth |
| `HF_TOKEN` | yes | — | Hugging Face/LLM auth |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | yes (unless `DRY_RUN`) | — | Service-account JSON string |
| `GOOGLE_SHEET_ID` | yes (unless `DRY_RUN`) | — | Target spreadsheet (or set `GOOGLE_SHEET_NAME`) |
| `GOOGLE_SHEET_NAME` | alt to ID | — | Spreadsheet title fallback |
| `GOOGLE_WORKSHEET_NAME` | no | `Outreach Pipeline - Daily Top 10` | Worksheet tab |
| `APIFY_ACTOR_ID` | no | `compass/crawler-google-places` | Scraper actor |
| `HF_MODEL` | no | `meta-llama/Meta-Llama-3.1-8B-Instruct` | Scoring/drafting model |
| `MAX_CANDIDATES` | no | `50` | Scrape batch size |
| `DRY_RUN` | no | `false` | `true` = skip Sheets write |

## Notes & limits

- Llama calls always use `response_format={"type": "json_object"}` to avoid markdown breakage.
- The Meta Llama 3.1 model on Hugging Face is gated: you must accept its license while logged in, otherwise inference returns 403/404.
- Apify actor input includes `scrapeContacts/scrapeEmails`; email backfill is best-effort
  homepage scraping — expect some candidates to be dropped for missing emails (by design).
- Never commit `service-account.json` or keys — secrets live only in env vars / GitHub Secrets.
