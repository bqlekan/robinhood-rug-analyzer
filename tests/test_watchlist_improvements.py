"""Unit tests for M21 watchlist improvements.

Three layers:
- watchlist_store.get_watchlist — kind filter + whitelisted sort + prior_tokens enrichment.
- watchlist_store.get_wallet — single-wallet detail carries prior_tokens.
- api.routes — /watchlist (kind/sort params) and /watchlist/refresh contract.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from app.api import routes
from app.models.token import WalletActivity
from app.services import watchlist_store, wallet_intel


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _temp_db():
    tmp = Path(tempfile.mkdtemp()) / "watchlist.db"
    watchlist_store.reset_for_tests(str(tmp))
    yield
    watchlist_store.reset_for_tests()


def _seed(wallet: str, kind: str, proxy_score: int, tokens: list[str]) -> None:
    watchlist_store.upsert_wallet(wallet, kind, proxy_score=proxy_score)
    watchlist_store.record_activity(
        wallet,
        [WalletActivity(token_address=t, symbol="X", timestamp=f"2024-01-0{i+1}T00:00:00Z")
         for i, t in enumerate(tokens)],
    )


# --- store: filter + sort ---


def test_filter_by_kind():
    _seed("0xsmart", "smart", 90, ["0xA"])
    _seed("0xins", "insider", 50, ["0xB"])
    smart = watchlist_store.get_watchlist(kind="smart")
    assert [e.address for e in smart] == ["0xsmart"]
    insider = watchlist_store.get_watchlist(kind="insider")
    assert [e.address for e in insider] == ["0xins"]


def test_sort_by_score_desc():
    _seed("0xlow", "smart", 70, ["0xA"])
    _seed("0xhigh", "smart", 95, ["0xB"])
    ordered = watchlist_store.get_watchlist(kind="smart", sort="score")
    assert [e.address for e in ordered] == ["0xhigh", "0xlow"]


def test_sort_by_recency():
    # 0xold refreshed first, then 0xnew -> recency puts 0xnew first regardless of score.
    _seed("0xold", "smart", 99, ["0xA"])
    _seed("0xnew", "smart", 10, ["0xB"])
    ordered = watchlist_store.get_watchlist(kind="smart", sort="recency")
    assert ordered[0].address == "0xnew"


def test_unknown_sort_falls_back_to_score():
    _seed("0xlow", "smart", 70, ["0xA"])
    _seed("0xhigh", "smart", 95, ["0xB"])
    ordered = watchlist_store.get_watchlist(kind="smart", sort="not-a-key")
    assert [e.address for e in ordered] == ["0xhigh", "0xlow"]


# --- store: prior_tokens enrichment ---


def test_get_watchlist_enriches_prior_tokens():
    _seed("0xsmart", "smart", 90, ["0xA", "0xB", "0xC"])
    entry = watchlist_store.get_watchlist(kind="smart")[0]
    assert entry.prior_tokens == 3


def test_get_wallet_carries_prior_tokens():
    _seed("0xsmart", "smart", 90, ["0xA", "0xB"])
    entry = watchlist_store.get_wallet("0xsmart")
    assert entry is not None
    assert entry.prior_tokens == 2


def test_get_wallet_missing_is_none():
    assert watchlist_store.get_wallet("0xnope") is None


# --- endpoint contract ---


def test_endpoint_filters_by_kind():
    _seed("0xsmart", "smart", 90, ["0xA"])
    _seed("0xins", "insider", 50, ["0xB"])
    resp = _run(routes.get_watchlist(kind="smart"))
    assert [w.address for w in resp.smart_wallets] == ["0xsmart"]
    assert resp.insider_wallets == []  # filtered out


def test_endpoint_both_kinds_when_unfiltered():
    _seed("0xsmart", "smart", 90, ["0xA"])
    _seed("0xins", "insider", 50, ["0xB"])
    resp = _run(routes.get_watchlist())
    assert len(resp.smart_wallets) == 1
    assert len(resp.insider_wallets) == 1


def test_endpoint_rejects_bogus_kind_gracefully():
    # A bogus kind is coerced to "both", never an error.
    _seed("0xsmart", "smart", 90, ["0xA"])
    resp = _run(routes.get_watchlist(kind="garbage"))
    assert len(resp.smart_wallets) == 1


def test_refresh_endpoint_reuses_wallet_intel(monkeypatch):
    called = {"n": 0}

    async def fake_refresh(batch):
        called["n"] = batch
        return 7

    monkeypatch.setattr(routes, "refresh_watchlisted", fake_refresh)
    result = _run(routes.refresh_watchlist())
    assert result == {"refreshed": 7}
    assert called["n"] > 0  # bounded batch passed through
