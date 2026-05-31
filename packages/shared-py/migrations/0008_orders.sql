-- Order lifecycle for (paper) execution. One row per submitted order.

CREATE TABLE IF NOT EXISTS orders (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,            -- buy | sell
    intent       TEXT NOT NULL,            -- open | close
    quantity     NUMERIC(20,8) NOT NULL,
    limit_price  NUMERIC(20,8),
    fill_price   NUMERIC(20,8),
    status       TEXT NOT NULL,            -- filled | rejected | pending
    broker       TEXT NOT NULL,            -- paper | alpaca
    realized_pnl NUMERIC(20,6),            -- set on a close fill
    detail       TEXT,
    signal_ref   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS orders_user_idx ON orders (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS orders_symbol_idx ON orders (symbol, created_at DESC);
