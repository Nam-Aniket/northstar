# Job Outreach Runbook

Pipeline lives in the project root. Six stages, four
new scripts, one SQLite DB.

```
[1a] job_posting_scraper.py     -->  active_hirers.csv         (postings <= 14d old)
              |
              v
[1b] validate_active_hirers.py  -->  active_hirers_companies.csv  (one row per company, validated)
              |
              v
[2]  job_outreach_pipeline.py   -->  job_outreach_people.csv      (people + emails)
              |
              v
[3]  personalization_scraper.py -->  + 1 personalization signal/person
              |
              v
[4]  draft_emails.py            -->  drafts/pending_review__*.md
              |
              v
     (YOU REVIEW + EDIT each draft, rename to ready_to_send__*.md)
              |
              v
[5]  send_outreach.py           -->  Gmail send + outreach.db
              |
              v
[6]  followup.py (daily)        -->  follow-up drafts + reply check
```

---

## One-time setup

### A. Python dependencies
```bash
pip install curl_cffi pypdf                                  # crawler extras
pip install google-auth google-auth-oauthlib google-api-python-client  # Gmail
```
DeepSeek/OpenAI use a stdlib HTTP call — no SDK install needed.

### B. LLM API key
DeepSeek (recommended — ~$0.0002 per email):
1. Sign up at https://platform.deepseek.com
2. Generate API key
3. Add to your shell rc:
   ```bash
   export LLM_API_KEY="sk-..."
   export LLM_BASE_URL="https://api.deepseek.com/v1"
   export LLM_MODEL="deepseek-chat"
   ```

Alternatives (just change env vars):
- OpenAI: `LLM_BASE_URL=https://api.openai.com/v1`, `LLM_MODEL=gpt-4o-mini`
- Local Ollama: `LLM_BASE_URL=http://localhost:11434/v1`, `LLM_MODEL=llama3.1:8b`, `LLM_API_KEY=ollama`

### C. Sender identity (for drafts)
```bash
export SENDER_NAME="Your Name"
export SENDER_HEADLINE="Analyst | open to new roles."
export SENDER_LINKEDIN="https://www.linkedin.com/in/your-handle/"
export SENDER_EMAIL="you@gmail.com"
```

### D. Gmail OAuth (one-time, ~10 min)
1. Go to https://console.cloud.google.com → New Project ("Job Outreach")
2. APIs & Services → Library → enable **Gmail API**
3. OAuth consent screen → External → fill required fields → add your Gmail as **Test user**
4. Credentials → Create credentials → OAuth client ID → **Desktop app**
5. Download JSON → save as `gmail_oauth_client.json` in `JOB/CRAW/`
6. First run of `send_outreach.py` opens a browser for consent. Token cached in `gmail_oauth_token.json`.

---

## Daily workflow (the actual day-to-day)

### Step 0 — Find companies that are actually hiring right now (weekly / bi-weekly)

This replaces "pick 24 companies by guesswork" with "pick from companies with verifiably fresh postings."

```bash
# 0a. Scrape Seek + Indeed for fresh Data/BI postings
python3 job_posting_scraper.py \
  --queries "data analyst" "data scientist" "data engineer" "bi analyst" "power bi" "analytics engineer" \
  --location Melbourne \
  --max-age-days 14 \
  --max-pages 3 \
  --output active_hirers.csv

# 0b. Validate freshness, drop dead URLs, dedup, flag recruiters
python3 validate_active_hirers.py \
  --input active_hirers.csv \
  --output-prefix active_hirers \
  --max-age-days 14 \
  --flag-recruiters
```

Outputs:
- `active_hirers.csv` — raw postings (one row per posting)
- `active_hirers_validated.csv` — confirmed-live postings within freshness window
- `active_hirers_companies.csv` — **one row per unique company, freshest first** — this is what you feed to the pipeline
- `active_hirers_audit.json` — QA stats (recruiter %, age distribution, fixed dates, dropped reasons)

**Eyeball check after every run.** `validate_active_hirers.py` prints 5 random validated postings to stdout — open the URLs and confirm:
- Page still loads
- The role is what was advertised
- The "posted X days ago" matches reality (or the JSON-LD date is plausible)

If 2+ of 5 fail, the scraper markup may have drifted — re-run and check Seek/Indeed for layout changes.

### Step 1 — Use the validated companies as your outreach pool
```bash
python3 job_outreach_pipeline.py \
  --input active_hirers_companies.csv \
  --output-prefix job_outreach \
  --config job_outreach_config.json \
  --name-col company_name \
  --workers 4 --resume --identify \
  --contact-url "$SENDER_LINKEDIN"
```
Produces `job_outreach_people.csv` (people + verified emails for each fresh-hirer company).

You can still merge your hand-curated CSV in — concatenate the two CSVs before feeding to `job_outreach_pipeline.py` if you want both sources.

### Step 1 — Add personalization signals (run on the day's batch)
```bash
python3 personalization_scraper.py \
  --input job_outreach_people.csv \
  --output job_outreach_people_personalised.csv \
  --limit 30
```
Adds `personalization_signal` + `signal_source` columns. ~3–5 min for 30 people.

### Step 2 — Draft personalised emails
```bash
python3 draft_emails.py \
  --input job_outreach_people_personalised.csv \
  --drafts-dir drafts \
  --skip-no-email \
  --limit 30
```
Writes ~30 files: `drafts/pending_review__<company>__<person>.md`. Cost on
DeepSeek: ~$0.01 total. On OpenAI gpt-4o-mini: ~$0.05.

### Step 3 — **You** review every draft

For each `pending_review__*.md`:
1. Open the file
2. Read the body. Check:
   - First sentence references the right specific thing
   - No hallucinated facts about the person/company
   - Subject line in the frontmatter makes sense
   - Email address in the frontmatter is correct
3. Edit anything off
4. When happy: **rename the file** from `pending_review__...` to `ready_to_send__...`
5. If the draft is bad/wrong target/no signal worth sending: rename to `skipped__...` or delete

Bulk rename one-by-one in a file manager, or:
```bash
cd drafts && mv pending_review__example_co__jordan-rivera.md ready_to_send__example_co__jordan-rivera.md
```

This is the **non-negotiable** human gate. Never skip it.

### Step 4 — Send the approved batch
```bash
python3 send_outreach.py \
  --drafts-dir drafts \
  --max-per-run 20 \
  --min-gap 90 --max-gap 180 \
  --bh-start 9 --bh-end 17
```
- Sends one at a time, 90–180s random gap between sends
- Only sends 9 AM – 5 PM local weekdays (use `--ignore-business-hours` to override)
- Stops if it hits `--max-per-run` or runs out of `ready_to_send__` files
- Renames sent files to `sent__...`
- Logs to `outreach.db`

For 20 emails this takes 30–60 min of wall-clock time. Start it, leave it alone.

### Step 5 — Follow up + reply check (daily, e.g. 8am cron)
```bash
python3 followup.py --check-replies --max-drafts 20
```
- Polls Gmail for any replies on open threads → marks `replied_at`, drops them from the follow-up queue
- Drafts a short follow-up #1 for anyone unreplied 4+ days since send
- Drafts an even shorter follow-up #2 for anyone unreplied 6 days after follow-up #1 (10 days total)
- Output: `drafts/pending_review__followup1__*.md` — review + send the same way

---

## Realistic daily volume

| Plan | Pros | Cons |
|---|---|---|
| **20/day, heavy review** ✅ recommended | 5–10% reply rate, no deliverability risk, sustainable | 1–1.5 hrs/day total time |
| 30/day, medium review | ~5% reply, fits Gmail comfortably | 2 hrs/day total |
| 50/day | Bigger funnel | Quality drops, reply rate halves, Gmail starts watching |
| 100/day | Theoretically possible | Spam folder, account flags, ~1% reply rate. **Not worth it.** |

Math: at 20/day × 5% reply = 1 real conversation/day. That's what you actually need.

---

## Quick reference

| Want to... | Run |
|---|---|
| Find new companies' people | `job_outreach_pipeline.py` |
| Add signal to a CSV | `personalization_scraper.py` |
| Draft today's batch | `draft_emails.py` |
| Send today's batch | `send_outreach.py` |
| Check replies + draft follow-ups | `followup.py --check-replies` |
| See sent + reply stats | `sqlite3 outreach.db 'SELECT status, COUNT(*) FROM outreach GROUP BY status'` |
| Find who hasn't replied | `sqlite3 outreach.db "SELECT person_name, company, sent_at FROM outreach WHERE status='sent' AND replied_at IS NULL"` |

---

## Stop conditions (set these in advance)

- **They reply** → followup.py auto-detects, won't follow up
- **They ask to stop** → manually: `sqlite3 outreach.db "UPDATE outreach SET status='unsubscribed' WHERE email='x@y.com'"`. Never email them again.
- **Bounce** → manually mark `status='bounced'` after seeing the bounce email
- **No reply after 2 follow-ups** → leave it. Don't chase a third time.

---

## Failure modes to watch for

| Symptom | Cause | Fix |
|---|---|---|
| Gmail sends fail with `403 insufficient permissions` | Wrong OAuth scope | Delete `gmail_oauth_token.json`, re-auth |
| All drafts have hallucinated facts | Personalization signal was generic boilerplate | Tighten `_is_real_signal` filters in personalization_scraper.py, or skip drafts for low-confidence signals |
| DDG starts returning empty results | You hit their rate limit | Wait 1 hour. Don't increase `--workers` for the personalization scraper above 1. |
| Verified-email rate is low | M365 mail servers refusing SMTP RCPT | Expected. Use the guessed email as a starting point, validate the highest-priority ones manually. |
| Reply check misses obvious replies | Sender email mismatch | Set `SENDER_EMAIL` env var to your exact Gmail address |

---

## Legal / ethical guardrails

For 1:1 job outreach to people in their professional capacity, at ≤30/day,
with real personalization and an honest signature — this falls comfortably
under legitimate interest (GDPR, Spam Act AU). To stay there:

- Always identify yourself honestly (real name, real LinkedIn)
- Never use a misleading subject line
- Stop on first request — even an implicit one ("not the right time", "please don't")
- Don't reuse the same list for non-job-search purposes
- Don't sell or share the email list

If you scale past 50/day, get a dedicated domain + Workspace mailbox and
revisit this checklist.
