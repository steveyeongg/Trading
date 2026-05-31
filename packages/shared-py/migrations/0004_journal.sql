-- Trade journal. One row per closed trade — auto-written from backtest fills
-- or (later) live broker fills. The human-readable post-mortem layer.

CREATE TABLE IF NOT EXISTS journal_entries (
    id              BIGSERIAL PRIMARY KEY,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,                 -- long | short
    strategy        TEXT NOT NULL,
    opened_at       TIMESTAMPTZ NOT NULL,
    closed_at       TIMESTAMPTZ,
    entry_price     NUMERIC(20,8) NOT NULL,
    exit_price      NUMERIC(20,8),
    quantity        NUMERIC(20,8) NOT NULL,
    realized_pnl    NUMERIC(20,6),
    realized_return NUMERIC(12,6),
    r_multiple      NUMERIC(10,4),                 -- realized PnL / initial risk
    max_run_up      NUMERIC(10,6),                 -- MFE
    max_drawdown    NUMERIC(10,6),                 -- MAE
    bars_held       INT,
    exit_reason     TEXT,                          -- stop | target | time | eod | manual
    fees_paid       NUMERIC(20,6) DEFAULT 0,
    source          TEXT NOT NULL DEFAULT 'backtest',  -- backtest | live | paper
    notes           JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS journal_entries_symbol_idx ON journal_entries (symbol, closed_at DESC);
CREATE INDEX IF NOT EXISTS journal_entries_closed_idx ON journal_entries (closed_at DESC);
