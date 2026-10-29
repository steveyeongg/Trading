-- ATLAS schema v11 - normalise alert_rules.symbol (NULL means any ticker).
-- Idempotent: safe to re-run, but the migrate runner only runs each file once.
-- Keep colons out of this file. The runner executes it through text(), which reads
-- a colon followed by a word as a bind parameter.

-- The dashboard's alert form used to default the symbol to the text ANY, which the
-- matcher read as a ticker, so those rules never fired. Rows saved that way meant
-- "any ticker", so they become NULL, along with blank and * values. ALL is left as
-- is, because it is Allstate's ticker. A rule genuinely meant for Sphere 3D (ANY)
-- cannot be told apart from the old default, so it widens to every ticker.
UPDATE alert_rules
SET symbol = NULL
WHERE symbol IS NOT NULL
  AND upper(btrim(symbol)) IN ('', '*', 'ANY');

-- The rest are tickers. Store them trimmed and upper-cased, as the API now writes them.
UPDATE alert_rules
SET symbol = upper(btrim(symbol))
WHERE symbol IS NOT NULL
  AND symbol <> upper(btrim(symbol));
