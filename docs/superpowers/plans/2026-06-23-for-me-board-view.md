# "For me" Smart Board View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the jobs board open to a "For me" view that hides off-role jobs and sinks over-level (senior/manager) jobs, derived from the user's tracked roles, with a one-click "Show everything" escape.

**Architecture:** A new pure-Python module `app/relevance.py` computes two read-time signals per job — title-token relevance against the user's tracked titles, and a seniority level parsed from the job title. The `home()` route applies these as a post-filter/sort over the existing `get_jobs()` result. No database migration; the scorer (`score_jobs.py`) is untouched.

**Tech Stack:** Python 3 stdlib (`re`, `unittest`), SQLite (`app/db.py`), FastAPI + Jinja2 templates, HTMX.

**Reference spec:** `docs/superpowers/specs/2026-06-23-for-me-board-view-design.md`

---

## File Structure

- **Create** `app/relevance.py` — pure functions: `parse_seniority`, `title_tokens`, `target_profile`, `classify_job`, `apply_for_me_view`. One responsibility: relevance/seniority classification. No DB writes; only reads `tracked_positions`.
- **Create** `test_relevance.py` (repo root, matching existing `test_tracker_queries.py` convention) — unit tests for every function in `app/relevance.py`.
- **Modify** `app/app.py` `home()` — build the target profile, accept `everything` + `level` params, apply `apply_for_me_view`, pass view metadata to the template.
- **Modify** `app/templates/jobs.html` — "Show everything" toggle, seniority-cap dropdown, "Showing N matched to your targets" banner.
- **Modify** `app/templates/_job_card.html` — seniority tag on over-level rows.

Ranks (shared vocabulary used across all tasks):

```
intern=0  graduate=1  entry=1  mid=2  senior=3  lead=3  principal=4  manager=4  director=5  exec=6
MID_RANK = 2   (the default when a title carries no seniority word)
```

---

### Task 1: Seniority parser

**Files:**
- Create: `app/relevance.py`
- Test: `test_relevance.py`

- [ ] **Step 1: Write the failing test**

```python
# test_relevance.py
"""test_relevance.py — For-me board view relevance/seniority classification."""
import unittest
from app import relevance


class TestParseSeniority(unittest.TestCase):
    def test_plain_title_is_mid(self):
        self.assertEqual(relevance.parse_seniority("Data Analyst"), ("mid", 2))

    def test_senior(self):
        self.assertEqual(relevance.parse_seniority("Senior Data Analyst"), ("senior", 3))

    def test_lead(self):
        self.assertEqual(relevance.parse_seniority("Lead BI Developer"), ("lead", 3))

    def test_manager(self):
        self.assertEqual(relevance.parse_seniority("Analytics Manager"), ("manager", 4))

    def test_principal_staff(self):
        self.assertEqual(relevance.parse_seniority("Principal Data Engineer"), ("principal", 4))
        self.assertEqual(relevance.parse_seniority("Staff Analyst"), ("principal", 4))

    def test_director_head_of(self):
        self.assertEqual(relevance.parse_seniority("Director of Analytics"), ("director", 5))
        self.assertEqual(relevance.parse_seniority("Head of Data"), ("director", 5))

    def test_exec(self):
        self.assertEqual(relevance.parse_seniority("VP of Engineering"), ("exec", 6))

    def test_graduate_and_intern(self):
        self.assertEqual(relevance.parse_seniority("Graduate Analyst"), ("graduate", 1))
        self.assertEqual(relevance.parse_seniority("Data Analyst Intern"), ("intern", 0))

    def test_junior_is_entry(self):
        self.assertEqual(relevance.parse_seniority("Junior Data Analyst"), ("entry", 1))

    def test_senior_word_not_matched_inside_other_word(self):
        # "Engineer" must not trip "entry"/"senior"; plain SWE title is mid
        self.assertEqual(relevance.parse_seniority("Software Engineer"), ("mid", 2))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_relevance.TestParseSeniority -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.relevance'` (or `AttributeError: parse_seniority`).

- [ ] **Step 3: Write minimal implementation**

```python
# app/relevance.py
"""app/relevance.py — read-time relevance + seniority classification for the board.

Pure functions, stdlib only. No DB writes. The scorer is untouched; these signals
are an orthogonal presentation layer used by the "For me" board view.
"""
from __future__ import annotations

import re

MID_RANK = 2

# Checked most-senior first so a multi-word title resolves to its highest level.
# Each entry: (label, rank, [trigger phrases]). Phrases are matched on word
# boundaries against the lowercased title.
_SENIORITY_RULES = [
    ("exec",      6, ["vp", "vice president", "chief", "ceo", "cto", "cfo", "c-level"]),
    ("director",  5, ["director", "head of"]),
    ("manager",   4, ["manager", "mgr"]),
    ("principal", 4, ["principal", "staff"]),
    ("senior",    3, ["senior", "snr", "sr"]),
    ("lead",      3, ["lead"]),
    ("entry",     1, ["junior", "jnr", "entry", "associate"]),
    ("graduate",  1, ["graduate", "grad", "trainee"]),
    ("intern",    0, ["intern", "internship"]),
]


def parse_seniority(title: str) -> tuple[str, int]:
    """Return (label, rank) parsed from a job title. Default ('mid', 2)."""
    t = (title or "").lower()
    for label, rank, phrases in _SENIORITY_RULES:
        for p in phrases:
            if re.search(r"\b" + re.escape(p) + r"\b", t):
                return (label, rank)
    return ("mid", MID_RANK)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest test_relevance.TestParseSeniority -v`
Expected: PASS (all 11 assertions).

- [ ] **Step 5: Commit**

```bash
git add app/relevance.py test_relevance.py
git commit -m "feat(board): seniority parser for For-me view"
```

---

### Task 2: Title tokens + target profile

**Files:**
- Modify: `app/relevance.py`
- Test: `test_relevance.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to test_relevance.py
import sqlite3
from app.db import init_schema


def _seed_positions(titles):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    init_schema(con)
    for t in titles:
        con.execute(
            "INSERT INTO tracked_positions (title, display, created_at) VALUES (?,?,?)",
            (t, t, "2026-06-23"),
        )
    con.commit()
    return con


class TestTitleTokens(unittest.TestCase):
    def test_drops_seniority_and_filler(self):
        self.assertEqual(relevance.title_tokens("Senior Data Analyst (Remote)"), {"data", "analyst"})

    def test_software_engineer_tokens(self):
        self.assertEqual(relevance.title_tokens("Software Engineer II"), {"software", "engineer"})


class TestTargetProfile(unittest.TestCase):
    def test_tokens_union_across_positions(self):
        con = _seed_positions(["Data Analyst", "Business Analyst"])
        p = relevance.target_profile(con)
        self.assertEqual(p["title_tokens"], {"data", "analyst", "business"})

    def test_max_rank_mid_when_no_senior_word(self):
        con = _seed_positions(["Data Analyst", "Business Analyst"])
        p = relevance.target_profile(con)
        self.assertEqual(p["max_seniority_rank"], 2)

    def test_max_rank_lifts_with_senior_title(self):
        con = _seed_positions(["Senior Data Analyst"])
        p = relevance.target_profile(con)
        self.assertEqual(p["max_seniority_rank"], 3)

    def test_empty_profile_when_no_positions(self):
        con = sqlite3.connect(":memory:"); con.row_factory = sqlite3.Row
        init_schema(con)
        p = relevance.target_profile(con)
        self.assertEqual(p["title_tokens"], set())
        self.assertEqual(p["max_seniority_rank"], 2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_relevance.TestTitleTokens test_relevance.TestTargetProfile -v`
Expected: FAIL with `AttributeError: module 'app.relevance' has no attribute 'title_tokens'`.

- [ ] **Step 3: Write minimal implementation**

```python
# Append to app/relevance.py

# Words removed before comparing titles: seniority words + generic filler.
_SENIORITY_WORDS = {
    "vp", "vice", "president", "chief", "ceo", "cto", "cfo",
    "director", "head", "manager", "mgr", "principal", "staff",
    "senior", "snr", "sr", "lead", "junior", "jnr", "entry",
    "associate", "graduate", "grad", "trainee", "intern", "internship",
}
_FILLER_WORDS = {
    "the", "a", "an", "of", "and", "or", "to", "for", "in", "at", "with",
    "remote", "hybrid", "onsite", "contract", "permanent", "fulltime", "parttime",
    "full", "part", "time", "new", "role", "job", "position", "officer",
    "i", "ii", "iii", "iv",
}
_STOP = _SENIORITY_WORDS | _FILLER_WORDS
_TOKEN_RE = re.compile(r"[a-z0-9&]+")


def title_tokens(title: str) -> set[str]:
    """Meaningful lowercase tokens of a title, with seniority + filler removed."""
    return {w for w in _TOKEN_RE.findall((title or "").lower()) if w not in _STOP}


def target_profile(con) -> dict:
    """Derive the user's relevance profile from tracked_positions.

    Returns {title_tokens, tracked_titles, max_seniority_rank}.
    Empty title_tokens signals 'unconfigured' -> the view hides nothing.
    """
    rows = con.execute("SELECT title FROM tracked_positions").fetchall()
    titles = [r["title"] for r in rows if r["title"]]
    tokens: set[str] = set()
    max_rank = MID_RANK
    for t in titles:
        tokens |= title_tokens(t)
        max_rank = max(max_rank, parse_seniority(t)[1])
    return {
        "title_tokens": tokens,
        "tracked_titles": titles,
        "max_seniority_rank": max_rank,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest test_relevance.TestTitleTokens test_relevance.TestTargetProfile -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/relevance.py test_relevance.py
git commit -m "feat(board): target profile derived from tracked positions"
```

---

### Task 3: Per-job classification

**Files:**
- Modify: `app/relevance.py`
- Test: `test_relevance.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to test_relevance.py
class TestClassifyJob(unittest.TestCase):
    def setUp(self):
        self.profile = {"title_tokens": {"data", "analyst", "business"},
                        "tracked_titles": ["Data Analyst", "Business Analyst"],
                        "max_seniority_rank": 2}

    def test_off_target_software_engineer(self):
        j = relevance.classify_job({"role_title": "Software Engineer", "match_score": 60},
                                   self.profile, cap=2)
        self.assertFalse(j["on_target"])

    def test_on_target_and_over_level_senior(self):
        j = relevance.classify_job({"role_title": "Senior Data Analyst", "match_score": 70},
                                   self.profile, cap=2)
        self.assertTrue(j["on_target"])
        self.assertEqual(j["seniority_label"], "senior")
        self.assertTrue(j["over_level"])

    def test_on_target_in_level(self):
        j = relevance.classify_job({"role_title": "Business Intelligence Analyst", "match_score": 65},
                                   self.profile, cap=2)
        self.assertTrue(j["on_target"])
        self.assertFalse(j["over_level"])

    def test_unconfigured_profile_marks_all_on_target(self):
        empty = {"title_tokens": set(), "tracked_titles": [], "max_seniority_rank": 2}
        j = relevance.classify_job({"role_title": "Software Engineer", "match_score": 60},
                                   empty, cap=2)
        self.assertTrue(j["on_target"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_relevance.TestClassifyJob -v`
Expected: FAIL with `AttributeError: ... 'classify_job'`.

- [ ] **Step 3: Write minimal implementation**

```python
# Append to app/relevance.py
def classify_job(job: dict, profile: dict, cap: int) -> dict:
    """Merge relevance + seniority flags onto a shaped job dict and return it.

    on_target: title shares >=1 meaningful token with the tracked titles.
               If the profile is unconfigured (no tokens), everything is on_target.
    over_level: seniority rank exceeds `cap`.
    """
    title = job.get("role_title", "")
    label, rank = parse_seniority(title)
    targets = profile.get("title_tokens") or set()
    if targets:
        on_target = bool(title_tokens(title) & targets)
    else:
        on_target = True
    job["on_target"] = on_target
    job["seniority_label"] = label
    job["seniority_rank"] = rank
    job["over_level"] = rank > cap
    return job
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest test_relevance.TestClassifyJob -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/relevance.py test_relevance.py
git commit -m "feat(board): per-job relevance + level classification"
```

---

### Task 4: Apply the For-me view (hide + sink + sort + override)

**Files:**
- Modify: `app/relevance.py`
- Test: `test_relevance.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to test_relevance.py
class TestApplyForMeView(unittest.TestCase):
    def setUp(self):
        self.profile = {"title_tokens": {"data", "analyst"},
                        "tracked_titles": ["Data Analyst"], "max_seniority_rank": 2}
        self.jobs = [
            {"role_title": "Software Engineer", "match_score": 90},     # off-target -> hidden
            {"role_title": "Senior Data Analyst", "match_score": 80},   # over-level -> sunk
            {"role_title": "Data Analyst", "match_score": 60},          # in-level
            {"role_title": "Junior Data Analyst", "match_score": 55},   # in-level
        ]

    def test_hides_off_target_and_sinks_over_level(self):
        out = relevance.apply_for_me_view(self.jobs, self.profile)
        titles = [j["role_title"] for j in out]
        self.assertNotIn("Software Engineer", titles)          # hidden
        self.assertEqual(titles[-1], "Senior Data Analyst")    # sunk to bottom
        self.assertEqual(titles[0], "Data Analyst")            # in-level, highest score first

    def test_empty_profile_hides_nothing(self):
        empty = {"title_tokens": set(), "tracked_titles": [], "max_seniority_rank": 2}
        out = relevance.apply_for_me_view(self.jobs, empty)
        self.assertEqual(len(out), 4)

    def test_override_rank_lifts_cap(self):
        # override "senior" (rank 3) -> Senior Data Analyst no longer over-level
        out = relevance.apply_for_me_view(self.jobs, self.profile, override_rank=3)
        senior = next(j for j in out if j["role_title"] == "Senior Data Analyst")
        self.assertFalse(senior["over_level"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_relevance.TestApplyForMeView -v`
Expected: FAIL with `AttributeError: ... 'apply_for_me_view'`.

- [ ] **Step 3: Write minimal implementation**

```python
# Append to app/relevance.py
def apply_for_me_view(jobs: list[dict], profile: dict, override_rank: int | None = None) -> list[dict]:
    """Hide off-target jobs, sink over-level jobs, sort for the For-me board.

    Sort key: in-level before over-level, then match_score descending.
    `override_rank` (None = auto) replaces the profile's seniority cap.
    """
    cap = override_rank if override_rank is not None else profile.get("max_seniority_rank", MID_RANK)
    classified = [classify_job(j, profile, cap) for j in jobs]
    kept = [j for j in classified if j["on_target"]]
    kept.sort(key=lambda j: (1 if j["over_level"] else 0, -(j.get("match_score") or 0)))
    return kept
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest test_relevance.TestApplyForMeView -v`
Then the whole module: `python -m unittest test_relevance -v`
Expected: PASS (all classes).

- [ ] **Step 5: Commit**

```bash
git add app/relevance.py test_relevance.py
git commit -m "feat(board): apply For-me view (hide off-target, sink over-level)"
```

---

### Task 5: Wire into the home() route

**Files:**
- Modify: `app/app.py` (`home()` route, around lines 89–148)
- Test: manual run + existing suite

- [ ] **Step 1: Add the `level` ↔ rank map near the top of `app/app.py`** (after imports, with the other module-level constants)

```python
from app import relevance  # add to the existing `from app import ...` imports if grouped

# Override dropdown values -> seniority cap rank. "" / "auto" = derive from tracked roles.
_LEVEL_OVERRIDE = {"entry": 1, "mid": 2, "senior": 3}
```

- [ ] **Step 2: Extend the `home()` signature** to accept the two new params

Change (around line 90):

```python
def home(request: Request, q: str = "", sector: str = "", min_score: int = 0,
         status: str = "", show_dismissed: int = 0, starred: int = 0,
         view: str = "", day: str = "", everything: int = 0, level: str = ""):
```

- [ ] **Step 3: Apply the For-me view after `jobs` is built**

Insert immediately AFTER the day-fallback block (after the `if not day and effective_day and not jobs:` block, before the prev/next day navigation, ~line 110). The For-me view only applies to the default board (not the applied/starred/to_review views) and only when the user has not clicked "Show everything":

```python
    # "For me" smart view: default board only, unless the user clicked "Show everything".
    for_me_active = (view in ("", None)) and not everything
    hidden_count = 0
    if for_me_active:
        profile = relevance.target_profile(con)
        override = _LEVEL_OVERRIDE.get(level)  # None when level is "" / "auto" / unknown
        before = len(jobs)
        jobs = relevance.apply_for_me_view(jobs, profile, override_rank=override)
        hidden_count = before - len(jobs)
```

- [ ] **Step 4: Pass the new flags into the template context**

Add these keys to the `ctx` dict (alongside `"view": view,`):

```python
        "for_me_active": for_me_active,
        "hidden_count": hidden_count,
        "everything": everything,
        "level": level,
```

And add `everything` + `level` into the existing `"f": {...}` filter-state dict so they round-trip through the filter form:

```python
        "f": {"q": q, "sector": sector, "min_score": min_score, "status": status,
              "show_dismissed": show_dismissed, "starred": starred,
              "view": view, "day": current_day, "everything": everything, "level": level},
```

- [ ] **Step 5: Verify nothing broke — run the existing suites and the app**

```bash
python -m unittest test_relevance test_tracker_queries -v
python -m unittest test_ats_engine -v
```
Expected: PASS (no scoring change, so `test_ats_engine` is unaffected).

Then smoke-test the server boots:
```bash
python -c "import app.app"
```
Expected: no import errors.

- [ ] **Step 6: Commit**

```bash
git add app/app.py
git commit -m "feat(board): apply For-me view in home route with level override"
```

---

### Task 6: Template — toggle, dropdown, banner, seniority tag

**Files:**
- Modify: `app/templates/jobs.html` (filter bar + banner)
- Modify: `app/templates/_job_card.html` (seniority tag)

- [ ] **Step 1: Add the banner + "Show everything" toggle to `jobs.html`**

Place directly above the job list container (the element with `id="joblist"`). Match the existing markup/classes used elsewhere in the file for chips/links. The link toggles between the For-me view and the firehose by flipping `everything`:

```html
{% if for_me_active %}
  <div class="board-banner">
    Showing jobs matched to your targets{% if hidden_count %} · {{ hidden_count }} off-target hidden{% endif %}
    <a href="?everything=1{% if current_day and current_day != 'all' %}&day={{ current_day }}{% endif %}">Show everything</a>
  </div>
{% else %}
  <div class="board-banner">
    Showing all jobs ·
    <a href="?{% if current_day and current_day != 'all' %}day={{ current_day }}{% endif %}">Back to your matches</a>
  </div>
{% endif %}
```

- [ ] **Step 2: Add the seniority-cap override dropdown to the filter bar in `jobs.html`**

Inside the existing filter `<form>` (the one that GETs `/` and targets `joblist` via HTMX), add the select. Keep `name="level"` so it binds to the route param; preselect from `f.level`:

```html
<select name="level" hx-get="/" hx-target="#joblist" hx-include="closest form">
  <option value="" {% if not f.level %}selected{% endif %}>Level: auto</option>
  <option value="entry" {% if f.level == 'entry' %}selected{% endif %}>Entry & below</option>
  <option value="mid" {% if f.level == 'mid' %}selected{% endif %}>Mid & below</option>
  <option value="senior" {% if f.level == 'senior' %}selected{% endif %}>Senior & below</option>
</select>
```

- [ ] **Step 3: Add the seniority tag to `_job_card.html`**

Where the card renders its title/meta chips, add a tag shown only when the job is over-level (the sunk rows). The classification keys (`over_level`, `seniority_label`) are present on every job dict the For-me view returns; guard with `j.get` semantics via Jinja's `default`:

```html
{% if j.over_level %}
  <span class="tag tag-level">{{ j.seniority_label | capitalize }}</span>
{% endif %}
```

- [ ] **Step 4: Add minimal styles** (only if `board-banner` / `tag-level` are not already covered) to `app/static/app.css`:

```css
.board-banner { font-size: 0.85rem; opacity: 0.8; margin: 0 0 0.75rem; }
.board-banner a { margin-left: 0.4rem; }
.tag-level { background: #6b5b2e; color: #f5e9c8; }
```

- [ ] **Step 5: Manual verification**

Run the app the project's normal way and load `/`:
```bash
python -c "import app.app"   # import smoke test
```
Then start the server as usual and confirm in the browser:
- Board opens in For-me mode: Software-Engineer-type rows are absent; senior/manager rows sit at the bottom with a level tag.
- "Show everything" reveals the hidden rows; "Back to your matches" returns.
- The "Level" dropdown changes which rows are sunk (Senior & below stops sinking senior rows).

Expected: all three behaviors confirmed. (If the user has no tracked positions, the board shows everything and the banner reflects that — by design.)

- [ ] **Step 6: Commit**

```bash
git add app/templates/jobs.html app/templates/_job_card.html app/static/app.css
git commit -m "feat(board): For-me view UI (toggle, level dropdown, banner, tag)"
```

---

## Self-Review Notes

- **Spec coverage:** target_profile (Task 2) ✓, seniority parser (Task 1) ✓, classify_job (Task 3) ✓, apply_for_me_view hide/sink/sort + override (Task 4) ✓, route default + toggle + dropdown (Task 5) ✓, banner + tag UI (Task 6) ✓, fallbacks (empty profile / unparseable seniority) covered by Task 2/3/4 tests ✓, no migration / scorer untouched ✓ (Task 5 Step 5 runs `test_ats_engine`).
- **Type consistency:** `classify_job(job, profile, cap)` is called by `apply_for_me_view` with a resolved `cap` int; `target_profile` returns `max_seniority_rank` consumed as that cap. `_LEVEL_OVERRIDE` returns `None` for "" so auto-derivation kicks in. Keys `on_target` / `over_level` / `seniority_label` / `seniority_rank` are written in Task 3 and read in Tasks 4 & 6.
- **Decision (flippable):** seniority tag shows on over-level rows only (Task 6 Step 3). To show on all rows, drop the `{% if j.over_level %}` guard.
