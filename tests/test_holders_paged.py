"""Unit tests for M12: bounded paged holder set + true holder count.

Covers the client's `next_page_params` following (multi-page aggregation, bound
respected, graceful stop) and that `analyze_holders` concentration/LP-exclusion
still hold when fed a multi-page set.
"""

import asyncio

import pytest

from app.services import analyzers, blockscout_client


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    monkeypatch.setattr(blockscout_client.settings, "http_cache_enabled", False)


def _holder(addr, value, is_contract=False):
    return {"address": {"hash": addr, "is_contract": is_contract}, "value": value}


# --- client paging (get_token_holders_paged) ---


def test_paging_follows_next_page_params_up_to_bound(monkeypatch):
    # 3 pages available, but bound at 2 -> only 2 fetched, params threaded through.
    pages = {
        None: {"items": [_holder("0xA", "1")], "next_page_params": {"k": 1}},
        1: {"items": [_holder("0xB", "2")], "next_page_params": {"k": 2}},
        2: {"items": [_holder("0xC", "3")], "next_page_params": None},
    }
    seen_params = []

    async def fake_get(client, path, params=None):
        seen_params.append(params)
        return pages[(params or {}).get("k")]

    monkeypatch.setattr(blockscout_client, "_get", fake_get)
    items = _run(blockscout_client.get_token_holders_paged("0xtok", pages=2))
    assert [i["address"]["hash"] for i in items] == ["0xA", "0xB"]  # page 3 not reached
    assert seen_params == [None, {"k": 1}]  # cursor threaded, stopped at bound


def test_paging_stops_early_when_no_next_params(monkeypatch):
    async def fake_get(client, path, params=None):
        return {"items": [_holder("0xA", "1")], "next_page_params": None}

    monkeypatch.setattr(blockscout_client, "_get", fake_get)
    items = _run(blockscout_client.get_token_holders_paged("0xtok", pages=5))
    assert len(items) == 1  # one page, no crash despite pages=5


def test_paging_survives_a_failed_page(monkeypatch):
    # A mid-scan None (HTTP/JSON error) must not crash; it just ends paging.
    async def fake_get(client, path, params=None):
        return None

    monkeypatch.setattr(blockscout_client, "_get", fake_get)
    assert _run(blockscout_client.get_token_holders_paged("0xtok", pages=3)) == []


# --- analyze_holders over a multi-page set ---


def test_concentration_over_multi_page_set():
    # 12 holders (more than a single ~50 page would surface in the top slice); a
    # whale at "rank 11" must still be counted in top10/concentration.
    holders = [_holder(f"0x{i:02x}", str(v)) for i, v in enumerate(
        [50, 5, 5, 5, 5, 5, 5, 5, 5, 5, 3, 2]  # sums to 100
    )]
    dist = analyzers.analyze_holders(holders, holder_count=12, total_supply="100", decimals=0)
    assert dist.top1_percentage == 50.0
    assert dist.sampled_holders == 12
    # top10 = 50 + nine 5s = 95
    assert dist.top10_percentage == 95.0


def test_lp_exclusion_holds_across_pages():
    # LP appears among many holders spanning pages; must still be peeled out.
    holders = [_holder("0xLP", "800", is_contract=True)]
    holders += [_holder(f"0x{i:02x}", "10") for i in range(20)]  # 20 real wallets, 10 each
    dist = analyzers.analyze_holders(holders, 21, "1000", 0, lp_address="0xLP")
    assert "0xLP" not in {h.address for h in dist.top_holders}
    assert dist.lp_percentage == 80.0
    assert dist.top1_percentage == 1.0  # each real wallet holds 10/1000 = 1%
