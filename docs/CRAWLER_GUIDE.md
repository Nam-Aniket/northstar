# Crawler & Outreach Pipeline — Complete Guide

Everything in the project root: what each piece does,
how to run it, what works, what's currently blocked, and how to work around it.

---

## 1. The big picture

This is a **free, local, self-hosted** stack for two jobs:

1. **Web crawling / company enrichment** — find a company's website, contact
   details, leadership, and structured data. (Originally built for the WGEA
   gender-balance sector work.)
2. **Job-search outreach** — find people at target companies, get their emails,
   draft personalised emails, send them, and follow up.

Core principle: be powerful but polite. The crawler disguises itself like a real
browser, but respects rate limits and robots.txt, and caches everything so
re-runs are nearly free.

---

## 2. The files

### Crawler core
| File | Role |
|---|---|
| `local_apify_crawler.py` | Base crawler. Domain discovery, page crawling, email/phone extraction, evidence logging. Pure stdlib. |
| `superpowered_crawler_final.py` | The main engine. Subclasses the base and adds: browser disguise, per-host rate limiting, robots.txt, disk cache, PDF parsing, JSON-LD/OpenGraph extraction, Playwright fallback, multi-pattern email verification. **Use this one.** |
| `superpowered_crawler.py` | Older version. Superseded by `_final`. Kept for reference. |

### Job-outreach pipeline (each stage is a separate script)
| File | Stage | Status |
|---|---|---|
| `job_posting_scraper.py` | [0a] Find companies hiring now (Seek + Indeed) | works if job boards reachable |
| `validate_active_hirers.py` | [0b] Validate freshness, dedupe, flag recruiters | works |
| `job_outreach_pipeline.py` | [2] Find people + emails per company | **x-ray blocked on this network** (see §7) |
| `seed_from_reachout.py` | Convert your reachout CSV → people CSV (no scraping) | ✅ works |
| `personalization_scraper.py` | [3] Add 1 personalization signal per person | **search-engine blocked** (see §7) |
| `draft_emails.py` | [4] Draft a personalised email per person via LLM | ✅ works (needs API key) |
| `send_outreach.py` | [5] Send approved drafts via Gmail, throttled | works (needs Gmail OAuth) |
| `followup.py` | [6] Reply detection + follow-up drafts | works (needs Gmail OAuth) |

### Config & helpers
| File | Role |
|---|---|
| `job_outreach_config.json` | Role taxonomy (data / talent / hiring-leadership) + custom regex rules |
| `dotenv_loader.py` | Stdlib `.env` loader (no pip dependency) |
| `.env` | Your secrets (API keys, sender identity). **Never commit.** |
| `.env.example` | Template to copy to `.env` |
| `.gitignore` | Excludes secrets, cache, DB, drafts |
| `outreach_runbook.md` | Operational day-to-day runbook |
| `CRAWLER_GUIDE.md` | This file |

---

## 3. The crawler engine (`superpowered_crawler_final.py`)

### What makes it "superpowered"

| Feature | How | Flag |
|---|---|---|
| Browser disguise | `curl_cffi` impersonates real Chrome/Safari TLS+HTTP2 fingerprint; falls back to urllib | auto (install `curl_cffi`) |
| Per-host rate limit | Token bucket per root domain; never exceeds N req/sec/host regardless of threads | `--rps` (default 1.0) |
| robots.txt respect | Fetched + cached per host; disallowed URLs return status 999 | `--ignore-robots` to disable |
| Disk cache | Pages cached by URL hash; re-runs skip the network | `--no-cache`, `--cache-dir`, `--cache-ttl` |
| PDF parsing | `application/pdf` responses parsed via `pypdf` into text | auto (install `pypdf`) |
| JSON-LD / OpenGraph | Structured data parsed before regex (Organization, Person, JobPosting) | always on |
| Playwright fallback | JS-rendered/blocked pages rendered in headless Chromium | auto if `playwright` installed |
| Honest mode | Sends a contactable User-Agent (good for goodwill scraping) | `--identify --contact-url URL` |
| Multi-pattern email verify | Tries `first@`, `first.last@`, `flast@`, SMTP-pings each | always on |

### Search-engine UA handling
Search engines (DDG/Google/Bing/Brave) reject "honest" bot UAs and serve empty
pages. The crawler automatically uses a **rotating browser UA for search engines**
even when `--identify` is on — see `_pick_ua_for()`.

### Standalone use (just the crawler, no outreach)
```bash
python3 superpowered_crawler_final.py \
  --input companies.csv \
  --output-prefix out \
  --name-col company_name \
  --website-col website \
  --rps 1.0 --workers 4 --resume
```
Outputs: `out_deep_enriched.json`, `out_companies.csv`, `out_people.csv`, `out_checkpoint.json`.

---

## 4. One-time setup

### A. Install dependencies
```bash
pip install curl_cffi pypdf                                       # crawler extras (free)
pip install google-auth google-auth-oauthlib google-api-python-client  # Gmail send
# optional, for JS-heavy sites:
pip install playwright && playwright install chromium
```

### B. Create `.env`
```bash
cp .env.example .env
open -a TextEdit .env     # fill in real values
chmod 600 .env            # lock permissions
```
Required keys:
```
LLM_API_KEY=sk-...                       # DeepSeek (cheap) or OpenAI key
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
SENDER_NAME=Your Name
SENDER_HEADLINE=Analyst | open to new roles.
SENDER_LINKEDIN=https://www.linkedin.com/in/your-handle/
SENDER_EMAIL=you@gmail.com
```
Verify:
```bash
python3 -c "from dotenv_loader import load_env; load_env(); import os; print('key set:', bool(os.environ.get('LLM_API_KEY')))"
```

### C. Gmail OAuth (for sending — do later, when ready to send)
1. console.cloud.google.com → new project → enable **Gmail API**
2. OAuth consent screen → External → add your email as test user
3. Credentials → OAuth client ID → **Desktop app** → download JSON
4. Save as `gmail_oauth_client.json` in this folder
5. First run of `send_outreach.py` opens a browser to authorise; token cached in `gmail_oauth_token.json`

---

## 5. The full outreach workflow

```
[0a] job_posting_scraper.py     → active_hirers.csv          (companies hiring now)
[0b] validate_active_hirers.py  → active_hirers_companies.csv (validated, fresh)
        │
[2]  job_outreach_pipeline.py   → job_outreach_people.csv     (people + emails)   ⚠ x-ray blocked
        │  (or: seed_from_reachout.py → seed_people.csv  — no scraping needed)
        │
[3]  personalization_scraper.py → *_personalised.csv          (+ 1 signal/person) ⚠ search blocked
        │
[4]  draft_emails.py            → drafts/pending_review__*.md  ✅ works
        │
     YOU REVIEW + EDIT, rename pending_review__ → ready_to_send__
        │
[5]  send_outreach.py           → Gmail send + outreach.db     ✅ works
        │
[6]  followup.py (daily)        → reply check + follow-up drafts ✅ works
```

### Stage 0 — Find companies hiring now (weekly)
```bash
python3 job_posting_scraper.py \
  --queries "data analyst" "data scientist" "bi analyst" "power bi" \
  --location Melbourne --max-age-days 14 --max-pages 3 \
  --output active_hirers.csv

python3 validate_active_hirers.py \
  --input active_hirers.csv --output-prefix active_hirers \
  --max-age-days 14 --flag-recruiters
```
**Always eyeball the 5-sample printout** that `validate_active_hirers.py` prints —
open the URLs, confirm the posting is real and recent. Check `active_hirers_audit.json`:
keep `validated/input > 0.6` and `fixed_posting_dates/validated < 0.2`.

### Stage 2 — Get people (two paths)

**Path A (preferred when scraping works):**
```bash
python3 job_outreach_pipeline.py \
  --input active_hirers_companies.csv --name-col company_name \
  --output-prefix job_outreach --config job_outreach_config.json \
  --workers 2 --resume
```

**Path B (when x-ray is blocked — uses your existing reachout CSV):**
```bash
python3 seed_from_reachout.py \
  --input "data/reachout_data.csv" \
  --output seed_people.csv
```
This extracts people from the Person 1/2/3 LinkedIn URLs you already curated.
Verified: produces 58 people from 26 companies, no scraping.

### Stage 3 — Personalization (optional; currently search-blocked)
```bash
python3 personalization_scraper.py \
  --input seed_people.csv --output seed_people_personalised.csv --limit 30
```

### Stage 4 — Draft emails
```bash
# dry-run first (no API calls, shows what would happen):
python3 draft_emails.py --input seed_people.csv --limit 5 --dry-run

# real run:
python3 draft_emails.py --input seed_people.csv --limit 10
```
Drafts land in `drafts/pending_review__<company>__<person>.md`.

### Review (the human gate — never skip)
For each `pending_review__*.md`:
1. Read the body. Check the opener references the right thing, no hallucinated facts.
2. Confirm the `to_email` in the frontmatter is correct.
3. Edit anything off.
4. Rename `pending_review__...` → `ready_to_send__...` to approve.
5. Bad target → rename to `skipped__...` or delete.

### Stage 5 — Send
```bash
python3 send_outreach.py --drafts-dir drafts --max-per-run 20 \
  --min-gap 90 --max-gap 180 --bh-start 9 --bh-end 17
```
Sends only `ready_to_send__*.md`, one at a time, 90–180s apart, business hours
only. Logs to `outreach.db`, renames sent files to `sent__...`.

### Stage 6 — Follow up (daily)
```bash
python3 followup.py --check-replies --max-drafts 20
```
Marks anyone who replied, drafts follow-up #1 at day 4, #2 at day 10.

---

## 6. Getting emails (works without search engines)

Email verification uses **DNS + SMTP directly** — no search engine needed, so it
works even when x-ray is blocked.

How it works (in `guess_and_verify_emails_locally`):
1. Resolve company domain (from website discovery or manual override).
2. Guess 3 patterns per person: `first@`, `first.last@`, `flast@domain`.
3. SMTP-ping each pattern (`host -t MX` for the mail server, then `smtplib` RCPT check).
4. Catch-all servers fall back to a web-search existence check (Brave/Bing — this part needs search engines).

**Realistic hit rate:** 30–50%. Higher for `.com.au`/`.org.au` SMEs on Google
Workspace; lower for big SaaS on Microsoft 365 (which often refuses SMTP RCPT).

**To get emails for the seed people**, you need the company domain. Either let the
crawler discover it, or supply a manual `company → domain` map (most reliable for
a known list of 26 companies).

---

## 7. KNOWN ISSUE: search engines are blocked on this network

This is the main limitation discovered during setup. **Three of the people-finding
features depend on web search, and all three search backends fail here:**

| Engine | Symptom | Cause |
|---|---|---|
| DuckDuckGo | `status=000` (connection times out) | Blocked by your ISP |
| Brave Search | `HTTP 429 Too Many Requests` | Rate-limited after a few queries |
| Bing / Startpage | `status=200` but **0 results** | Anti-bot serves a stripped page to non-browser clients |

**What this breaks:**
- `job_outreach_pipeline.py` LinkedIn x-ray (finding the full team per company)
- `personalization_scraper.py` (finding a personal signal)
- Catch-all email fallback (minor)

**What still works (no search dependency):**
- `seed_from_reachout.py` — uses your existing curated URLs
- Email SMTP verification — direct DNS/SMTP
- `draft_emails.py`, `send_outreach.py`, `followup.py` — the whole back half
- Company website crawling (direct fetch, not search)

**Workarounds, best first:**

1. **Apollo.io free tier (recommended).** 1,200 records/month free, licensed data
   (no scraping), people search by company + role + verified emails. Export CSV,
   then run it through `personalization_scraper.py` (optional) and `draft_emails.py`.
   This bypasses the entire blocked stage.

2. **`curl_cffi` may bypass Bing's anti-bot** — its real-browser TLS fingerprint
   sometimes gets real results where urllib gets stripped pages. Install it and retry.

3. **Apify LinkedIn actor on a burner account** — ~$5 free credit, maintained
   scraper. Use a throwaway LinkedIn account, never your real one.

4. **Hand-curate** from LinkedIn manually into the `seed_people.csv` schema.

**Do NOT scrape LinkedIn directly with your real account** — ban risk is high and,
as a job seeker, losing your own LinkedIn account is far costlier than missing data.

---

## 8. Daily volume guidance

| Plan | Reply rate | Conversations/day | Notes |
|---|---|---|---|
| **20–30/day, heavy personalization** ✅ | 5–10% | 1–2 | Recommended. Sustainable on free Gmail. |
| 50/day | ~3% | ~1.5 | Quality drops, Gmail starts watching |
| 100/day | ~1% | ~1 | Not worth it; spam-folder + account-flag risk |

For sustained 50+/day you'd need a dedicated domain + Google Workspace ($6/mo) +
2–4 weeks of warmup. For a job hunt, 20–30/day of genuinely personalised emails
beats 100 templated ones.

---

## 9. Monitoring a running job

```bash
# Cache growth = work happening
echo "cache: $(ls .crawler_cache 2>/dev/null | wc -l | tr -d ' ') files"

# Checkpoint progress
[ -f job_outreach_checkpoint.json ] && \
  python3 -c "import json; print('done:', len(json.load(open('job_outreach_checkpoint.json'))), '/26')" || \
  echo "checkpoint: not written yet"

# Outreach DB stats
sqlite3 outreach.db 'SELECT status, COUNT(*) FROM outreach GROUP BY status' 2>/dev/null

# Who hasn't replied
sqlite3 outreach.db "SELECT person_name, company, sent_at FROM outreach WHERE status='sent' AND replied_at IS NULL" 2>/dev/null
```

`Ctrl+C` is always safe — the checkpoint preserves finished work; re-run with `--resume`.

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `curl_cffi not installed` hint | optional dep missing | `pip install curl_cffi` (or ignore — urllib fallback works) |
| `[xray] DDG/brave/bing empty` | search engines blocked (see §7) | Use Apollo / curl_cffi / burner Apify |
| `0 hits` for every company | search-engine block | §7 |
| `HTTP 429` from Brave | rate-limited | wait 15–60 min; lower `--rps`; fewer queries |
| `robots: disallowed` (status 999) | crawler respecting robots.txt | expected; that host forbids the path |
| DeepSeek `TimeoutError` | API slow / network | retry with longer timeout; check `curl https://api.deepseek.com/v1` |
| Verified-email rate low | M365 refusing SMTP RCPT | expected; use guessed pattern, verify top targets manually |
| Gmail `403 insufficient permissions` | wrong OAuth scope | delete `gmail_oauth_token.json`, re-auth |
| Process hangs, CPU 0% | blocked on a network read | `Ctrl+C`, re-run with `--resume`; lower `--workers` |
| Output not appearing | stdout buffered when piped | run with `python3 -u` |
| Commands find nothing | wrong directory | always run from the project root |

---

## 11. Current status snapshot (as of setup)

| Component | State |
|---|---|
| Crawler core (TLS, rate limit, robots, cache, PDF, JSON-LD) | ✅ working |
| Seed people extraction (`seed_from_reachout.py`) | ✅ 58 people from 26 companies |
| Drafter (`draft_emails.py`, dry-run) | ✅ working |
| DeepSeek API key | ✅ configured in `.env` (was timing out on test — retry) |
| Sender identity | ✅ configured |
| Email SMTP verification | ✅ works (no search needed) — needs company domains |
| LinkedIn x-ray (search-based) | ❌ blocked (see §7) |
| Personalization (search-based) | ❌ blocked (see §7) |
| Gmail OAuth | ⬜ not set up yet |
| `curl_cffi` / `pypdf` / Playwright | ⬜ not installed yet |

**Fastest path to sending real emails:** Apollo free tier for people+emails →
`draft_emails.py` → review → Gmail OAuth → `send_outreach.py`.

---

## 12. Quick command reference

```bash
# People from your existing curated list (no scraping)
python3 seed_from_reachout.py --input "data/reachout_data.csv" --output seed_people.csv

# Draft (dry-run, then real)
python3 draft_emails.py --input seed_people.csv --limit 5 --dry-run
python3 draft_emails.py --input seed_people.csv --limit 10

# Send (after Gmail OAuth + renaming drafts to ready_to_send__)
python3 send_outreach.py --max-per-run 20

# Follow up daily
python3 followup.py --check-replies

# Find fresh hirers
python3 job_posting_scraper.py --location Melbourne --max-age-days 14 --output active_hirers.csv
python3 validate_active_hirers.py --input active_hirers.csv --flag-recruiters
```
