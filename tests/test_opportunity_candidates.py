"""D1 regression tests: candidate discovery filtering.

Tests age, liquidity, dedup, and established-token filtering via the
multi-source discovery pipeline (candidate_discovery.discover_candidates).
"""

import asyncio
import time

from app.core.config import settings
from app.models.token import DiscoveryDiagnostics
from app.services import blockscout_client, candidate_discovery
from app.services.candidate_discovery import DiscoveredCandidate, discover_candidates


def _run(coro):
    return asyncio.run(coro)


def _blockscout_token(address, name="Token", symbol="TKN", holders=10):
    return {"address_hash": address, "name": name, "symbol": symbol,
            "holders_count": holders, "type": "ERC-20"}


def _dex_pair(address, liq_usd=5000, created_ms=None):
    if created_ms is None:
        created_ms = int(time.time() * 1000) - 3600_000
    return {
        "chainId": "robinhood",
        "pairCreatedAt": created_ms,
        "pairAddress": f"0xpair_{address[-4:]}",
        "liquidity": {"usd": liq_usd},
        "baseToken": {"address": address, "name": "T", "symbol": "T"},
        "quoteToken": {"symbol": "WETH"},
        "priceUsd": "1.0",
        "dexId": "uniswap",
    }


def _stub_single(monkeypatch, tokens, pair_map=None):
    """Stub blockscout_tokens as the only enabled provider + DexScreener enrichment."""
    async def fake_new_tokens(pages=2):
        return tokens

    async def fake_list_tokens_by(sort="circulating_market_cap", order="desc", pages=2):
        return []

    async def fake_token_pairs(address):
        if pair_map and address in pair_map:
            return [pair_map[address]]
        return []

    monkeypatch.setattr(blockscout_client, "list_new_tokens", fake_new_tokens)
    monkeypatch.setattr(blockscout_client, "list_tokens_by", fake_list_tokens_by)
    monkeypatch.setattr(blockscout_client, "get_address_transactions_paged",
                        lambda addr, pages=1: asyncio.coroutine(lambda: [])())
    monkeypatch.setattr(candidate_discovery, "fetch_token_pairs", fake_token_pairs)
    monkeypatch.setattr(candidate_discovery, "choose_best_pair", lambda ps: ps[0] if ps else None)
    monkeypatch.setattr(settings, "discovery_blockscout_contracts_enabled", False)
    monkeypatch.setattr(settings, "discovery_launchpad_enabled", False)
    monkeypatch.setattr(settings, "discovery_dexscreener_enabled", False)


def test_stale_launch_passes_discovery(monkeypatch):
    """Old tokens now pass discovery — age is a scoring input, not a gate."""
    now_ms = int(time.time() * 1000)
    addr_stale = f"0x{'a1' * 20}"
    addr_fresh = f"0x{'b2' * 20}"
    monkeypatch.setattr(settings, "scan_max_launch_age_days", 3.0)
    _stub_single(monkeypatch,
        tokens=[_blockscout_token(addr_stale), _blockscout_token(addr_fresh)],
        pair_map={
            addr_stale: _dex_pair(addr_stale, created_ms=now_ms - 90 * 86_400_000),
            addr_fresh: _dex_pair(addr_fresh, created_ms=now_ms - 1 * 86_400_000),
        })
    cands, _ = _run(discover_candidates())
    addrs = [c.address_hash for c in cands]
    assert addr_fresh in addrs
    assert addr_stale in addrs


def test_low_liquidity_passes_discovery(monkeypatch):
    """Low-liq tokens now pass discovery — liquidity is a scoring input, not a gate."""
    addr_dead = f"0x{'c3' * 20}"
    addr_live = f"0x{'d4' * 20}"
    monkeypatch.setattr(settings, "scan_min_candidate_liquidity_usd", 500.0)
    _stub_single(monkeypatch,
        tokens=[_blockscout_token(addr_dead), _blockscout_token(addr_live)],
        pair_map={
            addr_dead: _dex_pair(addr_dead, liq_usd=10.0),
            addr_live: _dex_pair(addr_live, liq_usd=5000.0),
        })
    cands, _ = _run(discover_candidates())
    addrs = [c.address_hash for c in cands]
    assert addr_live in addrs
    assert addr_dead in addrs


def test_no_tokens_returns_empty(monkeypatch):
    _stub_single(monkeypatch, tokens=[])
    cands, _ = _run(discover_candidates())
    assert cands == []


def test_established_tokens_skipped(monkeypatch):
    addr_weth = f"0x{'e5' * 20}"
    addr_new = f"0x{'f6' * 20}"
    _stub_single(monkeypatch,
        tokens=[
            _blockscout_token(addr_weth, name="Wrapped Ether", symbol="WETH"),
            _blockscout_token(addr_new, name="New Token", symbol="NEW"),
        ])
    cands, _ = _run(discover_candidates())
    assert len(cands) == 1
    assert cands[0].symbol == "NEW"
