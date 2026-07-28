"""D3 pipeline stage tests: decouple discovery from display.

Validates: lite scoring formula, source diversity, deep pool cap, pagination
after ranking, backward compat, missing data → low confidence not exclusion,
deep analysis cache hits, page_size independence from discovery.
"""

import asyncio
import math

import pytest

from app.core.config import settings
from app.models.token import (
    DiscoveryDiagnostics,
    HolderDistribution,
    LiquiditySnapshot,
    RugAnalysis,
    ScanRequest,
    TokenAnalysisResponse,
    TokenMarketData,
    VolumeSnapshot,
)
from app.services import candidate_discovery, rug_analyzer
from app.services.candidate_discovery import DiscoveredCandidate
from app.services.opportunity_score import score_opportunity_lite


def _run(coro):
    return asyncio.run(coro)


def _make_candidate(addr, name="T", symbol="T", holder_count=10, pair=None, source="test", source_count=1):
    return DiscoveredCandidate(
        address_hash=addr, name=name, symbol=symbol,
        source=source, holder_count=holder_count,
        pair=pair, source_count=source_count,
    )


def _stub_discovery(monkeypatch, cands):
    """Stub discover_candidates to return the given list."""
    async def fake_discover():
        return list(cands), DiscoveryDiagnostics(total_raw=len(cands), enriched=len(cands))
    monkeypatch.setattr(candidate_discovery, "discover_candidates", fake_discover)


def _stub_analysis(monkeypatch, risk=30):
    """Stub analyze_token_contract with a simple passing result."""
    async def fake(address, include_lore=False):
        return TokenAnalysisResponse(
            contract_address=address, chain="test", status="ok", message="stub",
            holders=HolderDistribution(holder_count=50),
            market_data=TokenMarketData(
                pair_address="0xpair",
                liquidity=LiquiditySnapshot(usd=5000.0),
                volume=VolumeSnapshot(h24=1000.0),
                price_usd="1.0",
            ),
            analysis=RugAnalysis(
                risk_score=risk, risk_level="low",
                signals=[], data_sources=[], limitations=[],
            ),
            watchlist_hits=[],
        )
    monkeypatch.setattr(rug_analyzer, "analyze_token_contract", fake)


# ---------------------------------------------------------------------------
# 1. page_size does not affect discovery
# ---------------------------------------------------------------------------


class TestPageSizeIndependence:
    def test_page_size_does_not_affect_discovery(self, monkeypatch):
        call_count = [0]
        cands = [_make_candidate(f"0x{'a' * 38}{i:02x}") for i in range(5)]

        async def fake_discover():
            call_count[0] += 1
            return list(cands), DiscoveryDiagnostics(total_raw=5, enriched=5)

        monkeypatch.setattr(candidate_discovery, "discover_candidates", fake_discover)
        _stub_analysis(monkeypatch)

        _run(rug_analyzer.scan_and_rank(page_size=2))
        c1 = call_count[0]
        _run(rug_analyzer.scan_and_rank(page_size=50))
        c2 = call_count[0]
        # discover_candidates called once per scan, with no args — page_size has no effect
        assert c2 == c1 + 1


# ---------------------------------------------------------------------------
# 2. Lite scoring formula
# ---------------------------------------------------------------------------


class TestLiteScoringFormula:
    def test_known_pair_scores_higher_than_no_pair(self):
        with_pair = _make_candidate("0x01", pair={"liquidity": {"usd": 5000}})
        no_pair = _make_candidate("0x02", pair=None)
        assert score_opportunity_lite(with_pair) > score_opportunity_lite(no_pair)

    def test_higher_liquidity_scores_higher(self):
        high_liq = _make_candidate("0x01", pair={"liquidity": {"usd": 100000}})
        low_liq = _make_candidate("0x02", pair={"liquidity": {"usd": 100}})
        assert score_opportunity_lite(high_liq) > score_opportunity_lite(low_liq)

    def test_score_in_range(self):
        c = _make_candidate("0x01", pair={"liquidity": {"usd": 5000}}, holder_count=100)
        s = score_opportunity_lite(c)
        assert 0 <= s <= 100


# ---------------------------------------------------------------------------
# 3. Source diversity
# ---------------------------------------------------------------------------


class TestSourceDiversity:
    def test_multi_source_scores_higher(self):
        single = _make_candidate("0x01", source_count=1)
        multi = _make_candidate("0x02", source_count=3)
        assert score_opportunity_lite(multi) > score_opportunity_lite(single)


# ---------------------------------------------------------------------------
# 4. Deep pool cap
# ---------------------------------------------------------------------------


class TestDeepPoolCap:
    def test_deep_pool_caps_analysis_calls(self, monkeypatch):
        analyzed = []
        cands = [_make_candidate(f"0x{'a' * 38}{i:02x}") for i in range(20)]
        _stub_discovery(monkeypatch, cands)
        monkeypatch.setattr(settings, "scan_deep_pool", 5)

        async def fake(address, include_lore=False):
            analyzed.append(address)
            return TokenAnalysisResponse(
                contract_address=address, chain="test", status="ok", message="stub",
                holders=HolderDistribution(holder_count=50),
                market_data=TokenMarketData(
                    pair_address="0xpair", liquidity=LiquiditySnapshot(usd=5000.0),
                ),
                analysis=RugAnalysis(risk_score=30, risk_level="low", signals=[], data_sources=[], limitations=[]),
                watchlist_hits=[],
            )

        monkeypatch.setattr(rug_analyzer, "analyze_token_contract", fake)
        _run(rug_analyzer.scan_and_rank(page_size=15))
        assert len(analyzed) <= 5


# ---------------------------------------------------------------------------
# 5. Pagination after ranking
# ---------------------------------------------------------------------------


class TestPagination:
    def test_pagination_returns_correct_page(self, monkeypatch):
        cands = [_make_candidate(f"0x{'a' * 38}{i:02x}") for i in range(10)]
        _stub_discovery(monkeypatch, cands)
        _stub_analysis(monkeypatch)
        monkeypatch.setattr(settings, "scan_deep_pool", 10)

        resp = _run(rug_analyzer.scan_and_rank(page=2, page_size=3))
        assert resp.page == 2
        assert resp.page_size == 3
        assert resp.total_ranked >= 3
        assert len(resp.ranked_tokens) <= 3

    def test_total_pages_correct(self, monkeypatch):
        cands = [_make_candidate(f"0x{'a' * 38}{i:02x}") for i in range(7)]
        _stub_discovery(monkeypatch, cands)
        _stub_analysis(monkeypatch)
        monkeypatch.setattr(settings, "scan_deep_pool", 10)

        resp = _run(rug_analyzer.scan_and_rank(page_size=3))
        expected_pages = math.ceil(resp.total_ranked / 3)
        assert resp.total_pages == expected_pages


# ---------------------------------------------------------------------------
# 6. Backward compat: limit → page_size
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_limit_maps_to_page_size(self):
        req = ScanRequest(limit=5)
        assert req.effective_page_size() == 5
        assert req.page == 1

    def test_explicit_page_size_wins(self):
        req = ScanRequest(limit=5, page_size=10)
        assert req.effective_page_size() == 10

    def test_default_page_size(self):
        req = ScanRequest()
        assert req.effective_page_size() == 15


# ---------------------------------------------------------------------------
# 7. Missing data → low confidence, not excluded
# ---------------------------------------------------------------------------


class TestMissingDataNotExcluded:
    def test_no_pair_no_liquidity_still_ranked(self, monkeypatch):
        cands = [_make_candidate("0xnopair", pair=None)]
        _stub_discovery(monkeypatch, cands)
        monkeypatch.setattr(settings, "scan_deep_pool", 5)

        async def fake(address, include_lore=False):
            return TokenAnalysisResponse(
                contract_address=address, chain="test", status="ok", message="stub",
                holders=HolderDistribution(holder_count=50),
                market_data=TokenMarketData(pair_address=None, price_usd=None, liquidity=None),
                analysis=RugAnalysis(risk_score=60, risk_level="medium", signals=[], data_sources=[], limitations=[]),
                watchlist_hits=[],
            )

        monkeypatch.setattr(rug_analyzer, "analyze_token_contract", fake)
        resp = _run(rug_analyzer.scan_and_rank(page_size=5))
        # Token should be ranked (high_risk), not excluded
        assert len(resp.ranked_tokens) == 1
        assert resp.ranked_tokens[0].qualification_level != "excluded"


# ---------------------------------------------------------------------------
# 8. Deep analysis cache
# ---------------------------------------------------------------------------


class TestDeepAnalysisCache:
    def test_cache_prevents_reanalysis(self, monkeypatch):
        """Second scan_and_rank should hit cache for same token."""
        call_count = [0]
        addr = f"0x{'ab' * 20}"
        cands = [_make_candidate(addr)]
        _stub_discovery(monkeypatch, cands)
        monkeypatch.setattr(settings, "scan_deep_pool", 5)
        monkeypatch.setattr(settings, "deep_analysis_cache_ttl", 300.0)

        async def fake(address, include_lore=False):
            call_count[0] += 1
            return TokenAnalysisResponse(
                contract_address=address, chain="test", status="ok", message="stub",
                holders=HolderDistribution(holder_count=50),
                market_data=TokenMarketData(
                    pair_address="0xpair", liquidity=LiquiditySnapshot(usd=5000.0),
                ),
                analysis=RugAnalysis(risk_score=30, risk_level="low", signals=[], data_sources=[], limitations=[]),
                watchlist_hits=[],
            )

        monkeypatch.setattr(rug_analyzer, "analyze_token_contract", fake)

        # Clear the module-level cache before test
        rug_analyzer._deep_cache._store.clear()

        _run(rug_analyzer.scan_and_rank(page_size=5))
        first_calls = call_count[0]
        _run(rug_analyzer.scan_and_rank(page_size=5))
        # Second run should hit cache — no new analyze calls
        assert call_count[0] == first_calls

    def test_diagnostics_report_cache_hits(self, monkeypatch):
        addr = f"0x{'cd' * 20}"
        cands = [_make_candidate(addr)]
        _stub_discovery(monkeypatch, cands)
        monkeypatch.setattr(settings, "scan_deep_pool", 5)
        monkeypatch.setattr(settings, "deep_analysis_cache_ttl", 300.0)

        async def fake(address, include_lore=False):
            return TokenAnalysisResponse(
                contract_address=address, chain="test", status="ok", message="stub",
                holders=HolderDistribution(holder_count=50),
                market_data=TokenMarketData(
                    pair_address="0xpair", liquidity=LiquiditySnapshot(usd=5000.0),
                ),
                analysis=RugAnalysis(risk_score=30, risk_level="low", signals=[], data_sources=[], limitations=[]),
                watchlist_hits=[],
            )

        monkeypatch.setattr(rug_analyzer, "analyze_token_contract", fake)
        rug_analyzer._deep_cache._store.clear()

        resp1 = _run(rug_analyzer.scan_and_rank(page_size=5))
        assert resp1.discovery.deep_cache_misses >= 1

        resp2 = _run(rug_analyzer.scan_and_rank(page_size=5))
        assert resp2.discovery.deep_cache_hits >= 1
