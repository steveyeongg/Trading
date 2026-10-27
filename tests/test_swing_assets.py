"""Swing engine asset classification: which Alpaca assets are operating-company stocks.

Misclassifying a real company as an ETF silently drops it from the universe, and letting an
ETF, warrant or unit through pollutes it, so the table below pins both directions.
"""

from __future__ import annotations

import pytest
from swing_engine.data.assets import (
    REFERENCE_SYMBOLS,
    TRADEABLE_KINDS,
    AssetKind,
    candidate_symbols,
    classify_asset,
)


def _asset(sym: str, name: str, exch: str, cls: str = "us_equity") -> dict[str, object]:
    return {"symbol": sym, "name": name, "exchange": exch, "class": cls}


@pytest.mark.parametrize(
    "sym,name,exch,kind",
    [
        ("AAPL", "Apple Inc. Common Stock", "NASDAQ", "common"),
        ("TSM", "Taiwan Semiconductor Manufacturing Company Ltd.", "NYSE", "common"),
        ("BRK.B", "Berkshire Hathaway Inc. Class B", "NYSE", "common"),
        ("BABA", "Alibaba Group Holding Limited American Depositary Shares", "NYSE", "common"),
        ("SPY", "SPDR S&P 500 ETF Trust", "ARCA", "etf"),
        ("QQQ", "Invesco QQQ Trust, Series 1", "NASDAQ", "etf"),
        ("PDI", "PIMCO Dynamic Income Fund", "NYSE", "fund"),
        ("ABCDW", "Abcd Corp - Warrant", "NASDAQ", "warrant"),
        ("XYZU", "XYZ Acquisition Corp - Units", "NASDAQ", "unit"),
        ("XYZ", "XYZ Acquisition Corp - Class A Ordinary Shares", "NASDAQ", "spac"),
        ("XYZR", "XYZ Acquisition Corp - Rights", "NASDAQ", "right"),
        (
            "JPM.PRC",
            "JPMorgan Chase & Co. Depositary Shares Series C Preferred",
            "NYSE",
            "preferred",
        ),
        ("GBTC", "Grayscale Bitcoin Trust (BTC)", "ARCA", "etf"),
        ("OTCX", "Some Co", "OTC", "otc"),
        # Controller ruling: operating companies named "... Trust ..." are not ETFs.
        ("NTRS", "Northern Trust Corporation Common Stock", "NASDAQ", "common"),
    ],
)
def test_classify(sym, name, exch, kind):
    assert classify_asset(_asset(sym, name, exch)) == kind


@pytest.mark.parametrize(
    "name",
    [
        "Example Trust Corporation",
        "Example Trust Corp",
        "Example Trust Bancorp",
        "Example Trust Inc",
        "Example Trust Incorporated",
        "Example Trust Co.",
        "Example Trust Company",
    ],
)
def test_trust_in_an_operating_company_name_is_common(name):
    assert classify_asset(_asset("EXT", name, "NASDAQ")) == AssetKind.COMMON


@pytest.mark.parametrize(
    "sym,name",
    [
        ("FRT", "Federal Realty Investment Trust"),
        ("CPT", "Camden Property Trust Common Shares of Beneficial Interest"),
        ("PMT", "PennyMac Mortgage Investment Trust Common Shares of Beneficial Interest"),
        ("PEB", "Pebblebrook Hotel Trust Common Shares of Beneficial Interest"),
        # No REIT keyword in the name: only the share form ("Shares of Beneficial Interest").
        ("DHC", "Diversified Healthcare Trust Common Shares of Beneficial Interest"),
        ("NSA", "National Storage Affiliates Trust Common Shares of Beneficial Interest"),
        ("EXR", "Example Storage Trust Shares of Beneficial Interest"),
    ],
)
def test_real_estate_trusts_are_common(sym, name):
    assert classify_asset(_asset(sym, name, "NYSE")) == AssetKind.COMMON


def test_royalty_trust_units_of_beneficial_interest_are_not_common():
    # Only "shares" of beneficial interest are REIT equity; royalty trusts issue "units".
    name = "Example Basin Royalty Trust Units of Beneficial Interest"
    assert classify_asset(_asset("RTU", name, "NYSE")) == AssetKind.ETF


@pytest.mark.parametrize(
    "name",
    [
        "SPDR Gold Trust",
        "iShares Silver Trust",
        "ProShares Trust II",
        "Grayscale Ethereum Trust (ETH)",
        "Sprott Physical Gold Trust Units",
        "WisdomTree Trust",
    ],
)
def test_trust_products_are_etfs(name):
    assert classify_asset(_asset("TRST", name, "ARCA")) == AssetKind.ETF


@pytest.mark.parametrize(
    "name,kind",
    [
        ("Vanguard Total Stock Market ETF", "etf"),
        ("ProShares UltraPro QQQ", "etf"),
        ("SPDR Gold Shares", "etf"),
        ("iPath Series B S&P 500 VIX Short-Term Futures ETN", "etf"),
        ("Financial Select Sector SPDR Fund", "etf"),
        ("Invesco DB Commodity Index Tracking Fund", "fund"),
        ("Adams Diversified Equity Fund, Inc.", "fund"),
    ],
)
def test_fund_family_names(name, kind):
    assert classify_asset(_asset("FND", name, "ARCA")) == kind


def test_a_wealth_manager_named_like_an_etf_issuer_stays_common():
    # WisdomTree, Inc. is an operating company; only its funds carry the ETF markers.
    assert classify_asset(_asset("WT", "WisdomTree, Inc. Common Stock", "NYSE")) == "common"


@pytest.mark.parametrize(
    "name,kind",
    [
        # A SPAC unit lists its components, including the warrant: the unit wins.
        (
            "XYZ Acquisition Corp. Units, each consisting of one Class A ordinary share "
            "and one-half of one redeemable warrant",
            "unit",
        ),
        ("XYZ Acquisition Corp. Redeemable Warrants", "warrant"),
        ("XYZ Acquisition Corp. Class A Ordinary Shares", "spac"),
        # Master limited partnerships trade "units" that are their common equity.
        ("Energy Transfer LP Common Units", "common"),
        ("Brookfield Infrastructure Partners L.P. Limited Partnership Units", "common"),
    ],
)
def test_units_warrants_and_partnerships(name, kind):
    assert classify_asset(_asset("SPC", name, "NASDAQ")) == kind


@pytest.mark.parametrize(
    "sym,kind",
    [
        ("ABC.WS", "warrant"),
        ("ABC.W", "warrant"),
        ("ABC.U", "unit"),
        ("ABC.UN", "unit"),
        ("ABC.R", "right"),
        ("ABC.RT", "right"),
        ("ABC.PRA", "preferred"),
        ("ABC.PRB", "preferred"),
        ("BF.B", "common"),
        ("HEI.A", "common"),
        ("abc.ws", "warrant"),
    ],
)
def test_dot_suffix_rules_use_the_symbol_alone(sym, kind):
    assert classify_asset(_asset(sym, "Abc Holdings", "NYSE")) == kind


@pytest.mark.parametrize(
    "name,kind",
    [
        ("Bank of America Corporation 6.5% Non-Cumulative Preferred Stock, Series Z", "preferred"),
        ("Example Corp Depositary Shares, each representing 1/1000th interest", "preferred"),
        # Real companies that happen to say "preferred" must stay common.
        ("Preferred Bank Common Stock", "common"),
        (
            "Banco Bradesco S.A. American Depositary Shares "
            "(each representing one Preferred Share)",
            "common",
        ),
    ],
)
def test_preferred_by_name(name, kind):
    assert classify_asset(_asset("PFD", name, "NYSE")) == kind


def test_notes_and_debentures_are_other():
    name = "Example Corp 6.50% Senior Notes due 2028"
    assert classify_asset(_asset("EXN", name, "NYSE")) == AssetKind.OTHER


def test_non_equity_asset_class_is_other():
    assert classify_asset(_asset("BTC/USD", "Bitcoin", "CRYPTO", cls="crypto")) == AssetKind.OTHER


def test_missing_fields_do_not_raise():
    assert classify_asset({"symbol": "AAPL", "name": None}) == AssetKind.COMMON
    assert classify_asset({}) == AssetKind.COMMON


def test_only_common_stock_is_tradeable():
    assert frozenset({AssetKind.COMMON}) == TRADEABLE_KINDS
    assert REFERENCE_SYMBOLS == ("SPY",)


def test_candidate_symbols_includes_spy_reference_once():
    assets = [_asset("SPY", "SPDR S&P 500 ETF Trust", "ARCA")]
    assert candidate_symbols(assets) == ["SPY"]


def test_candidate_symbols_keeps_listed_common_stock_sorted_and_unique():
    assets = [
        _asset("MSFT", "Microsoft Corporation Common Stock", "NASDAQ"),
        _asset("AAPL", "Apple Inc. Common Stock", "NASDAQ"),
        _asset("AAPL", "Apple Inc. Common Stock", "NASDAQ"),
        _asset("BRK.B", "Berkshire Hathaway Inc. Class B", "NYSE"),
        _asset("F", "Ford Motor Company Common Stock", "nyse"),
        _asset("GE", "General Electric Company", "AMEX"),
        _asset("IBM", "International Business Machines Corporation", "BATS"),
    ]
    assert candidate_symbols(assets) == ["AAPL", "BRK.B", "F", "GE", "IBM", "MSFT", "SPY"]


def test_candidate_symbols_drops_everything_that_is_not_listed_common_stock():
    assets = [
        _asset("AAPL", "Apple Inc. Common Stock", "NASDAQ"),
        _asset("OTCX", "Some Co", "OTC"),
        _asset("QQQ", "Invesco QQQ Trust, Series 1", "NASDAQ"),
        _asset("PDI", "PIMCO Dynamic Income Fund", "NYSE"),
        _asset("ABCDW", "Abcd Corp - Warrant", "NASDAQ"),
        _asset("XYZU", "XYZ Acquisition Corp - Units", "NASDAQ"),
        _asset("JPM.PRC", "JPMorgan Chase & Co. Depositary Shares Series C Preferred", "NYSE"),
        _asset("NOEX", "Listed Nowhere Inc. Common Stock", ""),
        _asset("CRY", "Crypto Thing", "CRYPTO", cls="crypto"),
        {"symbol": "", "name": "Nameless Inc.", "exchange": "NYSE", "class": "us_equity"},
    ]
    assert candidate_symbols(assets) == ["AAPL", "SPY"]


@pytest.mark.parametrize("sym", ["ZVZZT", "ZWZZT", "ZXZZT", "ZJZZT", "ZBZZT"])
def test_exchange_test_symbols_are_other_whatever_their_name(sym):
    """Exchanges list dummy issues for testing their systems; they print bars like a stock."""
    assert classify_asset(_asset(sym, f"{sym} Common Stock", "NASDAQ")) == AssetKind.OTHER


@pytest.mark.parametrize("name", ["NASDAQ TEST STOCK", "NYSE Arca Test Symbol", "Test Issue"])
def test_test_issues_by_name_are_other(name):
    assert classify_asset(_asset("ZZQT", name, "NYSE")) == AssetKind.OTHER


@pytest.mark.parametrize(
    ("sym", "name"),
    [
        ("AEHR", "Aehr Test Systems Common Stock"),  # a real company with "Test" in its name
        ("INTT", "inTEST Corporation Common Stock"),
        ("TSTX", "Testmark Holdings, Inc."),
    ],
)
def test_a_company_with_test_in_its_name_stays_common(sym, name):
    assert classify_asset(_asset(sym, name, "NASDAQ")) == AssetKind.COMMON


def test_nysearca_listings_are_candidates_and_test_issues_are_not():
    assets = [
        _asset("XYZ", "XYZ Holdings Inc. Common Stock", "NYSEARCA"),
        _asset("ZVZZT", "NASDAQ TEST STOCK", "NASDAQ"),
        _asset("ZZQT", "NYSE Arca Test Symbol", "NYSEARCA"),
    ]
    assert candidate_symbols(assets) == ["SPY", "XYZ"]


def test_candidate_symbols_accepts_a_generator_and_an_empty_list():
    assert candidate_symbols(iter([])) == ["SPY"]
    gen = (a for a in [_asset("AAPL", "Apple Inc. Common Stock", "NASDAQ")])
    assert candidate_symbols(gen) == ["AAPL", "SPY"]
