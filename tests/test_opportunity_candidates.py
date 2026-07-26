"""D1 regression tests: DexScreener-based candidate discovery.

The scanner discovers recent launches directly from DexScreener newest-pairs,
filtering by launch age, minimum liquidity, and established-token exclusion.
"""

import asyncio

from app.core.config import settings
from app.services import rug_analyzer

NOW_MS = 1_700_000_000_000
DAY_MS = 86_400_000


def _run(coro):
    return asyncio.run(coro)


def _stub_latest(monkeypatch, pairs):
    """Stub fetch_latest_pairs to return the given pair list."""
    async def fake_latest():
        return pairs

    monkeypatch.setattr(rug_analyzer, "fetch_latest_pairs", fake_latest)
    monkeypatch.setattr(rug_analyzer, "_pair_age_ms", lambda c: (c, NOW_MS))


def _pair(address, name, symbol, created_ms, liq_usd):
    return {
        "chainId": "robinhood",
        "pairCreatedAt": created_ms,
        "liquidity": {"usd": liq_usd},
        "baseToken": {"address": address, "name": name, "symbol": symbol},
    }


def test_recent_launch_ranks_before_old(monkeypatch):
    _stub_latest(monkeypatch, [
        _pair("0xold", "Old", "OLD", NOW_MS - 200 * DAY_MS, 10_000),
        _pair("0xnew", "New", "NEW", NOW_MS - 1 * DAY_MS, 10_000),
    ])
    out = _run(rug_analyzer._discover_recent_candidates(limit=1))
    assert [t["address_hash"] for t in out] == ["0xnew"]


def test_stale_launch_is_dropped(monkeypatch):
    monkeypatch.setattr(settings, "scan_max_launch_age_days", 3.0)
    _stub_latest(monkeypatch, [
        _pair("0xstale", "Stale", "STL", NOW_MS - 90 * DAY_MS, 10_000),
        _pair("0xfresh", "Fresh", "FRH", NOW_MS - 1 * DAY_MS, 10_000),
    ])
    out = _run(rug_analyzer._discover_recent_candidates(limit=5))
    assert [t["address_hash"] for t in out] == ["0xfresh"]


def test_dead_token_below_liquidity_floor_is_dropped(monkeypatch):
    monkeypatch.setattr(settings, "scan_min_candidate_liquidity_usd", 500.0)
    _stub_latest(monkeypatch, [
        _pair("0xdead", "Dead", "DED", NOW_MS - 1 * DAY_MS, 10.0),
        _pair("0xlive", "Live", "LIV", NOW_MS - 1 * DAY_MS, 5_000.0),
    ])
    out = _run(rug_analyzer._discover_recent_candidates(limit=5))
    assert [t["address_hash"] for t in out] == ["0xlive"]


def test_unknown_age_pairs_are_excluded(monkeypatch):
    _stub_latest(monkeypatch, [
        {"chainId": "robinhood", "liquidity": {"usd": 5000},
         "baseToken": {"address": "0xa", "name": "A", "symbol": "A"}},
    ])
    monkeypatch.setattr(rug_analyzer, "_pair_age_ms", lambda c: (c, NOW_MS))
    out = _run(rug_analyzer._discover_recent_candidates(limit=5))
    assert out == []


def test_no_pairs_returns_empty(monkeypatch):
    _stub_latest(monkeypatch, [])
    out = _run(rug_analyzer._discover_recent_candidates(limit=5))
    assert out == []


def test_duplicate_tokens_deduplicated(monkeypatch):
    _stub_latest(monkeypatch, [
        _pair("0xsame", "Token", "TKN", NOW_MS - 1 * DAY_MS, 5_000),
        _pair("0xsame", "Token", "TKN", NOW_MS - 2 * DAY_MS, 3_000),
    ])
    monkeypatch.setattr(rug_analyzer, "_pair_age_ms", lambda c: (c, NOW_MS))
    out = _run(rug_analyzer._discover_recent_candidates(limit=5))
    assert len(out) == 1
    assert out[0]["address_hash"] == "0xsame"


def test_established_tokens_skipped(monkeypatch):
    _stub_latest(monkeypatch, [
        _pair("0xweth", "Wrapped Ether", "WETH", NOW_MS - 1 * DAY_MS, 50_000),
        _pair("0xnew", "New", "NEW", NOW_MS - 1 * DAY_MS, 5_000),
    ])
    monkeypatch.setattr(rug_analyzer, "_pair_age_ms", lambda c: (c, NOW_MS))
    out = _run(rug_analyzer._discover_recent_candidates(limit=5))
    assert [t["address_hash"] for t in out] == ["0xnew"]
