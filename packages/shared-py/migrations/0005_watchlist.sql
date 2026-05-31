-- Watchlist persistence. Single-user MVP: one well-known 'default' watchlist
-- (auth + per-user watchlists land with Clerk/Auth0 in Phase 3).

CREATE TABLE IF NOT EXISTS watchlists (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    symbols     TEXT[] NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO watchlists (id, name, symbols)
VALUES (
    '00000000-0000-0000-0000-0000000000b1'::uuid,
    'Default',
    ARRAY['AAPL','MSFT','NVDA','TSLA','SPY','QQQ','BTC','ETH']
)
ON CONFLICT (id) DO NOTHING;
