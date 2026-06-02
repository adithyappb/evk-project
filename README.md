# EVkids — Student Opportunity Pipeline

An AI-assisted pipeline that turns noisy opportunity newsletters into short, **personalised** emails for individual students — with a mandatory **human-approval gate** in front of every send.

**Current stack:** Python · FastAPI · Jinja2 + HTMX · Gemini 2.0 Flash · Gmail (IMAP + SMTP) · Local JSON store (pilot) / Firestore (production)

---

## How it works

```
 Gmail inbox (evkidsorchestrator@gmail.com)
      │  IMAP poll — daily automatic, or manual trigger
      ▼
 IngestionAgent
      ├─ ClassifierAgent     Gemini 2.0 Flash — extracts N opportunities per email,
      │                      flags uncertain ones for admin review
      └─ PersonalizerAgent   rule-based match score → personalised draft per student

 WebScraperAgent             fetches YouthLine NE + Boston.gov pages,
                             feeds them through the same pipeline

 JSON / Firestore store  ──▶  Admin dashboard  (http://localhost:8080)
                                   │  human reviews, approves, or rejects
                                   ▼
                          DistributorAgent
                               ├─ Gmail SMTP send  (batched, paced, daily quota)
                               └─ WhatsApp (Twilio — optional, per student preference)
                                   │
                          ReminderAgent    deadline reminders (daily, idempotent)
```

**No email is ever sent without `DraftMessage.status == APPROVED`.**

---

## Roles

| Role | What they see |
|---|---|
| **Admin** | Full dashboard — drafts, opportunities, students, agents, KPI, user management |
| **NGO Admin** | Opportunities catalogue + KPI dashboard |
| **Student** | Personalised dashboard — matched opportunities, outreach history, profile editor |

---

## 60-second quickstart (no credentials needed)

### Prerequisites

- Python **3.11+**
- [`uv`](https://docs.astral.sh/uv/) — `pip install uv`

### Install

```bash
git clone <repo-url> evkids
cd evkids
uv sync
cp .env.example .env        # macOS / Linux
copy .env.example .env      # Windows
```

### Seed and run

```bash
uv run evk seed             # loads demo students + opportunities into ./data
uv run evk serve            # http://localhost:8080
```

Log in at [http://localhost:8080](http://localhost:8080). Local dev accounts and the shared password are in `credentials.txt`.

In local mode (no real credentials), verification codes appear as an amber banner in the browser — no email account needed.

### Verify

```bash
curl http://localhost:8080/healthz      # liveness — always 200
curl http://localhost:8080/health       # readiness — checks repos + Gmail + Gemini
uv run pytest -q                        # full test suite (167 tests)
```

---

## Configuration

| File | Committed? | Purpose |
|---|---|---|
| `.env.example` | ✅ Yes | Template — stub placeholders only, safe to share |
| `.env` | ❌ No (gitignored) | Your real credentials — never commit |
| `credentials.txt` | ❌ No (gitignored) | Local dev account list — share via secure channel only |

### Key `.env` settings

```bash
# Mode
EVK_MODE=local                        # local = JSON store + Gmail; production = Firestore

# Gemini — get a free key at aistudio.google.com/apikey
GOOGLE_API_KEY=your-key-here
GEMINI_MODEL=gemini-2.0-flash

# Gmail — used for both inbound poll and outbound send
GMAIL_USER=evkidsorchestrator@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx   # Google Account → Security → App Passwords

# Scheduler
AUTO_POLL=true
POLL_INTERVAL_MINUTES=1440            # 1440 = once daily

# Auth
AUTH_LOCAL_DEMO_PASSWORD=your-shared-dev-password

# WhatsApp (optional — requires Twilio account + uv add twilio)
# TWILIO_ACCOUNT_SID=ACxxxxxxxx
# TWILIO_AUTH_TOKEN=xxxxxxxx
# TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
```

---

## Modes

| `EVK_MODE` | Data store | Email | Gemini |
|---|---|---|---|
| `local` (no API key) | File-backed JSON in `./data/` | Gmail or stub log | Pattern-matching stub |
| `local` + `GOOGLE_API_KEY` | File-backed JSON | Gmail or stub log | **Real Gemini Dev API** |
| `production` | Firestore | Gmail | Vertex AI Gemini |

Switching modes is a single env-var change — `evk.factory` picks the right backend automatically.

---

## Architecture detail

### Agents

| Agent | File | What it does |
|---|---|---|
| `IngestionAgent` | `agents/ingestion.py` | Polls Gmail, sanitises footers, runs classify → dedup → personalise |
| `ClassifierAgent` | `agents/classifier.py` | Gemini structured output — extracts N opps per email, flags uncertain ones for admin review |
| `PersonalizerAgent` | `agents/personalizer.py` | Rule-based match score + Gemini copy → `PENDING_APPROVAL` draft |
| `DistributorAgent` | `agents/distributor.py` | Sends `APPROVED` drafts via Gmail SMTP (batched ≤45, 0.2s spacing, daily quota) |
| `DigestAgent` | `agents/digest.py` | Weekly top-N picks per opted-in student, idempotent per ISO week |
| `ReminderAgent` | `agents/reminder.py` | Deadline reminder sweep — email or WhatsApp, idempotent per `(student, opp, window)` |
| `WebScraperAgent` | `agents/scraper.py` | Fetches YouthLine NE + Boston.gov pages, feeds text through the classifier pipeline |

### Classifier review queue

When Gemini is uncertain about an opportunity (missing deadline, vague eligibility, tracking URL, past deadline) it sets `needs_review=True`. Those opportunities are held in the **review queue** — visible at `/opportunities` — and never reach students until an admin clears them.

### WhatsApp reminders

Students can opt in to WhatsApp reminders by setting their notification method to **WhatsApp** and adding their phone number in **Edit my profile**. Requires Twilio credentials in `.env` and `uv add twilio`.

---

## Project layout

```
evkids/
├── Dockerfile                       # container build for Railway / Render / Fly
├── railway.toml                     # Railway-specific deploy config
├── pyproject.toml                   # Python deps + uv lockfile
├── .env.example                     # zero-credential template
├── credentials.txt                  # local dev accounts (gitignored)
├── seed/
│   ├── students.json                # demo student profiles
│   ├── opportunities.json           # seed opportunities
│   └── sample_newsletter.txt        # pipeline demo input
└── src/evk/
    ├── config.py                    # typed settings (pydantic-settings)
    ├── factory.py                   # local/prod backend resolution
    ├── models.py                    # all domain models + Gemini output schemas
    ├── api.py                       # FastAPI app — routes, lifespan scheduler
    ├── auth.py                      # MFA login (email OTP), session management
    ├── gmail_client.py              # Gmail IMAP inbound + SMTP outbound
    ├── gemini_client.py             # google-genai wrapper (Dev API + Vertex, retry x3)
    ├── firestore_repo.py            # production Firestore repos
    ├── local_store.py               # file-backed JSON repos (local mode)
    ├── stubs.py                     # stub Gmail + stub Gemini for zero-cred dev
    ├── twilio_client.py             # optional WhatsApp + SMS via Twilio
    ├── privacy.py                   # student PII pseudonymiser (salted hash, age bands)
    ├── ratelimit.py                 # send batching, pacing, daily circuit breaker
    ├── dedup.py                     # type+deadline pre-filter → Jaccard similarity
    ├── email_sanitizer.py           # strip newsletter footers / unsubscribe boilerplate
    ├── cli.py                       # `evk` CLI (serve / poll / remind / digest / seed)
    ├── seed.py                      # data loader
    ├── ui/
    │   ├── routes.py                # all HTML routes (Jinja + HTMX)
    │   └── templates/               # Jinja templates
    │       ├── base.html            # shared layout + nav
    │       ├── dashboard.html       # admin main dashboard
    │       ├── ngo_dashboard.html   # NGO admin view
    │       ├── student_dashboard.html
    │       ├── student_profile.html # student profile editor (interests, WhatsApp, frequency)
    │       ├── admin_kpi.html       # KPI dashboard — pipeline stats + outcome tracker
    │       ├── admin_agents.html    # agent trigger panel (poll, remind, digest, scrape)
    │       ├── opportunities_page.html  + opportunity_detail.html
    │       ├── drafts_page.html
    │       └── students_page.html
    └── agents/
        ├── ingestion.py
        ├── classifier.py
        ├── personalizer.py
        ├── distributor.py
        ├── digest.py
        ├── reminder.py
        └── scraper.py               # YouthLine NE + Boston.gov web scrapers
```

---

## CLI

| Command | What it does |
|---|---|
| `evk serve` | Run FastAPI server + admin dashboard on `APP_PORT` (default 8080) |
| `evk seed` | Upsert demo students + opportunities into the data store |
| `evk poll` | Poll Gmail inbox now (one-shot) |
| `evk remind` | Run deadline reminder sweep now |
| `evk digest` | Build this week's digest drafts (one per opted-in student) |
| `evk simulate <file>` | Feed a `.txt` / `.eml` file through the pipeline (no Gmail needed) |
| `evk drafts` | List drafts by status |
| `evk approve <id>` | Approve and send a draft |
| `evk scheduler` | Long-running background scheduler (poll + remind) |

---

## Dashboard routes

| Method | Path | Role | Description |
|---|---|---|---|
| `GET` | `/` | Any | Landing / redirect to role home |
| `GET` | `/app/admin` | Admin | Main dashboard |
| `GET` | `/app/ngo` | NGO Admin | NGO dashboard |
| `GET` | `/app/student` | Student | Student dashboard |
| `GET` | `/admin/kpi` | Admin / NGO | KPI dashboard + outcome tracker |
| `GET` | `/opportunities` | Admin / NGO | Opportunity catalogue + review queue |
| `GET` | `/opportunities/{id}` | Admin / NGO | Opportunity detail + match scores |
| `GET` | `/students` | Admin | Student list |
| `GET` | `/profile` | Student | Edit profile (interests, WhatsApp, frequency) |
| `GET` | `/admin/agents` | Admin | Agent trigger panel |
| `GET` | `/admin/test-logins` | Admin | Generate login codes for test accounts |
| `POST` | `/ui/poll` | Admin | Trigger Gmail poll |
| `POST` | `/ui/remind` | Admin | Trigger reminder sweep |
| `POST` | `/ui/digest` | Admin | Build digest drafts |
| `POST` | `/ui/scrape` | Admin | Scrape a web source (`source=youthline` or `boston_gov`) |
| `POST` | `/ui/drafts/{id}/approve` | Admin | Approve + send |
| `POST` | `/ui/drafts/{id}/reject` | Admin | Reject |
| `GET` | `/healthz` | — | Liveness probe (always 200) |
| `GET` | `/health` | — | Readiness probe — 503 if Gmail or Gemini unreachable |

---

## Deploying to Railway (recommended for pilot)

The app is a standard container — deploy anywhere that runs Docker.

### One-time setup (~20 min)

1. **Push to GitHub** — `evk-project/` is the repo root; push it as its own repo.

2. **Create Railway project** — New Project → Deploy from GitHub → select the repo.
   - Railway auto-detects `pyproject.toml` + `uv.lock`
   - Start command: `uvicorn evk.api:app --host 0.0.0.0 --port $PORT`

3. **Add a Volume** — Railway → Add Service → Volume → mount at `/data`.
   Then set `LOCAL_DATA_DIR=/data` in env vars.

4. **Set env vars** in Railway → Variables:

```bash
GOOGLE_API_KEY=          # aistudio.google.com/apikey — add billing for no rate limits
GMAIL_APP_PASSWORD=      # Google Account → Security → App Passwords
GMAIL_USER=evkidsorchestrator@gmail.com
ADMIN_BASE_URL=https://your-app.up.railway.app
AUTH_EMAIL_DELIVERY_MODE=smtp
AUTO_POLL=true
POLL_INTERVAL_MINUTES=1440
PRIVACY_SALT=            # python -c "import secrets; print(secrets.token_hex(24))"
AUTH_LOCAL_DEMO_PASSWORD=  # set a real password
```

5. **Deploy** — Railway builds the Dockerfile and starts the server. `/healthz` goes green in ~2 min.

### Moving to Firestore (post-pilot)

When the pilot outgrows a single JSON store:

1. Set `EVK_MODE=production` and `FIRESTORE_PROJECT=your-gcp-project`
2. Create service account with `Cloud Datastore User` role
3. Inject service-account JSON via Railway secret or Secret Manager
4. Deploy compound indexes: `gcloud firestore indexes composite create --config=firestore.indexes.json`

---

## Safety rails

- **Approval-only send** — `DistributorAgent` refuses any draft that isn't `APPROVED`; new drafts default to `PENDING_APPROVAL`
- **Idempotent everywhere** — inbound dedup on Gmail message ID, opportunity dedup by content hash + fuzzy `(type, deadline±30d)` Jaccard match, reminder dedup by `(student, opp, window)`, digest dedup by ISO week
- **Admin review queue** — classifier flags uncertain opportunities (`needs_review=True`); they never reach students until an admin manually clears them
- **PII pseudonymisation** — all student data passed to Gemini goes through `evk.privacy`: salted-hash ID, age band (not birth year), region (not address)
- **Send pacing + circuit breaker** — batches capped at 45 recipients, 0.2s spacing, soft daily quota (`DELIVERY_DAILY_QUOTA`); exceeded quota aborts and leaves drafts in `APPROVED` for the next run
- **Gemini quota guard** — 429 rate-limit errors are not retried (to avoid burning the daily allowance); the pipeline degrades gracefully and logs `gemini.quota_exhausted`
- **Retry with backoff** — transient Gemini and Gmail errors are retried 3× with exponential backoff
- **Structured Gemini output** — Pydantic-schema-validated at `temperature=0.1`; malformed JSON raises `GeminiError`
- **Zero-credential local mode** — full pipeline runs on file-backed JSON + Gmail stub; flip two env vars to go live

---

## Development

```bash
uv sync --all-groups
uv run pytest -q                        # 167 tests
uv run ruff check src tests
uv run ruff format src tests
```

Tests cover: classifier, personalizer, dedup, privacy, ratelimit, digest, reminder, distributor, ingestion, API surface, health/auth, UI template rendering, stress (concurrent webhooks, 1k-doc upserts).

**Reset local state:** `rm -rf data/ && uv run evk seed`

---

## FAQ

**Why Gmail instead of a dedicated email API?**
The pilot inbox is already a Gmail account. Gmail IMAP/SMTP with an App Password requires zero infrastructure changes and works with the existing `evkidsorchestrator@gmail.com` identity. The `GmailEmailClient` is behind the same interface as the stub, so swapping to SendGrid or Postmark later only touches `factory.py`.

**Why local JSON instead of a database?**
For a pilot with tens of students, a JSON file store backed by a persistent volume is simpler to operate than provisioning a database. All repos share the same `Repos` interface; switching to Firestore is a single env-var change (`EVK_MODE=production`).

**Why is the Gemini healthcheck sometimes failing?**
The free tier of `gemini-2.0-flash` has a daily request limit. The healthcheck is a live API call — if the daily quota is exhausted it returns `degraded`. The classifier and all pipeline functionality continue to work until the quota is fully spent. Add billing to the Google Cloud project to remove the limit entirely.

**How do I add a new web scraper source?**
Add an entry to `SCRAPER_SOURCES` in `src/evk/agents/scraper.py` with a unique key, display name, and URL. It will appear automatically as a button in the Admin Agents panel.

**How do I add a real student?**
Admin → Students → Import CSV (columns: `name`, `email`, `school`, `level`) → Activate → student receives a welcome email with a profile setup link.

**How do I reset local state?**
`rm -rf data/ && uv run evk seed`
