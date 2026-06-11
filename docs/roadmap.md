# EVK Roadmap

Canonical stack: **Python FastAPI** (`src/evk/`), **Jinja2 + HTMX UI**, **Gemini agents**, **Gmail/Inkbox**, **local JSON / Firestore** data.

Legacy Next.js / Supabase code at repo root is **not** wired to CI or production — reference only when porting ideas.

---

## Integration status

| Service | Wired | Gap |
|---------|-------|-----|
| **FastAPI UI** | Auth, admin, student dashboards, HTMX queue | Pin Tailwind in CI deploy step |
| **Gmail / Inkbox** | Poll + send via agents | Push/watch not implemented |
| **Gemini** | Classify, match, digest intro | Multi-opp extraction from one email |
| **Firestore** | Production repo adapter | Local JSON default for dev |
| **Student portal** | Dashboard, profile, suggestions, outcomes | Unified profile_setup + profile templates |
| **Security** | CSRF, OTP rate limit, secure cookies | Optional CSRF rotation per session |

---

## Completed (recent)

- Split monolithic `routes.py` → `ui/routes/{auth,student,admin}.py`
- CSRF double-submit on all browser POSTs + HTMX header
- Pinned Tailwind build (`npm run build:css` → `/static/css/app.css`)
- Student recommendations panel fix (`opportunities` template variable)
- 253 pytest tests green (UAT, stress, CSRF)

---

## Priority backlog

### P1 — Production hardening

| Item | Action |
|------|--------|
| Deploy runbook | Document Cloud Run / VM + env vars + `APP_ENV=prod` |
| Tailwind in CI | Add `npm run build:css` to GitHub Actions before deploy |
| Firestore indexes | Verify query patterns for drafts/opps at scale |
| Gmail push | Optional Pub/Sub watch to replace 30-min poll |

### P2 — Product polish

| Item | Action |
|------|--------|
| Profile templates | Merge `profile_setup.html` + `student_profile.html` shared partials |
| Parsed drafts tab | Replace synthetic “parsed” rows with real pipeline state or hide tab |
| Student analytics | Wire `audit_log` / engagement schema if added |
| Digest preview | Admin preview before weekly send |

### P3 — Legacy cleanup

| Item | Action |
|------|--------|
| Next.js tree | Archive or delete unused `app/`, `lib/`, `dashboard/` when confirmed |
| Docs sync | Keep `techarch.md` aligned with Python stack (done) |

---

## Local development

```bash
uv sync
uv run pytest -q
uv run evk serve
npm install && npm run build:css   # after template/CSS changes
```

OneDrive-safe commits: `.\scripts\git-commit.ps1 -m "message"`

---

## Test plan (release gate)

- [ ] `uv run pytest -q` — all green
- [ ] Manual: student login → dashboard shows matched opps with % fit
- [ ] Manual: admin approve draft → student sees outreach
- [ ] Manual: POST form without CSRF → 403
- [ ] Prod: verify `/static/css/app.css` loads (no CDN)
