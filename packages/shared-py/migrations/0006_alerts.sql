-- Alert rules + delivery audit. Single-user MVP (per-user rules with auth,
-- Phase 3). Rules are evaluated against signals by the alert engine, which
-- runs inside the stream broadcaster loop.

CREATE TABLE IF NOT EXISTS alert_rules (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    -- Predicate: which metric, comparison, threshold.
    metric      TEXT NOT NULL,                 -- composite | confidence | tech | quant | macro | sent | ...
    op          TEXT NOT NULL,                 -- >= | <= | > | < | ==
    threshold   DOUBLE PRECISION NOT NULL,
    symbol      TEXT,                           -- NULL = any symbol; else exact match
    direction   TEXT,                           -- NULL = any; else long|short
    channels    TEXT[] NOT NULL DEFAULT '{log}', -- log | webhook | telegram | email
    cooldown_s  INTEGER NOT NULL DEFAULT 1800,  -- min seconds between fires per (rule, symbol)
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS alert_deliveries (
    id          BIGSERIAL PRIMARY KEY,
    rule_id     UUID REFERENCES alert_rules(id) ON DELETE SET NULL,
    symbol      TEXT NOT NULL,
    channel     TEXT NOT NULL,
    ok          BOOLEAN NOT NULL,
    detail      TEXT,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    fired_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS alert_deliveries_fired_idx ON alert_deliveries (fired_at DESC);

-- A starter rule so the dashboard shows something on first run.
INSERT INTO alert_rules (id, name, metric, op, threshold, channels)
VALUES (
    '00000000-0000-0000-0000-0000000000c1'::uuid,
    'Strong composite',
    'composite', '>=', 70, ARRAY['log']
)
ON CONFLICT (id) DO NOTHING;
