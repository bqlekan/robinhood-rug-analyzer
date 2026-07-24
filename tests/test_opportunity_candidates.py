"""D1 regression tests: opportunity-scanner candidate selection.

The scanner must surface recently-launched, tradeable tokens instead of the old
established assets Blockscout /tokens lists first — while still falling back to the
prior behaviour when launch age is unknown.
"""

import asyncio

from app.core.config import settings
from app.services import rug_analyzer

NOW_MS = 1_700_000_000_000
DAY_MS = 86_400_000


def _run(coro):
    return asyncio.run(coro)


def _stub_pairs(monkeypatch, pairs_by_addr):
    async def fake_pairs(address):
        return pairs_by_addr.get(address, [])

    monkeypatch.setattr(rug_analyzer, "fetch_token_pairs", fake_pairs)
    # Freeze "now" so age math is deterministic.
    monkeypatch.setattr(rug_analyzer, "_pair_age_ms", lambda c: (c, NOW_MS))


def _pair(created_ms, liq_usd):
    return [{"pairCreatedAt": created_ms, "liquidity": {"usd": liq_usd}}]


def test_recent_launch_ranks_before_old(monkeypatch):
    old = {"address_hash": "0xold", "name": "Old"}
    new = {"address_hash": "0xnew", "name": "New"}
    _stub_pairs(monkeypatch, {
        "0xold": _pair(NOW_MS - 200 * DAY_MS, 10_000),
        "0xnew": _pair(NOW_MS - 1 * DAY_MS, 10_000),
    })
    # Old is listed first (market-cap order); newest launch must win the single slot.
    out = _run(rug_analyzer._select_opportunity_candidates([old, new], limit=1))
    assert [t["address_hash"] for t in out] == ["0xnew"]


def test_stale_launch_is_dropped(monkeypatch):
    monkeypatch.setattr(settings, "scan_max_launch_age_days", 30.0)
    stale = {"address_hash": "0xstale", "name": "Stale"}
    fresh = {"address_hash": "0xfresh", "name": "Fresh"}
    _stub_pairs(monkeypatch, {
        "0xstale": _pair(NOW_MS - 90 * DAY_MS, 10_000),
        "0xfresh": _pair(NOW_MS - 2 * DAY_MS, 10_000),
    })
    out = _run(rug_analyzer._select_opportunity_candidates([stale, fresh], limit=5))
    assert [t["address_hash"] for t in out] == ["0xfresh"]


def test_dead_token_below_liquidity_floor_is_dropped(monkeypatch):
    monkeypatch.setattr(settings, "scan_min_candidate_liquidity_usd", 500.0)
    dead = {"address_hash": "0xdead", "name": "Dead"}
    live = {"address_hash": "0xlive", "name": "Live"}
    _stub_pairs(monkeypatch, {
        "0xdead": _pair(NOW_MS - 1 * DAY_MS, 10.0),      # has a market but abandoned
        "0xlive": _pair(NOW_MS - 1 * DAY_MS, 5_000.0),
    })
    out = _run(rug_analyzer._select_opportunity_candidates([dead, live], limit=5))
    assert [t["address_hash"] for t in out] == ["0xlive"]


def test_unknown_age_falls_back_to_original_order(monkeypatch):
    a = {"address_hash": "0xa", "name": "A"}
    b = {"address_hash": "0xb", "name": "B"}
    _stub_pairs(monkeypatch, {})  # no pairs for anyone -> age unknown for all
    out = _run(rug_analyzer._select_opportunity_candidates([a, b], limit=5))
    # Graceful fallback: original order preserved, nothing dropped.
    assert [t["address_hash"] for t in out] == ["0xa", "0xb"]


def test_dated_launches_rank_above_unknown_age(monkeypatch):
    dated = {"address_hash": "0xdated", "name": "Dated"}
    unknown = {"address_hash": "0xunknown", "name": "Unknown"}
    _stub_pairs(monkeypatch, {"0xdated": _pair(NOW_MS - 5 * DAY_MS, 5_000)})
    out = _run(rug_analyzer._select_opportunity_candidates([unknown, dated], limit=5))
    assert out[0]["address_hash"] == "0xdated"  # datable launch beats unknown-age tail
