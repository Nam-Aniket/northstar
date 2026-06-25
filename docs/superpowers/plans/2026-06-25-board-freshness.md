# Board Freshness (sort + display) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the board open freshest-first ("posted 12m ago", 🔥 just-posted), add a Fresh filter and 15m/1h/4h recency options, and capture finer posted time - composing cleanly with the already-built For-me view.

**Architecture:** Freshness is a read-time presentation layer (same pattern as `app/relevance.py`) - a new pure `app/freshness.py` derives a sortable `posted_at` per shaped job from the existing `jd_posted_date` (which already flows end-to-end) with fallback to `first_seen_date`. NO new DB column and NO migration: `jd_posted_date` is TEXT and `_DATE_RE` already matches an ISO prefix, so it can hold a full timestamp. The board sort (both `get_jobs` default and `apply_for_me_view`) keys on `posted_at` descending; relative display + 🔥 are Jinja filters.

**Tech Stack:** Python stdlib (datetime, re), FastAPI route, Jinja2 templates, `unittest`.

**Out of scope:** the paste-a-JD quick-add (separate plan). Sub-day LinkedIn capture (Task 9) is best-effort; everything else degrades gracefully to day-level if only a date is available.

---

## File structure

- **Create** `app/freshness.py` - pure freshness helpers (sortable timestamp, humanize, just-posted, fresh-bucket).
- **Create** `test_freshness.py` - unit tests for the helpers.
- **Modify** `app/queries.py` - `_shape` adds `posted_at`; `get_jobs` adds `fresh` param + freshest-first sort.
- **Modify** `app/relevance.py` - `apply_for_me_view` composes freshness into its sort.
- **Modify** `test_relevance.py` - update the sort expectation.
- **Modify** `app/app.py` - register `ago` / `isfresh` Jinja filters; add `fresh` param to `home()`.
- **Modify** `app/templates/_job_card.html` - relative "posted X ago" + 🔥 just-posted.
- **Modify** `app/templates/jobs.html` - Fresh filter control in the filter bar.
- **Modify** `app/templates/_ob_steps.html` - add 15min/1h/4h recency options.
- **Modify** `00_search_linkedin_guest.py` - best-effort finer posted-time capture + tpr help text.
- **Verify/Modify** `daily_run.py` - ensure Seek runs in the discover stage (best-effort).

All test commands use the repo venv: `.venv/bin/python -m unittest <module> -v`.

---

### Task 1: Pure freshness helpers

**Files:**
- Create: `app/freshness.py`
- Test: `test_freshness.py`

- [ ] **Step 1: Write the failing test**

```python
# test_freshness.py
"""Unit tests for app/freshness.py — pure, deterministic (now is injected)."""
import unittest
from datetime import datetime, timezone

from app import freshness as F

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)


class PostedAtOf(unittest.TestCase):
    def test_prefers_jd_posted_date_timestamp(self):
        job = {"jd_posted_date": "2026-06-25T11:50:00", "first_seen_date": "2026-06-20"}
        self.assertEqual(F.posted_at_of(job), "2026-06-25T11:50:00")

    def test_date_only_jd_posted_date(self):
        self.assertEqual(F.posted_at_of({"jd_posted_date": "2026-06-24"}), "2026-06-24")

    def test_falls_back_to_first_seen(self):
        self.assertEqual(F.posted_at_of({"jd_posted_date": "", "first_seen_date": "2026-06-20"}),
                         "2026-06-20")

    def test_empty_when_nothing(self):
        self.assertEqual(F.posted_at_of({}), "")


class HumanizeAgo(unittest.TestCase):
    def test_minutes(self):
        self.assertEqual(F.humanize_ago("2026-06-25T11:48:00", NOW), "12m ago")

    def test_hours(self):
        self.assertEqual(F.humanize_ago("2026-06-25T09:00:00", NOW), "3h ago")

    def test_just_now(self):
        self.assertEqual(F.humanize_ago("2026-06-25T11:59:50", NOW), "just now")

    def test_date_only_today(self):
        self.assertEqual(F.humanize_ago("2026-06-25", NOW), "today")

    def test_date_only_days(self):
        self.assertEqual(F.humanize_ago("2026-06-22", NOW), "3d ago")

    def test_empty(self):
        self.assertEqual(F.humanize_ago("", NOW), "")


class JustPosted(unittest.TestCase):
    def test_within_30_min(self):
        self.assertTrue(F.is_just_posted("2026-06-25T11:45:00", NOW))

    def test_over_30_min(self):
        self.assertFalse(F.is_just_posted("2026-06-25T11:00:00", NOW))

    def test_date_only_never_just_posted(self):
        self.assertFalse(F.is_just_posted("2026-06-25", NOW))


class FreshOk(unittest.TestCase):
    def test_within_1h(self):
        self.assertTrue(F.fresh_ok("2026-06-25T11:30:00", NOW, "1h"))

    def test_outside_1h(self):
        self.assertFalse(F.fresh_ok("2026-06-25T10:00:00", NOW, "1h"))

    def test_unknown_bucket_passes(self):
        self.assertTrue(F.fresh_ok("2026-06-20", NOW, ""))

    def test_date_only_passes_only_24h(self):
        self.assertTrue(F.fresh_ok("2026-06-25", NOW, "24h"))
        self.assertFalse(F.fresh_ok("2026-06-25", NOW, "1h"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m unittest test_freshness -v`
Expected: FAIL/ERROR — `ModuleNotFoundError: No module named 'app.freshness'`.

- [ ] **Step 3: Write the implementation**

```python
# app/freshness.py
"""Read-time freshness helpers for the board. Pure, stdlib only.

Derives a sortable posted timestamp from the existing jd_posted_date (which may
be a date or a full ISO datetime) with fallback to first_seen_date — so no DB
column or migration is needed. `now` is always injected for deterministic tests.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

_ISO_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")


def posted_at_of(job: dict) -> str:
    """Best sortable posted timestamp (ISO string) for a shaped job, or "".

    Prefers jd_posted_date (date or full ISO), falls back to first_seen_date.
    ISO strings sort lexicographically in chronological order.
    """
    jd = (job.get("jd_posted_date") or "").strip()
    if jd and _ISO_PREFIX.match(jd):
        return jd
    fs = (job.get("first_seen_date") or "").strip()
    return fs if _ISO_PREFIX.match(fs) else ""


def _parse(iso: str):
    """Parse an ISO date or datetime to an aware UTC datetime, or None."""
    if not iso:
        return None
    s = iso.strip().replace("Z", "+00:00")
    for candidate in (s, s[:10]):
        try:
            dt = datetime.fromisoformat(candidate)
            break
        except ValueError:
            dt = None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def humanize_ago(iso: str, now: datetime) -> str:
    """'12m ago' / '3h ago' (timestamps) or 'today' / '3d ago' (date-only)."""
    dt = _parse(iso)
    if dt is None:
        return ""
    secs = max(0.0, (now - dt).total_seconds())
    if "T" in iso:
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{int(secs // 60)}m ago"
        if secs < 86400:
            return f"{int(secs // 3600)}h ago"
    days = int(secs // 86400)
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days}d ago"


def is_just_posted(iso: str, now: datetime) -> bool:
    """True if posted < 30 min ago. Requires a sub-day timestamp."""
    if "T" not in (iso or ""):
        return False
    dt = _parse(iso)
    return dt is not None and 0 <= (now - dt).total_seconds() < 1800


def fresh_ok(iso: str, now: datetime, bucket: str) -> bool:
    """bucket in {'1h','4h','24h'} (else passes). Date-only rows pass only '24h'."""
    limits = {"1h": 3600, "4h": 14400, "24h": 86400}
    lim = limits.get(bucket)
    if lim is None:
        return True
    dt = _parse(iso)
    if dt is None:
        return False
    secs = (now - dt).total_seconds()
    if "T" not in iso:
        return bucket == "24h" and secs < 2 * 86400
    return 0 <= secs < lim
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m unittest test_freshness -v`
Expected: PASS (all tests OK).

- [ ] **Step 5: Commit**

```bash
git add app/freshness.py test_freshness.py
git commit -m "feat(board): freshness helpers (sortable posted_at, humanize, fresh buckets)"
```

---

### Task 2: `_shape` derives `posted_at`

**Files:**
- Modify: `app/queries.py` (`_shape`, ends ~line 729)

- [ ] **Step 1: Add the import and field**

At the top of `app/queries.py` add (near the other imports):

```python
from app import freshness
```

In `_shape(r)`, immediately before `return d` (after the `job_day` block, ~line 728), add:

```python
    d["posted_at"] = freshness.posted_at_of(d)
```

- [ ] **Step 2: Write a behavior test**

Add to `test_tracker_filters.py` (or create `test_board_freshness.py` with the standard isolation header `os.environ.setdefault("JOBENGINE_*", "*.example.json")` before importing). Minimal direct check:

```python
def test_shape_sets_posted_at(self):
    from app.queries import _shape
    row = {"match_score": 60, "jd_posted_date": "2026-06-25T10:00:00",
           "first_seen_date": "2026-06-20"}
    self.assertEqual(_shape(row)["posted_at"], "2026-06-25T10:00:00")
```

- [ ] **Step 3: Run it**

Run: `.venv/bin/python -m unittest test_board_freshness -v` (or the file you added to)
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add app/queries.py test_board_freshness.py
git commit -m "feat(board): derive sortable posted_at in _shape"
```

---

### Task 3: `get_jobs` — Fresh filter + freshest-first sort

**Files:**
- Modify: `app/queries.py` (`get_jobs`, lines 732-779)

- [ ] **Step 1: Write the failing test**

In the same `test_board_freshness.py`:

```python
def test_get_jobs_sorts_freshest_first(self):
    # Two jobs, fresher one has the LOWER score — freshness must win the default sort.
    import app.queries as Q
    rows = [
        {"row_key": "a", "match_score": 90, "jd_posted_date": "2026-06-20", "starred": 0},
        {"row_key": "b", "match_score": 50, "jd_posted_date": "2026-06-25", "starred": 0},
    ]
    shaped = Q._sort_board([Q._shape(r) for r in rows])  # helper extracted in Step 3
    self.assertEqual([j["row_key"] for j in shaped], ["b", "a"])
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `.venv/bin/python -m unittest test_board_freshness -v`
Expected: FAIL — `AttributeError: module 'app.queries' has no attribute '_sort_board'`.

- [ ] **Step 3: Implement**

In `app/queries.py`:

Add a small sort helper (so it is unit-testable) near `_shape`:

```python
def _sort_board(jobs: list[dict]) -> list[dict]:
    """Default board order: freshest first, then Fit, then starred."""
    jobs.sort(key=lambda d: (d.get("posted_at") or "",
                             d.get("match_score") or 0,
                             d.get("starred") or 0), reverse=True)
    return jobs
```

Change `get_jobs` signature (line 732) to add `fresh=None`:

```python
def get_jobs(con, q=None, sector=None, min_score=0, status=None,
             show_dismissed=False, starred_only=False, view=None, day=None,
             fresh=None) -> list[dict]:
```

Add the Fresh filter inside the per-row filter loop (next to the `min_score` check, ~line 768). First compute `now` once at the top of the function body:

```python
    from datetime import datetime, timezone
    _now = datetime.now(timezone.utc)
```

Then in the loop:

```python
        if fresh and not freshness.fresh_ok(d.get("posted_at") or "", _now, fresh):
            continue
```

Replace the final sort (line 778) `out.sort(key=lambda d: (d["match_score"] or 0, d["starred"]), reverse=True)` with:

```python
    _sort_board(out)
```

- [ ] **Step 4: Run it**

Run: `.venv/bin/python -m unittest test_board_freshness -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/queries.py test_board_freshness.py
git commit -m "feat(board): freshest-first default sort + Fresh filter in get_jobs"
```

---

### Task 4: `apply_for_me_view` composes freshness

**Files:**
- Modify: `app/relevance.py` (`apply_for_me_view`, lines 101-111)
- Modify: `test_relevance.py`

- [ ] **Step 1: Update the test to express the new order**

In `test_relevance.py`, find the test asserting the For-me sort and update it (or add one) so that, among in-level on-target jobs, the fresher `posted_at` comes first even with a lower score:

```python
def test_for_me_sorts_in_level_by_freshness_then_score(self):
    jobs = [
        {"role_title": "Data Analyst", "match_score": 90,
         "posted_at": "2026-06-20", "over_level": False},
        {"role_title": "Data Analyst", "match_score": 50,
         "posted_at": "2026-06-25", "over_level": False},
    ]
    profile = {"title_tokens": {"data", "analyst"}, "tracked_titles": ["Data Analyst"],
               "max_seniority_rank": 2}
    out = apply_for_me_view(jobs, profile)
    self.assertEqual([j["match_score"] for j in out], [50, 90])  # fresher first
```

- [ ] **Step 2: Run it to confirm failure**

Run: `.venv/bin/python -m unittest test_relevance -v`
Expected: FAIL — current sort is `(over_level, -score)`, so 90 comes before 50.

- [ ] **Step 3: Implement the composed sort**

Replace the sort line in `apply_for_me_view` (line 110):

```python
    kept.sort(key=lambda j: (j["over_level"],            # in-level group first
                             _neg(j.get("posted_at")),   # then freshest
                             -(j.get("match_score") or 0)))  # then Fit
```

Add a tiny module-level helper near the top of `app/relevance.py` (ISO strings are not numerically negatable, so invert ordering via a sortable wrapper):

```python
class _Desc:
    """Wrap a value so ascending sort yields descending order of the original."""
    __slots__ = ("v",)
    def __init__(self, v): self.v = v or ""
    def __lt__(self, other): return self.v > other.v
    def __eq__(self, other): return self.v == other.v


def _neg(iso):
    return _Desc(iso)
```

- [ ] **Step 4: Run it**

Run: `.venv/bin/python -m unittest test_relevance -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/relevance.py test_relevance.py
git commit -m "feat(board): compose freshness into For-me sort (in-level, freshest, Fit)"
```

---

### Task 5: Jinja filters + `home()` Fresh param

**Files:**
- Modify: `app/app.py` (`home()`, lines 94-162; filter registration near other custom filters ~lines 36-45)

- [ ] **Step 1: Register the filters**

Near the existing custom Jinja filters in `app/app.py`, add:

```python
from datetime import datetime, timezone
from app import freshness

def _ago(iso):
    return freshness.humanize_ago(iso or "", datetime.now(timezone.utc))

def _isfresh(iso):
    return freshness.is_just_posted(iso or "", datetime.now(timezone.utc))

templates.env.filters["ago"] = _ago
templates.env.filters["isfresh"] = _isfresh
```

- [ ] **Step 2: Thread the `fresh` param through `home()`**

Add `fresh: str = ""` to the `home()` signature. Pass it into `get_jobs`:

```python
        ... view=view or None, day=effective_day, fresh=fresh or None)
```

Add `fresh` to the `f` filter dict in the template context and to the fallback `get_jobs` call (lines 110-113) so day-fallback keeps the filter.

- [ ] **Step 3: Smoke-test the app imports**

Run: `.venv/bin/python -c "import app.app"`
Expected: no error.

- [ ] **Step 4: Commit**

```bash
git add app/app.py
git commit -m "feat(board): ago/isfresh Jinja filters + Fresh param on home route"
```

---

### Task 6: Card shows "posted X ago" + 🔥

**Files:**
- Modify: `app/templates/_job_card.html` (the `jc-chips` block, the date chip ~line with `j.job_day[5:]`)

- [ ] **Step 1: Replace the date chip with a relative posted chip**

Replace:

```html
{% if j.job_day %}<span class="chip"><svg ...calendar...></svg>{{ j.job_day[5:] }}</span>{% endif %}
```

with:

```html
{% if j.posted_at %}<span class="chip{% if j.posted_at|isfresh %} tag-good{% endif %}"><svg viewBox="0 0 24 24" class="w-3 h-3" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>{% if j.posted_at|isfresh %}🔥 {% endif %}{{ j.posted_at|ago }}</span>{% endif %}
```

- [ ] **Step 2: Verify in the running app** (see Verification section). Confirm cards read "posted Xm ago / today / 3d ago" and a 🔥 chip appears on sub-30-min postings.

- [ ] **Step 3: Commit**

```bash
git add app/templates/_job_card.html
git commit -m "feat(board): relative 'posted X ago' + just-posted highlight on cards"
```

---

### Task 7: Fresh filter control in the filter bar

**Files:**
- Modify: `app/templates/jobs.html` (filter bar, lines 45-91)

- [ ] **Step 1: Add the control** next to the min-score slider:

```html
<label class="filter-label">Fresh</label>
<select name="fresh" class="filter-select" onchange="this.form.requestSubmit()">
  <option value="" {{ 'selected' if not f.fresh }}>Any time</option>
  <option value="1h"  {{ 'selected' if f.fresh == '1h' }}>Past hour</option>
  <option value="4h"  {{ 'selected' if f.fresh == '4h' }}>Past 4 hours</option>
  <option value="24h" {{ 'selected' if f.fresh == '24h' }}>Past 24 hours</option>
</select>
```

(Match the existing controls' submit mechanism — if the bar uses a GET form, this select posts back the `fresh` query param.)

- [ ] **Step 2: Verify** the dropdown narrows the board in the running app.

- [ ] **Step 3: Commit**

```bash
git add app/templates/jobs.html
git commit -m "feat(board): Fresh time filter control"
```

---

### Task 8: 15min / 1h / 4h recency options

**Files:**
- Modify: `app/templates/_ob_steps.html` (recency select, lines 31-41)
- Modify: `00_search_linkedin_guest.py` (tpr help text, ~line 112)

- [ ] **Step 1: Add the options** above "Past 24 hours" in `_ob_steps.html`:

```html
<option value="r900"   {{ 'selected' if onb and onb.recency_tpr == 'r900' }}>Past 15 minutes</option>
<option value="r3600"  {{ 'selected' if onb and onb.recency_tpr == 'r3600' }}>Past 1 hour</option>
<option value="r14400" {{ 'selected' if onb and onb.recency_tpr == 'r14400' }}>Past 4 hours</option>
```

- [ ] **Step 2: Update help text** in `00_search_linkedin_guest.py` line ~112:

```python
ap.add_argument("--tpr", default="r86400",
                help="Recency seconds: r900=15m r3600=1h r14400=4h r86400=24h r172800=48h r604800=7d.")
```

- [ ] **Step 3: Verify** selecting "Past 1 hour" persists (POST /onboarding/recency) and round-trips into `daily_run.py` → `--tpr r3600` (grep the run log or print).

- [ ] **Step 4: Commit**

```bash
git add app/templates/_ob_steps.html 00_search_linkedin_guest.py
git commit -m "feat(discovery): 15m/1h/4h recency window options"
```

---

### Task 9 (best-effort): finer LinkedIn posted time

**Files:**
- Modify: `00_search_linkedin_guest.py` (`parse_cards`, date extraction line ~68)

**Context:** Today it captures only `datetime="YYYY-MM-DD"`. Sub-day "12m ago" needs a finer signal. This is best-effort — if LinkedIn guest HTML carries no time, leave date-level (everything degrades gracefully).

- [ ] **Step 1: Try to capture a fuller timestamp or relative phrase**

In `parse_cards`, after the existing `date_m`, attempt a relative-time capture and convert to an absolute timestamp (UTC), falling back to the date:

```python
import re
from datetime import datetime, timezone, timedelta

def _abs_from_relative(text: str) -> str | None:
    m = re.search(r"(\d+)\s*(minute|hour|day|week)s?\s*ago", text, re.I)
    if not m:
        return None
    n = int(m.group(1)); unit = m.group(2).lower()
    delta = {"minute": timedelta(minutes=n), "hour": timedelta(hours=n),
             "day": timedelta(days=n), "week": timedelta(weeks=n)}[unit]
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")
```

Then prefer a full `datetime="...T..."` if present, else `_abs_from_relative(chunk)`, else the date:

```python
dt_m = re.search(r'datetime="([0-9T:\-\+\.Z]{16,})"', chunk)
posted = (dt_m.group(1) if dt_m
          else _abs_from_relative(chunk)
          or (date_m.group(1) if date_m else ""))
# emit:
"posted_date": posted,
```

- [ ] **Step 2: Verify** a real discovery run populates some rows with a `T` timestamp (grep `job_alerts_raw.csv` for `T`). If none ever appear, accept day-level and note it.

- [ ] **Step 3: Commit**

```bash
git add 00_search_linkedin_guest.py
git commit -m "feat(discovery): best-effort sub-day posted time from LinkedIn cards"
```

---

### Task 10 (verify/best-effort): Seek in the discover stage

**Files:**
- Inspect: `daily_run.py` discover stage; `00a_scrape_job_postings.py` (`scrape_seek` exists, lines 94-177)

- [ ] **Step 1: Check** whether `daily_run.py`'s discover stage already invokes `00a_scrape_job_postings.py` (Seek). Run: `grep -n "00a_scrape\|scrape_seek\|seek" daily_run.py`.

- [ ] **Step 2:** If NOT wired, add a best-effort call in the discover stage that runs the Seek scraper and merges its CSV via `csv_merge`, logging `[seek-warning]` + continuing on any failure (never fail the run; LinkedIn stays primary). If already wired, no change — note it.

- [ ] **Step 3: Verify** a discovery run logs either Seek rows merged or a `[seek-warning]`, and the run still completes.

- [ ] **Step 4: Commit** (only if changed)

```bash
git add daily_run.py
git commit -m "feat(discovery): run Seek best-effort in the discover stage"
```

---

## Final verification

- [ ] Run the full suite: `.venv/bin/python -m unittest discover -p 'test_*.py'` → all green.
- [ ] Run the app (`python3 bootstrap.py`) and confirm on the board:
  - cards show "posted X ago" and a 🔥 chip on sub-30-min postings;
  - the default For-me board lists fresher on-target jobs above older ones (senior still sunk + tagged);
  - the Fresh filter narrows to the past hour/4h/24h;
  - "Show everything" still works and is also freshest-first.

## Self-review notes (done)

- **Spec coverage:** Parts B (recency windows - Task 8), C (posted time + freshness sort/display/filter/highlight - Tasks 1-7, 9), A (Seek - Task 10) of the fresh-jobs spec are covered. Part D (paste-a-JD) is intentionally deferred to a separate plan.
- **No migration:** confirmed - `posted_at` is derived in `_shape`, `jd_posted_date` (TEXT, already flowing) carries any finer timestamp; `_DATE_RE`/`_ISO_PREFIX` match a timestamp's date prefix so existing `job_day` logic is unaffected.
- **Type consistency:** `posted_at` is an ISO string everywhere; `_Desc`/`_neg` handle descending string sort in `apply_for_me_view`; `fresh` is the string bucket `''|'1h'|'4h'|'24h'` in route, query, and template.
