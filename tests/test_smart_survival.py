"""Unit tests for M16 smart-wallet cross-token survival (bounded, mocked network)."""

import asyncio

import pytest

from app.services import blockscout_client, wallet_intel


def _run(coro):
    return asyncio.run(coro)


def _holding(addr, value, ttype="ERC-20"):
    return {"token": {"address_hash": addr, "type": ttype}, "value": str(value)}


# --- _count_surviving_tokens (bounded, defensive) ---


def test_survival_counts_distinct_positive_erc20(monkeypatch):
    async def fake_holdings(addr):
        return [
            _holding("0xt1", 100),
            _holding("0xt2", 5),
            _holding("0xt3", 0),          # zero balance -> not surviving
            _holding("0xnft", 1, "ERC-721"),  # not ERC-20
        ]

    monkeypatch.setattr(blockscout_client, "get_address_token_holdings", fake_holdings)
    counts = _run(wallet_intel._count_surviving_tokens(["0xw"]))
    assert counts == {"0xw": 2}


def test_survival_excludes_token_under_analysis(monkeypatch):
    async def fake_holdings(addr):
        return [_holding("0xthis", 100), _holding("0xother", 100)]

    monkeypatch.setattr(blockscout_client, "get_address_token_holdings", fake_holdings)
    counts = _run(wallet_intel._count_surviving_tokens(["0xw"], exclude_token="0xTHIS"))
    assert counts == {"0xw": 1}


def test_survival_lookup_failure_degrades_to_zero(monkeypatch):
    async def boom(addr):
        raise RuntimeError("api down")

    monkeypatch.setattr(blockscout_client, "get_address_token_holdings", boom)
    counts = _run(wallet_intel._count_surviving_tokens(["0xw"]))
    assert counts == {"0xw": 0}


def test_survival_empty_wallet_list_makes_no_call(monkeypatch):
    called = {"n": 0}

    async def fake_holdings(addr):
        called["n"] += 1
        return []

    monkeypatch.setattr(blockscout_client, "get_address_token_holdings", fake_holdings)
    assert _run(wallet_intel._count_surviving_tokens([])) == {}
    assert called["n"] == 0


# --- profile_token_wallets: survival lifts a wallet above threshold, bounded lookups ---


def _norm(frm, to, value):
    # Already-normalized transfer record (as the orchestrator passes in).
    return {"from": frm, "to": to, "value": value, "ts": "t", "method": "transfer", "block": 1}


def test_survival_lifts_wallet_above_threshold(monkeypatch):
    # 0xsmart enters first and holds -> 65 on-token; +survival should clear 70.
    transfers = [
        _norm(wallet_intel.ZERO, "0xsmart", 100),
        _norm(wallet_intel.ZERO, "0xb", 50),
        _norm(wallet_intel.ZERO, "0xc", 50),
    ]

    async def fake_holdings(addr):
        # 0xsmart survives on 3 other tokens; others hold nothing.
        if addr == "0xsmart":
            return [_holding(f"0xt{i}", 10) for i in range(3)]
        return []

    monkeypatch.setattr(blockscout_client, "get_address_token_holdings", fake_holdings)
    monkeypatch.setattr(wallet_intel.watchlist_store, "upsert_wallet", lambda *a, **k: None)
    monkeypatch.setattr(wallet_intel.watchlist_store, "record_activity", lambda *a, **k: None)

    _insiders, smart = _run(
        wallet_intel.profile_token_wallets(
            "0xtoken", None, {"0xsmart": 5.0}, transfers=transfers, known_contracts=set()
        )
    )
    addrs = {s.address for s in smart}
    assert "0xsmart" in addrs
    sw = next(s for s in smart if s.address == "0xsmart")
    assert sw.surviving_tokens == 3
    assert sw.proxy_score >= 70


def test_survival_lookups_are_capped(monkeypatch):
    # Many near-threshold candidates, but only N survival lookups may fire.
    monkeypatch.setattr(wallet_intel.settings, "smart_wallet_survival_candidates", 2)
    # 6 early holders, all with the same "held" profile -> all near-threshold candidates.
    transfers = [_norm(wallet_intel.ZERO, f"0x{i}", 100) for i in range(6)]
    looked_up: list[str] = []

    async def fake_holdings(addr):
        looked_up.append(addr)
        return []

    monkeypatch.setattr(blockscout_client, "get_address_token_holdings", fake_holdings)
    monkeypatch.setattr(wallet_intel.watchlist_store, "upsert_wallet", lambda *a, **k: None)
    monkeypatch.setattr(wallet_intel.watchlist_store, "record_activity", lambda *a, **k: None)

    _run(
        wallet_intel.profile_token_wallets(
            "0xtoken", None, {}, transfers=transfers, known_contracts=set()
        )
    )
    assert len(looked_up) <= 2


def test_contracts_and_creator_excluded_from_candidates(monkeypatch):
    transfers = [
        _norm(wallet_intel.ZERO, "0xlp", 100),
        _norm(wallet_intel.ZERO, "0xcreator", 100),
        _norm(wallet_intel.ZERO, "0xreal", 100),
    ]
    looked_up: list[str] = []

    async def fake_holdings(addr):
        looked_up.append(addr)
        return []

    monkeypatch.setattr(blockscout_client, "get_address_token_holdings", fake_holdings)
    monkeypatch.setattr(wallet_intel.watchlist_store, "upsert_wallet", lambda *a, **k: None)
    monkeypatch.setattr(wallet_intel.watchlist_store, "record_activity", lambda *a, **k: None)

    _run(
        wallet_intel.profile_token_wallets(
            "0xtoken", "0xCreator", {}, transfers=transfers,
            known_contracts={"0xlp"},
        )
    )
    # Neither the LP contract nor the creator should ever get a survival lookup.
    assert "0xlp" not in looked_up
    assert "0xcreator" not in looked_up
