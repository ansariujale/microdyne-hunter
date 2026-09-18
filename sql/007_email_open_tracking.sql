-- ═══════════════════════════════════════════════════════════════
-- 007: Email open tracking — device details + per-open history
-- Written by the "email-open" Supabase Edge Function.
-- ═══════════════════════════════════════════════════════════════

-- Latest-open details on the tracking row
ALTER TABLE email_tracking ADD COLUMN IF NOT EXISTS last_opened_at TIMESTAMPTZ;
ALTER TABLE email_tracking ADD COLUMN IF NOT EXISTS device_type TEXT;
ALTER TABLE email_tracking ADD COLUMN IF NOT EXISTS os TEXT;
ALTER TABLE email_tracking ADD COLUMN IF NOT EXISTS email_client TEXT;

-- So the funnel can show when a lead first opened
ALTER TABLE leads ADD COLUMN IF NOT EXISTS email_opened_at TIMESTAMPTZ;

-- One row per open, so repeat opens stay visible as history
CREATE TABLE IF NOT EXISTS email_open_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tracking_id UUID,
    lead_id UUID REFERENCES leads(id) ON DELETE SET NULL,
    opened_at TIMESTAMPTZ DEFAULT now(),
    user_agent TEXT,
    ip_address TEXT,
    device_type TEXT,
    os TEXT,
    email_client TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_open_events_tracking ON email_open_events(tracking_id);
CREATE INDEX IF NOT EXISTS idx_open_events_lead ON email_open_events(lead_id);
CREATE INDEX IF NOT EXISTS idx_open_events_opened ON email_open_events(opened_at DESC);

ALTER TABLE email_open_events ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Full access" ON email_open_events;
CREATE POLICY "Full access" ON email_open_events FOR ALL USING (true) WITH CHECK (true);

-- Opens broken down by device, for the dashboard
CREATE OR REPLACE VIEW email_open_devices AS
SELECT
    coalesce(device_type, 'Unknown') AS device_type,
    coalesce(email_client, 'Unknown') AS email_client,
    count(*) AS opens,
    count(DISTINCT lead_id) AS leads
FROM email_open_events
GROUP BY 1, 2
ORDER BY opens DESC;
