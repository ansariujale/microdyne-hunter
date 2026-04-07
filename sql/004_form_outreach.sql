-- ═══════════════════════════════════════════════════════════════
-- 004: Form Outreach Tracking
-- Adds granular form submission tracking columns to leads table
-- ═══════════════════════════════════════════════════════════════

-- Add form outreach tracking columns
ALTER TABLE leads ADD COLUMN IF NOT EXISTS contact_page_url TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS form_submission_status TEXT DEFAULT 'pending';
ALTER TABLE leads ADD COLUMN IF NOT EXISTS form_error_message TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS form_last_attempted_at TIMESTAMPTZ;

-- Index for efficient form outreach queries
CREATE INDEX IF NOT EXISTS idx_leads_form_submission_status ON leads(form_submission_status);

-- Composite index for fetching pending leads
CREATE INDEX IF NOT EXISTS idx_leads_form_pending ON leads(form_submission_status, form_filled)
    WHERE form_submission_status = 'pending' AND (form_filled = false OR form_filled IS NULL);
