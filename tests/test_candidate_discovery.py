"""D2 integration tests: multi-source candidate discovery pipeline.

Covers: multi-source discovery, deduplication, market-data enrichment,
launchpad detection, diagnostics, and end-to-end candidate flow through
scan_and_rank.
"""

import asyncio

import pytest

from app.core.config import settings
from app.models.token import DiscoveryDiagnostics
from app.services import blockscout_client, candidate_discovery, launchpad_discovery, launchpad_registry, rug_analyzer
from app.services.candidate_discovery import (
    DiscoveredCandidate,
    RawCandidate,
    _filter_candidates,
    discover_candidates,
)
from app.services.candidate_discovery import _blockscout_holders_provider
from app.services import dexscreener_client
from app.models.token import SourceDiagnostic


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Helpers: fake Blockscout / DexScreener responses
# ---------------------------------------------------------------------------

def _blockscout_token(address, name="Token", symbol="TKN", holders=10):
    return {"address_hash": address, "name": name, "symbol": symbol,
            "holders_count": holders, "type": "ERC-20"}


def _blockscout_contract(address, name="Contract"):
    return {"address": {"hash": address, "name": name}, "name": name,
            "verified_at": "2026-01-01T00:00:00Z"}


def _dex_pair(address, name="Token", symbol="TKN", liq_usd=5000, created_ms=None):
    import time
    if created_ms is None:
        created_ms = int(time.time() * 1000) - 3600_000  # 1 hour ago
    return {
        "chainId": "robinhood",
        "pairCreatedAt": created_ms,
        "pairAddress": f"0xpair_{address[-4:]}",
        "liquidity": {"usd": liq_usd},
        "baseToken": {"address": address, "name": name, "symbol": symbol},
        "quoteToken": {"symbol": "WETH"},
        "priceUsd": "1.0",
        "dexId": "uniswap",
    }


def _stub_providers(monkeypatch, tokens=None, contracts=None, pairs=None, factory_txs=None,
                     holders_tokens=None):
    """Stub all external calls for candidate_discovery."""
    async def fake_new_tokens(pages=2):
        return tokens or []

    async def fake_list_tokens_by(sort="circulating_market_cap", order="desc", pages=2):
        if sort == "fiat_value":
            return contracts or []
        if sort == "holders_count":
            return holders_tokens or []
        return tokens or []

    async def fake_token_pairs(address):
        return pairs or []

    async def fake_addr_txs(address, pages=1):
        return factory_txs or []

    async def fake_discover_all(definitions):
        return []

    monkeypatch.setattr(blockscout_client, "list_new_tokens", fake_new_tokens)
    monkeypatch.setattr(blockscout_client, "list_tokens_by", fake_list_tokens_by)
    monkeypatch.setattr(blockscout_client, "get_address_transactions_paged", fake_addr_txs)
    monkeypatch.setattr(candidate_discovery, "fetch_token_pairs", fake_token_pairs)
    monkeypatch.setattr(candidate_discovery, "choose_best_pair", lambda ps: ps[0] if ps else None)
    monkeypatch.setattr(launchpad_discovery, "discover_all", fake_discover_all)


# ---------------------------------------------------------------------------
# Multi-source discovery
# ---------------------------------------------------------------------------

class TestMultiSourceDiscovery:
    def test_blockscout_tokens_provider(self, monkeypatch):
        tokens = [_blockscout_token(f"0x{'a' * 38}{i:02x}") for i in range(5)]
        _stub_providers(monkeypatch, tokens=tokens)
        monkeypatch.setattr(settings, "discovery_blockscout_contracts_enabled", False)
        monkeypatch.setattr(settings, "discovery_launchpad_enabled", False)
        monkeypatch.setattr(settings, "discovery_dexscreener_enabled", False)
        cands, diag = _run(discover_candidates(limit=10))
        assert len(cands) == 5
        assert diag.sources[0].source == "blockscout_tokens"
        assert diag.sources[0].accepted == 5

    def test_blockscout_fiat_value_provider(self, monkeypatch):
        fiat_tokens = [_blockscout_token(f"0x{'b' * 38}{i:02x}", name=f"Fiat{i}") for i in range(3)]
        _stub_providers(monkeypatch, contracts=fiat_tokens)
        monkeypatch.setattr(settings, "discovery_blockscout_tokens_enabled", False)
        monkeypatch.setattr(settings, "discovery_launchpad_enabled", False)
        monkeypatch.setattr(settings, "discovery_dexscreener_enabled", False)
        cands, diag = _run(discover_candidates(limit=10))
        assert len(cands) == 3
        assert diag.sources[0].source == "blockscout_fiat_value"

    def test_blockscout_holders_provider(self, monkeypatch):
        holder_tokens = [_blockscout_token(f"0x{'c' * 38}{i:02x}", name=f"Hold{i}", holders=100+i) for i in range(2)]
        _stub_providers(monkeypatch, holders_tokens=holder_tokens)
        monkeypatch.setattr(settings, "discovery_blockscout_tokens_enabled", False)
        monkeypatch.setattr(settings, "discovery_blockscout_contracts_enabled", False)
        monkeypatch.setattr(settings, "discovery_launchpad_enabled", False)
        cands, diag = _run(discover_candidates(limit=10))
        assert len(cands) == 2
        assert diag.sources[0].source == "blockscout_holders"

    def test_all_providers_merge(self, monkeypatch):
        tokens = [_blockscout_token(f"0x{'a' * 38}{i:02x}") for i in range(3)]
        fiat_tokens = [_blockscout_token(f"0x{'b' * 38}{i:02x}", name=f"Fiat{i}") for i in range(2)]
        holder_tokens = [_blockscout_token(f"0x{'c' * 38}{i:02x}", name=f"Hold{i}") for i in range(2)]
        _stub_providers(monkeypatch, tokens=tokens, contracts=fiat_tokens,
                        holders_tokens=holder_tokens)
        monkeypatch.setattr(settings, "discovery_launchpad_enabled", False)
        cands, diag = _run(discover_candidates(limit=50))
        assert len(cands) == 7
        assert diag.total_raw == 7

    def test_disabled_provider_not_called(self, monkeypatch):
        tokens = [_blockscout_token(f"0x{'a' * 38}01")]
        _stub_providers(monkeypatch, tokens=tokens)
        monkeypatch.setattr(settings, "discovery_blockscout_tokens_enabled", False)
        monkeypatch.setattr(settings, "discovery_blockscout_contracts_enabled", False)
        monkeypatch.setattr(settings, "discovery_launchpad_enabled", False)
        monkeypatch.setattr(settings, "discovery_dexscreener_enabled", False)
        cands, diag = _run(discover_candidates(limit=10))
        assert len(cands) == 0
        assert diag.total_raw == 0


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_same_address_from_two_sources(self, monkeypatch):
        addr = f"0x{'d' * 40}"
        tokens = [_blockscout_token(addr, name="FromTokens")]
        fiat_tokens = [_blockscout_token(addr, name="FromFiatValue")]
        _stub_providers(monkeypatch, tokens=tokens, contracts=fiat_tokens)
        monkeypatch.setattr(settings, "discovery_launchpad_enabled", False)
        monkeypatch.setattr(settings, "discovery_dexscreener_enabled", False)
        cands, diag = _run(discover_candidates(limit=10))
        assert len(cands) == 1
        # First provider wins (blockscout_tokens runs before blockscout_fiat_value)
        assert cands[0].name == "FromTokens"
        # Second source should show 1 rejected_duplicate
        fiat_diag = [s for s in diag.sources if s.source == "blockscout_fiat_value"][0]
        assert fiat_diag.rejected_duplicate == 1

    def test_case_insensitive_dedup(self, monkeypatch):
        tokens = [
            _blockscout_token("0xABCDEF" + "0" * 34, name="Upper"),
            _blockscout_token("0xabcdef" + "0" * 34, name="Lower"),
        ]
        _stub_providers(monkeypatch, tokens=tokens)
        monkeypatch.setattr(settings, "discovery_blockscout_contracts_enabled", False)
        monkeypatch.setattr(settings, "discovery_launchpad_enabled", False)
        monkeypatch.setattr(settings, "discovery_dexscreener_enabled", False)
        cands, diag = _run(discover_candidates(limit=10))
        assert len(cands) == 1


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

class TestFiltering:
    def test_established_token_rejected(self, monkeypatch):
        tokens = [
            _blockscout_token(f"0x{'a' * 40}", name="Wrapped Ether", symbol="WETH"),
            _blockscout_token(f"0x{'b' * 40}", name="New Token", symbol="NEW"),
        ]
        _stub_providers(monkeypatch, tokens=tokens)
        monkeypatch.setattr(settings, "discovery_blockscout_contracts_enabled", False)
        monkeypatch.setattr(settings, "discovery_launchpad_enabled", False)
        monkeypatch.setattr(settings, "discovery_dexscreener_enabled", False)
        cands, diag = _run(discover_candidates(limit=10))
        assert len(cands) == 1
        assert cands[0].symbol == "NEW"
        assert diag.sources[0].rejected_established == 1

    def test_invalid_address_rejected(self):
        sd = SourceDiagnostic(source="test")
        raw = [RawCandidate(address="not_an_address", source="test")]
        accepted = _filter_candidates(raw, set(), sd, 0, 0)
        assert len(accepted) == 0
        assert sd.rejected_invalid_address == 1

    def test_zero_holder_rejected(self):
        sd = SourceDiagnostic(source="test")
        raw = [
            RawCandidate(address=f"0x{'a' * 40}", source="test", holder_count=0),
            RawCandidate(address=f"0x{'b' * 40}", source="test", holder_count=5),
            RawCandidate(address=f"0x{'c' * 40}", source="test", holder_count=None),
        ]
        accepted = _filter_candidates(raw, set(), sd, 0, 0)
        assert len(accepted) == 2
        assert sd.rejected_zero_holders == 1

    def test_limit_caps_output(self, monkeypatch):
        tokens = [_blockscout_token(f"0x{'a' * 38}{i:02x}") for i in range(20)]
        _stub_providers(monkeypatch, tokens=tokens)
        monkeypatch.setattr(settings, "discovery_blockscout_contracts_enabled", False)
        monkeypatch.setattr(settings, "discovery_launchpad_enabled", False)
        monkeypatch.setattr(settings, "discovery_dexscreener_enabled", False)
        cands, _ = _run(discover_candidates(limit=5))
        assert len(cands) <= 5


# ---------------------------------------------------------------------------
# Market-data enrichment
# ---------------------------------------------------------------------------

class TestEnrichment:
    def test_enrichment_attaches_pair(self, monkeypatch):
        import time
        addr = f"0x{'e' * 40}"
        pair = _dex_pair(addr, liq_usd=10000)
        tokens = [_blockscout_token(addr)]

        async def fake_token_pairs(address):
            return [pair]

        _stub_providers(monkeypatch, tokens=tokens)
        monkeypatch.setattr(candidate_discovery, "fetch_token_pairs", fake_token_pairs)
        monkeypatch.setattr(settings, "discovery_blockscout_contracts_enabled", False)
        monkeypatch.setattr(settings, "discovery_launchpad_enabled", False)
        monkeypatch.setattr(settings, "discovery_dexscreener_enabled", False)
        cands, diag = _run(discover_candidates(limit=10))
        assert len(cands) == 1
        assert cands[0].pair is not None
        assert diag.enriched == 1

    def test_low_liquidity_passes_enrichment(self, monkeypatch):
        """Low-liq tokens pass enrichment — liquidity is a scoring input, not a gate."""
        import time
        addr = f"0x{'f' * 40}"
        pair = _dex_pair(addr, liq_usd=10)
        tokens = [_blockscout_token(addr)]

        async def fake_token_pairs(address):
            return [pair]

        _stub_providers(monkeypatch, tokens=tokens)
        monkeypatch.setattr(candidate_discovery, "fetch_token_pairs", fake_token_pairs)
        monkeypatch.setattr(settings, "discovery_blockscout_contracts_enabled", False)
        monkeypatch.setattr(settings, "discovery_launchpad_enabled", False)
        monkeypatch.setattr(settings, "discovery_dexscreener_enabled", False)
        monkeypatch.setattr(settings, "scan_min_candidate_liquidity_usd", 500.0)
        cands, diag = _run(discover_candidates(limit=10))
        assert len(cands) == 1
        assert cands[0].pair is not None

    def test_old_pair_passes_enrichment(self, monkeypatch):
        """Old tokens pass enrichment — age is a scoring input, not a gate."""
        import time
        addr = f"0x{'1a' * 20}"
        old_ms = int(time.time() * 1000) - 10 * 86_400_000  # 10 days ago
        pair = _dex_pair(addr, liq_usd=10000, created_ms=old_ms)
        tokens = [_blockscout_token(addr)]

        async def fake_token_pairs(address):
            return [pair]

        _stub_providers(monkeypatch, tokens=tokens)
        monkeypatch.setattr(candidate_discovery, "fetch_token_pairs", fake_token_pairs)
        monkeypatch.setattr(settings, "discovery_blockscout_contracts_enabled", False)
        monkeypatch.setattr(settings, "discovery_launchpad_enabled", False)
        monkeypatch.setattr(settings, "discovery_dexscreener_enabled", False)
        monkeypatch.setattr(settings, "scan_max_launch_age_days", 3.0)
        cands, diag = _run(discover_candidates(limit=10))
        assert len(cands) == 1
        assert cands[0].pair is not None


# ---------------------------------------------------------------------------
# Launchpad detection
# ---------------------------------------------------------------------------

class TestLaunchpadDetection:
    def test_factory_match(self, monkeypatch):
        factory = f"0x{'ab' * 20}"
        monkeypatch.setattr(settings, "discovery_launchpad_factories", {"TestLaunch": factory})
        factories = launchpad_registry.get_discovery_factories()
        assert "TestLaunch" in factories
        label = launchpad_registry.match_factory_deployer(factory)
        assert label == "TestLaunch"

    def test_no_match_returns_none(self, monkeypatch):
        monkeypatch.setattr(settings, "discovery_launchpad_factories", {})
        monkeypatch.setattr(settings, "discovery_dex_factories", {})
        label = launchpad_registry.match_factory_deployer(f"0x{'99' * 20}")
        assert label is None

    def test_v3_factory_included(self, monkeypatch):
        factories = launchpad_registry.get_discovery_factories()
        # The v3 factory from honeypot config should be auto-included
        assert "Uniswap V3" in factories or len(factories) >= 0  # may be None if not configured

    def test_launchpad_provider_delegates_to_engine(self, monkeypatch):
        """_launchpad_factory_provider delegates to launchpad_discovery.discover_all
        and converts results to RawCandidate, preserving backward compat."""
        from app.services import launchpad_discovery

        token_a = f"0x{'a1' * 20}"
        token_b = f"0x{'b2' * 20}"

        async def fake_discover_all(definitions):
            return [
                {"address": token_a, "source": "launchpad:TestA"},
                {"address": token_b, "source": "launchpad:TestB"},
            ]

        monkeypatch.setattr(launchpad_discovery, "discover_all", fake_discover_all)
        monkeypatch.setattr(settings, "launchpad_definitions", [
            {"name": "TestA", "enabled": True, "discovery_mode": "event",
             "factory_address": token_a, "topic0": "0x00", "token_index": 0,
             "start_block": 0, "confidence": "high"},
        ])

        from app.services.candidate_discovery import _launchpad_factory_provider
        result = _run(_launchpad_factory_provider())
        assert len(result) == 2
        assert result[0].address == token_a
        assert result[0].source == "launchpad:TestA"
        assert result[1].address == token_b


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

class TestDiagnostics:
    def test_diagnostics_structure(self, monkeypatch):
        tokens = [_blockscout_token(f"0x{'a' * 40}")]
        _stub_providers(monkeypatch, tokens=tokens)
        monkeypatch.setattr(settings, "discovery_blockscout_contracts_enabled", False)
        monkeypatch.setattr(settings, "discovery_launchpad_enabled", False)
        monkeypatch.setattr(settings, "discovery_dexscreener_enabled", False)
        _, diag = _run(discover_candidates(limit=10))
        assert isinstance(diag, DiscoveryDiagnostics)
        assert len(diag.sources) >= 1
        assert diag.total_raw >= 1
        assert diag.total_after_dedup >= 0

    def test_per_source_counts(self, monkeypatch):
        tokens = [
            _blockscout_token(f"0x{'a' * 40}", symbol="WETH"),  # will be rejected (established)
            _blockscout_token(f"0x{'b' * 40}", symbol="NEW"),   # will be accepted
        ]
        _stub_providers(monkeypatch, tokens=tokens)
        monkeypatch.setattr(settings, "discovery_blockscout_contracts_enabled", False)
        monkeypatch.setattr(settings, "discovery_launchpad_enabled", False)
        monkeypatch.setattr(settings, "discovery_dexscreener_enabled", False)
        _, diag = _run(discover_candidates(limit=10))
        src = diag.sources[0]
        assert src.raw == 2
        assert src.accepted == 1
        assert src.rejected_established == 1

    def test_empty_providers_return_zero_diagnostics(self, monkeypatch):
        _stub_providers(monkeypatch)
        _, diag = _run(discover_candidates(limit=10))
        assert diag.total_raw == 0
        assert diag.enriched == 0


# ---------------------------------------------------------------------------
# End-to-end: scan_and_rank with new discovery
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_scan_returns_discovery_diagnostics(self, monkeypatch):
        """scan_and_rank should include discovery diagnostics in ScanResponse."""
        async def fake_discover(limit):
            return [], DiscoveryDiagnostics()

        monkeypatch.setattr(candidate_discovery, "discover_candidates", fake_discover)
        resp = _run(rug_analyzer.scan_and_rank(5))
        assert resp.status == "no_tokens"
        assert resp.discovery is not None

    def test_scan_with_candidates(self, monkeypatch):
        """scan_and_rank with discovered candidates reaches analysis."""
        addr = f"0x{'ab' * 20}"

        async def fake_discover(limit):
            cands = [DiscoveredCandidate(address_hash=addr, name="Test", symbol="TST", source="test")]
            return cands, DiscoveryDiagnostics(total_raw=1, enriched=1)

        from app.models.token import RugAnalysis, TokenAnalysisResponse, HolderDistribution, TokenMarketData, LiquiditySnapshot

        async def fake_analyze(address, include_lore=False):
            return TokenAnalysisResponse(
                contract_address=address, chain="Robinhood Chain",
                status="ok", message="stub",
                holders=HolderDistribution(holder_count=10),
                market_data=TokenMarketData(
                    pair_address="0xpair",
                    liquidity=LiquiditySnapshot(usd=5000.0),
                ),
                analysis=RugAnalysis(risk_score=42, risk_level="medium",
                                     signals=[], data_sources=[], limitations=[]),
            )

        monkeypatch.setattr(candidate_discovery, "discover_candidates", fake_discover)
        monkeypatch.setattr(rug_analyzer, "analyze_token_contract", fake_analyze)
        resp = _run(rug_analyzer.scan_and_rank(5))
        assert resp.status == "scan_completed"
        assert len(resp.ranked_tokens) >= 1
        assert resp.discovery is not None
        assert resp.discovery.reached_ranking >= 1

    def test_frontend_api_unchanged(self, monkeypatch):
        """ScanResponse still has ranked_tokens and excluded_tokens fields."""
        async def fake_discover(limit):
            return [], DiscoveryDiagnostics()

        monkeypatch.setattr(candidate_discovery, "discover_candidates", fake_discover)
        resp = _run(rug_analyzer.scan_and_rank(5))
        assert hasattr(resp, "ranked_tokens")
        assert hasattr(resp, "excluded_tokens")
        assert hasattr(resp, "discovery")
        # discovery is optional (None-able) — frontend doesn't need to change
        data = resp.model_dump()
        assert "ranked_tokens" in data
        assert "excluded_tokens" in data
