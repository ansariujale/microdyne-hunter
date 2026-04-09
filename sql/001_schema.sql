-- ═══════════════════════════════════════════════════════════════
-- FlowLockHunter v2 — Supabase Database Schema
-- Run this in your Supabase SQL Editor to set up the database
-- Go to: https://supabase.com/dashboard → Your Project → SQL Editor → Paste & Run
-- ═══════════════════════════════════════════════════════════════

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ═══════ LEADS TABLE ═══════
CREATE TABLE IF NOT EXISTS leads (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  company_name TEXT NOT NULL,
  company_domain TEXT NOT NULL,
  website_url TEXT,
  contact_name TEXT,
  contact_email TEXT,
  contact_title TEXT,
  contact_phone TEXT,
  country TEXT NOT NULL DEFAULT '',
  city TEXT DEFAULT '',
  state TEXT DEFAULT '',
  lead_type TEXT DEFAULT 'other',
  source TEXT DEFAULT 'other',
  keyword_used TEXT,
  score INTEGER DEFAULT 0,
  score_reason TEXT,
  company_size TEXT,
  email_sent BOOLEAN DEFAULT FALSE,
  email_sent_at TIMESTAMPTZ,
  form_filled BOOLEAN DEFAULT FALSE,
  form_filled_at TIMESTAMPTZ,
  has_contact_form BOOLEAN DEFAULT FALSE,
  email_opened BOOLEAN DEFAULT FALSE,
  replied BOOLEAN DEFAULT FALSE,
  replied_at TIMESTAMPTZ,
  interested BOOLEAN DEFAULT FALSE,
  closed BOOLEAN DEFAULT FALSE,
  closed_at TIMESTAMPTZ,
  revenue_monthly NUMERIC DEFAULT 0,
  sequence_stage INTEGER DEFAULT 0,
  next_followup TIMESTAMPTZ,
  sending_domain TEXT,
  excluded BOOLEAN DEFAULT FALSE,
  exclude_reason TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  CONSTRAINT unique_domain UNIQUE (company_domain)
);

CREATE INDEX IF NOT EXISTS idx_leads_country ON leads(country);
CREATE INDEX IF NOT EXISTS idx_leads_type ON leads(lead_type);
CREATE INDEX IF NOT EXISTS idx_leads_source ON leads(source);
CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(score DESC);
CREATE INDEX IF NOT EXISTS idx_leads_email_sent ON leads(email_sent);
CREATE INDEX IF NOT EXISTS idx_leads_replied ON leads(replied);
CREATE INDEX IF NOT EXISTS idx_leads_closed ON leads(closed);
CREATE INDEX IF NOT EXISTS idx_leads_excluded ON leads(excluded);
CREATE INDEX IF NOT EXISTS idx_leads_state ON leads(state);
CREATE INDEX IF NOT EXISTS idx_leads_city ON leads(city);
CREATE INDEX IF NOT EXISTS idx_leads_followup ON leads(next_followup) WHERE next_followup IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at DESC);

-- ═══════ SOURCE TRACKER ═══════
CREATE TABLE IF NOT EXISTS source_tracker (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  source TEXT NOT NULL,
  keyword TEXT NOT NULL,
  country TEXT NOT NULL DEFAULT '',
  city TEXT DEFAULT '',
  lead_type TEXT,
  total_found INTEGER DEFAULT 0,
  last_batch_new INTEGER DEFAULT 0,
  status TEXT DEFAULT 'active',
  last_scraped TIMESTAMPTZ DEFAULT NOW(),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  CONSTRAINT unique_source_keyword_country UNIQUE (source, keyword, country)
);

CREATE INDEX IF NOT EXISTS idx_source_status ON source_tracker(status);

-- ═══════ SEGMENT PERFORMANCE ═══════
CREATE TABLE IF NOT EXISTS segment_performance (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  segment_type TEXT NOT NULL,
  segment_value TEXT NOT NULL,
  total_leads INTEGER DEFAULT 0,
  emailed INTEGER DEFAULT 0,
  replies INTEGER DEFAULT 0,
  interested INTEGER DEFAULT 0,
  closed INTEGER DEFAULT 0,
  revenue NUMERIC DEFAULT 0,
  reply_rate NUMERIC DEFAULT 0,
  close_rate NUMERIC DEFAULT 0,
  is_paused BOOLEAN DEFAULT FALSE,
  pause_reason TEXT,
  last_updated TIMESTAMPTZ DEFAULT NOW(),
  CONSTRAINT unique_segment UNIQUE (segment_type, segment_value)
);

CREATE INDEX IF NOT EXISTS idx_segment_type ON segment_performance(segment_type);
CREATE INDEX IF NOT EXISTS idx_segment_paused ON segment_performance(is_paused);

-- ═══════ OUTREACH LOG ═══════
CREATE TABLE IF NOT EXISTS outreach_log (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  lead_id UUID REFERENCES leads(id) ON DELETE CASCADE,
  channel TEXT NOT NULL,
  sequence_stage INTEGER DEFAULT 1,
  subject TEXT,
  sending_domain TEXT,
  form_url TEXT,
  form_submitted BOOLEAN DEFAULT FALSE,
  sent_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_outreach_lead ON outreach_log(lead_id);
CREATE INDEX IF NOT EXISTS idx_outreach_channel ON outreach_log(channel);
CREATE INDEX IF NOT EXISTS idx_outreach_sent ON outreach_log(sent_at DESC);

-- ═══════ INTELLIGENCE REPORTS ═══════
CREATE TABLE IF NOT EXISTS intelligence_reports (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  report_type TEXT NOT NULL,
  report_data JSONB,
  actions_taken JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ═══════ VIEWS ═══════
CREATE OR REPLACE VIEW daily_stats AS
SELECT
  DATE(created_at) AS day,
  COUNT(*) AS leads_added,
  COUNT(*) FILTER (WHERE email_sent) AS emailed,
  COUNT(*) FILTER (WHERE form_filled) AS forms_filled,
  COUNT(*) FILTER (WHERE replied) AS replies,
  COUNT(*) FILTER (WHERE closed) AS closed
FROM leads
GROUP BY DATE(created_at)
ORDER BY day DESC;

CREATE OR REPLACE VIEW followup_due AS
SELECT * FROM leads
WHERE replied = FALSE AND excluded = FALSE AND sequence_stage < 4 AND next_followup <= NOW()
ORDER BY score DESC;

CREATE OR REPLACE VIEW hot_leads AS
SELECT * FROM leads
WHERE replied = TRUE AND interested = TRUE AND closed = FALSE
ORDER BY replied_at DESC;

CREATE OR REPLACE VIEW country_summary AS
SELECT
  country,
  COUNT(*) AS total_leads,
  COUNT(*) FILTER (WHERE email_sent) AS emailed,
  COUNT(*) FILTER (WHERE replied) AS replies,
  COUNT(*) FILTER (WHERE closed) AS closed,
  SUM(revenue_monthly) AS total_revenue,
  ROUND(100.0 * COUNT(*) FILTER (WHERE replied) / NULLIF(COUNT(*) FILTER (WHERE email_sent), 0), 2) AS reply_rate,
  ROUND(100.0 * COUNT(*) FILTER (WHERE closed) / NULLIF(COUNT(*), 0), 2) AS close_rate
FROM leads
GROUP BY country
ORDER BY total_leads DESC;

-- ═══════ AUTO-UPDATED_AT TRIGGER ═══════
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS leads_updated_at ON leads;
CREATE TRIGGER leads_updated_at
  BEFORE UPDATE ON leads
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ═══════ RLS — Allow full access via API key ═══════
ALTER TABLE leads ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_tracker ENABLE ROW LEVEL SECURITY;
ALTER TABLE segment_performance ENABLE ROW LEVEL SECURITY;
ALTER TABLE outreach_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE intelligence_reports ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Full access" ON leads;
CREATE POLICY "Full access" ON leads FOR ALL USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "Full access" ON source_tracker;
CREATE POLICY "Full access" ON source_tracker FOR ALL USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "Full access" ON segment_performance;
CREATE POLICY "Full access" ON segment_performance FOR ALL USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "Full access" ON outreach_log;
CREATE POLICY "Full access" ON outreach_log FOR ALL USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "Full access" ON intelligence_reports;
CREATE POLICY "Full access" ON intelligence_reports FOR ALL USING (true) WITH CHECK (true);
