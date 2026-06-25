# Business mode (B2B outreach) + job-tracker contact fixes — design

**Date:** 2026-06-26
**Status:** Approved (brainstorm), pending spec review
**Scope:** First sub-project of the B2B outbound engine. Two deliverables built together:
- **Part 1 — Business mode scaffold (A):** a separate sales-prospect space + "Business" tab.
- **Part 2 — Job-side contact fixes:** the Prophix company-view bug, copy-all-emails, and a "companies with contacts" filter on the existing job tracker.

The AI message engine (B) and Gmail send (C) are **out of scope** here and get their own specs.

## Why two modes, not one

Decided in brainstorm: **business outreach is a different mode entirely from the job hunt.** Selling a service to a company and networking into a company to get hired are different intents, different messages, different pipelines. So Business mode gets its **own data namespace** (`biz_*` tables) and its own tab, while reusing the *plumbing* (the LinkedIn-People parser, email-pattern generation, and later the AI draft + Gmail send). Job-hunt contacts (`people` + `tracker_people`, incl. Prophix's 34) stay exactly where they are; Business mode starts empty.

This also sidesteps the repo's migration landmine: the design adds **new tables only** (which auto-apply via `CREATE TABLE IF NOT EXISTS`), and adds **no columns to existing tables**.

---

## Part 1 — Business mode scaffold (A)

### Data model (new tables, isolated)

```sql
CREATE TABLE IF NOT EXISTS biz_companies (
    company_key  TEXT PRIMARY KEY,
    company_name TEXT,
    domain       TEXT,
    website      TEXT,
    priority     INTEGER DEFAULT 0,   -- flagged for the deep-research message tier (B)
    notes        TEXT DEFAULT '',
    created_at   TEXT,
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS biz_prospects (
    prospect_key TEXT PRIMARY KEY,    -- f"{company_key}|{slugify(name)}"
    company_key  TEXT,
    company_name TEXT,
    name         TEXT,
    title        TEXT,
    email        TEXT,
    pattern      TEXT,
    stage        TEXT DEFAULT 'lead', -- sales pipeline stage
    notes        TEXT DEFAULT '',
    needs_review INTEGER DEFAULT 0,   -- low-confidence parse / email flagged
    created_at   TEXT,
    updated_at   TEXT
);
```

Shape deliberately mirrors `tracker_people` so the existing parser/ingest/email-pattern logic drops in with a different write target.

### Sales pipeline stages

`BIZ_STAGE_FLOW = ["lead", "contacted", "replied", "meeting", "won", "lost"]`
(Approved.) A stage-count summary bar sits above the list.

### Routes (`app/app.py`)

- `GET /business` — Business mode tracker: `biz_companies` grouped with their `biz_prospects`, the stage summary, bulk-select, ingest/upload entry points.
- `POST /business/ingest` — paste LinkedIn-People text + company name + email pattern → parsed into `biz_prospects` (reuses `linkedin_people_parser`); emails generated from pattern, low-confidence rows flagged `needs_review`.
- `POST /business/upload-csv` — CSV (company, name, title, email-or-pattern) → `biz_prospects`.
- `POST /business/prospect/{prospect_key}/stage` — set sales stage (HTMX row swap).
- `POST /business/company/{company_key}/priority` — toggle the priority flag.
- (Copy-all-emails is client-side; see below.)

### Queries (`app/queries.py`)

- `ingest_biz_prospects(con, company, pattern, raw_text)` — sibling of `ingest_people`, writing `biz_*`. Factor the shared parse step so both modes call one parser.
- `biz_groups(con)` — companies + their prospects (mirrors `tracker_groups`).
- `set_biz_stage(con, prospect_key, stage)`, `set_biz_priority(con, company_key, on)`.
- `biz_stage_summary(con)` — counts per stage for the summary bar.

### UI (`app/templates`)

- New nav item **"Business"** in `base.html`. Clicking it switches the screen into sales mode.
- `business.html` (mirrors the contacts/tracker layout), `_biz_company.html`, `_biz_prospect_row.html`, reusing the existing ingest-drawer pattern.
- Stage dropdown per prospect (sales-flavored), priority star per company, multi-select checkboxes.

### Bulk actions

- Multi-select prospects (and select-all per company).
- **Copy-all-emails:** client-side. Each prospect row carries `data-email`; a "Copy emails" button collects the selected (or all visible) emails and writes `emails.join(", ")` to the clipboard via `navigator.clipboard`. No endpoint needed; works without the send pipeline.
- **Flag-as-priority:** marks a company for the deep-research tier consumed later by the message engine (B).

### Isolation contract

Business routes touch only `biz_*` tables; job routes touch only `people` / `tracker_people` / `manual_people`. No cross-reads either direction. This is asserted by tests.

---

## Part 2 — Job-side contact fixes (existing job tracker)

### 2a. Prophix bug — company view shows 0 contacts

**Root cause (confirmed against live DB):** `company_detail()` (`app/queries.py` ~1037–1077) reads only `people` + `manual_people`; the new ingest writes `tracker_people`. Prophix has 34 rows in `tracker_people`, 0 in the legacy tables → company page shows 0.

**Fix:** `company_detail()` also reads `tracker_people WHERE company_key=?`, merged with the legacy rows and de-duped by `person_key`. Same `company_key` derivation on both sides (already consistent), so it's a pure read-path fix — no data move.

### 2b. Copy-all-emails on the job tracker

Same client-side copy button as Business mode, on company groups / company detail. Shared JS helper, used by both modes.

### 2c. "Companies with contacts" filter

The company list gains a per-company contact count and a toggle to show only companies that have ≥1 contact. (Counts come from a `GROUP BY company_key` over the unified job-contact read.)

---

## Data flow

```
Business mode:
  paste/CSV ─► parser (shared) ─► biz_prospects ─► /business view ─► stage/priority/copy
                                       │
                              flag priority ──► (later: research message engine B)

Job mode (fix):
  company page ─► company_detail() ─► people ∪ manual_people ∪ tracker_people  (was: missing tracker_people)
```

## Error handling / fallbacks

- Low-confidence parse or missing email → `needs_review = 1`, row shown with a flag, never dropped.
- CSV with unknown columns → best-effort map + per-row review flag; never hard-fail the whole import.
- Empty Business mode (fresh) → friendly empty state with the two ingest entry points, not a blank page.
- `company_detail` de-dupe: if a person exists in both legacy and `tracker_people`, show once (prefer `tracker_people`).

## Testing plan

New `test_biz_mode.py` + additions to the tracker tests:

- **Ingest into business mode:** paste → `biz_prospects` rows with generated emails + correct stage default.
- **CSV import:** maps columns, flags bad rows, never crashes on a malformed line.
- **Stage transitions:** `set_biz_stage` moves a prospect through the flow; summary counts update.
- **Copy payload:** the selected-emails string is correctly comma-joined and de-duped.
- **Mode isolation:** a `biz_prospect` never appears in any job-tracker query, and a `tracker_people`/`people` row never appears in `/business`.
- **Prophix fix:** `company_detail(company_key='prophix')` returns the `tracker_people` contacts; de-dupes across stores.
- **Companies-with-contacts filter:** counts are correct; toggle hides zero-contact companies.
- **Regression:** existing tracker + scoring suites stay green.

## Out of scope (own specs later)

- **B — the tiered message engine:** bulk template + lead-magnet for the long tail; deep website-research + persuasion-tuned AI draft (grounded in the user's sales frameworks) for flagged-priority accounts; human review gate.
- **C — send + tracking:** wire the existing Gmail send (rate-limited, business-hours, follow-ups) into business mode; bounce/reply tracking feeds the pipeline and the free verify-by-bounce.
- **Paid email verification** (Hunter/NeverBounce hook) — deferred toggle.
- **Apollo lead sourcing** — deferred; CSV + ingest cover v1.
