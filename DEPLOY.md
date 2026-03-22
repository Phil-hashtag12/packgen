# PackGen — Setup & Deployment Guide

## File structure

```
packgen/
├── app.py                  # Flask backend (all routes)
├── classifier.py           # AI classification (OpenRouter / Anthropic)
├── extractor.py            # PDF extraction (PyMuPDF)
├── packer.py               # Pack generation + cover pages
├── scores.py               # Mark tracking + spaced repetition
├── store.py                # Redis-backed job store
├── auth.py                 # Supabase JWT verification
├── api_keys.py             # Per-user PackGen key management
├── cleanup.py              # Background file cleanup daemon
├── gunicorn.conf.py        # Production server config
├── requirements.txt        # Python dependencies
├── supabase_schema.sql     # Run this in Supabase SQL editor
├── Procfile                # Railway / Render start command
├── runtime.txt             # Python 3.11
├── .env.example            # Copy to .env and fill in
└── templates/
    └── index.html          # Full frontend (single file)
```

---

## Step 1 — Supabase (auth + database)

1. Go to **supabase.com** → New project
2. Go to **SQL Editor** → paste the entire contents of `supabase_schema.sql` → Run
3. Go to **Authentication → Providers → Email** → Enable
4. Go to **Authentication → Email Templates** → enable "Confirm signup" (stops bots burning your AI quota)
5. Go to **Settings → API** → copy:
   - Project URL → `SUPABASE_URL`
   - `anon` `public` key → `SUPABASE_ANON_KEY`
   - `service_role` key → `SUPABASE_SERVICE_ROLE_KEY` (**keep secret**)

---

## Step 2 — AI backend (pick one)

### Option A: OpenRouter (recommended — cheapest)
1. Go to **openrouter.ai** → sign up → **Keys** → Create key
2. Set `AI_BACKEND=openrouter` and `OPENROUTER_API_KEY=sk-or-v1-...`
3. Default model: `google/gemini-flash-1.5-8b` (~$0.04 per 1M tokens)
4. Top up credits — $5 will classify thousands of papers

### Option B: Anthropic
1. Go to **console.anthropic.com** → API Keys → Create
2. Set `AI_BACKEND=anthropic` and `ANTHROPIC_API_KEY=sk-ant-...`
3. Model: `claude-haiku-4-5-20251001`

---

## Step 3 — Deploy to Railway (recommended)

Railway gives you a proper container, persistent disk, and Redis in one place.

```bash
# Install Railway CLI
npm install -g @railway/cli
railway login
```

```bash
# In your packgen folder:
git init
git add .
git commit -m "PackGen initial"
railway init          # creates a new project
railway up            # deploys
```

**Add Redis:**
- Railway dashboard → your project → **+ New** → **Database** → **Redis** → Deploy
- Click the Redis service → **Variables** → copy `REDIS_URL`

**Add persistent disk:**
- Click your app service → **Settings** → **Volumes** → **+ Add Volume**
- Mount path: `/app/uploads`  Size: 10 GB
- Repeat for `/app/outputs` and `/app/sessions` (or use one 20 GB volume at `/app`)

**Set environment variables** (your app service → **Variables** → **Raw Editor**):
```
AI_BACKEND=openrouter
OPENROUTER_API_KEY=sk-or-v1-...
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_ANON_KEY=eyJ...
SUPABASE_SERVICE_ROLE_KEY=eyJ...
REDIS_URL=redis://default:xxx@xxx.railway.app:6379
FLASK_SECRET=<run: python -c "import secrets; print(secrets.token_hex(32))">
ADMIN_TOKEN=<another random string>
SENTRY_DSN=https://xxx@sentry.io/xxx
```

**Set start command:**
- Your app service → **Settings** → **Deploy** → Start Command:
```
gunicorn app:app -c gunicorn.conf.py
```

That's it. Railway auto-detects `requirements.txt` and builds.

---

## Step 4 — Local development

```bash
# Clone / enter the project folder
cd packgen

# Create virtualenv
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy and fill in env vars
cp .env.example .env
# Edit .env with your keys

# Start Redis locally (optional — app falls back to in-memory without it)
docker run -d -p 6379:6379 redis:alpine

# Run
python app.py
# Open http://localhost:5050
```

---

## Step 5 — First time user flow

1. Open the app → **Get Started** → **Sign Up** with email + password
2. Check email for confirmation link (if email confirmation is enabled in Supabase)
3. Sign in → app automatically calls `/api/user/provision-key` → stores your PackGen key in `localStorage`
4. Upload QP + MS PDF pairs → pipeline runs → packs generated
5. Download ZIP → do the pack on paper → come back → **Enter Marks** → progress updates

---

## Admin dashboard

Visit `/admin?token=YOUR_ADMIN_TOKEN` to see:
- Active jobs count
- Total users
- Total classifications this month
- Redis status
- AI backend in use

---

## Rotating AI models (OpenRouter)

To switch models without redeploying, just change env vars:
```
OR_TEXT_MODEL=anthropic/claude-3-haiku          # more accurate, slightly pricier
OR_TEXT_MODEL=google/gemini-flash-1.5-8b        # default, very cheap
OR_TEXT_MODEL=mistralai/mistral-7b-instruct     # open source option
OR_VISION_MODEL=google/gemini-flash-1.5         # for diagram questions
```

---

## Environment variables reference

| Variable | Required | Description |
|---|---|---|
| `AI_BACKEND` | Yes | `openrouter` or `anthropic` |
| `OPENROUTER_API_KEY` | If using OpenRouter | Your OpenRouter key |
| `ANTHROPIC_API_KEY` | If using Anthropic | Your Anthropic key |
| `SUPABASE_URL` | Yes (prod) | Supabase project URL |
| `SUPABASE_ANON_KEY` | Yes (prod) | Supabase anon key |
| `SUPABASE_SERVICE_ROLE_KEY` | Yes (prod) | Supabase service role key |
| `REDIS_URL` | Yes (prod) | Redis connection URL |
| `FLASK_SECRET` | Yes (prod) | Random 32-byte hex string |
| `ADMIN_TOKEN` | Yes (prod) | Secret for `/admin` |
| `SENTRY_DSN` | No | Sentry error monitoring |
| `FREE_QUOTA_PER_MONTH` | No | Questions/month free tier (default: 500) |
| `PRO_QUOTA_PER_MONTH` | No | Questions/month pro tier (default: 5000) |
| `OR_TEXT_MODEL` | No | OpenRouter text model |
| `OR_VISION_MODEL` | No | OpenRouter vision model |
| `FILE_TTL_SECONDS` | No | How long files live on disk (default: 8h) |
| `JOB_TTL_SECONDS` | No | How long job state lives in Redis (default: 6h) |

---

## Upgrading a user to Pro

In Supabase → **Table Editor** → `packgen_profiles` → find the row → edit `plan` to `pro`.

---

## Troubleshooting

**"No matched QP/MS pairs found"**
File naming must end in `QP` or `MS` before the `.pdf` extension.
Examples that work: `June 2022 QP.pdf`, `4MA1_June22_MS.pdf`

**Classification gives "Unknown" for everything**
Check your AI key is set and has credits. Visit `/health` to confirm Redis is connected.

**Session won't load after redeployment**
Sessions store absolute PDF paths. If you moved to a new server, re-upload the original PDFs first.

**Rate limit errors (429)**
Free users get 200 API calls/hour. Upgrade the user to `pro` in Supabase, or have them provide their own Anthropic key in the Account tab.
