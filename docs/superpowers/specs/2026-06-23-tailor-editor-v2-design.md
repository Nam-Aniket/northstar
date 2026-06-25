# Tailor & Edit v2 - Evidence-Bullet Placement + Granular Editing - Design Spec

**Date:** 2026-06-23
**Status:** Approved design (pre-implementation-plan)
**Scope:** Upgrades to the existing per-job resume editor (`/jobs/{row_key}/edit`). Deterministic, no LLM. This is the change that ships first; the fresh-jobs upgrade (`2026-06-23-fresh-jobs-upgrade-design.md`) follows and reuses the single-job tailoring surface this hardens.

## Goal

Turn the per-job editor from "claim a skill, it lands in the skills line" into "claim a skill, drop the real evidence bullet for it into the right experience" - so a claimed skill produces top-half, in-context evidence (what real ATS ranking and a human recruiter actually reward), not just a keyword in a list. Plus make the editor edit cleanly bullet-by-bullet and let the user preview the cover letter, not just the resume.

## Honest framing (drives the design)

- Placing a bullet moves the **keyword-coverage meter** (it scans resume text) and real-world ATS density. It does **not** move Northstar's internal **Fit %** - that is driven by `skills.json`, which the existing claim-skill already updates. Two different signals, both real. The UI must not imply placing a bullet re-scores the Fit gauge.
- Bullets are **fact-bank only**. The tool never invents experience. Every placeable bullet is one the user authored (either earlier, in `facts.json`, or just now via the write-once box). A fact-bank bullet is authored for a specific role (slot), so placement targets that role - it cannot be dropped under a company the user did not work for.

## Current state (verified in code)

- Editor: `app/templates/job_editor.html` - left form (textareas) + right read-only live preview, all client-side JS. Experience bullets are one `\n`-separated textarea per role (`.exp-bullets`). Cover letter already has an editable textarea (`f-cover`, line 98) that bundles into the `.zip` download, but **no formatted preview**.
- Claimable gaps: `app/templates/_job_fit.html` - each missing skill is a button that `hx-post`s to `/jobs/{row_key}/skills/add` and swaps `#job-fit` (outerHTML). Client JS also mirrors the claimed word into the skills textarea (`job_editor.html` ~line 445).
- Prefill: `app/app.py` `_editor_prefill` (line 784) builds the draft via `generate_accepted_resumes.build_content`; experiences come from `gen.EXPERIENCE_SLOTS` + `content["bullets_by_slot"][slot]`.
- Fact bank: `generate_accepted_resumes.FACT_BANK[slot]` = list of `{"text": str, "evidences": [skill, ...]}`. `EXPERIENCE_SLOTS` = `[(header_text, slot), ...]`; `_split_slot_header` parses "Role | Company | Dates". Source of truth is `facts.json` (`facts_bridge.py`).
- Skill claim writes `skills.json` via `queries.add_supported_skill`, guarded by `queries.SKILLS_LOCK`.

## In scope (4 parts)

### Part A - Evidence-bullet placement (fact-bank)

When a skill is claimed (or already evidenced), surface the fact-bank bullets that evidence it and are **not already in this job's resume**, each as a card labeled with its role + an **Add** button. Add inserts that bullet into the matching experience block in the editor.

- New read endpoint: `GET /jobs/{row_key}/bullets?skill=<label>` returns the placeable bullets for that skill: for each slot, the `FACT_BANK[slot]` entries whose `evidences` (case-insensitive) include the skill, minus the bullets already selected for this job (compare against `content["bullets_by_slot"][slot]`). Each item: `{slot, role_header, text}`.
- Rendered as a partial (`_bullet_suggestions.html`) appended in the Match-fit panel under the claimed skill. Each card: role header (so the user sees where it lands), bullet text, **Add** button carrying `data-add-bullet` (the text) + `data-target-role` / `data-target-company` (from the slot header).
- Client JS handles **Add**: find the `.exp-block` whose role+company match the target (normalised compare); append the bullet as a new bullet row (Part C's row), dedupe if already present, `scheduleRender()`. Then remove/disable that card.
- When the claim-skill response (`_job_fit.html`) comes back, it triggers a fetch of the bullet suggestions for the just-claimed skill (HTMX `hx-trigger` or a small JS hook), so claiming a gap immediately shows its placeable bullets.
- If a skill has placeable bullets but the user has not claimed it yet, the same suggestions are reachable - claiming and bullet-surfacing are independent reads, but the everyday entry point is "claim gap -> see its bullets."

### Part B - Write-once new bullet -> fact bank

When a claimed skill has **no** fact-bank bullet evidencing it, the suggestions partial shows an inline write box instead of cards:

- Textarea for the bullet (user's own words) + a `<select>` of the user's real experiences (the `EXPERIENCE_SLOTS` headers) to choose which role it belongs to.
- **Save** posts to `POST /jobs/{row_key}/bullets/add` with `{skill, slot, text}`. Server appends `{"text": text, "evidences": [skill]}` to `FACT_BANK[slot]` in `facts.json` (new writer `add_fact_bullet(slot, text, skill)`, guarded by a `FACTS_LOCK`), and returns the saved bullet as an Add-card.
- Client then inserts it into the chosen experience (same path as Part A) so it lands in this job immediately, and it is now reusable for every future job.
- Honesty guard: `skill` must be a current gap or evidenced skill for this job (mirror the existing claim-skill validation); `slot` must be a known slot. Reject otherwise.

### Part C - Granular bullet editing

Replace each experience's single `.exp-bullets` textarea with **per-bullet rows**:

- Each bullet = one editable input (or auto-grow textarea) + an **X** delete button for that single bullet, in a `.bullet-list` container per experience block.
- An **"+ Add bullet"** affordance per experience (manual blank row), so manual authoring still works.
- `collect()` reads bullets from the rows instead of splitting a textarea on `\n`. `applyData()` builds rows from the prefill bullet arrays. Block-level remove (whole role) stays. Live render + coverage meter unchanged downstream (they already consume `experiences[].bullets`).
- Projects keep their existing textarea (out of scope to granularise) unless trivial to share the component.

### Part D - Tabbed preview: Resume | Cover letter

The right preview pane gets two tabs:

- **Resume** - today's live paper, unchanged.
- **Cover letter** - a new formatted page rendered from the `f-cover` textarea (paragraphs split on blank lines), styled like a letter, updating live on edit. Editing stays in the left `f-cover` box.
- Tabs are client-side only (toggle visibility); both render from existing data. Download is unchanged (still bundles resume + cover).

## Data model changes

- None in SQLite. `facts.json` gains entries in existing `FACT_BANK[slot]` arrays (append-only via the new writer). `facts.json` is already the source of truth and is gitignored PII.

## New / changed surfaces (file map)

- `app/app.py` - new `GET /jobs/{row_key}/bullets` (placeable suggestions for a skill) and `POST /jobs/{row_key}/bullets/add` (write-once persist). Small helper to compute placeable bullets from `FACT_BANK` minus selected.
- `generate_accepted_resumes.py` - expose a helper to list `FACT_BANK[slot]` pools + which are selected for a job (reuse `build_content`); or compute in app.py from existing returns. No scoring change.
- `facts_bridge.py` (or a new `facts_store.py`) - `add_fact_bullet(slot, text, skill)` writer + `FACTS_LOCK`, atomic write of `facts.json`.
- `app/templates/_job_fit.html` - hook to load bullet suggestions after a claim.
- `app/templates/_bullet_suggestions.html` (new) - Add-cards + write-once box partial.
- `app/templates/job_editor.html` - per-bullet rows (Part C), bullet-add handler (Part A/B), Resume|Cover tabs (Part D), cover render function.
- `app/static/app.css` - styles for bullet rows, suggestion cards, tabs.

## Error handling

- `facts.json` missing/unwritable: write-once Save returns an inline error, the bullet still inserts into the current job (not lost), just not persisted for reuse.
- Skill with no slot match / unknown slot: reject with a clear message; no silent write.
- Bullet already present in the target experience: Add is a no-op (dedupe), card marked "added."
- Suggestion fetch failure: Match-fit panel still works; suggestions area shows a quiet "couldn't load bullets" without breaking the claim flow.

## Testing plan

- `add_fact_bullet`: appends correct `{text, evidences}` to the right slot in `facts.json`; atomic; concurrent-safe under `FACTS_LOCK`; rejects unknown slot.
- Placeable-bullets computation: for a given skill, returns fact-bank bullets that evidence it and excludes ones already selected for the job; case-insensitive evidence match.
- Honesty guard: `/bullets/add` rejects a skill that is not a gap/evidenced for the job; rejects unknown slot.
- Editor JS (lightest-touch, manual or a small DOM test): per-bullet delete removes one bullet only; Add-card inserts into the role-matched experience; write-once insert + persist round-trips; Resume|Cover tabs toggle and the cover renders paragraphs.
- Regression: `test_ats_engine` stays green (no scoring/taxonomy change expected); existing editor download still produces resume + cover.

## Out of scope / follow-ups

- WYSIWYG editing directly on the rendered resume (click a bullet on the paper to edit). Deferred; the form stays the edit surface.
- Granular per-bullet rows for Projects (only Experience in v1).
- Per-job persistence of editor edits across reopen (today the editor regenerates from prefill each open; only the fact bank persists). Out of scope.
- Re-running set-cover after a write-once add (the new bullet is inserted directly this session; future jobs pick it up via normal selection).

## Assumptions to verify during planning (Stage 2)

1. `build_content` returns (or can cheaply expose) both the full `FACT_BANK[slot]` pool and the selected `bullets_by_slot[slot]` so "placeable = pool minus selected" is computable without re-deriving selection.
2. The `EXPERIENCE_SLOTS` header text matches the experience `role`/`company` the editor renders, so client-side role-matching for Add is reliable (else carry an explicit slot id into the exp block).
3. `facts.json` write path + lock interacts safely with `config`/generation caches (mirror how `add_supported_skill` + `reset_banks` is handled), so a newly written bullet is visible to the next prefill.
