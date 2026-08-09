# Relay

Personal second brain for prospect research and outreach. Streamlit UI + Gemini 2.0 Flash (with native Google Search grounding) + Apollo / ZoomInfo / RocketReach + Gmail + Apps Script scheduler + Netlify open/click tracking + Google Sheet database + local Chroma memory.

## Setup

### 1) Gemini API key (required)

1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. Create an API key
3. Free tier: ~1,500 requests/day, includes Google Search grounding
4. Set `GEMINI_API_KEY=AIza...` and optionally `GEMINI_MODEL=gemini-2.0-flash`

### 2) Prospect provider keys

- **Apollo**: apollo.io → Settings → Integrations → API → `APOLLO_API_KEY`
- **ZoomInfo** (optional, enterprise): `ZOOMINFO_USERNAME` + `ZOOMINFO_PASSWORD`
- **RocketReach**: rocketreach.co → Account → API → `ROCKETREACH_API_KEY`

### 3) Gmail OAuth

1. [console.cloud.google.com](https://console.cloud.google.com) → new project
2. Enable **Gmail API**
3. OAuth consent screen → External → add yourself as a test user
4. Credentials → OAuth Client ID → **Desktop App**
5. Download JSON to `./credentials/gmail_oauth.json`

### 4) Google Sheet database

1. Create a new Google Sheet
2. Copy the ID from the URL (`https://docs.google.com/spreadsheets/d/<ID>/edit`)
3. Set `GOOGLE_SHEET_ID=<ID>`
4. Or run `python scripts/bootstrap_google.py` (creates the Sheet + prints next Apps Script steps)

### 5) Environment

```bash
copy .env.example .env
# fill GEMINI_API_KEY and any provider keys you have
# leave APPS_SCRIPT_TRACKING_URL and TRACKING_BASE_URL blank until deploy
```

### 6) Python deps

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 7) Run Streamlit

```bash
streamlit run app.py
```

First Gmail action opens a browser for OAuth consent and writes `./credentials/gmail_token.json`.

### 8) Deploy Apps Script

1. [script.google.com](https://script.google.com) → New project
2. Paste `apps_script/Code.gs`
3. Set `SHEET_ID` and `TRACKING_BASE` at the top
4. Run `setup()`, then `installTrigger()`, then `installReplyWatcher()`
5. Deploy → New deployment → **Web App**
   - Execute as: **Me**
   - Who has access: **Anyone**
6. Copy the Web App URL into `.env` as `APPS_SCRIPT_TRACKING_URL`

You can also use `clasp` if logged in:

```bash
npm i -g @google/clasp
clasp login
cd apps_script
clasp create --type standalone --title "Relay Scheduler" --rootDir .
# then clasp push && clasp deploy
```

### 9) Deploy Netlify tracking

Repo: [github.com/durgappcmc-spec/durgaemailer](https://github.com/durgappcmc-spec/durgaemailer) (already contains this project)

1. In Netlify → Add new site → Import from Git → select `durgappcmc-spec/durgaemailer`
2. Set **Base directory** to `tracking-netlify` (important — functions live there)
3. Site settings → Environment variables:
   - `APPS_SCRIPT_LOG_URL` = your Apps Script Web App URL
   - `CLICK_FALLBACK_URL` = `https://karunamedia.org` (or your site)
4. Deploy; copy the site URL into:
   - `.env` → `TRACKING_BASE_URL`
   - Apps Script → `TRACKING_BASE` constant
5. Redeploy the Apps Script Web App after updating `TRACKING_BASE`

### 10) Public app URL (from Git — no PC tunnel)

**Netlify cannot run the Python app.** Deploy from the same GitHub repo on Render:

1. One-click: https://render.com/deploy?repo=https://github.com/durgappcmc-spec/durgaemailer
2. Uses `Dockerfile` + `render.yaml` → service `durgaemailer-relay`
3. Set env vars in Render: `GEMINI_API_KEY`, sheet/tracking URLs, and for Gmail
   `GMAIL_OAUTH_JSON` + `GMAIL_TOKEN_JSON` (paste JSON file contents)
4. Public app URL: `https://durgaemailer-relay.onrender.com` (exact host shown in Render)

Wire Netlify portal (no tunnel):

1. Netlify site `durgaemailer-relay` → `RELAY_APP_URL=https://….onrender.com`
2. Redeploy portal (base dir `relay-portal`)

Portal: https://durgaemailer-relay.netlify.app · Tracking stays on Netlify.
Free Render instances sleep after idle; first open can take ~30–60s.

## Architecture

| Piece | Role |
|-------|------|
| Streamlit | UI + Gemini chat + providers + Gmail extract |
| Google Sheet | Shared DB (Sends, Opens, Clicks, Links, Scheduled, Replies) |
| Apps Script | Offline scheduler, Gmail send, reply watcher, log ingest |
| Netlify Functions | GIF open pixel + click 302 (Apps Script cannot return image bytes) |
| Chroma | Local vector memory / RAG |

## Troubleshooting

- **Gemini quota exceeded** — wait for daily reset or upgrade billing in AI Studio.
- **Gmail token expired** — delete `credentials/gmail_token.json` and rerun; browser consent will refresh.
- **Apps Script permission prompts** — click Advanced → Go to &lt;project&gt; (unsafe) → Allow.
- **Netlify HTTPS delay** — custom domains can take up to ~5 minutes to provision certs.
- **Chroma on Streamlit Cloud** — local disk is not durable; use Chroma Cloud or Supabase pgvector for production.

## Pages

1. Chat — grounded Gemini answers
2. Prospects — search / enrich / save
3. Schedule — single, bulk, sequence, queue
4. Tracking — opens, clicks, hot leads, replies
5. Inbox Extract — structured Gmail extraction
6. Memory — Chroma search + manual notes
