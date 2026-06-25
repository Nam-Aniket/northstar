# Business Mode (B2B Outreach) + Job-Tracker Contact Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a separate B2B "Business" mode (own prospect data + sales pipeline + bulk actions), and fix three job-tracker contact issues (Prophix company-view bug, copy-all-emails, companies-with-contacts filter).

**Architecture:** Business mode is a parallel of the existing job tracker, reading/writing two new isolated tables (`biz_companies`, `biz_prospects`) and reusing the existing pure parser (`linkedin_people_parser`) and email-pattern logic. A new `/business` route mirrors `/tracker`. Job-hunt data is never touched by business routes and vice-versa. The job-side fixes are surgical reads/UI additions.

**Tech Stack:** FastAPI, SQLite (`app/db.py`), Jinja2 + HTMX, stdlib `unittest`. Run tests with `.venv/bin/python -m unittest`.

**Spec:** `docs/superpowers/specs/2026-06-26-business-mode-outreach-design.md`

---

## File Structure

**New files:**
- `app/templates/business.html` — Business mode page shell (mirrors `contacts.html`).
- `app/templates/_biz_groups.html` — group loop + empty state (mirrors `_groups.html`).
- `app/templates/_biz_company.html` — one company card with prospects + copy button (mirrors `_company_group.html`).
- `app/templates/_biz_prospect.html` — one prospect sub-row with stage pill (mirrors `_tperson.html`).
- `app/templates/_biz_ingest_drawer.html` — paste/CSV drawer (mirrors `_ingest_drawer.html`).
- `test_biz_mode.py` — all business-mode + job-side-fix tests.

**Modified files:**
- `app/db.py` — add `biz_companies` + `biz_prospects` tables to `init_schema`.
- `app/queries.py` — add `BIZ_STAGE_FLOW`, `ingest_biz_prospects`, `import_biz_csv`, `biz_groups`, `set_biz_stage`, `set_biz_priority`, `get_biz_prospect`, `set_biz_prospect_notes`; fix `company_detail`; add `needs_contacts_only` count exposure.
- `app/app.py` — add `/business*` routes; (filter param already exists on `/tracker`).
- `app/templates/base.html` — add "Business" nav link; add copy-emails button to job tracker.
- `app/templates/_company_group.html` — add per-company "Copy emails" button + `data-email` on rows (shared with business).

**Reused as-is:** `linkedin_people_parser.parse_people` / `make_email`, `queries.resolve_company`, `queries.slugify`, `queries.normalize_company`.

---

## PART 2 FIRST — Job-side fixes (small, independent, establishes shared copy helper)

### Task 1: Fix the Prophix bug — `company_detail` reads `tracker_people`

**Files:**
- Modify: `app/queries.py` (`company_detail`, ~1056–1063)
- Test: `test_biz_mode.py`

- [ ] **Step 1: Write the failing test**

```python
# test_biz_mode.py
import os
os.environ.setdefault("JOBENGINE_SKILLS", "skills.example.json")
os.environ.setdefault("JOBENGINE_CONFIG", "config.example.json")
os.environ.setdefault("JOBENGINE_FACTS", "facts.example.json")

import unittest
import app.db as db
import app.queries as q


def _fresh_con():
    con = db.connect()
    # isolate: clean the tables this suite touches
    for t in ("tracker_people", "people", "manual_people",
              "biz_companies", "biz_prospects"):
        con.execute(f"DELETE FROM {t}")
    con.commit()
    return con


class CompanyDetailTrackerPeopleTest(unittest.TestCase):
    def test_company_detail_includes_tracker_people(self):
        con = _fresh_con()
        now = q._now()
        con.execute(
            "INSERT INTO tracker_people (person_key, company_key, company_name, "
            "name, title, email, pattern, outreach_status, notes, needs_review, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("prophix|jane_doe", "prophix", "Prophix", "Jane Doe", "VP Data",
             "jane.doe@prophix.com", "{first}.{last}@prophix.com",
             "not_contacted", "", 0, now, now))
        con.commit()
        detail = q.company_detail(con, "prophix")
        names = [p["name"] for p in detail["people"]]
        emails = [p["email"] for p in detail["people"]]
        con.close()
        self.assertIn("Jane Doe", names)
        self.assertIn("jane.doe@prophix.com", emails)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest test_biz_mode.CompanyDetailTrackerPeopleTest -v`
Expected: FAIL — "Jane Doe" not in names (company_detail ignores tracker_people).

- [ ] **Step 3: Add the tracker_people read to `company_detail`**

In `app/queries.py`, immediately after the `manual_people` loop (the line ending `is_manual=True))` at ~1063), insert:

```python
    # Unified contact store: the LinkedIn-People ingest writes tracker_people,
    # which the legacy company view never read (the Prophix-shows-0 bug).
    for p in con.execute("SELECT * FROM tracker_people WHERE company_key=?", (company_key,)):
        if p["person_key"] in seen_keys:
            continue
        seen_keys.add(p["person_key"])
        people_rows.append({
            "person_key":     p["person_key"],
            "company_key":    p["company_key"],
            "name":           p["name"],
            "role":           p["title"] or "",
            "email":          p["email"] or "",
            "has_email":      bool(p["email"]),
            "linkedin_url":   "",
            "draft_state":    "",
            "replied":        False,
            "outreach_status": p["outreach_status"] or "not_contacted",
            "contacted_at":   "",
            "is_manual":      False,
        })
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m unittest test_biz_mode.CompanyDetailTrackerPeopleTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/queries.py test_biz_mode.py
git commit -m "fix(tracker): company_detail reads tracker_people (Prophix 0-people bug)"
```

---

### Task 2: "Companies with contacts" count + filter on the job tracker

**Files:**
- Modify: `app/queries.py` (`tracker_groups` — expose `people_count` is already present; add `companies_with_people` to stats)
- Modify: `app/templates/contacts.html` (the `needs_contacts_only` toggle already exists; relabel is not required) — **no change needed if `needs_contacts_only` already filters.**
- Test: `test_biz_mode.py`

Note: `tracker_groups` already accepts `needs_contacts_only`. This task only adds an inverse **"has contacts"** count to `stats` so the UI can show "X of Y companies have contacts". Verify the existing filter works and surface the count.

- [ ] **Step 1: Write the failing test**

```python
# test_biz_mode.py
class CompaniesWithContactsTest(unittest.TestCase):
    def test_stats_counts_companies_with_people(self):
        con = _fresh_con()
        now = q._now()
        con.execute(
            "INSERT INTO tracker_people (person_key, company_key, company_name, "
            "name, title, email, pattern, outreach_status, notes, needs_review, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("acme|sam_lee", "acme", "Acme", "Sam Lee", "Eng",
             "sam.lee@acme.com", "{first}.{last}@acme.com",
             "not_contacted", "", 0, now, now))
        con.commit()
        groups, stats = q.tracker_groups(con)
        con.close()
        self.assertEqual(stats["companies_with_people"], 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest test_biz_mode.CompaniesWithContactsTest -v`
Expected: FAIL — `KeyError: 'companies_with_people'`.

- [ ] **Step 3: Add the count to `tracker_groups` stats**

Find where `tracker_groups` builds its `stats` dict (search for `"people_total"`). Add a line computing companies that have ≥1 person and include it:

```python
        companies_with_people = sum(1 for g in groups if g.get("people_count", 0) > 0)
```

and add `"companies_with_people": companies_with_people,` to the returned `stats` dict.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m unittest test_biz_mode.CompaniesWithContactsTest -v`
Expected: PASS

- [ ] **Step 5: Surface it in the toolbar label (contacts.html)**

In `app/templates/contacts.html`, change the "Needs contacts" toggle label region to also show the count. Replace the `Needs contacts` label text with:

```html
      Needs contacts
      <span class="chip" style="margin-left:5px">{{ stats.companies_with_people }}/{{ stats.total_companies }} have contacts</span>
```

- [ ] **Step 6: Commit**

```bash
git add app/queries.py app/templates/contacts.html test_biz_mode.py
git commit -m "feat(tracker): surface companies-with-contacts count"
```

---

### Task 3: Copy-all-emails — shared JS helper + job-tracker button

**Files:**
- Modify: `app/templates/_company_group.html` (add `data-email` to prospect rows is in `_tperson.html`; add a per-company "Copy emails" button to the group header)
- Modify: `app/templates/base.html` (add the shared copy-emails delegated JS once, so both tracker and business reuse it)
- Test: manual (client-side JS; no unit test in this repo)

- [ ] **Step 1: Add `data-email` to each prospect row**

In `app/templates/_tperson.html`, add `data-email="{{ p.email or '' }}"` to the root `<div class="tperson" ...>` element (line 4):

```html
<div class="tperson" id="tperson-{{ p.person_key|domid }}" data-email="{{ p.email or '' }}">
```

- [ ] **Step 2: Add a "Copy emails" button to the company group header**

In `app/templates/_company_group.html`, inside the group header action area, add a button scoped to that group's container (use the group's existing id/`data-tgroup` wrapper). Add:

```html
<button type="button" class="btn-open" data-copy-emails
        title="Copy all contact emails in this company">
  <svg viewBox="0 0 24 24" class="w-[14px] h-[14px]" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
  Copy emails
</button>
```

Ensure the button sits inside the per-company wrapper element so the JS can scope `data-email` collection to that company.

- [ ] **Step 3: Add the shared delegated JS to `base.html`**

In `app/templates/base.html`, before `</body>`, add:

```html
<script>
/* Copy-all-emails: collects data-email from the nearest company group (or whole
   page if none) and writes them comma-separated to the clipboard. Shared by the
   job tracker and Business mode. */
document.addEventListener('click', function (e) {
  var btn = e.target.closest('[data-copy-emails]');
  if (!btn) return;
  var scope = btn.closest('[data-company-scope]') || document;
  var emails = Array.prototype.map.call(
      scope.querySelectorAll('[data-email]'),
      function (el) { return (el.getAttribute('data-email') || '').trim(); })
    .filter(Boolean);
  emails = Array.from(new Set(emails));
  if (!emails.length) { return; }
  navigator.clipboard.writeText(emails.join(', ')).then(function () {
    var orig = btn.textContent;
    btn.textContent = 'Copied ' + emails.length;
    setTimeout(function () { btn.textContent = orig; }, 1500);
  });
});
</script>
```

- [ ] **Step 4: Add `data-company-scope` to the company wrapper**

In `app/templates/_company_group.html`, add `data-company-scope` to the outermost element that wraps both the header button and the prospect rows, so collection is per-company.

- [ ] **Step 5: Manual verify**

Start the app (`python3 bootstrap.py`), open `/tracker`, expand a company with contacts, click "Copy emails", paste into a text field — expect comma-separated emails for that company only.

- [ ] **Step 6: Commit**

```bash
git add app/templates/_tperson.html app/templates/_company_group.html app/templates/base.html
git commit -m "feat(tracker): per-company copy-all-emails button (shared helper)"
```

---

## PART 1 — Business mode

### Task 4: Business tables

**Files:**
- Modify: `app/db.py` (`init_schema`)
- Test: `test_biz_mode.py`

- [ ] **Step 1: Write the failing test**

```python
# test_biz_mode.py
class BizSchemaTest(unittest.TestCase):
    def test_biz_tables_exist(self):
        con = db.connect()
        cols_c = {r[1] for r in con.execute("PRAGMA table_info(biz_companies)")}
        cols_p = {r[1] for r in con.execute("PRAGMA table_info(biz_prospects)")}
        con.close()
        self.assertTrue({"company_key", "company_name", "priority"} <= cols_c)
        self.assertTrue({"prospect_key", "company_key", "name", "email",
                         "stage"} <= cols_p)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest test_biz_mode.BizSchemaTest -v`
Expected: FAIL — empty PRAGMA sets (tables don't exist).

- [ ] **Step 3: Add the tables in `init_schema`**

In `app/db.py`, inside the `con.executescript("""...""")` block (before the closing `"""`), add:

```sql
        CREATE TABLE IF NOT EXISTS biz_companies (
            company_key  TEXT PRIMARY KEY,
            company_name TEXT,
            domain       TEXT,
            website      TEXT,
            priority     INTEGER DEFAULT 0,
            notes        TEXT DEFAULT '',
            created_at   TEXT,
            updated_at   TEXT
        );
        CREATE TABLE IF NOT EXISTS biz_prospects (
            prospect_key TEXT PRIMARY KEY,
            company_key  TEXT,
            company_name TEXT,
            name         TEXT,
            title        TEXT,
            email        TEXT,
            pattern      TEXT,
            stage        TEXT DEFAULT 'lead',
            notes        TEXT DEFAULT '',
            needs_review INTEGER DEFAULT 0,
            created_at   TEXT,
            updated_at   TEXT
        );
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m unittest test_biz_mode.BizSchemaTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/db.py test_biz_mode.py
git commit -m "feat(business): biz_companies + biz_prospects tables"
```

---

### Task 5: `ingest_biz_prospects` (reuses the shared parser)

**Files:**
- Modify: `app/queries.py` (add `BIZ_STAGE_FLOW` near `STATUS_FLOW`/`PERSON_STATUS_FLOW`, and `ingest_biz_prospects`)
- Test: `test_biz_mode.py`

- [ ] **Step 1: Write the failing test**

```python
# test_biz_mode.py
class IngestBizTest(unittest.TestCase):
    def test_ingest_creates_company_and_prospects(self):
        con = _fresh_con()
        people = [{"name": "Mia Stone", "title": "CFO",
                   "email": "mia.stone@acme.com", "pattern": "{first}.{last}@acme.com"}]
        counts = q.ingest_biz_prospects(con, "acme", "Acme Inc", people)
        comp = con.execute("SELECT * FROM biz_companies WHERE company_key='acme'").fetchone()
        pros = con.execute("SELECT stage, name FROM biz_prospects WHERE company_key='acme'").fetchone()
        con.close()
        self.assertEqual(counts["added"], 1)
        self.assertIsNotNone(comp)
        self.assertEqual(pros["stage"], "lead")
        self.assertEqual(pros["name"], "Mia Stone")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest test_biz_mode.IngestBizTest -v`
Expected: FAIL — `AttributeError: module 'app.queries' has no attribute 'ingest_biz_prospects'`.

- [ ] **Step 3: Implement `BIZ_STAGE_FLOW` + `ingest_biz_prospects`**

Near the top of `app/queries.py` where `STATUS_FLOW` / `PERSON_STATUS_FLOW` are defined, add:

```python
BIZ_STAGE_FLOW = ["lead", "contacted", "replied", "meeting", "won", "lost"]
```

Then add (anywhere after `ingest_people`):

```python
def ingest_biz_prospects(con, company_key: str, company_name: str, people: list) -> dict:
    """Upsert sales prospects into biz_prospects + ensure a biz_companies row.
    ON CONFLICT preserves stage and notes (workflow state). Mirrors ingest_people
    but writes the isolated business tables. Returns {added, updated, needs_review, skipped}."""
    now = _now()
    added = updated = needs_review = skipped = 0
    seen_emails = set()

    con.execute("""
        INSERT INTO biz_companies (company_key, company_name, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(company_key) DO UPDATE SET company_name=excluded.company_name,
                                               updated_at=excluded.updated_at
    """, (company_key, company_name, now, now))

    for person in people:
        name = person.get("name", "")
        title = person.get("title", "")
        email = person.get("email", "")
        is_review = person.get("needs_review", 0)
        pattern = person.get("pattern", "")
        if not name or not email:
            continue
        if email in seen_emails:
            skipped += 1
            continue
        seen_emails.add(email)
        existing = con.execute(
            "SELECT prospect_key FROM biz_prospects WHERE email=? AND company_key=?",
            (email, company_key)).fetchone()
        if existing:
            con.execute("""
                UPDATE biz_prospects SET title=?, pattern=?, updated_at=?
                WHERE email=? AND company_key=?
            """, (title, pattern, now, email, company_key))
            updated += 1
        else:
            prospect_key = f"{company_key}|{slugify(name)}"
            con.execute("""
                INSERT OR IGNORE INTO biz_prospects
                (prospect_key, company_key, company_name, name, title, email, pattern,
                 stage, notes, needs_review, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (prospect_key, company_key, company_name, name, title, email, pattern,
                  "lead", "", is_review, now, now))
            added += 1
            if is_review:
                needs_review += 1
    con.commit()
    return {"added": added, "updated": updated, "needs_review": needs_review, "skipped": skipped}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m unittest test_biz_mode.IngestBizTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/queries.py test_biz_mode.py
git commit -m "feat(business): ingest_biz_prospects + BIZ_STAGE_FLOW"
```

---

### Task 6: `biz_groups` + `biz_stage_summary`

**Files:**
- Modify: `app/queries.py`
- Test: `test_biz_mode.py`

- [ ] **Step 1: Write the failing test**

```python
# test_biz_mode.py
class BizGroupsTest(unittest.TestCase):
    def test_groups_and_summary(self):
        con = _fresh_con()
        q.ingest_biz_prospects(con, "acme", "Acme", [
            {"name": "Mia Stone", "title": "CFO", "email": "m@acme.com", "pattern": ""},
            {"name": "Ned Park", "title": "CTO", "email": "n@acme.com", "pattern": ""}])
        q.set_biz_stage(con, "acme|ned_park", "contacted")
        groups, summary = q.biz_groups(con)
        con.close()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["prospect_count"], 2)
        self.assertEqual(summary["lead"], 1)
        self.assertEqual(summary["contacted"], 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest test_biz_mode.BizGroupsTest -v`
Expected: FAIL — `ingest` exists but `set_biz_stage`/`biz_groups` do not.

- [ ] **Step 3: Implement `biz_groups` + `biz_stage_summary`**

Add to `app/queries.py`:

```python
def biz_groups(con) -> tuple[list, dict]:
    """Return (groups, summary) for the Business mode view.
    group: {company_key, company_name, priority, prospects[], prospect_count}
    summary: per-stage counts across all prospects + total_companies/total_prospects."""
    companies = {}
    for r in con.execute("SELECT * FROM biz_companies ORDER BY company_name"):
        companies[r["company_key"]] = {
            "company_key": r["company_key"],
            "company_name": r["company_name"] or r["company_key"],
            "priority": r["priority"] or 0,
            "prospects": [],
        }
    summary = {s: 0 for s in BIZ_STAGE_FLOW}
    total_prospects = 0
    for r in con.execute("SELECT * FROM biz_prospects ORDER BY company_key, name"):
        p = dict(r)
        stage = p.get("stage") or "lead"
        summary[stage] = summary.get(stage, 0) + 1
        total_prospects += 1
        grp = companies.get(p["company_key"])
        if grp is None:
            grp = companies[p["company_key"]] = {
                "company_key": p["company_key"],
                "company_name": p["company_name"] or p["company_key"],
                "priority": 0, "prospects": [],
            }
        grp["prospects"].append(p)
    groups = list(companies.values())
    for g in groups:
        g["prospect_count"] = len(g["prospects"])
    # priority companies first, then by name
    groups.sort(key=lambda g: (-g["priority"], g["company_name"].lower()))
    summary["total_companies"] = len(groups)
    summary["total_prospects"] = total_prospects
    return groups, summary
```

- [ ] **Step 4: Run test to verify it passes**

(After Task 7 adds `set_biz_stage`; if running this task alone, temporarily skip the `set_biz_stage` line. Recommended: implement Task 7's `set_biz_stage` now too, then run.)

Run: `.venv/bin/python -m unittest test_biz_mode.BizGroupsTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/queries.py test_biz_mode.py
git commit -m "feat(business): biz_groups + stage summary"
```

---

### Task 7: Stage / priority / notes mutations + getters

**Files:**
- Modify: `app/queries.py`
- Test: `test_biz_mode.py`

- [ ] **Step 1: Write the failing test**

```python
# test_biz_mode.py
class BizMutationsTest(unittest.TestCase):
    def setUp(self):
        self.con = _fresh_con()
        q.ingest_biz_prospects(self.con, "acme", "Acme",
            [{"name": "Mia Stone", "title": "CFO", "email": "m@acme.com", "pattern": ""}])

    def tearDown(self):
        self.con.close()

    def test_set_stage(self):
        q.set_biz_stage(self.con, "acme|mia_stone", "meeting")
        p = q.get_biz_prospect(self.con, "acme|mia_stone")
        self.assertEqual(p["stage"], "meeting")

    def test_set_stage_rejects_unknown(self):
        with self.assertRaises(ValueError):
            q.set_biz_stage(self.con, "acme|mia_stone", "bogus")

    def test_priority_toggle(self):
        q.set_biz_priority(self.con, "acme", True)
        g = {x["company_key"]: x for x in q.biz_groups(self.con)[0]}
        self.assertEqual(g["acme"]["priority"], 1)

    def test_notes(self):
        q.set_biz_prospect_notes(self.con, "acme|mia_stone", "warm intro via Sam")
        p = q.get_biz_prospect(self.con, "acme|mia_stone")
        self.assertEqual(p["notes"], "warm intro via Sam")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest test_biz_mode.BizMutationsTest -v`
Expected: FAIL — missing functions.

- [ ] **Step 3: Implement the mutations + getter**

Add to `app/queries.py`:

```python
def set_biz_stage(con, prospect_key: str, stage: str) -> None:
    if stage not in BIZ_STAGE_FLOW:
        raise ValueError(f"unknown stage: {stage}")
    con.execute("UPDATE biz_prospects SET stage=?, updated_at=? WHERE prospect_key=?",
                (stage, _now(), prospect_key))
    con.commit()


def set_biz_priority(con, company_key: str, on: bool) -> None:
    con.execute("UPDATE biz_companies SET priority=?, updated_at=? WHERE company_key=?",
                (1 if on else 0, _now(), company_key))
    con.commit()


def set_biz_prospect_notes(con, prospect_key: str, notes: str) -> None:
    con.execute("UPDATE biz_prospects SET notes=?, updated_at=? WHERE prospect_key=?",
                (notes or "", _now(), prospect_key))
    con.commit()


def get_biz_prospect(con, prospect_key: str) -> dict | None:
    r = con.execute("SELECT * FROM biz_prospects WHERE prospect_key=?",
                    (prospect_key,)).fetchone()
    return dict(r) if r else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m unittest test_biz_mode.BizMutationsTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/queries.py test_biz_mode.py
git commit -m "feat(business): stage/priority/notes mutations + getter"
```

---

### Task 8: CSV import

**Files:**
- Modify: `app/queries.py` (add `import_biz_csv`)
- Test: `test_biz_mode.py`

- [ ] **Step 1: Write the failing test**

```python
# test_biz_mode.py
class BizCsvTest(unittest.TestCase):
    def test_csv_import(self):
        con = _fresh_con()
        csv_text = (
            "company,name,title,email\n"
            "Acme Inc,Mia Stone,CFO,mia@acme.com\n"
            "Acme Inc,Ned Park,CTO,\n"          # missing email -> needs_review, kept
            "Globex,Ada Byte,CEO,ada@globex.com\n")
        counts = q.import_biz_csv(con, csv_text)
        n_companies = con.execute("SELECT COUNT(*) FROM biz_companies").fetchone()[0]
        n_prospects = con.execute("SELECT COUNT(*) FROM biz_prospects").fetchone()[0]
        con.close()
        self.assertEqual(n_companies, 2)
        self.assertEqual(n_prospects, 3)          # Ned kept, flagged
        self.assertEqual(counts["needs_review"], 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest test_biz_mode.BizCsvTest -v`
Expected: FAIL — no `import_biz_csv`.

- [ ] **Step 3: Implement `import_biz_csv`**

Add to `app/queries.py` (uses stdlib `csv`, already importable):

```python
def import_biz_csv(con, csv_text: str) -> dict:
    """Import prospects from CSV text. Recognised headers (case-insensitive):
    company, name, title, email (pattern optional). Rows missing an email are
    KEPT but flagged needs_review (you can fill from a pattern later). Never
    hard-fails on a bad row. Returns aggregate counts."""
    import csv as _csv
    import io as _io
    reader = _csv.DictReader(_io.StringIO(csv_text))
    norm = {(h or "").strip().lower(): h for h in (reader.fieldnames or [])}
    by_company: dict[str, list] = {}
    company_names: dict[str, str] = {}
    for row in reader:
        try:
            company = (row.get(norm.get("company", ""), "") or "").strip()
            name = (row.get(norm.get("name", ""), "") or "").strip()
            title = (row.get(norm.get("title", ""), "") or "").strip()
            email = (row.get(norm.get("email", ""), "") or "").strip()
            pattern = (row.get(norm.get("pattern", ""), "") or "").strip()
        except Exception:
            continue
        if not company or not name:
            continue
        key = slugify(company)
        company_names[key] = company
        person = {"name": name, "title": title, "email": email, "pattern": pattern}
        if not email:
            person["needs_review"] = 1
            person["email"] = ""  # ingest skips empty email; keep via placeholder below
        by_company.setdefault(key, []).append(person)

    total = {"added": 0, "updated": 0, "needs_review": 0, "skipped": 0}
    for key, people in by_company.items():
        # ingest_biz_prospects requires an email; for CSV we keep emailless rows
        # by writing them directly with needs_review.
        emailed = [p for p in people if p.get("email")]
        counts = ingest_biz_prospects(con, key, company_names[key], emailed)
        for k in total:
            total[k] += counts.get(k, 0)
        now = _now()
        for p in people:
            if p.get("email"):
                continue
            pk = f"{key}|{slugify(p['name'])}"
            con.execute("""
                INSERT OR IGNORE INTO biz_prospects
                (prospect_key, company_key, company_name, name, title, email, pattern,
                 stage, notes, needs_review, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, '', ?, 'lead', '', 1, ?, ?)
            """, (pk, key, company_names[key], p["name"], p["title"],
                  p.get("pattern", ""), now, now))
            total["added"] += 1
            total["needs_review"] += 1
    con.commit()
    return total
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m unittest test_biz_mode.BizCsvTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/queries.py test_biz_mode.py
git commit -m "feat(business): CSV prospect import"
```

---

### Task 9: `/business` routes

**Files:**
- Modify: `app/app.py`
- Test: `test_biz_mode.py` (route smoke via FastAPI TestClient)

- [ ] **Step 1: Write the failing test**

```python
# test_biz_mode.py
from fastapi.testclient import TestClient
from app.app import app

class BizRoutesTest(unittest.TestCase):
    def setUp(self):
        con = _fresh_con(); con.close()
        self.client = TestClient(app)

    def test_business_page_renders(self):
        r = self.client.get("/business")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Business", r.text)

    def test_ingest_then_stage(self):
        r = self.client.post("/business/ingest", data={
            "paste": "Mia Stone\n· 1st\nCFO at Acme",
            "company": "Acme Inc", "pattern": "{first}.{last}@acme.com"})
        self.assertEqual(r.status_code, 200)
        con = db.connect()
        pk = con.execute("SELECT prospect_key FROM biz_prospects LIMIT 1").fetchone()
        con.close()
        self.assertIsNotNone(pk)
        r2 = self.client.post(f"/business/prospect/{pk[0]}/stage", data={"stage": "meeting"})
        self.assertEqual(r2.status_code, 200)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest test_biz_mode.BizRoutesTest -v`
Expected: FAIL — 404 on `/business`.

- [ ] **Step 3: Implement the routes**

In `app/app.py`, after the tracker routes block, add:

```python
@app.get("/business", response_class=HTMLResponse)
def business(request: Request, q: str = "", stage: str = ""):
    con = conn()
    groups, summary = queries.biz_groups(con)
    if q or stage:
        ql = q.lower()
        for g in groups:
            g["prospects"] = [p for p in g["prospects"]
                              if (not ql or ql in p["name"].lower()
                                  or ql in (g["company_name"] or "").lower())
                              and (not stage or p["stage"] == stage)]
        groups = [g for g in groups if g["prospects"]]
    all_companies = [{"company_name": g["company_name"]} for g in queries.biz_groups(con)[0]]
    ctx = {
        "groups": groups, "summary": summary, "all_companies": all_companies,
        "f": {"q": q, "stage": stage},
        "biz_stage_flow": queries.BIZ_STAGE_FLOW,
    }
    con.close()
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "_biz_groups.html", ctx)
    return templates.TemplateResponse(request, "business.html", ctx)


@app.post("/business/ingest", response_class=HTMLResponse)
def business_ingest(request: Request, paste: str = Form(""), company: str = Form(""),
                    pattern: str = Form("")):
    from linkedin_people_parser import parse_people
    result = parse_people(paste, company, pattern)
    con = conn()
    company_key = queries.slugify(company)
    all_people = list(result.people)
    for p in result.needs_review:
        p2 = dict(p); p2["needs_review"] = 1; all_people.append(p2)
    # carry the pattern onto each parsed person so it persists
    for p in all_people:
        p.setdefault("pattern", pattern)
    counts = queries.ingest_biz_prospects(con, company_key, company, all_people)
    groups, summary = queries.biz_groups(con)
    all_companies = [{"company_name": g["company_name"]} for g in groups]
    ctx = {"groups": groups, "summary": summary, "all_companies": all_companies,
           "f": {"q": "", "stage": ""}, "biz_stage_flow": queries.BIZ_STAGE_FLOW}
    con.close()
    toast = (f"Added {counts['added']}, {counts['needs_review']} need review."
             if counts["added"] else "No new prospects found.")
    return templates.TemplateResponse(request, "_biz_groups.html", ctx,
        headers={"HX-Trigger": json.dumps({"toast": toast})})


@app.post("/business/upload-csv", response_class=HTMLResponse)
def business_upload_csv(request: Request, csv_text: str = Form("")):
    con = conn()
    counts = queries.import_biz_csv(con, csv_text)
    groups, summary = queries.biz_groups(con)
    all_companies = [{"company_name": g["company_name"]} for g in groups]
    ctx = {"groups": groups, "summary": summary, "all_companies": all_companies,
           "f": {"q": "", "stage": ""}, "biz_stage_flow": queries.BIZ_STAGE_FLOW}
    con.close()
    toast = f"Imported {counts['added']} prospects ({counts['needs_review']} need review)."
    return templates.TemplateResponse(request, "_biz_groups.html", ctx,
        headers={"HX-Trigger": json.dumps({"toast": toast})})


@app.post("/business/prospect/{prospect_key:path}/stage", response_class=HTMLResponse)
def business_set_stage(request: Request, prospect_key: str, stage: str = Form(...)):
    con = conn()
    try:
        queries.set_biz_stage(con, prospect_key, stage)
    except ValueError:
        con.close()
        return HTMLResponse("bad stage", status_code=400)
    p = queries.get_biz_prospect(con, prospect_key)
    con.close()
    if not p:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(request, "_biz_prospect.html",
        {"p": p, "biz_stage_flow": queries.BIZ_STAGE_FLOW})


@app.post("/business/prospect/{prospect_key:path}/notes", response_class=HTMLResponse)
def business_set_notes(request: Request, prospect_key: str, notes: str = Form("")):
    con = conn()
    queries.set_biz_prospect_notes(con, prospect_key, notes)
    con.close()
    return HTMLResponse('<span class="saved-tag">Saved</span>')


@app.post("/business/company/{company_key:path}/priority", response_class=HTMLResponse)
def business_set_priority(request: Request, company_key: str, on: int = Form(1)):
    con = conn()
    queries.set_biz_priority(con, company_key, bool(on))
    con.close()
    return HTMLResponse('<span class="saved-tag">Saved</span>')
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m unittest test_biz_mode.BizRoutesTest -v`
Expected: PASS (requires Task 10's templates to exist; implement Task 10 before running, or stub the templates first).

- [ ] **Step 5: Commit**

```bash
git add app/app.py test_biz_mode.py
git commit -m "feat(business): /business routes (view, ingest, csv, stage, notes, priority)"
```

---

### Task 10: Business templates + nav link

**Files:**
- Create: `app/templates/business.html`, `_biz_groups.html`, `_biz_company.html`, `_biz_prospect.html`, `_biz_ingest_drawer.html`
- Modify: `app/templates/base.html` (nav)
- Test: manual preview (covered by `BizRoutesTest` for render success)

- [ ] **Step 1: Add the nav link**

In `app/templates/base.html`, after the Tracker `<a>` (line ~40), add:

```html
        <a href="/business" class="navlink {{ 'active' if path.startswith('/business') }}">Business</a>
```

- [ ] **Step 2: Create `_biz_prospect.html`** (mirror of `_tperson.html`, sales stage pill)

```html
{# single prospect sub-row; p = biz_prospects dict, biz_stage_flow in context #}
{% set st = p.stage or 'lead' %}
<div class="tperson" id="bprospect-{{ p.prospect_key|domid }}" data-email="{{ p.email or '' }}">
  <div class="tperson__name">
    <span>{{ p.name }}</span>
    {% if p.needs_review %}<span class="review-badge" title="Check name/email">review</span>{% endif %}
  </div>
  <div class="tperson__title" title="{{ p.title or '' }}">{{ p.title or '—' }}</div>
  <div>
    <select name="stage" class="tpill tpill--{{ st }}"
            hx-post="/business/prospect/{{ p.prospect_key|urlk }}/stage"
            hx-target="#bprospect-{{ p.prospect_key|domid }}" hx-swap="outerHTML"
            hx-trigger="change" onclick="event.stopPropagation()">
      {% for s in biz_stage_flow %}
      <option value="{{ s }}" {{ 'selected' if st == s }}>{{ s|title }}</option>
      {% endfor %}
    </select>
  </div>
  <div class="tperson__email">
    {% if p.email %}<a href="mailto:{{ p.email }}" onclick="event.stopPropagation()">{{ p.email }}</a>
    {% else %}<span style="color:rgb(var(--c-muted))">—</span>{% endif %}
  </div>
  <div class="tperson__notes">
    <input type="text" name="notes" value="{{ p.notes or '' }}" placeholder="add note…" class="tnote"
           hx-post="/business/prospect/{{ p.prospect_key|urlk }}/notes"
           hx-trigger="change, blur" hx-swap="none"
           hx-on::after-request="if(event.detail.successful){let t=document.createElement('span');t.className='saved-tag';t.textContent='Saved';this.after(t);setTimeout(()=>t.remove(),1500)}"
           onclick="event.stopPropagation()">
  </div>
</div>
```

- [ ] **Step 3: Create `_biz_company.html`** (company card; copy-emails + priority)

```html
{# one company card; g = group dict, biz_stage_flow in context #}
<div class="panel" data-company-scope style="padding:0;overflow:hidden">
  <div class="tgroup-head" data-tgroup-toggle aria-expanded="true"
       aria-controls="bgrp-{{ g.company_key|domid }}"
       style="display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 18px;cursor:pointer">
    <div style="display:flex;align-items:center;gap:10px">
      <strong>{{ g.company_name }}</strong>
      <span class="chip">{{ g.prospect_count }} prospect{{ 's' if g.prospect_count != 1 }}</span>
      {% if g.priority %}<span class="chip" style="color:rgb(var(--c-good-ink))">priority</span>{% endif %}
    </div>
    <div style="display:flex;align-items:center;gap:8px">
      <button type="button" class="btn-open" data-copy-emails title="Copy all emails in this company">Copy emails</button>
      <button type="button" class="btn-open"
              hx-post="/business/company/{{ g.company_key|urlk }}/priority"
              hx-vals='{"on": {{ 0 if g.priority else 1 }}}' hx-swap="none"
              onclick="event.stopPropagation()">
        {{ 'Unflag' if g.priority else 'Flag priority' }}
      </button>
    </div>
  </div>
  <div id="bgrp-{{ g.company_key|domid }}" class="tgroup-body is-open" style="padding:6px 10px 12px">
    {% for p in g.prospects %}{% include "_biz_prospect.html" %}{% endfor %}
  </div>
</div>
```

- [ ] **Step 4: Create `_biz_groups.html`** (loop + empty state)

```html
{% if groups %}
  {% for g in groups %}{% include "_biz_company.html" %}{% endfor %}
{% else %}
<div style="text-align:center;padding:64px 24px;color:rgb(var(--c-muted))">
  <p style="font-size:1.05rem;font-weight:700;margin-bottom:6px">No prospects yet</p>
  <p style="font-size:13px">Paste a company's people or upload a CSV to start your outreach list.</p>
</div>
{% endif %}
```

- [ ] **Step 5: Create `_biz_ingest_drawer.html`** (paste + CSV; mirror `_ingest_drawer.html`)

```html
<input type="checkbox" id="biz-drawer-toggle" class="drawer-checkbox" aria-hidden="true">
<div class="drawer-overlay" onclick="document.getElementById('biz-drawer-toggle').checked=false"></div>
<aside class="ingest-drawer" role="dialog" aria-modal="true" aria-label="Add prospects">
  <div class="ingest-drawer__header">
    <h2 style="font-size:1rem;font-weight:700;color:rgb(var(--c-ink))">Add prospects</h2>
    <label for="biz-drawer-toggle" class="iconlink" style="cursor:pointer" title="Close">✕</label>
  </div>
  <form hx-post="/business/ingest" hx-target="#biz-groups" hx-swap="innerHTML"
        hx-on::after-request="if(event.detail.successful){document.getElementById('biz-drawer-toggle').checked=false}"
        style="display:flex;flex-direction:column;gap:14px;padding:0 20px 18px">
    <div>
      <label class="filter-label" for="biz-paste">LinkedIn People tab paste</label>
      <textarea id="biz-paste" name="paste" rows="8" placeholder="Paste LinkedIn 'People' text…"
                style="width:100%;font-size:12.5px;padding:10px 12px;border-radius:var(--r-md);border:1px solid rgb(var(--c-line));background:rgb(var(--c-bg));color:rgb(var(--c-ink))"></textarea>
    </div>
    <div>
      <label class="filter-label" for="biz-company">Company name</label>
      <input id="biz-company" name="company" type="text" placeholder="e.g. Globex"
             style="width:100%;font-size:13px;padding:9px 12px;border-radius:var(--r-md);border:1px solid rgb(var(--c-line));background:rgb(var(--c-bg));color:rgb(var(--c-ink))">
    </div>
    <div>
      <label class="filter-label" for="biz-pattern">Email pattern</label>
      <input id="biz-pattern" name="pattern" type="text" placeholder="{first}.{last}@company.com"
             style="width:100%;font-size:13px;padding:9px 12px;border-radius:var(--r-md);border:1px solid rgb(var(--c-line));background:rgb(var(--c-bg));color:rgb(var(--c-ink))">
    </div>
    <button type="submit" class="btn" style="align-self:flex-start">Parse &amp; import</button>
  </form>
  <form hx-post="/business/upload-csv" hx-target="#biz-groups" hx-swap="innerHTML"
        hx-on::after-request="if(event.detail.successful){document.getElementById('biz-drawer-toggle').checked=false}"
        style="display:flex;flex-direction:column;gap:8px;padding:0 20px 24px;border-top:1px solid rgb(var(--c-line))">
    <label class="filter-label" for="biz-csv" style="margin-top:12px">Or paste CSV (company,name,title,email)</label>
    <textarea id="biz-csv" name="csv_text" rows="5" placeholder="company,name,title,email&#10;Globex,Ada Byte,CEO,ada@globex.com"
              style="width:100%;font-size:12.5px;padding:10px 12px;border-radius:var(--r-md);border:1px solid rgb(var(--c-line));background:rgb(var(--c-bg));color:rgb(var(--c-ink))"></textarea>
    <button type="submit" class="btn-open" style="align-self:flex-start">Import CSV</button>
  </form>
</aside>
```

- [ ] **Step 6: Create `business.html`** (page shell)

```html
{% extends "base.html" %}
{% block title %}Business · Northstar{% endblock %}
{% block content %}
<div class="flex flex-wrap items-end justify-between gap-4 mb-5 reveal">
  <div>
    <h1 class="page-h">Business</h1>
    <p class="page-sub">B2B outreach — sales prospects</p>
  </div>
  <div class="flex items-center gap-2">
    <button type="button" class="btn-open" data-copy-emails title="Copy every visible email">Copy all emails</button>
    <label for="biz-drawer-toggle" class="btn" style="cursor:pointer">+ Add prospects</label>
  </div>
</div>

<div class="panel reveal" style="padding:16px 20px;margin-bottom:18px;display:flex;gap:14px;flex-wrap:wrap">
  {% for s in biz_stage_flow %}
  <div class="kpi" style="min-width:96px;padding:10px 14px">
    <div class="l">{{ s|title }}</div>
    <div class="v">{{ summary[s] }}</div>
  </div>
  {% endfor %}
</div>

<div class="ttoolbar reveal">
  <div>
    <label for="bz-q" class="filter-label">Search</label>
    <input id="bz-q" type="text" name="q" value="{{ f.q }}" placeholder="Company or person…"
           class="filter-select" style="max-width:220px"
           hx-get="/business" hx-target="#biz-groups" hx-swap="innerHTML"
           hx-trigger="keyup changed delay:300ms" hx-include="closest .ttoolbar">
  </div>
  <div>
    <label for="bz-stage" class="filter-label">Stage</label>
    <select id="bz-stage" name="stage" class="filter-select"
            hx-get="/business" hx-target="#biz-groups" hx-swap="innerHTML"
            hx-trigger="change" hx-include="closest .ttoolbar">
      <option value="">All stages</option>
      {% for s in biz_stage_flow %}
      <option value="{{ s }}" {{ 'selected' if f.stage == s }}>{{ s|title }}</option>
      {% endfor %}
    </select>
  </div>
</div>

<div id="biz-groups" style="display:flex;flex-direction:column;gap:12px;margin-top:12px">
  {% include "_biz_groups.html" %}
</div>

{% include "_biz_ingest_drawer.html" %}
{% endblock %}
```

- [ ] **Step 7: Run the route smoke tests + manual verify**

Run: `.venv/bin/python -m unittest test_biz_mode.BizRoutesTest -v`
Expected: PASS.
Then `python3 bootstrap.py`, open `/business`, add a prospect via paste, change a stage, click "Copy all emails", flag a company priority.

- [ ] **Step 8: Commit**

```bash
git add app/templates/business.html app/templates/_biz_groups.html app/templates/_biz_company.html app/templates/_biz_prospect.html app/templates/_biz_ingest_drawer.html app/templates/base.html
git commit -m "feat(business): Business tab UI (page, groups, prospect rows, ingest drawer, nav)"
```

---

### Task 11: Mode-isolation test + full-suite regression

**Files:**
- Test: `test_biz_mode.py`

- [ ] **Step 1: Write the isolation test**

```python
# test_biz_mode.py
class ModeIsolationTest(unittest.TestCase):
    def test_business_data_never_leaks_into_job_tracker(self):
        con = _fresh_con()
        q.ingest_biz_prospects(con, "globex", "Globex",
            [{"name": "Ada Byte", "title": "CEO", "email": "ada@globex.com", "pattern": ""}])
        # job tracker must not see business prospects
        groups, stats = q.tracker_groups(con)
        biz_names = {"Ada Byte"}
        job_names = {p["name"] for g in groups for p in g.get("people", [])}
        # company_detail for the biz company must not surface biz prospects as job people
        detail = q.company_detail(con, "globex")
        con.close()
        self.assertFalse(biz_names & job_names)
        self.assertNotIn("Ada Byte", [p["name"] for p in detail["people"]])
```

- [ ] **Step 2: Run test**

Run: `.venv/bin/python -m unittest test_biz_mode.ModeIsolationTest -v`
Expected: PASS (business writes only `biz_*`; job reads never touch `biz_*`).

- [ ] **Step 3: Full suite regression**

Run: `.venv/bin/python -m unittest discover -p 'test_*.py'`
Expected: OK, no failures.

- [ ] **Step 4: Commit**

```bash
git add test_biz_mode.py
git commit -m "test(business): mode-isolation + full-suite green"
```

---

## Self-Review (completed)

- **Spec coverage:** Part 1 — tables (T4), ingest+parser reuse (T5), pipeline/groups+summary (T6), stage/priority (T7), CSV (T8), routes (T9), tab/nav/UI/copy/priority (T10), isolation (T11). Part 2 — Prophix fix (T1), companies-with-contacts (T2), copy-all-emails (T3). All spec sections map to a task.
- **Placeholder scan:** none — every code step contains real code.
- **Type consistency:** `BIZ_STAGE_FLOW`, `prospect_key`, `company_key`, `stage`, `biz_groups()→(groups, summary)`, `_biz_groups.html`/`_biz_company.html`/`_biz_prospect.html` names are used identically across tasks; `data-copy-emails` + `data-company-scope` + `data-email` match the shared JS in T3.
- **Ordering note:** T6's test calls `set_biz_stage` (defined in T7) — implement T7's `set_biz_stage` alongside T6, or run T6's assertion after T7. T9's route test needs T10's templates — implement T10 before running T9's test (or stub templates).
