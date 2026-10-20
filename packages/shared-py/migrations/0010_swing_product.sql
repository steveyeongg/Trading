-- ATLAS schema v10 - swing engine product tables: settings, nightly scans and their ideas,
-- the user's open positions, job runs and sent digests.
-- Idempotent: safe to re-run, but the migrate runner only runs each file once.
-- Keep colons out of this file. The runner executes it through text(), which reads
-- a colon followed by a word as a bind parameter.
-- Every measure is DOUBLE PRECISION, so the API serialises plain floats.

-- The user's account settings. Exactly one row (id is pinned to 1); no row means the
-- code defaults in swing_engine.settings.
CREATE TABLE IF NOT EXISTS swing_settings (
    id                 SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    equity             DOUBLE PRECISION NOT NULL,
    risk_pct           DOUBLE PRECISION NOT NULL,
    max_position_pct   DOUBLE PRECISION NOT NULL,
    max_total_risk_pct DOUBLE PRECISION NOT NULL,
    min_price          DOUBLE PRECISION NOT NULL,
    min_dollar_volume  DOUBLE PRECISION NOT NULL,
    max_positions      INT              NOT NULL,
    telegram_digest    BOOLEAN          NOT NULL,
    updated_at         TIMESTAMPTZ      NOT NULL DEFAULT now()
);

-- One row per scanned session. session is the session whose close was scanned and
-- target_session the one the ideas are for. payload is the whole scan as JSON. reconstructed
-- marks a scan made later than its own evening (a catch-up after the machine slept).
CREATE TABLE IF NOT EXISTS swing_scans (
    session         DATE PRIMARY KEY,
    target_session  DATE             NOT NULL,
    generated_at    TIMESTAMPTZ      NOT NULL DEFAULT now(),
    regime          TEXT             NOT NULL,
    breadth         DOUBLE PRECISION,
    universe_size   INT,
    reconstructed   BOOLEAN          NOT NULL DEFAULT FALSE,
    payload         JSONB            NOT NULL
);

-- One row per (session, symbol, setup): the ideas of a scan, kept so their outcome can be
-- tracked forward. watch_only marks an idea parked at zero shares (a downtrend). The columns
-- from track_status on are written by forward tracking; known_date is the first session on
-- which the outcome could have been known, so live statistics never look ahead. OVERLAPS is a
-- reserved word in Postgres, so that column is always written with double quotes.
CREATE TABLE IF NOT EXISTS swing_ideas (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session         DATE             NOT NULL,
    target_session  DATE             NOT NULL,
    symbol          TEXT             NOT NULL,
    setup           TEXT             NOT NULL,
    version         TEXT             NOT NULL,
    live            BOOLEAN          NOT NULL,
    watch_only      BOOLEAN          NOT NULL DEFAULT FALSE,
    trigger         DOUBLE PRECISION NOT NULL,
    stop            DOUBLE PRECISION NOT NULL,
    atr14           DOUBLE PRECISION,
    rs_pct          DOUBLE PRECISION,
    rank            INT              NOT NULL,
    shares          INT              NOT NULL,
    risk_dollars    DOUBLE PRECISION NOT NULL,
    position_value  DOUBLE PRECISION NOT NULL,
    "overlaps"      TEXT[]           NOT NULL DEFAULT '{}',
    published_at    TIMESTAMPTZ      NOT NULL,
    reconstructed   BOOLEAN          NOT NULL DEFAULT FALSE,
    track_status    TEXT             NOT NULL DEFAULT 'pending',
    entry_date      DATE,
    entry_price     DOUBLE PRECISION,
    exit_date       DATE,
    exit_price      DOUBLE PRECISION,
    exit_reason     TEXT,
    known_date      DATE,
    r_multiple      DOUBLE PRECISION,
    current_stop    DOUBLE PRECISION,
    UNIQUE (session, symbol, setup)
);

-- Forward tracking reads only the ideas that are still pending or open.
CREATE INDEX IF NOT EXISTS swing_ideas_open_idx
    ON swing_ideas (session)
    WHERE track_status IN ('pending', 'open');

-- The positions the user says they hold. The app never places an order; the user enters
-- what they bought and the engine only advises. idea_id links back to the idea it came from.
CREATE TABLE IF NOT EXISTS swing_positions (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol           TEXT             NOT NULL,
    opened_on        DATE             NOT NULL,
    entry_price      DOUBLE PRECISION NOT NULL,
    shares           DOUBLE PRECISION NOT NULL,
    initial_stop     DOUBLE PRECISION NOT NULL,
    current_stop     DOUBLE PRECISION NOT NULL,
    trail_atr        DOUBLE PRECISION,
    idea_id          UUID REFERENCES swing_ideas (id) ON DELETE SET NULL,
    status           TEXT             NOT NULL DEFAULT 'open',
    closed_on        DATE,
    exit_price       DOUBLE PRECISION,
    exit_reason      TEXT,
    r_multiple       DOUBLE PRECISION,
    last_action      TEXT,
    last_action_text TEXT,
    last_action_on   DATE,
    notes            TEXT,
    created_at       TIMESTAMPTZ      NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS swing_positions_status_idx ON swing_positions (status, opened_on);

-- One row per run of the nightly job (or a manual scan). steps is the per-step log as JSON.
CREATE TABLE IF NOT EXISTS swing_job_runs (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind         TEXT        NOT NULL,
    session      DATE,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    status       TEXT        NOT NULL DEFAULT 'running',
    steps        JSONB,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS swing_job_runs_started_idx ON swing_job_runs (started_at DESC);

-- One row per digest attempt for a session and kind, so a digest is sent once. ok is false
-- when the last send failed (detail says why), which leaves a retry open.
CREATE TABLE IF NOT EXISTS swing_digests (
    session  DATE        NOT NULL,
    kind     TEXT        NOT NULL,
    sent_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ok       BOOLEAN     NOT NULL,
    detail   TEXT,
    PRIMARY KEY (session, kind)
);
