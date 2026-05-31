-- Per-user data scoping. Adds user_id to watchlists, alert_rules, portfolios.
-- Existing seeded rows are backfilled to the dashboard's dev user ('dashboard')
-- so the demo data stays visible when running in dev-auth mode.

ALTER TABLE watchlists ADD COLUMN IF NOT EXISTS user_id TEXT;
UPDATE watchlists SET user_id = 'dashboard' WHERE user_id IS NULL;
-- One watchlist per user (MVP). Enables ON CONFLICT (user_id) upserts.
CREATE UNIQUE INDEX IF NOT EXISTS watchlists_user_idx ON watchlists (user_id);

ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS user_id TEXT;
UPDATE alert_rules SET user_id = 'dashboard' WHERE user_id IS NULL;
CREATE INDEX IF NOT EXISTS alert_rules_user_idx ON alert_rules (user_id);

ALTER TABLE portfolios ADD COLUMN IF NOT EXISTS user_id TEXT;
UPDATE portfolios SET user_id = 'dashboard' WHERE user_id IS NULL;
CREATE INDEX IF NOT EXISTS portfolios_user_idx ON portfolios (user_id);
