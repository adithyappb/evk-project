# EVK Roadmap

Canonical stack: **Next.js → Vercel**, **Postgres/Auth → Supabase**, **Gmail + Gemini agents** in `lib/agents/`.  
Legacy Python/Firestore in `src/evk/` is **not** wired to CI or production — ignore unless porting logic.

---

## Integration status

| Service | Wired | Gap |
|---------|-------|-----|
| **Vercel** | Crons in `vercel.json`, API routes, `Dockerfile` | No deploy runbook; workbook import needs blob storage on serverless |
| **Supabase** | Migrations, service-role agents, admin auth | **No RLS read policies** → Realtime queue likely silent in prod |
| **Gmail** | Poll + send via `lib/gmail.ts` | Polling only (30 min); no push/watch |
| **Gemini** | Classify, embed, match, digest intro | Single-opportunity schema; no multi-opp extraction |
| **Workbook** | Local import via `MASTER_WORKBOOK_PATH` | No cloud upload path on Vercel |

---

## 1. Vercel — exact setup

### One-time

1. Create Vercel project from repo root (`next.config.mjs`, not `src/evk/`).
2. Link Supabase project; run migrations:
   ```bash
   supabase link --project-ref <ref>
   supabase db push
   ```
3. Set **all** env vars from `.env.example` in Vercel → Settings → Environment Variables (Production + Preview).
4. Generate `CRON_SECRET`; Vercel sends it as `Authorization: Bearer <CRON_SECRET>` on cron hits — must match `lib/env.ts` / `verifyCronAuth`.
5. Mint Gmail OAuth refresh token (Desktop app or script) for the monitored inbox; set `GMAIL_*` vars.
6. Deploy; confirm crons fire: Ingest `*/30 * * * *`, Match `0 2 * * *`, Deliver `0 8 * * 1` (`vercel.json`).

### Manual cron trigger (no admin UI yet)

```bash
curl -H "Authorization: Bearer $CRON_SECRET" https://<app>/api/cron/ingest
curl -H "Authorization: Bearer $CRON_SECRET" https://<app>/api/cron/match
curl -H "Authorization: Bearer $CRON_SECRET" https://<app>/api/cron/deliver
```

### Still to build

| Item | Do this |
|------|---------|
| Admin “Run ingest/match/deliver” buttons | Add POST handlers (or reuse cron routes with session auth) on `/admin` — only workbook import has a button today |
| Workbook on Vercel | Signed upload → Supabase Storage → `POST /api/import/workbook` reads from bucket, not disk |
| Cron timeout at scale | Match/deliver `maxDuration` 300s; split into chunked jobs or queue when student×opp grows |

---

## 2. Supabase — exact setup

### One-time

1. Enable Auth providers used by app: Magic link + Google (`app/auth/`).
2. Add admin emails to `ADMIN_EMAILS` (server allowlist in `lib/auth.ts`).
3. Apply migrations: `0001_init.sql`, `0002_match_rpc.sql`, `0003_master_workbook.sql`.
4. Confirm Realtime publication includes `opportunities` (migration `0001_init.sql` L138–147).

### Fix Realtime (bug)

`components/pending-queue.tsx` subscribes via **anon** client. RLS is default-deny with **zero policies** (`0001_init.sql` L126–136) → browser gets no events.

**Add migration `0004_admin_rls.sql`:**

```sql
-- Authenticated admins read pending queue + related rows
create policy "admin_read_opportunities" on opportunities
  for select to authenticated
  using (auth.jwt() ->> 'email' = any(string_to_array(current_setting('app.admin_emails', true), ',')));

-- Or simpler: hardcode allowlist check via auth.jwt()->>'email' IN (...)
```

Practical approach: pass admin emails via Supabase custom claim or check against a small `admin_users` table. Wire `app.admin_emails` or duplicate `ADMIN_EMAILS` into a DB setting at deploy time.

Also add `select` policies for `students`, `student_matches` if those pages use browser Realtime later.

### Still to build

| Item | Do this |
|------|---------|
| Generated types | `supabase gen types typescript --linked > lib/supabase/types.ts` |
| Engagement tracking | Write `digests.opened`, `clicked_opportunities` (columns exist, never populated) |
| Unused audit actions | Implement `override_match`, `profile_update` or drop from schema |

---

## 3. Gmail ingestion — exact flow & improvements

### Current flow (`lib/agents/ingestion.ts`)

```
Cron → read ingestion_state.last_ingestion_ts
     → fetchUnread(label, since, limit)          [lib/gmail.ts]
     → skip if source.external_id exists
     → classifyAndPersist()                      [lib/agents/classifier.ts]
     → advance watermark to newest received_at
```

### Improvements

| Priority | Change | Where |
|----------|--------|-------|
| P1 | Persist `fingerprint` (computed, never stored) on `opportunities.source` for cross-source dedup | `ingestion.ts` L52–70, classifier insert |
| P2 | Gmail push notifications (`users.watch` + Pub/Sub) instead of 30 min poll | New webhook route + renew watch cron |
| P3 | Don’t mark read until classification succeeds | `lib/gmail.ts` |
| P4 | Port `web` / `api` `source_type` — types exist, no route | New `POST /api/ingest/webhook` |

---

## 4. Classification intelligence — exact improvements

### Current (`lib/agents/classifier.ts`)

- Single Gemini call, **6k char** body cap, **one opportunity** per email.
- Non-opportunities **dropped silently** (no audit row).
- `relevance_score < MIN_CLASSIFICATION_SCORE` → `needs_review`; else `pending` → dedup.

### Port from Python (`src/evk/agents/classifier.py`)

Python already handles what TS lacks:

1. **Multi-opportunity extraction** — one JSON entry per distinct opp in a digest email.
2. **Stricter rules** — reject aggregator digests without dates/links; never invent URLs.
3. **Conservative default** — `is_opportunity=false` when ambiguous.
4. **`fields_of_study`** + richer `kind` enum.

### Implementation steps

1. **Extend schema** (`lib/schemas.ts`):
   ```ts
   { is_opportunity, confidence, reasoning, opportunities: [{ title, description, type, deadline, tags, url? }] }
   ```
2. **Update prompt** — copy rules from Python `_SYSTEM_INSTRUCTION`; add “return array, one per opp”.
3. **Loop insert** in `classifyAndPersist`: one DB row per extracted opp; run `dedupOpportunity` on each.
4. **Pre-filter** (cheap, no LLM): regex/keyword gate for obvious spam before Gemini (newsletter footers, “unsubscribe”, no apply link).
5. **Audit skipped mail**: insert `audit_log` action `classify_skip` with subject + reasoning instead of silent drop.
6. **Raise model** — align `GEMINI_MODEL` to `gemini-2.0-flash` or `gemini-2.5-flash` after eval; keep `temperature: 0.1`.
7. **Section splitting** for long digests: if body > 6k, split on `---` / `<h2>` / numbered lists, classify chunks, merge deduped opps.

### Tuning knobs (`.env`)

- `MIN_CLASSIFICATION_SCORE` (default 0.75) — lower = more auto-approve, higher noise.
- Add `CLASSIFIER_MIN_CONFIDENCE` separate from relevance if you split “is opp” vs “quality score”.

---

## 5. Dedup — exact improvements

### Current (`lib/agents/dedup.ts`)

1. Normalised title match (same `type`, last 90d, O(n) scan of 20 rows).
2. Embed title+description → `match_similar_opportunities` RPC (same `type_filter`).

### Improvements

| Priority | Change |
|----------|--------|
| P1 | **Cross-type dedup** — drop `type_filter` in RPC call or run second pass without filter when sim ≥ 0.95 |
| P2 | **Indexed title** — `generated column normalised_title` + btree index instead of app-side scan |
| P3 | **Backfill embeddings** on approve (not only at ingest) for older rows |
| P4 | Tune IVFFlat `lists` when embeddings > 1k (`0001_init.sql` comment) |

---

## 6. Matching & delivery — bugs + improvements

### Bugs

| Bug | Location | Fix |
|-----|----------|-----|
| Realtime alert sends to synthetic emails | `delivery.ts` L238 — no `isDeliverableEmail()` | Guard before `sendEmail`, same as weekly digest L53–55 |
| Realtime alert runs **full** `runMatching()` | `delivery.ts` L202 | Add `runMatchingForOpportunity(oppId)` — score one opp × all students |
| Stale low scores never cleared | `matching.ts` upsert only ≥ threshold | Delete or zero-out matches below `MIN_MATCH_SCORE` on rerun |

### Improvements

| Priority | Change |
|----------|--------|
| P1 | Port Python hybrid score (`src/evk/agents/personalizer.py` `score_match`) as pre-filter before Gemini |
| P2 | Deadline reminders — port `src/evk/agents/reminder.py` to cron route |
| P3 | Send rate limits (Python has daily quota + delay; TS only `pool(..., 5)`) |
| P4 | Open/click tracking pixels + link rewrite on digest HTML |

---

## 7. Cleanup (non-blocking)

- Remove or archive `src/evk/` once any useful logic is ported.
- Add Python tests to CI only if that tree is kept.
- Document operator flow: README says dashboard cron buttons — only workbook import exists; fix README or add buttons.

---

## Suggested order

1. **Supabase RLS** for Realtime queue (prod admin UX broken without it).
2. **Realtime alert deliverability bug** (may email `@import.evkids.local`).
3. **Multi-opportunity classifier** (biggest quality win for newsletter ingestion).
4. **Vercel env + cron verification** (operational baseline).
5. **Targeted matching** on approve (scale).
6. **Cloud workbook upload** (ops convenience).

---

## Key files

| Area | Path |
|------|------|
| Ingest | `lib/agents/ingestion.ts` |
| Classify | `lib/agents/classifier.ts`, `lib/schemas.ts`, `lib/gemini.ts` |
| Dedup | `lib/agents/dedup.ts`, `supabase/migrations/0002_match_rpc.sql` |
| Match | `lib/agents/matching.ts` |
| Deliver | `lib/agents/delivery.ts`, `lib/digest-template.ts` |
| Crons | `app/api/cron/{ingest,match,deliver}/route.ts`, `vercel.json` |
| Admin UI | `app/admin/`, `components/pending-queue.tsx` |
| Reference (port from) | `src/evk/agents/classifier.py`, `personalizer.py`, `reminder.py` |
