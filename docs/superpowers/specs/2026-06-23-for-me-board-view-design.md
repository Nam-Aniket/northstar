# "For me" smart board view — design

**Date:** 2026-06-23
**Status:** Approved (brainstorm), pending spec review
**Goal:** Cut the noisy ~100-job board down to the 8–10 jobs the user can actually apply to, by hiding off-role jobs and sinking over-level (senior/manager) jobs — all derived from the user's own onboarding fields, and fully reversible with one toggle.

## Problem

The board surfaces three kinds of noise when the user searches (e.g.) "data analyst / business analyst":

1. **Off-role jobs** (Software Engineer, etc.). LinkedIn discovery (`00_search_linkedin_guest.py`) is keyword-only and fuzzy; every returned card is kept. The scorer computes a `role_family`, but `role_family()` in `generate_accepted_resumes.py` **defaults to `data_analyst` for any unrecognized title** — so a "Software Engineer" job is silently tagged `data_analyst` and looks on-target. Family is therefore *not* a reliable off-role signal.
2. **Over-level jobs** (Senior / Lead / Manager). Seniority is detected in `score_jobs.py` (`_SENIORITY_SENIOR` regex) but only applies a soft `×0.85` cap (and only if `config.seniority_cap` is set). No seniority level is stored or surfaced, so the board cannot filter or sort by it.
3. **No relevant filters.** `get_jobs()` (`app/queries.py`) supports only: text `q`, `sector`, `min_score`, `status`, `show_dismissed`, `starred_only`, `view`, `day`. Default sort is `match_score DESC`. Nothing narrows by role relevance or seniority.

## Key design decisions

- **No database migration.** Both new signals are computed at *read time* in Python when the board loads (~hundreds of jobs, not millions). This avoids the repo's known "no such column" migration landmine and keeps the change surgical.
- **Relevance is title-token based, not `role_family` based** — because `role_family` defaults to `data_analyst` and cannot distinguish a Software Engineer. Title-token overlap against the user's tracked titles is both the correct root-cause fix and self-deriving for any user (a SWE-targeting user gets a SWE default automatically).
- **The scorer is untouched.** `match_score` / `score_jobs.py` stay exactly as they are. Relevance and seniority are an orthogonal *presentation* layer. This keeps `test_ats_engine` green. Strengthening the scorer's off-target penalty is a possible later lever — out of scope (YAGNI).
- **Self-configuring from onboarding.** The "relevant" set and seniority cap derive from `tracked_positions` (and `tracked_locations`), so the default re-derives when the user changes their targets. An override dropdown is provided.

## Components

Each unit has one purpose, a clear interface, and is independently testable.

### 1. Target profile — `target_profile(con) -> dict`

Reads `tracked_positions` and `tracked_locations`. Returns:

```python
{
  "title_tokens": set[str],     # meaningful tokens across all tracked titles, seniority+filler words removed
  "tracked_titles": list[str],  # raw tracked titles, for token-overlap comparison
  "max_seniority_rank": int,    # highest seniority rank implied by any tracked title; default = MID rank if none
  "locations": list[str],
}
```

- **Where:** new module, e.g. `app/relevance.py` (pure functions, stdlib only, unit-testable).
- **Max seniority rule:** scan each tracked title for seniority words; `max_seniority_rank` = the highest rank found, or `MID` if none of the tracked titles carry a seniority word.
- **Edge:** no tracked positions → `title_tokens` empty → signals "unconfigured" so the view hides nothing.

### 2. Seniority parser — `parse_seniority(title: str) -> (label, rank)`

Ordered scale (rank in parens):

| label | rank | trigger words |
|---|---|---|
| intern | 0 | intern, internship |
| graduate | 1 | graduate, grad, trainee |
| entry | 1 | junior, entry, associate (early) |
| mid | 2 | *(default when nothing matches)* |
| senior | 3 | senior, snr, sr |
| lead | 3 | lead |
| principal | 4 | principal, staff |
| manager | 4 | manager, mgr |
| director | 5 | director, head of |
| exec | 6 | vp, vice president, chief, c-level |

- **Where:** `app/relevance.py`.
- Parse from `role_title` (primary). Title is reliable for seniority; JD scanning is not needed here.
- **Edge:** nothing matches → `mid` (rank 2). Never treat ambiguity as over-level (don't hide on uncertainty).

### 3. Relevance + level classification — `classify_job(job, profile) -> dict`

Given a shaped job dict and a `target_profile`, returns flags merged onto the job:

```python
{
  "on_target": bool,        # title shares >=1 meaningful token with any tracked title
  "seniority_label": str,
  "seniority_rank": int,
  "over_level": bool,       # seniority_rank > profile["max_seniority_rank"]
}
```

- **on_target rule:** normalize `role_title` → token set (drop seniority words + generic filler like "the", "remote", level words); `on_target = True` if it shares ≥1 meaningful token with the union of tracked-title tokens. Tunable threshold (start at ≥1 shared domain token).
- **Edge:** empty `title_tokens` (unconfigured user) → `on_target = True` for all (hide nothing).
- **Edge:** missing/unknown role data → fall back to title-overlap only (which is all this uses anyway).

### 4. View + filters — `get_jobs(...)` extension + `home()` route

- New view value `view="for_me"` becomes the **default** when the board loads.
- `for_me` behavior:
  - **Hide** jobs where `on_target` is False.
  - **Keep but sink** `over_level` jobs to the bottom.
  - **Sort:** `(on_target DESC, over_level ASC, match_score DESC, freshness DESC)`.
- `view="all"` (the **"Show everything"** toggle) → no relevance/level filtering; today's behavior.
- New adjustable controls on the board filter bar:
  - relevance toggle (on-target only ⇄ everything),
  - seniority-cap **override dropdown** (Auto / Entry / Mid / Senior+),
  - existing fit slider + search unchanged.
- Classification runs in Python after `_shape()`; filtering/sorting applied in the route or a thin `apply_for_me_view(jobs, profile, opts)` helper for testability.

### 5. UI — row tags + view banner

- Seniority tag (e.g. "Senior", "Manager") shown **only on over-level (sunk) rows**, so the user sees *why* a row is at the bottom. (Decision: tag over-level rows only, to keep the board clean. Can be flipped to all-rows later.)
- Default-view banner: `Showing N jobs matched to your targets · Show everything`.
- Templates: `jobs.html` (filter bar + banner) and the job row partial (tag).

## Data flow

```
tracked_positions / tracked_locations
        │
        ▼
  target_profile(con) ──────────────┐
                                     ▼
jobs (get_jobs _shape) ──► classify_job(job, profile) ──► apply_for_me_view ──► template
                                                              (hide off-target,
                                                               sink over-level,
                                                               sort)
```

## Error handling / fallbacks

- **No tracked roles:** profile `title_tokens` empty → hide nothing; banner reflects "all jobs" so the user never faces an empty board.
- **Unparseable seniority:** treat as `mid`; never over-level on ambiguity.
- **Missing role_family / package:** irrelevant — relevance uses title overlap only.
- **Toggle is always available:** "Show everything" guarantees the firehose is one click away.

## Testing plan

Unit tests (stdlib `unittest`, mirroring existing test files at repo root):

- **Seniority parser:** "Senior Data Analyst"→senior/3; "Data Analyst"→mid/2; "Analytics Manager"→manager/4; "Lead BI Developer"→lead/3; "Graduate Analyst"→graduate/1; "Software Engineer"→mid/2.
- **Relevance:** tracked ["Data Analyst","Business Analyst"] → "Software Engineer" off-target; "Senior Data Analyst" on-target **and** over-level; "Business Intelligence Analyst" on-target.
- **target_profile derivation:** tracked titles without senior words → `max_seniority_rank == MID`; tracked "Senior Data Analyst" → `max_seniority_rank == SENIOR`.
- **apply_for_me_view:** given a mixed job list, `for_me` hides off-target and sinks over-level to the bottom; `all` returns everything; empty profile hides nothing.
- **Regression:** existing `get_jobs` filters (q, sector, min_score, status, starred, day) still behave; `test_ats_engine` stays green (no scoring change).

## Out of scope (this spec)

- Changing the scorer or the off-target penalty in `score_jobs.py`.
- The fresh-jobs / posted-at work (separate spec; this view's "freshness DESC" sort consumes that signal if/when present, but does not depend on it).
- The combined people+tracker ingestion feature (separate, later).
