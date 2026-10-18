-- ATLAS schema v9 - swing engine asset universe, plus an index for daily-bar scans.
-- Idempotent: safe to re-run, but the migrate runner only runs each file once.
-- Keep colons out of this file. The runner executes it through text(), which reads
-- a colon followed by a word as a bind parameter.

-- One row per Alpaca asset (active and inactive). kind is the swing engine's own
-- classification of the asset, and first_bar and last_bar cache the extent of its
-- daily bars so the universe screen need not scan the hypertable.
CREATE TABLE IF NOT EXISTS assets (
    symbol      TEXT PRIMARY KEY,
    name        TEXT,
    exchange    TEXT,
    status      TEXT        NOT NULL,
    kind        TEXT        NOT NULL,
    tradable    BOOLEAN,
    first_bar   DATE,
    last_bar    DATE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The primary key leads with symbol, so scans of one resolution across all symbols
-- (liquidity prefilter, bulk loads for a date range) need their own index.
CREATE INDEX IF NOT EXISTS bars_resolution_ts_idx ON bars (resolution, ts);
