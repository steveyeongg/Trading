"""Asset classification: which Alpaca assets are operating-company stocks.

Alpaca's ``GET /v2/assets`` mixes common stocks and ADRs with ETFs, closed-end funds,
SPAC shells, warrants, units, rights, preferreds and OTC names. The swing universe wants
operating-company common stock only, so :func:`classify_asset` sorts every asset into an
:class:`AssetKind` using its symbol, name, exchange and class.

Two mistakes cost differently but both matter. Calling a real company an ETF silently drops
it from the universe; letting an ETF, warrant or unit through pollutes it. The rules are an
ordered table (:data:`_RULES`), first match wins, and each rule is a small predicate. The
heuristics lean on the shape of exchange-listed names, so the exemptions are deliberate:

* "Trust" marks a trust product (SPDR Gold Trust, Grayscale Bitcoin Trust) unless the name
  also carries an operating-entity marker such as "Corporation", "Bancorp" or "Realty"
  (Northern Trust Corporation, Federal Realty Investment Trust).
* "Common Units" and "Limited Partnership Units" are an MLP's common equity, not a SPAC unit.
* An ADR whose underlying shares are preferred (Bradesco, Itau) is still an operating company,
  and "Preferred Bank Common Stock" is a bank, not a preferred share.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final


class AssetKind(StrEnum):
    COMMON = "common"
    ETF = "etf"
    FUND = "fund"
    WARRANT = "warrant"
    UNIT = "unit"
    RIGHT = "right"
    PREFERRED = "preferred"
    SPAC = "spac"
    OTC = "otc"
    OTHER = "other"


# Only operating-company common stock and ADRs may be traded.
TRADEABLE_KINDS: Final = frozenset({AssetKind.COMMON})
# Always backfilled as the market benchmark, even though SPY itself is an ETF.
REFERENCE_SYMBOLS: Final = ("SPY",)
# Exchanges whose common stock can enter the universe (OTC is excluded).
LISTED_EXCHANGES: Final = frozenset({"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"})


@dataclass(frozen=True, slots=True)
class _Asset:
    """The four fields of an Alpaca asset that classification looks at, normalised."""

    symbol: str
    name: str
    exchange: str
    asset_class: str

    @classmethod
    def of(cls, raw: Mapping[str, Any]) -> _Asset:
        return cls(
            symbol=str(raw.get("symbol") or "").strip().upper(),
            name=str(raw.get("name") or "").strip(),
            exchange=str(raw.get("exchange") or "").strip().upper(),
            asset_class=str(raw.get("class") or "us_equity").strip().lower(),
        )

    @property
    def suffix(self) -> str:
        """The share-class suffix after the first dot: ``JPM.PRC`` -> ``PRC``, ``BRK.B`` -> ``B``."""
        return self.symbol.partition(".")[2].split(".")[0]


def _words(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


_ETF_NAME = _words(
    r"\betfs?\b|\betns?\b|\bexchange[- ]traded\b"
    # Brands that only sponsor funds. Invesco, WisdomTree and Vanguard are left out on purpose:
    # Invesco Ltd. and WisdomTree, Inc. are operating companies, and their funds say ETF/Fund/Trust.
    r"|\b(?:ishares|spdr|proshares|powershares|direxion|velocityshares|ipath|etracs"
    r"|microsectors|graniteshares|kraneshares|yieldmax|roundhill|teucrium)\b"
)
_FUND_NAME = _words(r"\bfunds?\b")
_TRUST_NAME = _words(r"\btrust\b")
# Words that show a "Trust" is an operating entity (a bank, a REIT) rather than a product.
# "Shares of Beneficial Interest" is the REIT share form, which catches trust-form REITs
# with no REIT keyword (Diversified Healthcare Trust). Royalty trusts issue "units" instead.
_OPERATING_ENTITY = _words(
    r"\b(?:corporation|corp|incorporated|inc|co|company|bancorp|bancorporation|bancshares)\b"
    r"|\bcommon stock\b"
    r"|\bshares? of beneficial interest\b"
    r"|\b(?:reit|realty|real estate|properties|property|mortgage|hotel|hospitality|lodging)\b"
)
_DEBT_NAME = _words(r"\bnotes?\s+due\b|\b(?:senior|subordinated)\s+notes?\b|\bdebentures?\b")
_UNIT_NAME = _words(r"\bunits?\b")
_MLP_UNITS = _words(r"\bcommon units?\b|\blimited partner")
_WARRANT_NAME = _words(r"\bwarrants?\b")
_RIGHT_NAME = _words(r"\brights?\b")
_SPAC_NAME = _words(r"\bacquisition\s+(?:corp|corporation|co|company)\b")
_PREFERRED_NAME = _words(r"\bpfd\b|\bpreferred\b")
_COMMON_EQUITY_NAME = _words(r"\bcommon (?:stock|shares?)\b|\bordinary shares\b")
_DEPOSITARY_SHARES = _words(r"\bdepositary shares?\b")
# "ADS"/"ADR" are matched case-sensitively so a word like "Ads" is not mistaken for one.
_ADR_NAME = re.compile(r"(?i:\b(?:american|global)\s+depositary\b)|\bADRs?\b|\bADSs?\b")

Predicate = Callable[[_Asset], bool]


def _not_us_equity(a: _Asset) -> bool:
    return a.asset_class != "us_equity"


def _is_otc(a: _Asset) -> bool:
    return a.exchange == "OTC"


def _is_etf_name(a: _Asset) -> bool:
    return bool(_ETF_NAME.search(a.name))


def _is_fund_name(a: _Asset) -> bool:
    return bool(_FUND_NAME.search(a.name))


def _is_trust_product(a: _Asset) -> bool:
    return bool(_TRUST_NAME.search(a.name)) and not _OPERATING_ENTITY.search(a.name)


def _is_debt_name(a: _Asset) -> bool:
    return bool(_DEBT_NAME.search(a.name))


def _is_unit_name(a: _Asset) -> bool:
    # Checked before warrants and rights: a SPAC unit's name lists its warrant and right.
    return bool(_UNIT_NAME.search(a.name)) and not _MLP_UNITS.search(a.name)


def _is_warrant_name(a: _Asset) -> bool:
    return bool(_WARRANT_NAME.search(a.name))


def _is_right_name(a: _Asset) -> bool:
    return bool(_RIGHT_NAME.search(a.name))


def _is_spac_name(a: _Asset) -> bool:
    return bool(_SPAC_NAME.search(a.name))


def _is_preferred_name(a: _Asset) -> bool:
    if _ADR_NAME.search(a.name):
        return False  # an ADR over preferred shares is still the operating company
    if _DEPOSITARY_SHARES.search(a.name):
        return True  # US "Depositary Shares each representing 1/1000th of a preferred share"
    return bool(_PREFERRED_NAME.search(a.name)) and not _COMMON_EQUITY_NAME.search(a.name)


def _suffix_is(*suffixes: str) -> Predicate:
    return lambda a: a.suffix in suffixes


def _is_preferred_suffix(a: _Asset) -> bool:
    return a.suffix.startswith("PR")


# First match wins. Order matters: see the module docstring for the exemptions.
_RULES: Final[tuple[tuple[Predicate, AssetKind], ...]] = (
    (_not_us_equity, AssetKind.OTHER),
    (_is_otc, AssetKind.OTC),
    (_is_etf_name, AssetKind.ETF),
    (_is_fund_name, AssetKind.FUND),
    (_is_trust_product, AssetKind.ETF),
    (_is_debt_name, AssetKind.OTHER),
    (_is_unit_name, AssetKind.UNIT),
    (_is_warrant_name, AssetKind.WARRANT),
    (_is_right_name, AssetKind.RIGHT),
    (_is_spac_name, AssetKind.SPAC),
    (_is_preferred_name, AssetKind.PREFERRED),
    (_suffix_is("WS", "W"), AssetKind.WARRANT),
    (_suffix_is("U", "UN"), AssetKind.UNIT),
    (_suffix_is("R", "RT"), AssetKind.RIGHT),
    (_is_preferred_suffix, AssetKind.PREFERRED),
)


def _kind_of(asset: _Asset) -> AssetKind:
    for predicate, kind in _RULES:
        if predicate(asset):
            return kind
    return AssetKind.COMMON


def classify_asset(asset: Mapping[str, Any]) -> AssetKind:
    """Classify an Alpaca asset dict (uses ``symbol``, ``name``, ``exchange``, ``class``).

    Anything no rule flags is COMMON, including other dot classes such as ``BRK.B``.
    """
    return _kind_of(_Asset.of(asset))


def candidate_symbols(assets: Iterable[Mapping[str, Any]]) -> list[str]:
    """Symbols worth backfilling: listed common stock plus :data:`REFERENCE_SYMBOLS`.

    Sorted and unique. Alpaca's ``status`` and ``tradable`` flags are not consulted, so the
    caller decides whether to pass active assets, inactive ones, or both.
    """
    symbols = set(REFERENCE_SYMBOLS)
    for raw in assets:
        parsed = _Asset.of(raw)
        if (
            parsed.symbol
            and parsed.exchange in LISTED_EXCHANGES
            and _kind_of(parsed) in TRADEABLE_KINDS
        ):
            symbols.add(parsed.symbol)
    return sorted(symbols)
