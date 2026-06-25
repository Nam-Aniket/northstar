# Editor: auto-place a claimed-skill bullet into the right experience — design

**Date:** 2026-06-25
**Status:** Approved (brainstorm), pre-plan
**Goal:** When the user claims a skill (or writes a new bullet) in the per-job tailoring editor, the bullet should default to landing in the *best-fit* experience automatically — no manual role-picking — while staying fully overridable and truthful.

## Problem

Today in the editor:
- Fact-bank bullets already carry their slot (`role1`/`role2`/`role3`), so the "Add to resume" button places them into the correct experience block.
- BUT when the user claims a skill that has **no existing bullet**, the write-once box makes them choose which role the new line goes into, and nothing recommends the right home. That's the friction this removes.

## Key decisions

- **No schema change.** Slots already exist on experiences (`exp-block.dataset.slot`), in `facts.json` (`EXPERIENCE_SLOTS`, `FACT_BANK`), and in the editor form. We only add a *recommendation* and default the existing controls to it.
- **Scorer and honesty guard untouched.** Only JD skills (evidence/gaps) remain claimable; placement does not change what is truthful. `match_score` / `score_jobs.py` are not touched.
- **Pure, testable picker.** The best-fit logic is one pure function with deterministic output, unit-tested in isolation.

## Component: the picker

`best_slot_for_skill(skill, fact_bank, experience_slots) -> str` (slot id), in `generate_accepted_resumes.py` (next to `add_fact_bullet` / `placeable_bullets_for_skill`).

Ranking, in order:
1. **Evidence overlap** — score each slot by how many of its existing fact-bank bullets evidence the *same skill or its ontology group* as the claimed skill (a role that already proves "Machine Learning"/"NLP" is the natural home for an ML line). Use the existing ontology grouping that already tags bullets' `evidences`.
2. **Tie-break: recency** — prefer the most recent role (first in `EXPERIENCE_SLOTS`, i.e. `role1`), since recent experience is the strongest place to add.
3. **Fallback** — if no slot has any overlap (or the bank is empty), return the most recent slot (`role1`).

## Wire-in (read-time only)

- `GET /jobs/{row_key}/bullets?skill=`: compute `suggested_slot = best_slot_for_skill(...)` and include it (plus its role header) in the response context for `_bullet_suggestions.html`.
- `_bullet_suggestions.html` + editor JS: default the write-once **role selector** to `suggested_slot`, and label the suggestion "Suggested: add to {role header}". The user can still pick a different experience.
- `POST /jobs/{row_key}/bullets/add`: unchanged — it already accepts `slot`; it simply receives the defaulted value.

## Data flow

```
claim skill ──► GET /bullets?skill ──► placeable bullets (existing)
                                   └──► best_slot_for_skill() ──► suggested_slot
                                                                      │
                                          _bullet_suggestions.html ◄──┘
                                          (write-once role selector defaults here)
                                                     │  user keeps or overrides
                                          POST /bullets/add(slot) ──► facts.json (existing add_fact_bullet)
```

## Error handling / fallbacks

- Empty fact bank or unknown skill → return most-recent slot (`role1`); never error, never block placement.
- Skill not in this job's JD → already rejected by the existing honesty guard; picker is never reached for it.
- Single experience → picker trivially returns it.

## Testing plan

Unit tests for `best_slot_for_skill` (stdlib `unittest`, mirroring repo test files):
- ML-group skill → the slot whose existing bullets already evidence ML/NLP, even if it's not the most recent role.
- Skill with overlap in *two* slots → the most recent of the two (tie-break).
- Skill with no overlap anywhere → most recent slot (`role1`) fallback.
- Empty `FACT_BANK` → `role1` fallback, no error.
- Regression: existing editor bullet routes still return their current payload shape plus the new `suggested_slot` field; `test_ats_engine` stays green (no scoring change).

## Out of scope

- AI-drafting the bullet text (deferred — separate, opt-in LLM feature).
- Drag-and-drop placement (deferred — pure UX polish).
- Any change to fact-bank bullet *selection* (`select_bullets`) or scoring.
