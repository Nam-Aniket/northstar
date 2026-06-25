# Agent-driven outreach message engine (Epic B, v1) — design

**Date:** 2026-06-26
**Status:** Approved (brainstorm), pending spec review
**Scope:** First slice of the outreach message engine. Researched, personalized email **drafts** for priority B2B prospects, produced by a coding agent (Claude Code) and reviewed/approved inside Northstar. **Sending is Epic C — out of scope.**

## Core principle

**Northstar makes zero network or LLM calls.** The app must keep working smoothly and uninterrupted, so all research + drafting happens **out-of-band in a coding agent** (Claude Code preferred), which already has web search, fetch, and strong reasoning. Northstar only: holds the prospect **queue**, stores **drafts**, serves the **review/approve UI**, and exposes one **helper module** the agent calls. No DeepSeek/Tavily keys, no in-app crawler, nothing that can hang a request.

## Division of labor

- **Northstar (the app):** queue + draft storage + review UI + `outreach_agent.py` helper. Fast local SQLite reads/writes only.
- **The coding agent (Claude Code):** reads the queue, researches each company (its own WebSearch/WebFetch), drafts the email using the user's stored pitch + sales frameworks, writes drafts back via the helper.

## Components

### 1. Data model — new tables only (avoids the no-migration landmine)

```sql
CREATE TABLE IF NOT EXISTS biz_drafts (
    prospect_key     TEXT PRIMARY KEY,   -- one draft per prospect (v1)
    company_key      TEXT,
    subject          TEXT,
    body             TEXT,
    research_summary TEXT DEFAULT '',
    sources          TEXT DEFAULT '',    -- newline-separated URLs the agent used
    status           TEXT DEFAULT 'draft', -- draft | approved
    model            TEXT DEFAULT '',     -- which agent/model produced it (provenance)
    created_at       TEXT,
    updated_at       TEXT
);

CREATE TABLE IF NOT EXISTS biz_settings (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    pitch            TEXT DEFAULT '',     -- the user's offer / value-prop
    lead_magnet_path TEXT DEFAULT '',     -- for Epic C (attach); stored now, unused yet
    updated_at       TEXT
);
```

No columns added to the existing `biz_companies` / `biz_prospects` tables.

### 2. The helper — `outreach_agent.py` (new, top-level, importable)

The only interface between the agent and Northstar. Pure local SQLite; no network.

```python
load_context() -> dict
    # {pitch, lead_magnet_path, sender_name, sender_headline, sender_linkedin}
    # pitch/lead_magnet from biz_settings; sender_* from .env (SENDER_NAME/HEADLINE/LINKEDIN).

load_queue(limit: int | None = None) -> list[dict]
    # Priority prospects that still need a draft.
    # Queue = prospects whose company.priority = 1 AND that have NO row in biz_drafts.
    # Each: {prospect_key, name, title, email, company_key, company_name, website, domain, stage}

save_draft(prospect_key, subject, body, research_summary="", sources="", model="") -> dict
    # Upsert into biz_drafts (status='draft'); validates the prospect exists. Returns the saved row.

# Convenience (for tests + the review UI):
get_draft(prospect_key) -> dict | None
list_drafts(status: str | None = None) -> list[dict]
```

### 3. The agent runbook + Claude Code skill

A short runbook (`docs/outreach-agent-runbook.md`) documents the loop, and a `/outreach-draft` Claude Code skill makes it one command (preferred entry point):

```
from outreach_agent import load_context, load_queue, save_draft
ctx = load_context()
for p in load_queue():
    # research p["website"] + one web search (agent's own tools)
    # draft subject+body using ctx["pitch"] + sales frameworks + the real, cited hooks
    save_draft(p["prospect_key"], subject, body, research_summary, sources, model="claude-code")
```

The skill instructs the agent to: ground every claim in fetched facts (no fabrication), keep it short and persuasion-tuned (the user's frameworks), cite sources, and write one draft per queued prospect. Then the user reviews in Northstar.

### 4. Review UI (Business mode)

- Each prospect row gains a **draft badge**: none / `draft` / `approved`, and a **"Review draft"** button when a draft exists.
- Clicking opens a **review drawer**: read-only research summary + source links, an **editable subject** and **body**, and **Save** / **Mark approved**.
- A **Business settings** panel (drawer or section) to set your **pitch** + lead-magnet path (writes `biz_settings`).

### 5. Routes (`app/app.py`)

- `GET  /business/prospect/{key}/draft` → review drawer partial (from `biz_drafts`).
- `POST /business/prospect/{key}/draft` → save edited subject/body.
- `POST /business/prospect/{key}/draft/approve` → set `status='approved'`.
- `GET/POST /business/settings` → view/save pitch + lead-magnet path.

## Data flow

```
Northstar: flag company priority ──► load_queue() (helper)
                                          │
Claude Code agent: research website+search ─► draft (pitch+frameworks+hooks) ─► save_draft()
                                          │
Northstar review drawer ◄── biz_drafts ──┘   edit ► Save ► Mark approved  (Epic C: send)
```

The app never waits on the agent; drafts simply appear in the review UI once the agent writes them.

## Error handling

- Agent can't research a company (thin site / fetch fails) → it still `save_draft`s a lighter version; never blocks the queue.
- `save_draft` for an unknown prospect → raises ValueError (the agent skips it).
- No pitch set yet → `load_context` returns empty pitch; the settings panel prompts the user to add one; the skill warns before drafting.
- Northstar itself has no failure modes from this feature (no network/LLM calls).

## Testing plan

All deterministic, temp-DB, no live network (the agent's research is external and not unit-tested here):
- `outreach_agent.load_context` — merges `biz_settings` pitch + `.env` sender fields.
- `outreach_agent.load_queue` — returns only priority-company prospects, excludes already-drafted ones, includes website/domain.
- `outreach_agent.save_draft` — upserts; second call updates; rejects unknown prospect; `get_draft`/`list_drafts` round-trip.
- `biz_settings` save/read; review routes save + approve (smoke via uvicorn on a spare port, per the project's no-httpx convention).
- Mode isolation still holds (drafts never surface in the job tracker).
- Full suite stays green.

## Out of scope (later)

- **Epic C — sending:** wire the existing Gmail send (`05_send_outreach.py`) to push `approved` drafts, with lead-magnet attachment (`biz_settings.lead_magnet_path`) and reply/bounce tracking.
- **Bulk-template tier** for the long tail (non-researched, merge-field drafts).
- In-app LLM/search drafting — explicitly rejected; the agent does research/drafting.
