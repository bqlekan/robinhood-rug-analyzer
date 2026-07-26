"""D1 regression tests: opportunity-scanner candidate selection.

The scanner must surface recently-launched, tradeable tokens instead of the old
established assets Blockscout /tokens lists first. Tokens whose launch age
cannot be determined from any source are excluded — no unknown-age backfill.
"""

import asyncio

from app.core.config import settings
from app.services import blockscout_client, rug_analyzer

NOW_MS = 1_700_000_000_000
DAY_MS = 86_400_000


def _run(coro):
    return asyncio.run(coro)


def _stub_pairs(monkeypatch, pairs_by_addr, creation_ts_by_addr=None):
    async def fake_pairs(address):
        return pairs_by_addr.get(address, [])

    monkeypatch.setattr(rug_analyzer, "fetch_token_pairs", fake_pairs)
    # Freeze "now" so age math is deterministic.
    monkeypatch.setattr(rug_analyzer, "_pair_age_ms", lambda c: (c, NOW_MS))

    # Stub secondary age source (blockscout address info + tx timestamp).
    creation_ts = creation_ts_by_addr or {}

    async def fake_address_info(address):
        ts = creation_ts.get(address)
        if ts:
            return {"creation_transaction_hash": f"0xtx_{address}"}
        return {}

    async def fake_tx_timestamp(tx_hash):
        for addr, ts in creation_ts.items():
            if tx_hash == f"0xtx_{addr}":
                from datetime import datetime, timezone
                return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
        return None

    monkeypatch.setattr(blockscout_client, "get_address_info", fake_address_info)
    monkeypatch.setattr(blockscout_client, "get_transaction_timestamp", fake_tx_timestamp)


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


def test_unknown_age_tokens_are_excluded(monkeypatch):
    a = {"address_hash": "0xa", "name": "A"}
    b = {"address_hash": "0xb", "name": "B"}
    _stub_pairs(monkeypatch, {})  # no pairs, no creation-tx fallback -> fully unknown
    out = _run(rug_analyzer._select_opportunity_candidates([a, b], limit=5))
    assert out == []


def test_dated_launches_exclude_unknown_age(monkeypatch):
    dated = {"address_hash": "0xdated", "name": "Dated"}
    unknown = {"address_hash": "0xunknown", "name": "Unknown"}
    _stub_pairs(monkeypatch, {"0xdated": _pair(NOW_MS - 5 * DAY_MS, 5_000)})
    out = _run(rug_analyzer._select_opportunity_candidates([unknown, dated], limit=5))
    assert [t["address_hash"] for t in out] == ["0xdated"]


def test_creation_tx_fallback_recovers_age(monkeypatch):
    """Token with no DexScreener pair but a creation-tx timestamp is kept."""
    token = {"address_hash": "0xfallback", "name": "Fallback"}
    _stub_pairs(monkeypatch, {}, creation_ts_by_addr={
        "0xfallback": NOW_MS - 3 * DAY_MS,
    })
    out = _run(rug_analyzer._select_opportunity_candidates([token], limit=5))
    assert [t["address_hash"] for t in out] == ["0xfallback"]


def test_creation_tx_fallback_still_applies_age_filter(monkeypatch):
    """Token recovered via creation-tx but too old is still dropped."""
    monkeypatch.setattr(settings, "scan_max_launch_age_days", 30.0)
    token = {"address_hash": "0xold_fallback", "name": "OldFallback"}
    _stub_pairs(monkeypatch, {}, creation_ts_by_addr={
        "0xold_fallback": NOW_MS - 90 * DAY_MS,
    })
    out = _run(rug_analyzer._select_opportunity_candidates([token], limit=5))
    assert out == []
