"""Integration tests for M3: real contract-creation age.

Covers the cached client helper (timestamp extraction, missing data, API failure)
and the orchestrator fallback that uses it only when no pair timestamp exists.
"""

import asyncio

import pytest

from app.services import blockscout_client, rug_analyzer


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    # Disable the TTL cache so each stubbed _get call is exercised deterministically.
    monkeypatch.setattr(blockscout_client.settings, "http_cache_enabled", False)


# --- get_transaction_timestamp (client helper) ---


def test_tx_timestamp_extracted(monkeypatch):
    async def fake_get(client, path, params=None):
        assert "/transactions/0xabc" in path
        return {"timestamp": "2025-01-01T00:00:00.000000Z", "hash": "0xabc"}

    monkeypatch.setattr(blockscout_client, "_get", fake_get)
    ts = _run(blockscout_client.get_transaction_timestamp("0xabc"))
    assert ts == "2025-01-01T00:00:00.000000Z"


def test_tx_timestamp_missing_field(monkeypatch):
    async def fake_get(client, path, params=None):
        return {"hash": "0xabc"}  # no timestamp

    monkeypatch.setattr(blockscout_client, "_get", fake_get)
    assert _run(blockscout_client.get_transaction_timestamp("0xabc")) is None


def test_tx_timestamp_api_failure(monkeypatch):
    async def fake_get(client, path, params=None):
        return None  # _get swallows errors to None

    monkeypatch.setattr(blockscout_client, "_get", fake_get)
    assert _run(blockscout_client.get_transaction_timestamp("0xabc")) is None


# --- orchestrator fallback wiring (end-to-end through analyze_token_contract) ---

ADDR = "0x" + "a" * 40


def _stub_fetches(monkeypatch, *, pair, creation_tx, ts_calls):
    """Stub every concurrent fetch in analyze_token_contract; record ts lookups."""
    async def fake_pairs(addr):
        return [pair] if pair else []

    async def fake_token_info(addr):
        return {}

    async def fake_address_info(addr):
        return {"creation_transaction_hash": creation_tx} if creation_tx else {}

    async def fake_holders(addr, n=None):
        return []

    async def fake_holders_paged(addr, pages=1):
        return []

    async def fake_counters(addr):
        return None

    async def fake_smart_contract(addr):
        return None  # -> contract intel + privileges both degrade to inert, no RPC

    async def fake_ts(tx_hash):
        ts_calls.append(tx_hash)
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()

    async def fake_transfers(addr, pages=1):
        return []  # empty -> wallet-intel short-circuits, no client leak

    monkeypatch.setattr(rug_analyzer, "fetch_token_pairs", fake_pairs)
    monkeypatch.setattr(blockscout_client, "get_token_info", fake_token_info)
    monkeypatch.setattr(blockscout_client, "get_address_info", fake_address_info)
    monkeypatch.setattr(blockscout_client, "get_token_holders", fake_holders)
    monkeypatch.setattr(blockscout_client, "get_token_holders_paged", fake_holders_paged)
    monkeypatch.setattr(blockscout_client, "get_token_counters", fake_counters)
    monkeypatch.setattr(blockscout_client, "get_smart_contract", fake_smart_contract)
    monkeypatch.setattr(blockscout_client, "get_transaction_timestamp", fake_ts)
    monkeypatch.setattr(blockscout_client, "get_token_transfers", fake_transfers)
    monkeypatch.setattr(rug_analyzer.watchlist_store, "known_addresses", lambda: {})


def test_creation_age_used_when_no_pair(monkeypatch):
    ts_calls: list[str] = []
    _stub_fetches(monkeypatch, pair=None, creation_tx="0xcreate", ts_calls=ts_calls)
    resp = _run(rug_analyzer.analyze_token_contract(ADDR, include_lore=False))
    assert resp.token_age.source == "contract_creation"
    assert 5.5 < resp.token_age.age_hours < 6.5
    assert ts_calls == ["0xcreate"]  # fallback fired exactly once


def test_pair_timestamp_skips_creation_lookup(monkeypatch):
    import time

    ts_calls: list[str] = []
    pair = {"pairCreatedAt": int((time.time() - 5 * 86400) * 1000), "chainId": "robinhood"}
    _stub_fetches(monkeypatch, pair=pair, creation_tx="0xcreate", ts_calls=ts_calls)
    resp = _run(rug_analyzer.analyze_token_contract(ADDR, include_lore=False))
    assert resp.token_age.source == "pair_created_at"
    assert ts_calls == []  # creation lookup must be skipped when a pair timestamp exists


def test_unknown_age_when_no_pair_and_no_creation_tx(monkeypatch):
    ts_calls: list[str] = []
    _stub_fetches(monkeypatch, pair=None, creation_tx=None, ts_calls=ts_calls)
    resp = _run(rug_analyzer.analyze_token_contract(ADDR, include_lore=False))
    assert resp.token_age.source is None
    assert resp.token_age.age_days is None
    assert ts_calls == []  # no creation tx -> no lookup attempted
