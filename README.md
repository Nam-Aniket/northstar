# Northstar

**A local, private, open-source job-matching engine.** Tell it your target roles and
your skills, and it pulls live job postings and ranks them by how well each one fits
**you** — a deterministic **Fit %** based on the share of a posting's requirements your
skills actually cover. No account, no cloud, no subscription. Your résumé and data
never leave your machine.

> ### ⚠️ Local & self-hosted — read this first
> Northstar runs entirely on your own computer. **You are the operator**, and you are
> responsible for complying with the terms of service of any site it fetches from
> (including LinkedIn). It is intended for personal job-seeking use at human volume.
> The software is provided **as-is, with no warranty** (see `LICENSE`).

---

## What it does

1. **Searches** live job postings for your target roles + location.
2. **Scores** every posting with a transparent, deterministic **Fit %** =
   `requirements you can evidence ÷ all requirements in the posting`. The *ranking* is
   the product; the strong/fair bands are advisory.
3. **Shows** you a polished local web app — a Ramos-inspired UI in light or dark —
   with everything you need to run a job hunt:
   - **Board** — your postings ranked by Fit %, with day-by-day navigation and a score
     gauge on each card.
   - **Job detail** — the full JD with your matched requirements highlighted, the score
     breakdown, and one-click *Open* / *Mark applied* (kept separate, so opening a link
     never silently marks it applied).
   - **Insights** — score distribution, sectors, and an application funnel.
   - **People** — a lightweight outreach tracker; add companies and contacts by hand.
   - **Tracker** — an Excel-style table of every application with editable status.
   - **Builder** — a résumé builder with a live preview and one-click `.docx` export
     (with optional AI assistance).

The scorer is fully deterministic (no LLM, no ML) — the same inputs always give the
same scores, and every score is explainable. The optional AI résumé helper is the only
part that calls a model, and it is strictly opt-in.

---

## Quickstart

```bash
git clone <your-fork-url> northstar && cd northstar
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Tell Northstar who you are and what you want
python build_profile.py --resume path/to/your_resume.docx   # generates skills.json
# …then open skills.json and review the supported_skills list
cp config.example.json config.json      # target roles, location, recency, work-rights
# …then edit config.json in your editor

# 2. Open the app
./run_app.sh                            # macOS / Linux  (Windows: run_app.bat)
```

> **Windows:** use `run_app.bat` instead of `run_app.sh`. Everything else is the same —
> the pipeline is pure Python. For the optional daily auto-run, use **Task Scheduler**
> (command shown in [Run it daily](#run-it-daily-frictionlessly)) instead of launchd.

Then, in the app, click **Run** — Northstar fetches postings, scores them, and refreshes
the board for you. That's the whole loop. (Prefer the terminal? `python daily_run.py`
does the same thing in one command.)

### Run it daily, frictionlessly

Everything — search, JD fetch, dedupe, scoring, and the dashboard rebuild — is behind a
single command and a single button:

- **In the app:** the **Run** button runs the full pipeline in the background with a live
  progress bar, then refreshes the board. No terminal needed.
- **One command:** `python daily_run.py` (the Run button calls exactly this).
- **Automatic, once a day (opt-in):** point your scheduler at the same command.
  - **macOS (launchd):** edit the two `__PLACEHOLDERS__` in
    `scripts/com.northstar.dailyrun.plist.template`, copy it to
    `~/Library/LaunchAgents/com.northstar.dailyrun.plist`, then
    `launchctl load ~/Library/LaunchAgents/com.northstar.dailyrun.plist`.
  - **Linux / macOS (cron):** `30 8 * * * cd <ROOT> && <venv>/bin/python daily_run.py >> app/daily_run.log 2>&1`
  - **Windows (Task Scheduler):** `schtasks /Create /SC DAILY /ST 08:30 /TN NorthstarDaily /TR "<python> <ROOT>\daily_run.py"`

Your application state — what you've applied to, starred, or noted, and any contacts you
added by hand — is kept in a separate database zone and **survives every run**.

### Configure two files

- **`skills.json`** — `supported_skills` (everything you can genuinely claim) and
  `unsupported_skills` (tools you don't have). These drive your Fit %. Each entry lists
  aliases so spelling/phrasing variants in a JD still match. **Quickest way:** run the
  profile generator (below) instead of hand-editing.

- **`config.json`** — your identity line, `target_keywords` (roles to search),
  `target_location`, `recency_tpr` (`r86400` = 24h), and matching options
  (`needs_sponsorship`, `seniority_cap`, `keep_threshold`).

### Building your skill profile

Instead of hand-authoring `skills.json`, generate it from your résumé:

```bash
python build_profile.py --resume path/to/your_resume.docx
```

Then review `skills.json` — add any skills the matcher missed and remove false positives.
`unsupported_skills` lists everything in the taxonomy **not** found on your résumé; these
are tracked as gaps and lower your Fit % when a JD requires them.

**Supported input formats:** `.docx` (recommended), `.md` / `.txt`, and `.pdf`
(requires `pip install pypdf`; otherwise pass `--text "..."` or pipe text on stdin).

**Optional LLM pass** (catches skills the deterministic matcher may miss):

```bash
python build_profile.py --resume resume.docx --llm
```

Requires `LLM_API_KEY` in `.env`. If the key is missing or the call fails, it falls back
to the deterministic result. The matcher alone covers ~150–250 labelled skills across
Programming, Data & BI, Data Engineering, Data Science, Business Delivery, Soft Skills,
and Finance & Commercial. Extend `taxonomy.json` to add more.

---

## The Résumé Builder

The **Builder** page is a from-scratch résumé editor: fill in your details on the left,
watch an ATS-safe single-column résumé render live on the right, and download a clean
`.docx` in one click. Everything is your own content — nothing is invented.

**Optional AI assistance** (needs `LLM_API_KEY` in `.env`): the **✨ AI write** buttons
turn rough notes into polished, professional copy:

- **Summary** — a 2–3 sentence professional summary tailored to your target role.
- **Bullets** — your rough notes ("automated the monthly report with Python") become
  strong XYZ-formula bullet points.

The AI helper has a hard anti-fabrication guard: it produces **one bullet per note**,
preserves order, and **never invents or moves a number** between bullets. If you don't
set an API key, the Builder still works fully — you just write the copy yourself.

## People & outreach

The **People** page groups your contacts by company. You can:

- **Add a company with its first contacts** from the People page.
- **Add a person** to any company from that company's page.
- Track per-person outreach status (not contacted → contacted → replied → …).

Hand-added companies and contacts are stored in a database zone that **survives every
pipeline run**, so a daily refresh never wipes them.

## How scoring works

For each job, Northstar finds which of the posting's requirements you can evidence (from
`supported_skills`), which you can't (from `unsupported_skills`), and which it couldn't
classify. `Fit % ≈ covered ÷ (covered + lacked + unclassified)`. Requirements in the
JD's "must-have" section count double. Hard gates (e.g. a role demanding citizenship you
don't hold) apply as multiplicative caps, never as floors — so a posting with none of
your skills scores low, not high.

To avoid over-confidence on thin postings, the denominator is **Laplace-smoothed**: a JD
that lists only a couple of requirements you happen to match won't score a misleading
100% — there simply isn't enough evidence. Evidence-rich postings are essentially
unaffected. A "~ uncertain" flag also appears when a JD has too few recognised
requirements to score confidently.

## Project layout

```
00_search_linkedin_guest.py   search live postings for your roles
fill_missing_jds.py           fetch full job-description text
prepare_job_posts.py          dedupe + authenticity filter (prefers the JD-enriched file)
score_jobs.py                 the Fit % scorer (reads skills.json/config.json)
config.py                     loads skills.json + config.json
build_profile.py              generate skills.json from your résumé
daily_run.py                  one-shot full refresh (search → score → dashboard)
run_status.py                 progress/lock for the in-app Run button
generate_accepted_resumes.py  tailored-résumé engine (OFF by default — see below)
resume_docx.py                ATS-safe .docx writer used by the Builder
app/                          the local FastAPI + HTMX web app
scripts/                      opt-in daily-run scheduler templates
docs/                         deeper guides
```

## Résumé generation (v2)

A *tailored*-résumé generator (one résumé per matched job) is included but **off by
default** (`generation_enabled: false`). It needs truthful, hand-authored bullets to
select from, so the general-purpose version ships matching-first. For now, use the
**Builder** page to produce résumés; a generic per-job generator is planned for v2.

## License

MIT — see `LICENSE`.
