-- Email tracking table for open/click tracking
CREATE TABLE IF NOT EXISTS email_tracking (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tracking_id UUID UNIQUE NOT NULL DEFAULT gen_random_uuid(),
    lead_id UUID REFERENCES leads(id) ON DELETE SET NULL,
    sequence_stage INT DEFAULT 1,
    recipient_email TEXT,
    subject TEXT,
    opened BOOLEAN DEFAULT FALSE,
    opened_at TIMESTAMPTZ,
    open_count INT DEFAULT 0,
    user_agent TEXT,
    ip_address TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_email_tracking_tracking_id ON email_tracking(tracking_id);
CREATE INDEX idx_email_tracking_lead_id ON email_tracking(lead_id);
CREATE INDEX idx_email_tracking_opened ON email_tracking(opened);
CREATE INDEX idx_email_tracking_opened_at ON email_tracking(opened_at DESC);

ALTER TABLE email_tracking ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Full access" ON email_tracking FOR ALL USING (true) WITH CHECK (true);

CREATE OR REPLACE VIEW email_tracking_stats AS
SELECT
    count(*) AS total_tracked,
    count(*) FILTER (WHERE opened = true) AS total_opened,
    count(DISTINCT lead_id) FILTER (WHERE opened = true) AS unique_opens,
    CASE
        WHEN count(*) > 0
        THEN round((count(*) FILTER (WHERE opened = true))::numeric / count(*)::numeric * 100, 1)
        ELSE 0
    END AS open_rate
FROM email_tracking;
