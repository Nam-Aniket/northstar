# People Tracker — Visual Redesign Spec (2026-06-16)

Decisions (from user): **layout = grouped by company**, **energy = polished & calm** (match the Board, restrained motion, quiet progress). Build on the existing "Ramos" design system — do NOT introduce a new palette.

## Goal
Make the Tracker feel like a finished, premium tool you *want* to fill in — by (a) adopting Northstar's component kit instead of hand-rolled 11px inline styles, and (b) surfacing data already in the DB that the current page ignores.

## Data already available (wire it in)
- `application_status(row_key, status, status_changed_at, source)` — per-job application stage + the date it changed. This is the missing "Applied" signal.
- `jobs(row_key, company, role_title, location, job_url, match_score, jd_posted_date, jd_text, ...)` — link target, Fit %, location, posted date.
- `tracker_people(... outreach_status, notes, needs_review, created_at, updated_at)` — people + their dates.

## Layout: grouped by company
One **company group card** per company; company shown once. The company universe = union of companies that have tracker_people OR jobs (same as `company_suggestions` now returns).

### Query contract — `queries.tracker_groups(con, q='', status='', app_status='', needs_contacts_only=False, sort='activity', dir='desc') -> (groups, stats)`
Each `group` dict:
- `company_key`, `company_name`
- `people`: list of `{person_key, name, title, email, outreach_status, notes, needs_review, created_at, updated_at, color}` (color via outreach_status: replied/contacted→green, followup_due→blue, else gray)
- `jobs`: list of `{row_key, role_title, job_url, location, match_score, app_status, status_changed_at, jd_posted_date}`
- `people_count`, `jobs_count`
- `applied_count` — jobs whose status ∈ {applied, phone_screen, interview, offer}
- `contacted_count` — people whose outreach_status ∈ {contacted, followup_due, replied}
- `group_status` — rollup for color/pill, most-advanced wins: `offer` → `interview` → `applied` → `contacted` → (jobs but 0 people & 0 applied) `needs_contact` → else `neutral`
- `last_activity` — max(people.updated_at ∪ jobs.status_changed_at)
- `is_placeholder` — no people
`stats` dict (scoreboard): `total_companies`, `companies_contacted`, `companies_applied`, `people_total`, `added_this_week`.
Filters: `q` matches company OR person name/title; `status` filters people; `app_status` filters by a job stage present; `needs_contacts_only` keeps only `group_status == needs_contact`. Sort keys: `activity` (last_activity), `added` (max created_at), `company` (name), `fit` (max match_score), `applied` (applied_count). A group is kept if it has ≥1 matching person OR (no person filter active and it has jobs).

## Color semantics (status → accent, paired with text/icon — never color alone)
- `offer` → gold (`--c-warn`) — celebratory pill "Offer"
- `interview`/replied → green (`--c-good`) strong
- `applied`/contacted → green-soft — `.applied-pill` "Applied · N of M" / "Contacted"
- `needs_contact` → amber (`--c-warn-soft`) — left-accent + ❗→ use `ti`-style/SVG, nudge "Add people"
- `neutral` → none
Left 3px accent bar on the group card matching group_status.

## Templates
- `contacts.html`
  1. **Scoreboard header**: title + a calm progress bar `companies_contacted / total_companies` with a label, plus 3 `.kpi`-style stat chips (People, Applied, Added this week). Quiet, not gamified.
  2. **Toolbar** (sticky, prominent, 44px targets): search input; outreach-status select; application-status select; "Needs contacts only" toggle; sort select (Recent activity / Date added / Company / Fit / Applied). Reuse `.filter-select`.
  3. **Groups**: `{% for g in groups %}{% include "_company_group.html" %}{% endfor %}` inside a container `#contacts-table` (keep id so HTMX swaps + existing JS delegation still target it).
  4. Drawer include + JS block.
- `_company_group.html` (new): a card (`.panel`-like, radius-lg, soft shadow, left accent by status).
  - **Header** (button, click toggles expand): chevron icon, company name (→ `/company/{key}`), status pill, meta chips `N people · N jobs · last activity (relative)`, and a coral `.btn` "Add people" (`data-add-people data-company`).
  - **Body** (collapsible, default: expanded if group_status==needs_contact or has people; collapsed otherwise — keep simple, default expanded):
    - People sub-list: each → name (+`review` badge), title, outreach status pill-select (`hx-post /tracker/person/{key}/status`), email mailto, inline notes input (`hx-post /tracker/person/{key}/notes`, save-on-blur + toast), dates "added / last touch" (relative, exact on hover via `.tip`).
    - Jobs sub-list: each → role title link → `/jobs/{row_key}`, Fit% `.chip` (match_score), location, app-status pill-select (`hx-post /tracker/job/{row_key}/status`), applied/posted date.
    - If no people: friendly nudge row "No contacts yet — Add people →" (opens drawer prefilled).
- Keep `_ingest_drawer.html`. `_contact_row.html`/`_contacts_table.html` may be replaced by group partials (delete only if fully unreferenced).

## Routes (`app/app.py`)
- `GET /tracker`: call `tracker_groups`; cold → `contacts.html`, HX-Request → a `_groups.html` partial (the groups loop). Pass `groups`, `stats`, filter echoes, `person_status_flow`, `status_flow` (app stages).
- `POST /tracker/person/{key}/status` & `/notes`: keep; return updated person line partial (or 204 + HX-Trigger toast for notes).
- `POST /tracker/job/{row_key}/status`: set `application_status.status` + `status_changed_at = now`, `source='manual'` (UPSERT); return the updated job line partial (or 204 + toast). **Must write status_changed_at** so the Applied date + rollup work.
- Keep `/tracker/export`, `/tracker/companies`.

## CSS (`app/static/app.css`) — new classes, reuse tokens
`.tgroup` (card), `.tgroup__head` (grid, cursor pointer, hover bg), `.tgroup--offer/applied/contacted/needs/neutral` (3px left accent + subtle head tint), `.tgroup__body` (padding, divided sub-rows), `.tperson`/`.tjob` (row line, 48px+ min-height, 14px text), `.tprogress` (calm bar: track `--c-line`, fill `--c-good`), toolbar `.ttoolbar`. Respect reduced-motion (global rule already exists). All transitions 150–240ms via existing `--dur-*`.

## JS (in `contacts.html`, delegated on `#contacts-table` + document — survives HTMX swaps)
- Expand/collapse a group on header click (toggle `.is-open`; ignore clicks on links/selects/inputs/buttons). 
- "Add people" (group button + per-group nudge): prefill `#ingest-company` with the group's company, open drawer (`#drawer-toggle.checked=true`), focus paste.
- No standalone row-selection needed anymore (each group owns its Add-people action) — simpler than the previous version.

## Verify
- `python3 -m unittest test_tracker_queries test_tracker_migration test_linkedin_people` green (add/adjust tests for `tracker_groups`); other suites unaffected.
- App imports under `.venv/bin/python3`; restart `:8765`; screenshot `/tracker` (grouped, polished); confirm an applied company shows the green Applied pill and a needs-contact company shows amber.

## Accessibility checklist
4.5:1 contrast (use ramp -ink tokens on soft fills), color always paired with text/icon, 44px targets, visible focus rings, `aria-expanded` on group headers, labels on inputs, reduced-motion honored.
