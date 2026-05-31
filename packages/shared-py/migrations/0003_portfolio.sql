-- Portfolios + positions. Minimal slice of BLUEPRINT §5.1 for the Phase 2.5
-- portfolio view. A single 'default' portfolio is auto-seeded.

CREATE TABLE IF NOT EXISTS portfolios (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          TEXT NOT NULL,
    base_currency TEXT NOT NULL DEFAULT 'USD',
    cash_balance  NUMERIC(20,6) NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS positions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id UUID NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    symbol       TEXT NOT NULL,
    asset_class  TEXT NOT NULL DEFAULT 'equity',
    sector       TEXT,
    quantity     NUMERIC(20,8) NOT NULL,
    avg_cost     NUMERIC(20,8) NOT NULL,
    opened_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at    TIMESTAMPTZ,
    realized_pnl NUMERIC(20,6) DEFAULT 0,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS positions_portfolio_open_idx
    ON positions (portfolio_id) WHERE closed_at IS NULL;

-- Seed a single default portfolio with a stable, well-known id so the
-- dashboard can fetch /v1/portfolios/default without a lookup dance.
INSERT INTO portfolios (id, name, cash_balance)
VALUES ('00000000-0000-0000-0000-0000000000a7', 'Default', 100000)
ON CONFLICT (id) DO NOTHING;
