-- ═══════════════════════════════════════════════════════════════
-- 008: Country rotation history
-- One row per country the agent has campaigned in, so the auto
-- selector can avoid repeating a market until its cooldown expires.
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS country_usage (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    country TEXT NOT NULL UNIQUE,
    last_used_at TIMESTAMPTZ,
    next_eligible_at TIMESTAMPTZ,
    usage_count INTEGER DEFAULT 0,
    cooldown_days INTEGER DEFAULT 14,
    selected_by TEXT DEFAULT 'manual',   -- 'ai' | 'manual' | 'scrape'
    last_reason TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_country_usage_country ON country_usage(country);
CREATE INDEX IF NOT EXISTS idx_country_usage_next_eligible ON country_usage(next_eligible_at);

ALTER TABLE country_usage ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Full access" ON country_usage;
CREATE POLICY "Full access" ON country_usage FOR ALL USING (true) WITH CHECK (true);

-- The rotation table as the admin panel shows it:
-- Country | Last Used | Usage Count | Next Eligible Date
CREATE OR REPLACE VIEW country_rotation AS
SELECT
    country,
    last_used_at,
    usage_count,
    next_eligible_at,
    (next_eligible_at IS NULL OR next_eligible_at <= now()) AS eligible_now,
    selected_by,
    last_reason
FROM country_usage
ORDER BY last_used_at DESC NULLS LAST;
