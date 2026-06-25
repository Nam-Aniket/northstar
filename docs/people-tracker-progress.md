# People-Tracker Build — Progress / Resume Log

**Last updated:** 2026-06-16 · **Status:** ✅ COMPLETE — built, 96 tests pass, live-smoked.

## What this is
Unified people-centric Tracker replacing the old `/people` + `/tracker` pages. Spec: [people-tracker-plan.md](people-tracker-plan.md).

## Outcome
All 5 phases done. **96 tests pass** (72 feature + 24 regression). Live smoke on :8799 confirmed:
- `/tracker` renders the table-first page (filters, columns, drawer).
- Ingesting a real LinkedIn People-tab paste produced **7 correct contacts** with generated emails; junk dropped (`Ayaz M.`, `LinkedIn Member`, `Muhammad H.`); `M.Uzair Tariq` routed to needs-review; `bradley gooding` re-cased.
- Rows persisted; amber ❗ placeholder rows shown for the 191 real companies with jobs but no contacts.
- Nav collapsed to one **Tracker** link; old `/people` now returns 404; Board/Insights/Builder/Setup all still load.
- Decoupling holds: `tracker_people` is absent from `sync.py`'s drop/create list — the pipeline can't wipe it.

## Phase checklist
- [x] **P0 — Schema + migration** ✅
- [x] **P1 — Parser + email engine** ✅
- [x] **P2 — Query layer + fuzzy company resolution** ✅
- [x] **P3 — Routes + templates** ✅
- [x] **P4 — Cutover + cleanup** ✅
- [x] **Live browser smoke** ✅ (7 contacts ingested + screenshots taken; test rows then deleted)

## Build method note
First workflow run `wf_18ad9531-0a9` crashed all build agents via an invalid `model:'fable'` subagent override; P0–P2 still landed + verified. P3–P4 finished via direct `implementer` (Sonnet) agents with main-thread (Fable) verification. **Lesson: never pass `model:'fable'` to subagents — omit it to inherit the session model, or use sonnet/haiku/opus.**

## Minor polish backlog (non-blocking, feature works without these)
- needs-review rows have no visible "review" badge/filter yet (e.g. `M.Uzair Tariq` ingests but isn't flagged in the UI).
- Ingest toast is minimal (`"Added 7."`); plan wanted counts + match_type (`"Added 7, 1 needs review, created new company Calrom"`).
- `make_email` keeps a dot inside an abbreviated first name (`M.Uzair` → `m.uzair.tariq@…`); only affects needs-review rows.
- "Not contacted" rows render neutral rather than the planned blue tint.

## UX fixes — round 2 (2026-06-16, found on first real use)
- **Company selector incomplete** — root cause: `company_suggestions` (feeds the drawer datalist + the filter dropdown) only unioned `companies`/`tracker_people`/`manual_company`, never the `jobs` table, so the ~167 job-only companies (the placeholder rows) weren't selectable. Now also unions `jobs`. Datalist 25 → 214 companies. Also removed the `[:20]` cap and the unreliable hx-get datalist type-ahead (native autocomplete instead).
- **Clicking a row/job wiped the table with no way back** — the whole amber `<tr>` was an `hx-get` that filtered the table to that company. Removed it.
- **"N jobs" disclosure collapsed sub-second** — same cause: the row `hx-get` re-rendered the table and closed the just-opened `<details>`. Fixed by removing the row `hx-get`; native `<details>` now stays open.
- **Add-people-from-row flow** — added a small delegated-JS layer in `contacts.html`: click a row → select (highlight + remember company); top **Add people** opens the drawer pre-filled with the selected company; per-row **"Add people →"** opens the drawer pre-filled directly; clicking outside the table deselects.
- **Bonus** — placeholder rows displayed the lowercase normalized key as the company name; now show the real name (added `company` to the job dict in `tracker_table`).

Verified: 72 feature tests pass; `/tracker` + other pages 200; datalist = 214 companies; destructive `hx-get` gone; no JS console errors; screenshot confirms proper-cased names + link styling. The interactive click behaviors were not automated (no playwright/selenium installed) — markup wiring, render, and a clean console were verified instead. Files: `_ingest_drawer.html`, `_contact_row.html`, `contacts.html`, `app/static/app.css`, `app/queries.py`. (The now-unused `/tracker/companies` route was left in place, harmless.)

## Visual redesign — grouped-by-company (2026-06-16)
Spec: `docs/people-tracker-redesign-plan.md`. Decisions: layout = grouped-by-company, energy = polished & calm. Built (implementer) + main-thread (Fable) aesthetic pass.
- New query `queries.tracker_groups()` returns `(groups, stats)`: one card per company (union of companies with tracker_people OR jobs), people[] + jobs[] nested, applied/contacted counts, `group_status` rollup (offer→interview→applied→contacted→needs_contact→neutral), last_activity, is_placeholder; stats = scoreboard numbers.
- New templates `_company_group.html` (collapsible card: header w/ status pill + meta chips + Add-people; body = people + jobs sub-rows + empty nudge), `_tperson.html`, `_tjob.html`, `_groups.html`. `contacts.html` rewritten = scoreboard progress + KPIs, sticky `.ttoolbar` (search/outreach/app-status/needs-contacts/sort), groups, drawer, delegated JS (expand/collapse + add-people prefill).
- **"Applied now translates"**: per-job `application_status` rolled up to the company → green Applied pill + accent. Job-status route reuses `queries.set_status` (writes `status_changed_at`).
- Aesthetic pass (Fable): coloured pill-selects (`.tpill`/`.tjpill` by value), relative dates via new `_reldate` Jinja filter (today/3d ago/2w ago, exact on hover), ghost note inputs (`.tnote`), rebalanced person grid so titles stop truncating. CSS appended to `app/static/app.css`.
- Verified: 85 tests green; app imports (53 routes); `/tracker` 200, 0 server errors; live screenshots confirm grouped cards + green Applied rollup (real data: 20 applied jobs) + amber needs-contact + coloured status pills + relative dates. Old `_contact_row.html`/`_contacts_table.html` now unreferenced (left on disk, harmless).
- `:8765` restarted on the new code via background uvicorn (no `setsid` on macOS — use plain `nohup ... &` or the one-liner below to restart).

## Filter/sort audit + fixes (2026-06-16)
Systematic audit (3-layer trace + empirical matrix on a DB copy) found, beyond the reported ones:
- `added` sort no-op for 189/191 (key only from people.created_at) and `activity` no-op for 181/191 → **fixed**: every company now gets a key from people dates OR job posted/changed dates.
- `Company A–Z` actually sorted Z–A (global `dir=desc` default) → **fixed**: per-sort direction (names asc, dates/fit/applied desc); route+query `dir` default now `""`.
- `Needs contacts only` returned 183/191 and EXCLUDED applied → **redefined** (user choice) to "any company with jobs but no contacts" (189), excludes dead.
- Filter combinations were fragile (enumerated `hx-include` lists) → **fixed**: every toolbar control uses `hx-include="closest .ttoolbar"`, so all filters always travel together.
- Search worked server-side; in-browser it looked dead because matches rendered collapsed → **fixed**: `expand_all` auto-expands groups whenever a filter/search is active; search trigger now `keyup changed delay:300ms`.
- **New**: auto-hide (`is_archived`) — companies whose apps are all closed/rejected, OR cold (no people, no active app) and idle >7 days, are hidden by default. `Show all` toggle + `stats.archived_count` ("N hidden") reveal them. Any explicit filter/search/show-all suspends archiving so nothing is unreachable. Default view dropped 191 → 8.
- Verified: 97 tests pass (incl. new `test_tracker_filters.py`, the audit-as-regression); HTTP matrix green; screenshot confirms decluttered board + Show-all toggle. Files: `app/queries.py` (tracker_groups), `app/app.py` (route), `contacts.html`, `_company_group.html`, `test_tracker_filters.py`.

## Aurum palette redesign (2026-06-16)
Driven by user's `~/Downloads/color-palette.svg` (gold #f9c13a, bronze #b08542, slate #3c3d48, orange #f48144, yellow #f4d424). Replaced the coral "Ramos" palette via the `--c-*` tokens in `app/static/app.css` (`:root` + `[data-theme="dark"]`), so one swap re-skins the whole app. Mapping: orange→`--c-accent` (primary), gold→`--c-warn` (amber/highlight), slate→`--c-ink`+topbar+dark surfaces, bronze→button/brandmark gradient end. Kept green/blue/red semantic (applied=green, follow-up=blue, closed=red) for status legibility; neutrals kept clean (not cream — impeccable anti-slop). Also updated `--shadow-glow`, the hardcoded `rgb(204 58 26)` gradient (→`176 74 28`), and `.ns-topbar` (near-black→slate). Verified: Board + Tracker screenshots show cohesive slate/orange/gold/green; app imports fine.
- **Pending (user asked, not yet done): targeted impeccable polish pass** — critique + audit + typeset + layout + animate + delight per page (depth = "targeted polish", keep base layout). Optional: generate `DESIGN.md` via impeccable `document`.

## "Mirai" theme (2026-06-16) — supersedes Aurum
Palette + aesthetic matched to impeccable.style/neo-mirai (user request). Exact OKLCH→sRGB values from its `styles.css`, converted and dropped into Northstar's `--c-*` tokens (light + dark). Approach = "papery, non-shiny, luxe":
- **Palette (light):** warm sand bg `#f8e9d2`, rice cards `#fcf4e6`, warm ink `#1c1304`, burnt-orange accent `#e55900`/`#b53700`, gold warn `#cc8800`. **Dark = teal-black "night"** (`#001411`) — also the topbar in both themes.
- **Harmonized semantics (Fable's call, not stock):** success=**teal** `#009d82`, danger=**clay** `#aa3a2f`, info=**steel blue** `#346e9b`; offer=gold. Teal echoes neo-mirai's teal-black dark + complements gold. Kept contrast-safe (secondary text on `--c-ink2`, not muted).
- **Matte treatment:** `--shadow*` rebuilt as soft diffuse warm shadows (no glow); `--shadow-glow` redefined matte so all hover-glows became matte; flattened glossy gradients on `.btn/.btn-apply/.brandmark/.pavatar` to solid fills; added a faint SVG-feTurbulence **paper-grain** overlay (`body::after`, multiply ~.05 / soft-light in dark).
- **Type:** added **Chakra Petch** (Google Fonts) as the display font (`font-display` + `.page-h`); Urbanist stays body.
- Files: `app/templates/base.html` (font link + tailwind display), `app/static/app.css` (tokens, shadows, gradients, grain, page-h). Verified: Board + Tracker screenshots — warm/matte/teal-gold cohesive across pages; one token set re-skins everything.
### Phase 2 polish (in progress, 2026-06-16)
- **Topbar redesigned** — was a dark slate bar that fought the warm theme; now a warm frosted light header (`rgb(var(--c-bg)/.82)` + blur + warm hairline). Removed the dark-bar child overrides so default light nav/buttons apply; kept the filled-orange active pill + orange Run. Matches neo-mirai's light paper header.
- **Hovers smoothed** — redefined `--ease-bounce` from an overshoot curve to `cubic-bezier(.22,1,.36,1)` (gentle ease-out), so every hover lift/colour transition becomes the subtle neo-mirai-style motion instead of springy bounce. (No per-rule edits needed — they all reference the var.)
- **Resume preview decision** — kept export-faithful (navy professional document); only the Builder *chrome* is themed Mirai. Theming the document orange would misrepresent the .docx export and read unprofessionally. Verified on screenshot.
- **Insights + Builder verified cohesive** on the Mirai theme (warm cards, Chakra Petch titles, teal=positive / orange=attention chart semantics, smooth hovers). Per-page consistency essentially closed via the shared token kit.
- **Phone bug fixed:** contact phone was `1 0451059637` (US/Canada code). Corrected to `+61 451 059 637` (AU international) in BOTH stores: `config.json` `identity.contact` (drives generated/exported .docx) and `uploads/parsed_resume.json` `phone` (drives the live Builder form/preview via `/builder/prefill`). NOTE root cause is upstream in `resume_parser._extract_contact` (it emitted the stray `1`); re-uploading the same resume would reintroduce it unless that parser is hardened to normalise AU mobiles to `+61` — not yet done.
- Optional remaining: DESIGN.md via impeccable `document`; resume_parser AU-phone normalisation.

## Key facts
- Repo root: `/Users/aniketnamjoshi/Documents/northstar`. Interpreter: `.venv/bin/python3` (base `python3` lacks fastapi). App: `.venv/bin/uvicorn app.app:app`. DB: `app/control_panel.db` (hardcoded path).
- Restart the live server: `cd ~/Documents/northstar && nohup .venv/bin/uvicorn app.app:app --host 127.0.0.1 --port 8765 >/tmp/ns8765.log 2>&1 &`
- Tests: `python3 -m unittest test_<module>` from repo root.
- Pre-smoke DB backup: `/tmp/control_panel.db.backup-132944` (in case it's ever needed).
