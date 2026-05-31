"""Ticker extraction from free-text headlines.

Strategy (cheapest → most expensive):
  1. Cashtag detection — `$AAPL`, `$BTC`. Reliable when used.
  2. Symbol allowlist — match raw symbol tokens against a known universe.
  3. Company-name aliases — "Apple", "NVIDIA" → AAPL, NVDA.

Phase 2 keeps the alias table small and hand-curated for the universe we
actually trade. Phase 3 will use a NER model + entity-linking against a
real SecMaster.
"""

from __future__ import annotations

import re

# Hand-curated alias map. Extend per asset class.
# Keys are canonical symbols; values are lowercased name fragments.
ALIASES: dict[str, tuple[str, ...]] = {
    "AAPL": ("apple", "apple inc"),
    "MSFT": ("microsoft",),
    "NVDA": ("nvidia",),
    "GOOGL": ("alphabet", "google"),
    "GOOG": ("alphabet", "google"),
    "AMZN": ("amazon",),
    "META": ("meta platforms", "facebook"),
    "TSLA": ("tesla",),
    "TSM": ("tsmc", "taiwan semiconductor"),
    "AVGO": ("broadcom",),
    "JPM": ("jpmorgan", "jp morgan"),
    "BAC": ("bank of america",),
    "WMT": ("walmart",),
    "XOM": ("exxon",),
    "SPY": ("s&p 500", "sp500"),
    "QQQ": ("nasdaq 100", "nasdaq-100"),
    "BTC": ("bitcoin",),
    "ETH": ("ethereum",),
    "SOL": ("solana",),
}

# Build reverse index for fast scan.
_ALIAS_INDEX: list[tuple[re.Pattern, str]] = []
for sym, names in ALIASES.items():
    for n in names:
        # \b only works for word characters; we handle "s&p 500" by escaping.
        pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])", re.IGNORECASE)
        _ALIAS_INDEX.append((pat, sym))

# Cashtag pattern: `$AAPL`, `$BRK.B`.
_CASHTAG = re.compile(r"\$([A-Z]{1,6}(?:\.[A-Z])?)\b")


def extract_tickers(text: str, universe: set[str] | None = None) -> list[str]:
    """Return the unique symbols found in `text`, preserving first-mention order.

    `universe` (optional) filters to symbols you care about — useful so a
    stray "$X" doesn't trigger downstream work on an off-universe ticker.
    """
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()

    def _push(sym: str) -> None:
        sym = sym.upper()
        if universe is not None and sym not in universe:
            return
        if sym in seen:
            return
        seen.add(sym)
        found.append(sym)

    for m in _CASHTAG.finditer(text):
        _push(m.group(1))

    for pat, sym in _ALIAS_INDEX:
        if pat.search(text):
            _push(sym)

    return found


def salience(text: str, ticker: str) -> float:
    """Crude relevance signal — how strongly the text is about *this* ticker.

    Score in [0, 1] based on:
      - count of mentions
      - whether the ticker is in the first 60 chars (likely headline subject)
      - whether multiple tickers compete for the same article
    """
    if not text:
        return 0.0
    text_lc = text.lower()
    syms = extract_tickers(text)
    if ticker.upper() not in syms:
        return 0.0
    if not syms:
        return 0.0
    primary_share = 1.0 / len(syms)
    mention_count = text_lc.count(ticker.lower()) + sum(text_lc.count(a) for a in ALIASES.get(ticker.upper(), ()))
    in_lede = ticker.lower() in text_lc[:60] or any(a in text_lc[:60] for a in ALIASES.get(ticker.upper(), ()))
    # Start below 1.0 so the lede / multi-mention bonuses produce measurable
    # separation even for single-ticker articles.
    base = 0.6 * primary_share
    base += 0.10 * min(3, mention_count - 1)
    if in_lede:
        base += 0.30
    return float(min(1.0, base))
