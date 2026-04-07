-- ═══════════════════════════════════════════════════════════════
-- Migration 005: Add state column to leads table
-- ═══════════════════════════════════════════════════════════════

ALTER TABLE leads ADD COLUMN IF NOT EXISTS state TEXT DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_leads_state ON leads(state);
CREATE INDEX IF NOT EXISTS idx_leads_city ON leads(city);
