# MicrodyneHunter v2 — AI Sales Agent for Microdyne Engineering

AI-powered sales agent that finds top-quality manufacturer leads, emails them (max 10/day), submits their website contact forms (max 100/day), and only extracts new leads once every existing lead has been contacted.

## Architecture

```
main.py (orchestrator)
├── modules/scraper.py      → Apollo, Google Search, Google Maps, directories
├── modules/qualifier.py    → AI lead scoring with Claude
├── modules/database.py     → Supabase: dedup, CRUD, tracking
├── modules/emailer.py      → Instantly.dev: personalized emails + follow-ups
├── modules/form_filler.py  → Playwright: website contact form automation
├── modules/intelligence.py → Weekly reports + auto-exclusion engine
├── modules/notifier.py     → Hot lead alerts + daily summaries
├── config.py               → All settings in one place
└── sql/001_schema.sql      → Supabase database schema
```

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Set up environment variables

```bash
cp .env.example .env
# Edit .env with your actual API keys
```

### 3. Set up Supabase database

1. Create a new Supabase project at [supabase.com](https://supabase.com)
2. Go to SQL Editor
3. Paste and run `sql/001_schema.sql`
4. Copy your project URL and service_role key to `.env`

### 4. Set up Instantly.dev

1. Sign up at [instantly.ai](https://instantly.ai)
2. Connect your sending domains (see Multi-Domain Setup below)
3. Enable warmup on all mailboxes
4. Copy your API key to `.env`

### 5. Run the agent

```bash
# Full daily pipeline
python main.py

# Individual steps
python main.py --scrape      # Scrape leads only
python main.py --email       # Send emails only
python main.py --forms       # Fill contact forms only
python main.py --followup    # Send follow-up sequences
python main.py --report      # Generate intelligence report
python main.py --stats       # View current stats

# Run on daily schedule (cron-style)
python main.py --schedule
```

## Multi-Domain Email Setup

**Never send cold emails from microdyneengineering.com** — use outreach domains:

| Domain          | Mailboxes | Emails/Day |
|---------------|-----------|------------|
| getmicrodyne.com | 2 | ~130 |
| microdyne.io | 2 | ~130 |
| microdyneseals.com | 2 | ~130 |
| microdynecnc.com | 2 | ~130 |
| trymicrodyne.com | 2 | ~130 |
| microdyneindia.com | 2 | ~130 |
| microdyneglobal.com | 2 | ~130 |
| hellomicrodyne.com | 2 | ~130 |
| **TOTAL** | **16** | **~1,040/day** |

Per domain setup:
1. Register domain (~$10/year)
2. Set up 2 email accounts
3. Add SPF: `v=spf1 include:_spf.hostinger.com include:_spf.instantly.ai ~all`
4. Enable DKIM in Hostinger
5. Add DMARC: `v=DMARC1; p=none; rua=mailto:dmarc@microdyneengineering.com; pct=100`
6. Connect to Instantly and enable warmup
7. Wait 2-3 weeks before sending cold emails

## Email Warmup Schedule

| Week | Warmup/Day | Cold/Day | Status |
|------|-----------|----------|--------|
| 1-2 | 5-10 | 0 | Warmup only |
| 3 | 15-20 | 0 | Warmup only |
| 4 | 25-30 | 10-15 | Start light cold |
| 5-6 | 30-40 | 25-35 | Ramp up |
| 7-8 | 40 | 50-65 | Full speed |
| 9+ | 40 | 60-80 | Cruise speed |

## Daily Workflow

The server runs one cycle automatically every day after 11:05 (local time), or on demand with **Start Agent**.

1. **QUEUE CHECK** — If any lead is still un-emailed or its contact form un-submitted, extraction is skipped and the cycle continues with those leads.
2. **EXTRACT** — Only when both queues are empty: search the preserved keyword set, and store up to `DAILY_LEAD_TARGET` (default 50) leads that pass the quality gate.
3. **EMAIL** — Send to the oldest un-emailed leads. **Hard cap: 10 per day.**
4. **FORMS** — Submit contact forms on un-submitted leads. **Hard cap: 100 successful submissions per day.**
5. **SUMMARY** — Today's usage and what's still queued are shown on the dashboard's *Today's outreach* panel.

The caps are enforced inside the send functions themselves, so dashboard buttons, the CLI, and follow-ups all share the same daily limit. Daily limits reset at 11:00 local time. Environment variables can lower the caps (`DAILY_EMAIL_LIMIT`, `DAILY_FORM_LIMIT`) but never raise them. Set `AUTO_DAILY_RUN=false` to disable the automatic run.

### Lead quality gate

A lead is stored only if all of these hold:

- Its website loads and isn't a directory/marketplace (IndiaMART, JustDial, TradeIndia, …)
- It isn't another CNC job shop (competitor)
- The site shows real manufacturing signals
- It has a business email on its own domain (not gmail/yahoo, not careers@/noreply@) **or** a contact page
- It belongs to a target industry and scores at least `MIN_QUALIFY_SCORE` (default 60)

### Preserved keyword set

`BEST_SEARCH_KEYWORDS` in `config.py` is always searched and can't be removed from the admin panel. It targets **buyers** of turned parts and mechanical seals (pump, valve, hydraulic cylinder, gearbox, compressor, motor, agitator, machinery manufacturers) rather than other job shops. Extra keywords added in the admin panel are searched after it.

## Lead Scoring Logic

Leads are scored using a **dual-mode system**: AI-based scoring when an API key is available (OpenRouter/Gemini/Claude), or rule-based fallback otherwise.

### Rule-Based Scoring (Fallback)

| Factor | Points | Details |
|--------|--------|---------|
| **Base score** | 30 | Every lead starts here |
| **Pump/Valve, Process Equipment, Hydraulic/Pneumatic Mfr** | +25 | Heavy users of turned parts and seals |
| **Automotive Component, Machinery, Compressor/Blower Mfr** | +20 | Good potential buyers |
| **Electrical Equipment Mfr** | +15 | Motors, switchgear — turned components |
| **General Engineering** | +10 | Broad fit |
| **Business email on own domain** | +15 | +5 more for sales@/purchase@ inboxes |
| **Contact form** | +10 | Can be form-filled |
| **Decision-maker title** | +10 | Procurement, Purchase, Plant Manager |
| **Priority country** | +10 | India, UAE, US, Germany, Saudi Arabia, Singapore |
| **Phone listed** | +5 | Additional contact channel |
| **Strong manufacturing signals** | +5 | Many industry terms on the website |

**Max possible score: 100** (capped)

### Score Thresholds

| Score Range | Action |
|-------------|--------|
| **< 60** | Not stored |
| **60-79** | Stored and contacted |
| **80+** | High priority |

### AI-Based Scoring (Primary)

When an AI provider is available, leads are evaluated 0-100 based on:
1. Does their business need CNC turning job work, turned components, or mechanical seals?
2. Are they a manufacturer likely to outsource turning work when their own shop floor is at capacity?
3. Is the contact person a decision-maker?
4. Company size and relevance to industrial manufacturing

The AI returns a score, reasoning, estimated company size, likely products, and priority level.

## Auto-Optimization Rules

The intelligence system automatically adjusts targeting:

- **Country with 0 closes after 200+ leads** → Paused
- **Lead type with <0.5% close rate after 500+ leads** → Deprioritized
- **Source with <1% reply rate after 1,000+ sends** → Volume reduced
- **Keyword exhausted (no new leads found)** → Marked complete, moves on
- **Email domain open rate <10%** → Rotated to new domain

All rules can be overridden manually. Weekly report summarizes all auto-actions.

## Monthly Cost

| Service | Cost |
|---------|------|
| Apollo.io (lead data) | $99/mo |
| Instantly.dev (email sending) | $30-97/mo |
| Supabase (database + dedup) | $25/mo |
| Claude API (AI scoring + emails) | ~$50/mo |
| Playwright cloud (form filling) | ~$30/mo |
| 8 sending domains | ~$7/mo |
| **Total** | **~$284/mo** |

## Optional: SerpAPI for Google scraping

For reliable Google Search and Maps scraping, add a SerpAPI key:

```bash
# In your .env
SERPAPI_KEY=your-serpapi-key
```

Without SerpAPI, the agent uses direct HTTP scraping which may get rate-limited.
