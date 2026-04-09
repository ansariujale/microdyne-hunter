# FlowLockHunter v2 — AI Sales Agent for FlowLock Overseas

AI-powered sales agent that scrapes 1,000 leads/day, sends personalized cold emails, fills website contact forms, and auto-optimizes targeting based on what converts.

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

**Never send cold emails from flowlockoverseas.com** — use outreach domains:

| Domain          | Mailboxes | Emails/Day |
|---------------|-----------|------------|
| getflowlock.com | 2 | ~130 |
| flowlock.io | 2 | ~130 |
| flowlockseals.com | 2 | ~130 |
| flowlockcnc.com | 2 | ~130 |
| tryflowlock.com | 2 | ~130 |
| flowlockindia.com | 2 | ~130 |
| flowlockglobal.com | 2 | ~130 |
| helloflowlock.com | 2 | ~130 |
| **TOTAL** | **16** | **~1,040/day** |

Per domain setup:
1. Register domain (~$10/year)
2. Set up 2 email accounts
3. Add SPF: `v=spf1 include:_spf.hostinger.com include:_spf.instantly.ai ~all`
4. Enable DKIM in Hostinger
5. Add DMARC: `v=DMARC1; p=none; rua=mailto:dmarc@flowlockoverseas.com; pct=100`
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

1. **SCRAPE** — Find 1,000 new unique leads (Apollo, Google, Maps, directories)
2. **QUALIFY** — AI scores each lead 0-100, tags with metadata
3. **STORE** — Save to Supabase with 7-dimension dedup check
4. **EMAIL** — Send 1,000 personalized cold emails via Instantly
5. **FORM FILL** — Submit 1,000 website contact forms via Playwright
6. **FOLLOW UP** — 4-email sequence over 14 days for non-responders
7. **HAND OFF** — Hot leads notify Shohail instantly

## Lead Scoring Logic

Leads are scored using a **dual-mode system**: AI-based scoring when an API key is available (OpenRouter/Gemini/Claude), or rule-based fallback otherwise.

### Rule-Based Scoring (Fallback)

| Factor | Points | Details |
|--------|--------|---------|
| **Base score** | 30 | Every lead starts here |
| **Lead type: Chemical Plant** | +30 | Highest value target |
| **Lead type: Pharmaceutical / Oil & Gas** | +25 | Strong fit for mechanical seals |
| **Lead type: OEM Pump Manufacturer / Power Gen** | +20 | Good potential buyers |
| **Lead type: Water Treatment** | +15 | Industrial seal users |
| **Lead type: Food Processing** | +10 | Specialized seal requirements |
| **Has email** | +10 | Contactable lead |
| **Decision-maker title** | +10 | Plant Manager, Maintenance Head, Procurement Manager |
| **Priority country** | +10 | India, UAE, US, Germany, Saudi Arabia, Singapore |
| **Has website** | +5 | Can be form-filled |
| **Has phone** | +5 | Additional contact channel |

**Max possible score: 100** (capped)

### Score Thresholds

| Score Range | Action |
|-------------|--------|
| **< 20** | Auto-skipped |
| **20-39** | Low quality, skipped |
| **40-69** | Qualified for outreach |
| **70+** | High priority, fast-tracked |

### AI-Based Scoring (Primary)

When an AI provider is available, leads are evaluated 0-100 based on:
1. Does their business need mechanical seals or CNC precision components?
2. Are they in a country where FlowLock Overseas can supply competitively?
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
