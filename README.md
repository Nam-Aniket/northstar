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
3. **Shows** you a clean local web app (warm or dark theme) — a ranked board, per-job
   detail with the matched requirements highlighted, an Insights page, and an optional
   people/outreach tracker — so you spend your time applying, not scrolling.

It's fully deterministic (no LLM, no ML) — the same inputs always give the same scores,
and every score is explainable.

---

## Quickstart

```bash
git clone <your-fork-url> northstar && cd northstar
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Tell Northstar who you are and what you want
cp skills.example.json skills.json      # the skills you HAVE vs the ones you LACK
cp config.example.json config.json      # target roles, location, recency, work-rights
# …then edit both files in your editor.

# 2. Build your ranked board
python 00_search_linkedin_guest.py      # find postings for your target roles
python fill_missing_jds.py --input job_alerts_raw.csv
python prepare_job_posts.py --input job_alerts_raw.csv --out job_posts.csv
python score_jobs.py                    # writes matched_jobs.csv (your Fit %)

# 3. Open the app
./run_app.sh                            # then open the printed URL and click "Sync"
```

### Configure two files

- **`skills.json`** — `supported_skills` (everything you can genuinely claim) and
  `unsupported_skills` (tools you don't have). These drive your Fit %. Each entry lists
  aliases so spelling/phrasing variants in a JD still match. Start from
  `skills.example.json` and edit — ~20 skills you have and ~10 you lack is plenty.
- **`config.json`** — your identity line, `target_keywords` (roles to search),
  `target_location`, `recency_tpr` (`r86400` = 24h), and matching options
  (`needs_sponsorship`, `seniority_cap`, `keep_threshold`).

## How scoring works

For each job, Northstar finds which of the posting's requirements you can evidence
(from `supported_skills`), which you can't (from `unsupported_skills`), and which it
couldn't classify. `Fit % = covered ÷ total`. Requirements in the JD's "must-have"
section count double. Hard gates apply as multiplicative caps (e.g. a role demanding
citizenship you don't hold), never as floors — so a posting with none of your skills
scores low, not high. A "~ uncertain" flag appears when a JD has too few recognised
requirements to score confidently.

## Project layout

```
00_search_linkedin_guest.py   search live postings for your roles
fill_missing_jds.py           fetch full job-description text
prepare_job_posts.py          dedupe + authenticity filter
score_jobs.py                 the Fit % scorer (reads skills.json/config.json)
config.py                     loads skills.json + config.json
generate_accepted_resumes.py  tailored-résumé engine (OFF by default — see below)
app/                          the local FastAPI + HTMX web app
docs/                         deeper guides
```

## Résumé generation (v2)

A tailored-résumé generator is included but **off by default** (`generation_enabled:
false`). It needs truthful, hand-authored bullets to select from, so the general-purpose
version ships matching-first; a generic résumé helper is planned for v2.

## License

MIT — see `LICENSE`.
