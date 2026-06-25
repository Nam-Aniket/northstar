# Tailor & Edit v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the per-job resume editor place a real, fact-bank evidence bullet for a claimed skill into the exact role it was authored for (write a new one if none exists, persisting it for reuse), edit bullets one at a time, and preview the cover letter beside the resume.

**Architecture:** Deterministic, no LLM. Backend adds three pure helpers in `generate_accepted_resumes.py` (`reload_facts`, `add_fact_bullet`, `placeable_bullets_for_skill`) plus two FastAPI routes; all guarded so a skill must be a real gap/evidence for the job and a slot must be known. Frontend extends the existing vanilla-JS editor (`job_editor.html`): bullets become individual rows with per-row delete, claimed skills surface Add-cards that insert into the slot-matched experience, and the preview pane gains Resume|Cover tabs. Source of truth for bullets stays `facts.json` (gitignored PII), written atomically.

**Tech Stack:** Python 3 / FastAPI / Jinja2 / HTMX / vanilla JS, SQLite, pytest. Run app: `python3 bootstrap.py` → http://127.0.0.1:8765.

---

## File structure

- `generate_accepted_resumes.py` (modify) — owns the `FACT_BANK`/`EXPERIENCE_SLOTS`/`BULLET_BUDGETS` module globals; gains `FACTS_LOCK`, `reload_facts()`, `add_fact_bullet()`, `placeable_bullets_for_skill()`.
- `test_editor_v2.py` (create) — pytest for the three helpers + honesty guards.
- `app/app.py` (modify) — new `GET /jobs/{row_key}/bullets` and `POST /jobs/{row_key}/bullets/add`; add `slot` to `_editor_prefill` experiences; pass `claimed_skill` into `_job_fit.html` from the claim route.
- `app/templates/_bullet_suggestions.html` (create) — Add-cards or write-once box partial.
- `app/templates/_job_fit.html` (modify) — out-of-band suggestions loader after a claim.
- `app/templates/job_editor.html` (modify) — per-bullet rows, Add-card + write-once JS, persistent `#bullet-suggestions` container, Resume|Cover tabs + cover render.
- `app/static/app.css` (modify) — styles for bullet rows, suggestion cards, tabs.

---

## Task 1: Backend — `reload_facts()` + `add_fact_bullet()` (write-once persistence)

**Files:**
- Modify: `generate_accepted_resumes.py` (near the FACT_BANK load block, ~line 217)
- Test: `test_editor_v2.py` (create)

- [ ] **Step 1: Write the failing test**

Create `test_editor_v2.py`:

```python
import json
import importlib
import generate_accepted_resumes as gen
import config


def _fixture_banks(monkeypatch):
    monkeypatch.setattr(gen, "EXPERIENCE_SLOTS",
                        [("Analyst | Acme | 2022 - 2024", "role1"),
                         ("Intern | Beta | 2020 - 2021", "role2")])
    monkeypatch.setattr(gen, "BULLET_BUDGETS", {"role1": 3, "role2": 3})
    monkeypatch.setattr(gen, "FACT_BANK", {
        "role1": [{"text": "Owned the data pipeline.", "evidences": ["Data Pipelines"]}],
        "role2": [],
    })


def test_add_fact_bullet_persists_and_reloads(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.delenv("JOBENGINE_FACTS", raising=False)
    _fixture_banks(monkeypatch)

    entry = gen.add_fact_bullet("role1", "Built CI/CD in GitHub Actions.", "CI/CD")

    assert entry == {"text": "Built CI/CD in GitHub Actions.", "evidences": ["CI/CD"]}
    saved = json.loads((tmp_path / "facts.json").read_text())
    texts = [b["text"] for b in saved["FACT_BANK"]["role1"]]
    assert "Built CI/CD in GitHub Actions." in texts
    # reload made it visible in-process
    assert any(b["text"] == "Built CI/CD in GitHub Actions."
               for b in gen.FACT_BANK["role1"])


def test_add_fact_bullet_rejects_unknown_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    _fixture_banks(monkeypatch)
    try:
        gen.add_fact_bullet("nope", "x", "CI/CD")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_add_fact_bullet_dedupes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.delenv("JOBENGINE_FACTS", raising=False)
    _fixture_banks(monkeypatch)
    gen.add_fact_bullet("role1", "Owned the data pipeline.", "Automation")
    # "Owned the data pipeline." already exists; no duplicate text row
    texts = [b["text"] for b in gen.FACT_BANK["role1"]]
    assert texts.count("Owned the data pipeline.") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_editor_v2.py::test_add_fact_bullet_persists_and_reloads -v`
Expected: FAIL — `AttributeError: module 'generate_accepted_resumes' has no attribute 'add_fact_bullet'`

- [ ] **Step 3: Implement `reload_facts` + `add_fact_bullet`**

In `generate_accepted_resumes.py`, add near the top imports:

```python
import threading
```

Immediately after the `_facts_override` load block (where `FACT_BANK`, `BULLET_BUDGETS`, `EXPERIENCE_SLOTS` are assigned, ~line 221), add:

```python
FACTS_LOCK = threading.Lock()


def _current_facts_dict() -> dict:
    """Snapshot the in-memory banks as a facts.json-shaped dict."""
    return {
        "FACT_BANK": FACT_BANK,
        "EXPERIENCE_SLOTS": [list(s) for s in EXPERIENCE_SLOTS],
        "BULLET_BUDGETS": BULLET_BUDGETS,
    }


def reload_facts() -> None:
    """Re-read facts.json into the module globals (mirrors config.reset_banks
    for skills.json). After a write, the next build_content sees the new bullet."""
    global FACT_BANK, EXPERIENCE_SLOTS, BULLET_BUDGETS
    ov = config.load_facts_override()
    if ov:
        FACT_BANK = ov["FACT_BANK"]
        BULLET_BUDGETS = ov["BULLET_BUDGETS"]
        EXPERIENCE_SLOTS = [tuple(s) for s in ov["EXPERIENCE_SLOTS"]]


def add_fact_bullet(slot: str, text: str, skill: str) -> dict:
    """Append a user-authored bullet to FACT_BANK[slot] in facts.json, tagged
    with `skill`, then reload. Honest-by-construction: the caller picks the real
    role the bullet belongs to. Returns the saved {text, evidences} entry."""
    from facts_bridge import save_facts

    text = (text or "").strip()
    slot = (slot or "").strip()
    if not text:
        raise ValueError("empty bullet text")
    if slot not in {s for _, s in EXPERIENCE_SLOTS}:
        raise ValueError(f"unknown slot: {slot}")

    with FACTS_LOCK:
        facts = config.load_facts_override() or _current_facts_dict()
        bank = facts.setdefault("FACT_BANK", {})
        pool = bank.setdefault(slot, [])
        existing = next((b for b in pool if b.get("text", "").strip() == text), None)
        if existing is None:
            entry = {"text": text, "evidences": [skill] if skill else []}
            pool.append(entry)
        else:
            entry = existing
            if skill and skill not in entry.get("evidences", []):
                entry.setdefault("evidences", []).append(skill)
        save_facts(facts, config.ROOT)
        reload_facts()
    return entry
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_editor_v2.py -k add_fact_bullet -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add generate_accepted_resumes.py test_editor_v2.py
git commit -m "feat(editor): add_fact_bullet writer + reload_facts for write-once bullets

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 2: Backend — `placeable_bullets_for_skill()`

**Files:**
- Modify: `generate_accepted_resumes.py`
- Test: `test_editor_v2.py`

- [ ] **Step 1: Write the failing test**

Append to `test_editor_v2.py`:

```python
def test_placeable_excludes_already_selected(monkeypatch):
    monkeypatch.setattr(gen, "EXPERIENCE_SLOTS",
                        [("Analyst | Acme | 2022 - 2024", "role1")])
    monkeypatch.setattr(gen, "BULLET_BUDGETS", {"role1": 1})
    monkeypatch.setattr(gen, "FACT_BANK", {"role1": [
        {"text": "Selected pipeline bullet.", "evidences": ["CI/CD"]},
        {"text": "Bench CI/CD bullet.", "evidences": ["CI/CD"]},
    ]})

    # Force build_content to report the first bullet as the selected one.
    monkeypatch.setattr(gen, "build_content",
                        lambda target, jd: {"bullets_by_slot": {"role1": ["Selected pipeline bullet."]}})

    out = gen.placeable_bullets_for_skill({"role_title": "x", "company": "y"}, "jd", "CI/CD")
    texts = [b["text"] for b in out]
    assert texts == ["Bench CI/CD bullet."]          # selected one excluded
    assert out[0]["slot"] == "role1"
    assert out[0]["role_header"] == "Analyst | Acme | 2022 - 2024"


def test_placeable_case_insensitive_and_empty(monkeypatch):
    monkeypatch.setattr(gen, "EXPERIENCE_SLOTS",
                        [("Analyst | Acme | 2022 - 2024", "role1")])
    monkeypatch.setattr(gen, "FACT_BANK", {"role1": [
        {"text": "A bullet.", "evidences": ["Data Quality"]},
    ]})
    monkeypatch.setattr(gen, "build_content",
                        lambda target, jd: {"bullets_by_slot": {"role1": []}})

    assert [b["text"] for b in gen.placeable_bullets_for_skill({}, "jd", "data quality")] == ["A bullet."]
    assert gen.placeable_bullets_for_skill({}, "jd", "") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_editor_v2.py -k placeable -v`
Expected: FAIL — `AttributeError: ... has no attribute 'placeable_bullets_for_skill'`

- [ ] **Step 3: Implement the helper**

In `generate_accepted_resumes.py`, after `add_fact_bullet`:

```python
def placeable_bullets_for_skill(target: dict, jd_text: str, skill: str) -> list[dict]:
    """Fact-bank bullets that evidence `skill` and are NOT already selected for
    this job, each tagged with the slot + role header it belongs to.
    Returns [{"slot": str, "role_header": str, "text": str}]."""
    skill_lc = (skill or "").strip().lower()
    if not skill_lc:
        return []
    selected = build_content(target, jd_text).get("bullets_by_slot", {})
    out: list[dict] = []
    for header, slot in EXPERIENCE_SLOTS:
        sel = set(selected.get(slot, []))
        for b in FACT_BANK.get(slot, []):
            ev = {e.strip().lower() for e in b.get("evidences", [])}
            if skill_lc in ev and b.get("text") not in sel:
                out.append({"slot": slot, "role_header": header, "text": b["text"]})
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_editor_v2.py -v`
Expected: all passed (Task 1 + Task 2).

- [ ] **Step 5: Commit**

```bash
git add generate_accepted_resumes.py test_editor_v2.py
git commit -m "feat(editor): placeable_bullets_for_skill (pool minus selected)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 3: Backend — routes + `_bullet_suggestions.html` + prefill slot

**Files:**
- Modify: `app/app.py` (after `job_editor_add_skill`, ~line 905; and `_editor_prefill` experiences, ~line 802)
- Create: `app/templates/_bullet_suggestions.html`
- Test: `test_editor_v2.py`

- [ ] **Step 1: Add `slot` to prefill experiences**

In `app/app.py` `_editor_prefill`, change the experience append (~line 802) to carry the slot:

```python
        if title_text or bullets:
            exp_role, exp_company, exp_dates = _split_slot_header(title_text)
            experiences.append({"role": exp_role, "company": exp_company,
                                "dates": exp_dates, "bullets": bullets, "slot": slot})
```

- [ ] **Step 2: Write the failing route test**

Append to `test_editor_v2.py`:

```python
from starlette.testclient import TestClient


def _client():
    from app.app import app
    return TestClient(app)


def test_bullets_add_rejects_skill_not_in_job(monkeypatch):
    import app.queries as queries
    monkeypatch.setattr(queries, "get_job",
                        lambda con, rk: {"row_key": rk, "role_title": "A", "company": "B",
                                         "job_text": "", "gaps_list": ["CI/CD"], "evidence_list": []})
    c = _client()
    r = c.post("/jobs/abc/bullets/add",
               data={"skill": "Witchcraft", "slot": "role1", "text": "x"})
    assert r.status_code == 400
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest test_editor_v2.py -k bullets_add -v`
Expected: FAIL (404, route not defined).

- [ ] **Step 4: Create the suggestions partial**

Create `app/templates/_bullet_suggestions.html`:

```html
{# Placeable evidence bullets for a claimed skill. Either Add-cards (bullets the
   user already wrote, tagged with this skill) or a write-once box when none exist.
   data-target-slot drives exact placement into the matching experience block. #}
<div class="bullet-suggest" data-skill="{{ skill }}">
  {% if bullets %}
  <p class="text-xs uppercase tracking-wide text-muted mb-1.5">Evidence for "{{ skill }}"</p>
  <p class="text-xs text-muted mb-2">Add a real bullet you wrote into the role it belongs to.</p>
  {% for b in bullets %}
  <div class="suggest-card" data-add-bullet="{{ b.text }}" data-target-slot="{{ b.slot }}">
    <div class="suggest-role">{{ b.role_header }}</div>
    <div class="suggest-text">{{ b.text }}</div>
    <button type="button" class="suggest-add btn-apply">Add to resume</button>
  </div>
  {% endfor %}
  {% else %}
  <p class="text-xs uppercase tracking-wide text-muted mb-1.5">No evidence bullet yet for "{{ skill }}"</p>
  <p class="text-xs text-muted mb-2">Write one in your own words. It saves to your fact bank for future jobs too.</p>
  <form class="writeonce" data-skill="{{ skill }}" data-row="{{ row_key }}">
    <textarea class="filter-select wo-text" rows="2"
              placeholder="e.g. Built CI/CD pipelines in GitHub Actions, cutting deploy time 40%."></textarea>
    <label class="filter-label" style="margin-top:8px;display:block">Which role does this belong to?</label>
    <select class="filter-select wo-slot">
      {% for header, slot in slots %}<option value="{{ slot }}">{{ header }}</option>{% endfor %}
    </select>
    <button type="button" class="wo-save btn-apply" style="margin-top:8px">Save &amp; add</button>
  </form>
  {% endif %}
</div>
```

- [ ] **Step 5: Implement the two routes**

In `app/app.py`, after `job_editor_add_skill` (~line 905), add:

```python
def _job_target(job: dict) -> dict:
    return {"role_title": job.get("role_title", ""), "company": job.get("company", "")}


def _job_skill_ok(job: dict, skill: str) -> bool:
    allowed = {s.strip().lower() for s in
               (job.get("gaps_list", []) + job.get("evidence_list", []))}
    return (skill or "").strip().lower() in allowed


@app.get("/jobs/{row_key:path}/bullets", response_class=HTMLResponse)
def job_editor_bullets(request: Request, row_key: str, skill: str = ""):
    import generate_accepted_resumes as gen
    con = conn(); job = queries.get_job(con, row_key); con.close()
    if not job:
        return HTMLResponse("Job not found", status_code=404)
    skill = (skill or "").strip()
    bullets = []
    if skill and _job_skill_ok(job, skill):
        bullets = gen.placeable_bullets_for_skill(_job_target(job), job.get("job_text", "") or "", skill)
    return templates.TemplateResponse(request, "_bullet_suggestions.html", {
        "skill": skill, "bullets": bullets, "row_key": row_key,
        "slots": gen.EXPERIENCE_SLOTS,
    })


@app.post("/jobs/{row_key:path}/bullets/add")
def job_editor_add_bullet(row_key: str, skill: str = Form(...),
                          slot: str = Form(...), text: str = Form(...)):
    import generate_accepted_resumes as gen
    con = conn(); job = queries.get_job(con, row_key); con.close()
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    skill = (skill or "").strip()
    if not _job_skill_ok(job, skill):
        return JSONResponse({"error": "skill not part of this job"}, status_code=400)
    if slot not in {s for _, s in gen.EXPERIENCE_SLOTS}:
        return JSONResponse({"error": "unknown role"}, status_code=400)
    try:
        gen.add_fact_bullet(slot, text, skill)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    header = next((h for h, s in gen.EXPERIENCE_SLOTS if s == slot), "")
    return JSONResponse({"slot": slot, "text": text.strip(), "role_header": header})
```

- [ ] **Step 6: Pass `claimed_skill` from the claim route**

In `app/app.py` `job_editor_add_skill`, change the final return (~line 904) to surface the claimed skill so the suggestions loader can fire:

```python
    con.close()
    return templates.TemplateResponse(request, "_job_fit.html",
                                      {"j": job, "claimed_skill": label})
```

- [ ] **Step 7: Run the route test**

Run: `python3 -m pytest test_editor_v2.py -k bullets_add -v`
Expected: PASS (400 for a skill not in the job).

- [ ] **Step 8: Commit**

```bash
git add app/app.py app/templates/_bullet_suggestions.html test_editor_v2.py
git commit -m "feat(editor): bullets routes + suggestions partial + prefill slot

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 4: Frontend — per-bullet rows (Part C)

**Files:**
- Modify: `app/templates/job_editor.html` (`expBlock` ~167, `collect` ~209, `applyData` exp loop ~404)

- [ ] **Step 1: Add a `bulletRow` builder and rewrite `expBlock`**

In `job_editor.html`, replace `expBlock()` (lines 167-178) with:

```javascript
  function bulletRow(text) {
    var row = document.createElement('div');
    row.className = 'bullet-row';
    row.innerHTML =
      '<textarea class="filter-select bullet-text" rows="2"></textarea>' +
      '<button type="button" class="bullet-del" title="Delete this bullet">' +
        '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>' +
      '</button>';
    row.querySelector('.bullet-text').value = text || '';
    row.querySelector('.bullet-del').addEventListener('click', function () { row.remove(); scheduleRender(); });
    return row;
  }

  function expBlock() {
    var block = rowShell('exp-block', 'remove-exp', [
      '<div class="grid grid-cols-2 gap-3">',
        '<div class="col-span-2"><label class="filter-label">Role / heading</label>',
          '<input class="filter-select exp-role" type="text" style="padding-right:36px"></div>',
        '<div><label class="filter-label">Company</label><input class="filter-select exp-company" type="text"></div>',
        '<div><label class="filter-label">Dates</label><input class="filter-select exp-dates" type="text"></div>',
        '<div class="col-span-2"><label class="filter-label">Bullet points</label>',
          '<div class="bullet-list"></div>',
          '<button type="button" class="add-bullet">+ Add bullet</button>',
        '</div>',
      '</div>',
    ].join(''));
    block.querySelector('.add-bullet').addEventListener('click', function () {
      block.querySelector('.bullet-list').appendChild(bulletRow(''));
      scheduleRender();
    });
    return block;
  }
```

- [ ] **Step 2: Rewrite the experience branch of `collect()`**

Replace the `.exp-block` loop in `collect()` (lines 209-216) with:

```javascript
    document.querySelectorAll('.exp-block').forEach(function (b) {
      var bullets = [];
      b.querySelectorAll('.bullet-text').forEach(function (t) {
        var v = t.value.trim(); if (v) bullets.push(v);
      });
      exps.push({
        role: b.querySelector('.exp-role').value.trim(),
        company: b.querySelector('.exp-company').value.trim(),
        dates: b.querySelector('.exp-dates').value.trim(),
        bullets: bullets,
      });
    });
```

- [ ] **Step 3: Rewrite the experience branch of `applyData()`**

Replace the experiences loop in `applyData()` (lines 404-410) with:

```javascript
    (d.experiences && d.experiences.length ? d.experiences : [{}]).forEach(function (e) {
      var b = expBlock(); expList.appendChild(b);
      if (e.role) b.querySelector('.exp-role').value = e.role;
      if (e.company) b.querySelector('.exp-company').value = e.company;
      if (e.dates) b.querySelector('.exp-dates').value = e.dates;
      if (e.slot) b.dataset.slot = e.slot;
      var bl = b.querySelector('.bullet-list');
      (e.bullets || []).forEach(function (txt) { bl.appendChild(bulletRow(txt)); });
    });
```

- [ ] **Step 4: Manual verification**

Run: `python3 bootstrap.py`, open http://127.0.0.1:8765, go to any job → "Tailor & edit".
Expected: each experience shows its bullets as separate rows, each with an X; clicking an X deletes only that bullet and the preview updates; "+ Add bullet" adds a blank row; editing a row updates the preview live; Download .zip still produces a resume with the bullets.

- [ ] **Step 5: Commit**

```bash
git add app/templates/job_editor.html
git commit -m "feat(editor): per-bullet rows with single-bullet delete

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 5: Frontend — Add-card placement + write-once (Parts A & B)

**Files:**
- Modify: `app/templates/job_editor.html` (add container + handlers, init)
- Modify: `app/templates/_job_fit.html` (OOB suggestions loader)

- [ ] **Step 1: Add the persistent suggestions container**

In `job_editor.html`, the Match-fit panel currently is (lines 28-31):

```html
    <div class="panel p-5">
      <h2 class="section-h mb-3">Match fit</h2>
      {% include "_job_fit.html" %}
    </div>
```

Replace with:

```html
    <div class="panel p-5">
      <h2 class="section-h mb-3">Match fit</h2>
      {% include "_job_fit.html" %}
      <div id="bullet-suggestions" class="mt-4"></div>
    </div>
```

- [ ] **Step 2: Add the OOB loader to `_job_fit.html`**

At the very end of `app/templates/_job_fit.html`, after the closing `</div>` of `#job-fit`, add:

```html
{% if claimed_skill %}
<div id="bullet-suggestions" class="mt-4"
     hx-get="/jobs/{{ j.row_key|urlk }}/bullets?skill={{ claimed_skill|urlencode }}"
     hx-trigger="load" hx-swap="innerHTML" hx-swap-oob="true"></div>
{% endif %}
```

- [ ] **Step 3: Add the Add-card + write-once JS handlers**

In `job_editor.html`, just before the `/* ── Init ── */` block (line 459), add:

```javascript
  /* ── Find the experience block for a slot (exact), else by role text ──────── */
  function findExpBlock(slot, roleHeader) {
    var blocks = document.querySelectorAll('.exp-block');
    var i;
    if (slot) {
      for (i = 0; i < blocks.length; i++) {
        if (blocks[i].dataset.slot === slot) return blocks[i];
      }
    }
    if (roleHeader) {
      var head = roleHeader.toLowerCase();
      for (i = 0; i < blocks.length; i++) {
        var r = (blocks[i].querySelector('.exp-role') || {}).value || '';
        if (r && head.indexOf(r.trim().toLowerCase()) !== -1) return blocks[i];
      }
    }
    return blocks.length ? blocks[0] : null;
  }

  function insertBulletInto(block, text) {
    if (!block) return;
    var bl = block.querySelector('.bullet-list');
    var dup = Array.prototype.some.call(bl.querySelectorAll('.bullet-text'),
      function (t) { return t.value.trim() === text.trim(); });
    if (!dup) { bl.appendChild(bulletRow(text)); scheduleRender(); }
  }

  /* ── Add a fact-bank evidence bullet (Part A) ─────────────────────────────── */
  document.body.addEventListener('click', function (e) {
    var card = e.target.closest('.suggest-card');
    if (!card || !e.target.closest('.suggest-add')) return;
    insertBulletInto(findExpBlock(card.getAttribute('data-target-slot'),
                                  card.querySelector('.suggest-role').textContent),
                     card.getAttribute('data-add-bullet'));
    card.classList.add('is-added');
    card.querySelector('.suggest-add').textContent = 'Added';
    card.querySelector('.suggest-add').disabled = true;
  });

  /* ── Write-once: save a new bullet to the fact bank + place it (Part B) ───── */
  document.body.addEventListener('click', function (e) {
    if (!e.target.closest('.wo-save')) return;
    var form = e.target.closest('.writeonce');
    var text = form.querySelector('.wo-text').value.trim();
    var slot = form.querySelector('.wo-slot').value;
    var skill = form.getAttribute('data-skill');
    var row = form.getAttribute('data-row');
    if (!text) { form.querySelector('.wo-text').focus(); return; }
    var btn = e.target.closest('.wo-save'); btn.disabled = true; btn.textContent = 'Saving…';
    var body = new URLSearchParams({skill: skill, slot: slot, text: text});
    fetch('/jobs/' + encodeURIComponent(row) + '/bullets/add',
          {method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: body})
      .then(function (r) { return r.ok ? r.json() : r.json().then(function (j) { throw new Error(j.error || 'failed'); }); })
      .then(function (d) {
        insertBulletInto(findExpBlock(d.slot, d.role_header), d.text);
        form.parentNode.innerHTML = '<p class="text-xs text-leaf">Added to ' + esc(d.role_header) + ' and saved to your fact bank.</p>';
      })
      .catch(function (err) {
        btn.disabled = false; btn.textContent = 'Save & add';
        var msg = document.createElement('p'); msg.className = 'text-xs'; msg.style.color = 'rgb(185 28 28)';
        msg.textContent = 'Could not save: ' + err.message;
        form.appendChild(msg);
      });
  });
```

- [ ] **Step 4: Manual verification**

Run the app. Open a job with missing skills in "Tailor & edit".
Expected:
- Click a missing-skill chip → it re-scores AND a suggestions block appears below Match-fit.
- If you have a fact-bank bullet for that skill, an Add-card shows with its role; "Add to resume" drops the bullet into that exact experience and the button reads "Added".
- If you have no bullet, the write-once box appears; type a bullet, pick a role, "Save & add" inserts it into that role and confirms it was saved. Re-open the editor on another job and claim the same skill → the new bullet now appears as an Add-card (persisted).

- [ ] **Step 5: Commit**

```bash
git add app/templates/job_editor.html app/templates/_job_fit.html
git commit -m "feat(editor): place fact-bank bullets + write-once into the matched role

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 6: Frontend — Resume | Cover tabs (Part D)

**Files:**
- Modify: `app/templates/job_editor.html` (toolbar tabs, cover render, preview container)

- [ ] **Step 1: Add the tab buttons and a cover page to the preview pane**

In `job_editor.html`, in the preview toolbar (replace the left span at lines 108-111) with tab buttons:

```html
        <div class="flex items-center gap-2">
          <button type="button" id="tab-resume" class="prev-tab is-active">Resume</button>
          <button type="button" id="tab-cover" class="prev-tab">Cover letter</button>
          <span id="page-count-badge" style="display:none;font-size:11px;font-weight:600;padding:2px 8px;border-radius:20px;line-height:1.6"></span>
        </div>
```

Then inside `#paper-scaler` (after `#preview-paper`'s closing `</div>`, ~line 137), add a sibling cover page:

```html
          <div id="cover-paper"
            style="display:none;position:relative;background:#fff;color:#111;font-family:Calibri,Arial,sans-serif;font-size:11pt;
                   line-height:1.5;padding:76px;width:794px;min-height:1122px;box-sizing:border-box;
                   box-shadow:0 2px 20px rgba(0,0,0,.18)"></div>
```

- [ ] **Step 2: Add cover rendering + tab switching JS**

In `job_editor.html`, inside `render()` after `updateCoverage(d)` (line 307), add a call:

```javascript
    renderCover();
```

Then add these functions before `/* ── Init ── */`:

```javascript
  function renderCover() {
    var paper = document.getElementById('cover-paper');
    if (!paper) return;
    var raw = val('f-cover');
    var d = collect();
    var head = [];
    if (d.name) head.push('<div style="font-size:14pt;font-weight:700;color:#1F4E79">' + esc(d.name) + '</div>');
    var contact = [d.email, d.phone, d.location].filter(Boolean).map(esc).join('  ·  ');
    if (contact) head.push('<div style="font-size:9.5pt;color:#57534A;margin-bottom:18px">' + contact + '</div>');
    var paras = raw.split(/\n\s*\n/).map(function (p) { return p.trim(); }).filter(Boolean);
    var body = paras.map(function (p) {
      return '<p style="margin:0 0 11px;font-size:11pt">' + esc(p).replace(/\n/g, '<br>') + '</p>';
    }).join('');
    paper.innerHTML = head.join('') + (body || '<p style="color:#999;text-align:center;margin-top:40px">Your cover letter preview appears here.</p>');
  }

  function showTab(which) {
    var isCover = which === 'cover';
    document.getElementById('preview-paper').style.display = isCover ? 'none' : 'block';
    document.getElementById('cover-paper').style.display = isCover ? 'block' : 'none';
    document.getElementById('tab-resume').classList.toggle('is-active', !isCover);
    document.getElementById('tab-cover').classList.toggle('is-active', isCover);
    if (isCover) renderCover(); else layoutPaper();
  }
  document.getElementById('tab-resume').addEventListener('click', function () { showTab('resume'); });
  document.getElementById('tab-cover').addEventListener('click', function () { showTab('cover'); });
```

- [ ] **Step 3: Manual verification**

Run the app, open "Tailor & edit".
Expected: two tabs above the preview; "Cover letter" shows the formatted letter (name + contact header, paragraphs from the cover box); editing the cover textarea updates it live; "Resume" switches back to the paper with page markers intact; Download .zip still bundles both.

- [ ] **Step 4: Commit**

```bash
git add app/templates/job_editor.html
git commit -m "feat(editor): Resume | Cover letter preview tabs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 7: Styles for bullet rows, suggestion cards, tabs

**Files:**
- Modify: `app/static/app.css` (append)

- [ ] **Step 1: Append styles**

Add to `app/static/app.css`:

```css
/* Per-bullet editing rows (job editor) */
.bullet-row { display: flex; gap: 6px; align-items: flex-start; margin-bottom: 6px; }
.bullet-row .bullet-text { flex: 1; resize: vertical; }
.bullet-del { flex: none; width: 28px; height: 28px; border-radius: 8px;
  border: 1px solid rgb(var(--c-line)); background: rgb(var(--c-paper));
  color: rgb(var(--c-muted)); cursor: pointer; display: grid; place-items: center; }
.bullet-del:hover { color: rgb(185 28 28); }
.add-bullet { margin-top: 2px; font-size: 12px; padding: 4px 10px; border-radius: 8px;
  border: 1px dashed rgb(var(--c-line)); background: transparent; color: rgb(var(--c-muted)); cursor: pointer; }
.add-bullet:hover { color: rgb(var(--c-clay)); border-color: rgb(var(--c-clay)); }

/* Evidence-bullet suggestion cards */
.suggest-card { border: 1px solid rgb(var(--c-line)); border-radius: 10px;
  padding: 10px 12px; margin-bottom: 8px; background: rgb(var(--c-sand)); }
.suggest-card.is-added { opacity: .55; }
.suggest-role { font-size: 11px; font-weight: 600; color: rgb(var(--c-muted));
  text-transform: uppercase; letter-spacing: .04em; margin-bottom: 3px; }
.suggest-text { font-size: 13px; margin-bottom: 8px; }

/* Preview tabs */
.prev-tab { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em;
  padding: 4px 10px; border-radius: 20px; border: 1px solid transparent;
  background: transparent; color: rgb(var(--c-muted)); cursor: pointer; }
.prev-tab.is-active { color: rgb(var(--c-clay)); border-color: rgb(var(--c-line));
  background: rgb(var(--c-paper)); }
```

- [ ] **Step 2: Manual verification**

Reload the editor; confirm bullet rows, delete buttons, suggestion cards, and tabs are visually consistent with the rest of the app in both light and dark mode.

- [ ] **Step 3: Commit**

```bash
git add app/static/app.css
git commit -m "style(editor): bullet rows, suggestion cards, preview tabs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 8: Regression + final verification

- [ ] **Step 1: Run the new suite + the ATS regression gate**

Run: `python3 -m pytest test_editor_v2.py test_ats_engine.py -v`
Expected: all passed (no scoring/taxonomy change, so `test_ats_engine` stays green).

- [ ] **Step 2: Run the broader suite**

Run: `python3 -m pytest -q`
Expected: no new failures versus the pre-change baseline. Note any pre-existing failures unrelated to this change.

- [ ] **Step 3: End-to-end manual pass**

Run the app and confirm the full flow on one job: claim a gap → Add an existing evidence bullet into its role → claim a gap with no bullet → write one → it persists and re-appears on another job → delete a single bullet → switch to the Cover tab and read it → Download .zip and confirm the resume contains the placed bullets and the cover matches the preview.

- [ ] **Step 4: Final commit (if any cleanup)**

```bash
git add -A
git commit -m "test(editor): regression pass for Tailor & Edit v2

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-review notes

- **Spec coverage:** Part A → Tasks 2,3,5; Part B → Tasks 1,3,5; Part C → Task 4; Part D → Task 6; honesty guards → Task 3 (`_job_skill_ok`, slot check); fact-bank persistence + in-process reload (the key risk) → Task 1; styles → Task 7; regression incl. `test_ats_engine` → Task 8.
- **Assumptions resolved:** `build_content` exposes only selected text, so `placeable_bullets_for_skill` reads `FACT_BANK` directly (Task 2). No facts cache reset existed, so `reload_facts()` is added (Task 1). Slot identity is carried explicitly into each experience block via `data-slot` (Tasks 3,4) rather than fuzzy role matching.
- **Out of scope (per spec):** WYSIWYG on the rendered paper, granular rows for Projects, per-job edit persistence across reopen, set-cover re-run after write-once.
