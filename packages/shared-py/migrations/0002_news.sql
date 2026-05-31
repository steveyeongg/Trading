-- News items + scored sentiment.

CREATE TABLE IF NOT EXISTS news_items (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,
    external_id TEXT,                              -- stable id from upstream when available
    url         TEXT,
    title       TEXT NOT NULL,
    summary     TEXT,
    body        TEXT,
    published_at TIMESTAMPTZ NOT NULL,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    tickers     TEXT[] NOT NULL DEFAULT '{}',
    salience    REAL NOT NULL DEFAULT 1.0,
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (source, external_id)
);

CREATE INDEX IF NOT EXISTS news_items_published_idx ON news_items (published_at DESC);
CREATE INDEX IF NOT EXISTS news_items_tickers_gin_idx ON news_items USING GIN (tickers);

CREATE TABLE IF NOT EXISTS news_scores (
    news_id     BIGINT NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
    scorer      TEXT NOT NULL,                    -- 'lexicon' | 'finbert' | ...
    score       REAL NOT NULL,                    -- [-1, +1]
    confidence  REAL NOT NULL,                    -- [0, 1]
    scored_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (news_id, scorer)
);

CREATE INDEX IF NOT EXISTS news_scores_scorer_idx ON news_scores (scorer);
