"""Unit tests for M18 persistent deployer reputation.

Two layers:
- watchlist_store deployer table — persist launch history + classification, TTL staleness.
- rug_analyzer._scan_creator_launches — a fresh cache hit skips the live creator scan.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from app.core.config import settings
from app.services import rug_analyzer, watchlist_store


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _temp_db():
    tmp = Path(tempfile.mkdtemp()) / "watchlist.db"
    watchlist_store.reset_for_tests(str(tmp))
    yield
    watchlist_store.reset_for_tests()


DEPLOYER = "0x" + "de" * 20
LAUNCHES = [
    {"address": "0x1", "name": "A", "symbol": "A", "liquidity_usd": 0.0, "outcome": "likely_rugged"},
    {"address": "0x2", "name": "B", "symbol": "B", "liquidity_usd": 0.0, "outcome": "likely_rugged"},
    {"address": "0x3", "name": "C", "symbol": "C", "liquidity_usd": 0.0, "outcome": "likely_rugged"},
]


# --- store round-trip ---


def test_upsert_and_get_deployer_round_trip():
    watchlist_store.upsert_deployer(
        DEPLOYER, reputation="serial_rugger", tokens_launched=3,
        tokens_rugged=3, tokens_alive=0, launched_tokens=LAUNCHES,
    )
    rec = watchlist_store.get_deployer(DEPLOYER)
    assert rec is not None
    assert rec["reputation"] == "serial_rugger"
    assert rec["tokens_rugged"] == 3
    assert len(rec["launched_tokens"]) == 3
    # Retrieved without any live scan.
    assert rec["launched_tokens"][0]["outcome"] == "likely_rugged"


def test_get_deployer_case_insensitive():
    watchlist_store.upsert_deployer(
        DEPLOYER, reputation="clean", tokens_launched=1,
        tokens_rugged=0, tokens_alive=1, launched_tokens=[LAUNCHES[0]],
    )
    assert watchlist_store.get_deployer(DEPLOYER.upper()) is not None


def test_get_deployer_missing_returns_none():
    assert watchlist_store.get_deployer("0xnobody") is None
    assert watchlist_store.get_deployer("") is None


def test_get_deployer_ttl_staleness():
    watchlist_store.upsert_deployer(
        DEPLOYER, reputation="serial_rugger", tokens_launched=3,
        tokens_rugged=3, tokens_alive=0, launched_tokens=LAUNCHES,
    )
    # A zero max-age makes any stored record stale -> treated as a miss (forces refresh).
    assert watchlist_store.get_deployer(DEPLOYER, max_age_seconds=0) is None
    # A generous window is a hit.
    assert watchlist_store.get_deployer(DEPLOYER, max_age_seconds=10_000) is not None


# --- cache hit skips the live scan ---


def test_cache_hit_skips_live_scan(monkeypatch):
    watchlist_store.upsert_deployer(
        DEPLOYER, reputation="serial_rugger", tokens_launched=3,
        tokens_rugged=3, tokens_alive=0, launched_tokens=LAUNCHES,
    )

    async def boom(*a, **k):
        raise AssertionError("live creator scan must not run on a fresh cache hit")

    monkeypatch.setattr(rug_analyzer.blockscout_client, "get_address_transactions_paged", boom)
    monkeypatch.setattr(settings, "deployer_reputation_ttl_hours", 10_000)

    launched, from_cache = _run(rug_analyzer._scan_creator_launches(DEPLOYER, "0xtoken"))
    assert from_cache is True
    assert len(launched) == 3
    assert all(t.outcome == "likely_rugged" for t in launched)


def test_stale_cache_triggers_live_scan(monkeypatch):
    watchlist_store.upsert_deployer(
        DEPLOYER, reputation="serial_rugger", tokens_launched=3,
        tokens_rugged=3, tokens_alive=0, launched_tokens=LAUNCHES,
    )
    scanned = {"n": 0}

    async def fake_txs(addr, pages=1):
        scanned["n"] += 1
        return []  # no launches found on the (forced) live scan

    monkeypatch.setattr(rug_analyzer.blockscout_client, "get_address_transactions_paged", fake_txs)
    monkeypatch.setattr(settings, "deployer_reputation_ttl_hours", 0)  # everything stale

    _launched, from_cache = _run(rug_analyzer._scan_creator_launches(DEPLOYER, "0xtoken"))
    assert from_cache is False
    assert scanned["n"] == 1  # stale entry forced exactly one live scan


def test_no_creator_returns_empty_no_scan(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("no scan when creator is None")

    monkeypatch.setattr(rug_analyzer.blockscout_client, "get_address_transactions_paged", boom)
    launched, from_cache = _run(rug_analyzer._scan_creator_launches(None, "0xtoken"))
    assert launched == []
    assert from_cache is False
