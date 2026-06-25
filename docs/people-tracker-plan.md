# Implementation Plan — Unified People-Centric Tracker (Northstar)

**Status:** awaiting approval · **Owner:** Aniket · **Drafted:** 2026-06-16

## Goal

Replace the two separate `/people` and `/tracker` pages with **one table-first, people-centric Tracker**. The company is the organizing unit, **people are the rows**, and **jobs are read-only context attached to the company**. People are ingested by pasting a raw LinkedIn "People"-tab copy plus a company name and an email pattern; a parser cleans the mess, generates emails, and writes persistent rows that the daily pipeline can never wipe.

## Decisions locked in brainstorming

- **One page replaces both.** `/people` and `/tracker` are removed; a single new Tracker is the home.
- **Row = person**, never a job. A company with 2 jobs does **not** make 2 rows — jobs live in a per-company cell (1 inline, 2+ in a `2 jobs ▾` popover), each job carrying its own application status.
- **Outreach status is per-person; application status is per-job.** Two separate ladders, reusing the existing `PERSON_STATUS_FLOW` and `STATUS_FLOW`.
- **No cap on companies.** Any company can be added. A free-typed name resolves to an existing company by **fuzzy matching** (legal-suffix stripping + typo tolerance) so "Calrom Pty Ltd" links to an existing "Calrom" — no duplicates. See *Company resolution* below.
- **Company-with-jobs-but-no-people** = one amber **❗ placeholder row** per company (not per job); clicking it opens the ingestion panel pre-scoped to that company.
- **Decoupled.** The tracker owns people in a new persistent (Zone-2) table that `sync` never drops. Jobs are read via a read-only company join against Zone-1 `jobs`. A pipeline failure leaves contacts intact.
- **Parser rules** (from the real sample): anchor on the `· Nth` degree line; name above (dedup the doubled line, strip "is open to work"); title below; drop noise (followers, mutual connections, "Provides services", Message/Follow). Discard lone-initial surnames ("Ayaz M.", "Muhammad H.") and "LinkedIn Member"; **route abbreviated first names ("M.Uzair Tariq") to a needs-review bucket** rather than dropping (surname is intact, email still works, human fixes the first name); re-case all-lowercase names.
- **Email is generated, never parsed**, from pattern tokens `{first} {last} {f} {l}` + domain, lowercased and ASCII-only.
- **Color psychology:** amber+❗ = jobs/no-contacts (act); blue = contact added, not contacted; green+✓ = reached out / replied / applied; gray = baseline. One-time subtle glow on amber rows.

## Two corrections the architect found in the real repo

1. **Project root is `/Users/aniketnamjoshi/Documents/northstar`** (the `JOBS/` copy is an older variant — ignore it).
2. **Test command:** this repo has flat `test_*.py` at the root, run individually as `python3 -m unittest test_<module>` from `northstar/`. There is **no** `.venv` and the `TESTING=true … discover tests` form finds zero tests. All phases below use the flat form.

## Architecture & data model

New Zone-2 table, added to `init_schema` (`IF NOT EXISTS`, never dropped by sync):

```sql
CREATE TABLE IF NOT EXISTS tracker_people (
    person_key      TEXT PRIMARY KEY,   -- company_key|slugify(name)
    company_key     TEXT NOT NULL,       -- normalize_company(company)
    company_name    TEXT,                -- display name as entered
    name            TEXT,
    title           TEXT,
    email           TEXT,
    pattern         TEXT,
    outreach_status TEXT DEFAULT 'not_contacted',
    notes           TEXT DEFAULT '',
    needs_review    INT  DEFAULT 0,
    created_at      TEXT,
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_tracker_people_company ON tracker_people(company_key);
```

`company_key = normalize_company(name)` is the existing link contract used by `/company/{key}` and `add_manual_company`, so auto-mapping is free: same normalized key ⇒ same company.

**Idempotent migration** `migrate_tracker_people(con)` (one-shot, copy-only, never destructive): if `tracker_people` is empty and `manual_people` has rows, copy each `manual_people` row + its `person_state` status/notes + `manual_company` display name into `tracker_people`, keeping `person_key` stable. Legacy tables are left intact (keeps `company.html` working and the guard safe across re-runs). Zone-1 `people` (pipeline-sourced) is **not** migrated.

## Company resolution (fuzzy matching)

**Invariant:** the stored/link key stays deterministic — `company_key = normalize_company(name)`. Jobs↔contacts join on it and `/company/{key}` URLs use it; we never make the *stored* key fuzzy. Fuzzy matching is a **resolution layer** that maps a typed name to the best existing `company_key` *before* storing. No new dependency — `difflib` is stdlib.

`company_core(name)` = `normalize_company` → strip punctuation → drop trailing legal/entity suffix tokens (`pty, ltd, limited, pvt, private, inc, incorporated, llc, corp, corporation, co, company, gmbh, ag, sa, plc, group, holdings, technologies, technology, labs, software, solutions, systems`). So `"Calrom Pty Ltd"` and `"Calrom"` both → core `calrom`.

`resolve_company(typed, candidates) -> (company_key, company_name, match_type)`, candidates = union of known companies (`tracker_people` + `companies` + `manual_company`):
1. **exact** — `normalize_company(typed) == candidate.company_key` → auto-link.
2. **suffix** — `company_core(typed) == company_core(candidate)` (non-empty) → auto-link. *Deterministic, zero false-merge risk* — only links when cores are equal. Handles the "Pty Ltd / Limited / Inc / punctuation / spacing" cases.
3. **fuzzy** — best `difflib.SequenceMatcher(None, core_typed, core_cand).ratio() >= 0.90` (high threshold so "Calrom" ≠ "Calcom"). This is the *less certain* tier → **not auto-merged silently**: it drives ranked dropdown suggestions and a toast hint, but on submit a non-core match creates a **new** company unless the user picked the suggestion. This is the guard against silently fusing two real companies.
4. **new** — nothing above threshold → new company.

UX: the drawer's company field is backed by an HTMX suggestion endpoint that fuzzy-ranks existing companies as you type, so you can click the canonical one. The post-ingest toast always states what happened — `"Added N to Calrom (matched existing)"` or `"Created new company 'Calrom Pty Ltd' — similar existing: Calrom; re-pick if that's the one."` — so any match is visible and correctable, never silent.

## Files

**Create**
- `linkedin_people_parser.py` — pure parser + email-pattern engine (no DB/FastAPI imports).
- `test_linkedin_people.py` — parser/engine unit tests.
- `app/templates/contacts.html` — the table-first page.
- `app/templates/_contacts_table.html` — `<tbody>` rows partial (HTMX swap target).
- `app/templates/_contact_row.html` — one person row + placeholder row partial.
- `app/templates/_ingest_drawer.html` — slide-over ingestion form + parse-preview.

**Modify**
- `app/db.py` — table DDL + `migrate_tracker_people`.
- `app/queries.py` — new query/mutation layer; remove dead `people()`, `tracker_rows()`, `get_tracker_row()`, `tracker_summary()` after cutover.
- `app/app.py` — replace `/people` + `/tracker` route set; repoint `/tracker/export`; keep shared `/company/*` and `/person/{key}/status`.
- `app/templates/base.html` — collapse the two nav links into one "Tracker".
- `app/static/app.css` — 4 row-color classes + one glow keyframe.

**Delete**
- `app/templates/people.html`, `app/templates/tracker.html`, `app/templates/_tracker_row.html`.
- Keep `_person_row.html` (still used by `company.html`).

## Parser (`linkedin_people_parser.py`)

`parse_people(raw_text, company, pattern, *, domain="") -> {people, needs_review, dropped}`

1. Split lines; find anchors via `·?\s*(1st|2nd|3rd)\b`.
2. NAME = first non-noise line above the anchor; collapse the doubled name line; strip trailing "is open to work".
3. TITLE = first non-noise line below the anchor; light whitespace clean.
4. Noise filter: `^\d[\d,]*\s+followers?$`, `mutual connection`, `^Provides services`, `^(Message|Follow|Connect|Pending)$`, `degree connection`, blanks.
5. Discards: "LinkedIn Member" → drop (anonymized); lone-initial surname → drop (half_cut_surname); single-token name → drop (no_surname); abbreviated first name → **needs_review**.
6. Re-case all-lower / all-upper names with `.title()`.
7. Generate email; dedupe within paste on `slugify(name)`.

**Email engine** `make_email(name, pattern, domain="")`: NFKD strip diacritics, drop non-`[a-z0-9]`, first/last tokens, `{f}`/`{l}` initials; domain taken from after `@` in the pattern or the `domain` arg; lowercased.

**Tests** cover: "is open to work" strip + dedup; "LinkedIn Member"; "Ayaz M."/"Muhammad H."; "M.Uzair Tariq" → review with email; "bradley gooding" → "Bradley Gooding"; noise lines excluded; diacritics → ASCII; pattern variants.

## Queries (`app/queries.py`)

- `ingest_people(con, company_key, company_name, parse_result)` — upsert with `ON CONFLICT(person_key) DO UPDATE` that **omits `outreach_status` and `notes`** (re-upload refreshes title/email but preserves workflow state). Skip on matching email under same company. Returns `{added, updated, needs_review, skipped}`.
- `tracker_table(con, q, company, status, sort, dir)` — load `tracker_people`; build company→jobs map from Zone-1 `jobs` (LEFT JOIN `application_status`); attach jobs; emit one placeholder row per jobless-but-job-having company; compute color; filter + sort.
- `get_contact_row(con, person_key)` — single-row HTMX swap.
- `set_tracker_person_status(con, person_key, status)`.
- `tracker_export_rows(con)` — flatten jobs to `"Role (status); …"`.
- `company_core(name)` + `resolve_company(typed, candidates)` — fuzzy company resolution (see *Company resolution*). `ingest_people` calls `resolve_company` to pick the `company_key`/`company_name` to store, and returns the `match_type` for the toast.
- `company_suggestions(con, q)` — known companies (union of `tracker_people` / `companies` / `manual_company`, deduped by key) ranked by `resolve_company`'s scorer against `q`; feeds the drawer's type-ahead.

## Routes (`app/app.py`)

| Method/Path | Purpose |
|---|---|
| `GET /tracker` | Page (cold → `contacts.html`; HTMX → `_contacts_table.html`). Params `q, company, status, sort, dir`. |
| `POST /tracker/ingest` | Form `paste, company, pattern` → parse → `ingest_people` → refreshed table + toast counts. |
| `POST /tracker/person/{key}/status` | Per-person outreach status → `_contact_row.html`. |
| `POST /tracker/person/{key}/notes` | Inline notes save. |
| `POST /tracker/job/{row_key}/status` | Per-job application status from popover. |
| `GET /tracker/companies?q=` | Fuzzy-ranked company suggestions for the drawer type-ahead (HTMX). |
| `GET /tracker/export` | CSV (repointed). |

Shared routes untouched: `/company/{key}`, `/person/{key}/status`, `/company/{key}/people`, `/company`, `/person/{key}/delete`. All POSTs are form-encoded (matches the app).

## Layout — right slide-over drawer (table-first)

Default view is the full-width table; an **"+ Add people"** button opens a right slide-over drawer containing the paste box + company `<datalist>` + pattern field. Chosen over a top collapsible (which reflows the table) and a modal (which fights the table for focus). Implemented with a no-build CSS toggle; the form posts via HTMX targeting `#contacts-table`, with one inline `hx-on::after-request` to close on success. Placeholder amber rows open the drawer pre-scoped to that company via `?company=<name>`.

- Header: title, KPI chips (total people / contacted / companies-needing-contacts), `+ Add people`, `Export .csv`.
- Filter bar: search, company select, outreach-status select — all `hx-get="/tracker"` into `#contacts-table`.
- Columns: **Person · Title · Company · Jobs · Outreach · Email · Notes**.
- Jobs cell: 1 job inline with status pill; 2+ in a `<details>` popover, each with a per-job status select.
- Colors map to existing theme vars (`--c-warn`, `--c-good`, `--c-muted`); glow is a one-shot keyframe on placeholder rows.

## Phases (each ends green)

0. **Schema + migration** (`db.py`) → `test_tracker_migration.py`: run `init_schema` twice, assert one preserved row.
1. **Parser + email engine** → `test_linkedin_people` green (all cases above).
2. **Query layer** → `test_tracker_queries.py`: re-upload preserves status/notes; one placeholder per jobless company; `resolve_company` — "Calrom" and "Calrom Pty Ltd" both link to `calrom` (suffix); "Calcom" does **not** merge into "Calrom" (false-merge guard); a within-threshold typo surfaces as a suggestion but creates new on submit unless picked.
3. **Routes + templates** → manual smoke: load `/tracker`, open drawer, paste sample, see rows + toast, change person & job status, export CSV.
4. **Cutover + cleanup** → update nav, delete old templates + dead queries; smoke all pages; `python3 -m unittest test_resume_parser test_onboarding_search_wiring`; insert a row, run `python3 app/sync.py`, assert it survives (decoupling proof).

Test command throughout: `python3 -m unittest test_<module>` from `northstar/`.

## Risks / open edge cases

1. **Name-variant duplicates** without matching emails (e.g. "M.Uzair Tariq" vs later "Muhammad Uzair Tariq") can produce two rows. Email-dedupe mitigates when emails match. Acceptable for v1.
2. **Fuzzy company resolution false-merge.** The `suffix` tier is deterministic (zero false-merge risk); the `fuzzy` tier (`difflib >= 0.90`) is suggestion-only and never auto-merges, so a wrong fusion of two real companies can't happen silently. Residual risk is the opposite — a real variant the user doesn't notice in the suggestion list creates a duplicate company; the toast hint mitigates this. Threshold (0.90) and the suffix token list are the two tunables if matching feels too loose/tight.
3. **Parser brittleness** to LinkedIn layout drift — mitigated by keeping the parser pure + well-tested.
4. **needs_review surfacing** — recommend a small "review" badge on those rows; confirm if you want them filterable. *(open question)*
5. **Glow replay** on every HTMX refresh — visually fine for amber-only rows; gate to first load only if it annoys. *(defer)*
