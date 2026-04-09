-- ═══════════════════════════════════════════════════════════════
-- FlowLockHunter v2 — Email Variants & Warmup Schema
-- Run this in Supabase SQL Editor AFTER 001_schema.sql
-- ═══════════════════════════════════════════════════════════════

-- ═══════ EMAIL VARIANTS TABLE ═══════
-- Stores all 5 AI-generated variants per lead per sequence stage
CREATE TABLE IF NOT EXISTS email_variants (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  lead_id UUID REFERENCES leads(id) ON DELETE CASCADE,
  sequence_stage INTEGER NOT NULL DEFAULT 1,
  variant_number INTEGER NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  angle TEXT,
  score_total NUMERIC DEFAULT 0,
  score_subject NUMERIC DEFAULT 0,
  score_personalization NUMERIC DEFAULT 0,
  score_cta NUMERIC DEFAULT 0,
  score_spam_risk NUMERIC DEFAULT 0,
  is_winner BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_variants_lead ON email_variants(lead_id);
CREATE INDEX IF NOT EXISTS idx_variants_winner ON email_variants(is_winner) WHERE is_winner = TRUE;
CREATE INDEX IF NOT EXISTS idx_variants_stage ON email_variants(sequence_stage);

-- ═══════ EMAIL WARMUP TABLE ═══════
-- Tracks daily send volume per domain for warmup ramp-up
CREATE TABLE IF NOT EXISTS email_warmup (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  domain TEXT NOT NULL,
  send_date DATE NOT NULL DEFAULT CURRENT_DATE,
  emails_sent INTEGER DEFAULT 0,
  daily_limit INTEGER DEFAULT 5,
  warmup_day INTEGER DEFAULT 1,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  CONSTRAINT unique_domain_date UNIQUE (domain, send_date)
);

CREATE INDEX IF NOT EXISTS idx_warmup_domain ON email_warmup(domain);
CREATE INDEX IF NOT EXISTS idx_warmup_date ON email_warmup(send_date DESC);

-- ═══════ ALTER OUTREACH_LOG ═══════
-- Add columns for full email record keeping
ALTER TABLE outreach_log ADD COLUMN IF NOT EXISTS body TEXT;
ALTER TABLE outreach_log ADD COLUMN IF NOT EXISTS variant_score NUMERIC DEFAULT 0;
ALTER TABLE outreach_log ADD COLUMN IF NOT EXISTS variant_id UUID REFERENCES email_variants(id);
ALTER TABLE outreach_log ADD COLUMN IF NOT EXISTS delivery_status TEXT DEFAULT 'recorded';

CREATE INDEX IF NOT EXISTS idx_outreach_status ON outreach_log(delivery_status);

-- ═══════ RLS POLICIES ═══════
ALTER TABLE email_variants ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_warmup ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Full access" ON email_variants;
CREATE POLICY "Full access" ON email_variants FOR ALL USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "Full access" ON email_warmup;
CREATE POLICY "Full access" ON email_warmup FOR ALL USING (true) WITH CHECK (true);

-- ═══════ DOMAIN HEALTH VIEW ═══════
CREATE OR REPLACE VIEW email_domain_health AS
SELECT
  o.sending_domain AS domain,
  COUNT(*) AS total_sent,
  COUNT(*) FILTER (WHERE l.email_opened) AS opens,
  COUNT(*) FILTER (WHERE l.replied) AS replies,
  COUNT(*) FILTER (WHERE o.delivery_status = 'bounced') AS bounces,
  ROUND(100.0 * COUNT(*) FILTER (WHERE l.email_opened) / NULLIF(COUNT(*), 0), 2) AS open_rate,
  ROUND(100.0 * COUNT(*) FILTER (WHERE l.replied) / NULLIF(COUNT(*), 0), 2) AS reply_rate
FROM outreach_log o
LEFT JOIN leads l ON o.lead_id = l.id
WHERE o.channel = 'email' AND o.sending_domain IS NOT NULL
GROUP BY o.sending_domain
ORDER BY total_sent DESC;
