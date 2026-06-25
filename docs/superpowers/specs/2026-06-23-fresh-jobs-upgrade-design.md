# Fresh-Jobs + Targeted-Add Upgrade — Design Spec

**Date:** 2026-06-23
**Status:** Approved design (pre-implementation-plan)
**Scope:** Spec #1 of 2. The browser autofill extension is a separate sub-project (spec #2), brainstormed after this ships.

## Goal

Make Northstar a "be at the top" job engine: surface the freshest matching roles first, pull from a second source (Seek) as well as LinkedIn, let the user add a targeted job by pasting its JD (auto-scored + auto-tailored resume), and tighten the discovery recency window down to minutes. The unifying mechanism is a precise per-job **`posted_at`** timestamp that every source populates and the board sorts by.

## Workflow this serves

User runs discovery (or pastes a targeted JD), the board opens freshest-first with "posted Xm ago" + a "just posted" highlight, they fire tailored resumes at the newest 5–10 roles within minutes of posting, then spend the rest of their time on people-tracking + outreach.

## In scope (4 parts)

### Part A — Seek as a best-effort second source
- Revive `scrape_seek()` in `00a_scrape_job_postings.py`; map its JSON-LD output to the `job_alerts_raw.csv` schema with `source="seek"` and `posted_at` from JSON-LD `datePosted`.
- `daily_run`'s discover stage runs **both** `00_search_linkedin_guest.py` (LinkedIn) and the Seek scraper, merging into `job_alerts_raw.csv` via `csv_merge` (dedup by `row_key`).
- **Best-effort contract:** if Seek is blocked / returns 0 / errors, log `[seek-warning]` and continue. Seek never fails the run; LinkedIn remains primary. (Reuses the same "never silent" delta pattern added during the cache-bug fix.)
- Seek recency is day-granular (URL `daterange`); tight sub-day windows apply to LinkedIn only. Fine-grained freshness for both comes from the `posted_at` sort.
- Cross-source exact-dedup (same role on both boards) is a v2 refinement; v1 dedups within each source by its own id.

### Part B — Recency windows
- Add **15 min (`r900`) / 1 hour (`r3600`) / 4 hours (`r14400`)** options to the onboarding recency selector (`app/templates/_ob_steps.html` + `_onboarding.html`), alongside existing 24h/48h/week/month.
- `recency_tpr` is already plumbed: onboarding → DB → `daily_run` config → `00_search --tpr`. This is options + verifying the pass-through; tight windows narrow the LinkedIn search.

### Part C — Posted-time + freshness (the core)
- **Capture `posted_at`** (full ISO timestamp) per job at the source:
  - LinkedIn: upgrade `parse_cards` in `00_search_linkedin_guest.py` to capture the relative "X minutes/hours ago" (or full `datetime`) and compute an absolute timestamp. (Today it only captures `datetime="YYYY-MM-DD"` — date, no time.)
  - Seek: from JSON-LD `datePosted`.
  - Manual paste: from "20 hours ago" / "Posted 1d ago" → absolute, relative to ingest time.
- **Carry `posted_at`** through `job_alerts_raw.csv` (new column) → `prepare_job_posts.py` → `app/sync.py` → a new `posted_at TEXT` column on the `jobs` table (additive migration in `app/db.py`).
- **Board** (`app/queries.py` `_shape`/`get_jobs`/`JOB_SELECT`, `app/app.py` `home`):
  - Default sort = **`posted_at` DESC** (freshest first); fall back to `first_seen_date` when `posted_at` is missing.
  - Display "**posted 12m ago**" (relative, exact on hover).
  - **Fresh filter:** `<1h / <4h / <24h`.
  - **🔥 just-posted highlight** on listings `<30m` old.

### Part D — Paste-a-JD manual add → auto score + resume
- New `jd_paste_parser.py` (sibling to `linkedin_people_parser.py`):
  - Detects LinkedIn-detail vs Seek-detail vs generic paste shape heuristically.
  - Extracts **company · role_title · location · posted_at · salary · jd_text (body)**; strips chrome (Easy Apply, applicant counts, "Why Join Us", premium upsells, match-insights, privacy boilerplate).
  - Returns a structured dict + per-field confidence.
  - Test fixtures = the two real sample pastes (Robert Half / LinkedIn-style, Melbourne Water / Seek-style).
- New **"Add job" slide-over drawer** on the Board (mirrors the people-ingest drawer pattern):
  1. Paste → POST to a new route that parses and returns an **editable preview** of the extracted fields (uncertain fields flagged).
  2. **Confirm** → score the single job (reuse `score_jobs` scoring) → generate the tailored resume (reuse the resume generator for one job) **in-process** with a progress indicator → write to `jobs` + `resume_packages` + `job_seen(first_seen=now)` with `posted_at` and `source="manual_paste"` → the job appears on the board, freshest-sorted, resume ready.
- **In-process (not the CSV pipeline)** because a single hand-added job scored + tailored directly is instant and isolated; the bulk CSV path stays for discovery.
- `row_key` for a manual job = stable hash of `company_core + role + location`.

## Data model changes
- `jobs.posted_at TEXT` (additive; `db.py` `init_schema`).
- `job_alerts_raw.csv`: new `posted_at` column (existing `posted_date` retained for compat).
- New `source` values: `"seek"`, `"manual_paste"` (column already exists).

## Error handling
- Seek: best-effort, `[seek-warning]` + continue.
- Manual parse low-confidence: preview flags the uncertain field for inline edit before commit.
- `posted_at` missing: board sort falls back to `first_seen_date`.
- Resume-gen failure on manual add: job still saved (scored) with a "retry resume" affordance — never lost.

## Testing plan
- `jd_paste_parser` unit tests on both real sample formats + edge cases → correct field extraction + confidence.
- `posted_at` normalization unit tests: "20 hours ago", "Posted 1d ago", ISO `datePosted`, LinkedIn relative → correct absolute timestamps.
- Seek scrape smoke (best-effort): returns ≥1 job OR logs `[seek-warning]` gracefully (no crash).
- Board: freshest-first ordering; Fresh-filter bucket boundaries (<1h/<4h/<24h); `posted_at`-missing fallback.
- Manual-add end-to-end: paste → preview → confirm → job scored + resume generated + on board + correct `posted_at`.
- Recency: new tpr options round-trip onboarding → `daily_run` → `00_search --tpr`.
- Regression: existing pipeline + board + people-tracker suites stay green.

## Assumptions to verify during planning (Stage 2)
1. `scrape_seek()` in `00a_scrape_job_postings.py` still returns parseable JSON-LD (Seek may have changed markup / added blocking).
2. The resume generator and `score_jobs` can be invoked on a **single** job in-process (not only batch over CSVs).
3. LinkedIn guest search-card HTML actually carries a sub-day time signal ("X minutes ago" or a full `datetime`) to capture, not just a date.
4. `app/sync.py` cleanly accepts the new `posted_at` field end-to-end.

## Out of scope / follow-ups
- **Auto-radar (B-tier of the cadence decision):** scheduled background discovery every ~15–30 min + "new since last run" detection + notification. Documented fast-follow; sits on this spec's `posted_at` + freshness foundation.
- **Browser autofill extension (spec #2):** Chrome MV3 extension that reads an application form's fields, maps them to the user's structured resume data (pulled from a local Northstar endpoint), fills with a mandatory review/confirm panel (never auto-submit), surfacing exactly what was filled where + confidence. Hard core = cross-ATS field detection + accuracy UX; warrants its own brainstorm. Known limit: browsers block programmatic file-input attachment, so file-upload fields are filled manually.

## Files touched (map)
- `00_search_linkedin_guest.py` — capture `posted_at` (finer time) in `parse_cards`.
- `00a_scrape_job_postings.py` — revive/validate `scrape_seek`, emit `posted_at` + `source="seek"`.
- `daily_run.py` — run Seek in the discover stage (best-effort), merge with LinkedIn.
- `csv_merge.py` — carry `posted_at` column.
- `prepare_job_posts.py` — pass `posted_at` through.
- `app/sync.py` — write `posted_at` to `jobs`.
- `app/db.py` — `posted_at` column migration.
- `app/queries.py` — board sort by `posted_at`, Fresh filter, freshness display, `_shape`.
- `app/app.py` — board sort wiring; new `/board/add-job` (parse-preview) + confirm routes.
- `app/templates/_ob_steps.html`, `_onboarding.html` — recency options.
- `app/templates/` (board) + `app/static/app.css` — freshness display/highlight, "Add job" drawer.
- `jd_paste_parser.py` (new) + `test_jd_paste_parser.py` (new).
