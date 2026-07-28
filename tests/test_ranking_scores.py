"""Tests for the Intelligence Ranking Engine — dimension scores and composite."""

import asyncio

import pytest

from app.core.config import settings
from app.models.token import (
    HolderDistribution,
    RugAnalysis,
    TokenAnalysisResponse,
    TokenMarketData,
    LiquiditySnapshot,
    VolumeSnapshot,
    PriceChangeSnapshot,
    DeveloperReputationResult,
    DeveloperNetworkResult,
    WatchlistHit,
)
from app.services import candidate_discovery, rug_analyzer
from app.models.token import DiscoveryDiagnostics


def _run(coro):
    return asyncio.run(coro)


def _stub_discovery(monkeypatch, tokens):
    async def fake_discover(limit):
        cands = [
            candidate_discovery.DiscoveredCandidate(
                address_hash=t["address_hash"],
                name=t.get("name"),
                symbol=t.get("symbol"),
                holder_count=t.get("holders_count"),
                source="test",
            )
            for t in tokens[:limit]
        ]
        return cands, DiscoveryDiagnostics()

    monkeypatch.setattr(candidate_discovery, "discover_candidates", fake_discover)


def _stub_analysis(monkeypatch, risk=30, holders=100, liq=5000.0, volume=1000.0,
                    dev_score=50, net_score=40, smart_count=2, top10=40.0,
                    price_change=5.0):
    async def fake_analyze(address, include_lore=False):
        return TokenAnalysisResponse(
            contract_address=address, chain="Robinhood Chain",
            status="ok", message="stub",
            holders=HolderDistribution(holder_count=holders, top10_percentage=top10),
            market_data=TokenMarketData(
                pair_address="0xpair",
                liquidity=LiquiditySnapshot(usd=liq),
                volume=VolumeSnapshot(h24=volume),
                price_usd="1.0",
                market_cap=50000.0,
                price_change=PriceChangeSnapshot(h24=price_change),
            ),
            analysis=RugAnalysis(
                risk_score=risk, risk_level="medium",
                signals=[], data_sources=[], limitations=[],
            ),
            developer_reputation=DeveloperReputationResult(score=dev_score, deployer="0x" + "d" * 40),
            developer_network=DeveloperNetworkResult(score=net_score, cluster_size=3),
            watchlist_hits=[
                WatchlistHit(address=f"0x{'e' * 38}{i:02x}", kind="smart", proxy_score=80)
                for i in range(smart_count)
            ],
        )

    monkeypatch.setattr(rug_analyzer, "analyze_token_contract", fake_analyze)


class TestDimensionScores:
    def test_security_score_populated(self, monkeypatch):
        _stub_discovery(monkeypatch, [{"address_hash": "0xabc", "name": "T", "holders_count": 10}])
        _stub_analysis(monkeypatch, risk=30)
        resp = _run(rug_analyzer.scan_and_rank(5))
        t = resp.ranked_tokens[0]
        assert t.security_score == 70  # 100 - 30

    def test_liquidity_score_populated(self, monkeypatch):
        _stub_discovery(monkeypatch, [{"address_hash": "0xabc", "name": "T", "holders_count": 10}])
        _stub_analysis(monkeypatch, liq=10000.0)
        resp = _run(rug_analyzer.scan_and_rank(5))
        t = resp.ranked_tokens[0]
        assert t.liquidity_score is not None
        assert t.liquidity_score > 0

    def test_dev_scores_populated(self, monkeypatch):
        _stub_discovery(monkeypatch, [{"address_hash": "0xabc", "name": "T", "holders_count": 10}])
        _stub_analysis(monkeypatch, dev_score=75, net_score=60)
        resp = _run(rug_analyzer.scan_and_rank(5))
        t = resp.ranked_tokens[0]
        assert t.dev_reputation_score == 75
        assert t.dev_network_score == 60

    def test_smart_wallet_score(self, monkeypatch):
        _stub_discovery(monkeypatch, [{"address_hash": "0xabc", "name": "T", "holders_count": 10}])
        _stub_analysis(monkeypatch, smart_count=3)
        resp = _run(rug_analyzer.scan_and_rank(5))
        t = resp.ranked_tokens[0]
        assert t.smart_wallet_score == 75  # 3 * 25

    def test_holder_quality_score(self, monkeypatch):
        _stub_discovery(monkeypatch, [{"address_hash": "0xabc", "name": "T", "holders_count": 10}])
        _stub_analysis(monkeypatch, top10=60.0)
        resp = _run(rug_analyzer.scan_and_rank(5))
        t = resp.ranked_tokens[0]
        assert t.holder_quality_score == 40  # 100 - 60

    def test_composite_score_populated(self, monkeypatch):
        _stub_discovery(monkeypatch, [{"address_hash": "0xabc", "name": "T", "holders_count": 10}])
        _stub_analysis(monkeypatch)
        resp = _run(rug_analyzer.scan_and_rank(5))
        t = resp.ranked_tokens[0]
        assert t.composite_score is not None
        assert 0 <= t.composite_score <= 100

    def test_composite_sort_order(self, monkeypatch):
        _stub_discovery(monkeypatch, [
            {"address_hash": "0xhigh", "name": "High", "holders_count": 10},
            {"address_hash": "0xlow", "name": "Low", "holders_count": 10},
        ])

        call_count = [0]
        async def fake_analyze(address, include_lore=False):
            call_count[0] += 1
            risk = 20 if address == "0xhigh" else 80
            return TokenAnalysisResponse(
                contract_address=address, chain="Robinhood Chain",
                status="ok", message="stub",
                holders=HolderDistribution(holder_count=100, top10_percentage=40.0),
                market_data=TokenMarketData(
                    pair_address="0xpair",
                    liquidity=LiquiditySnapshot(usd=5000.0),
                    volume=VolumeSnapshot(h24=1000.0),
                    price_usd="1.0",
                ),
                analysis=RugAnalysis(
                    risk_score=risk, risk_level="medium" if risk < 50 else "high",
                    signals=[], data_sources=[], limitations=[],
                ),
                watchlist_hits=[],
            )

        monkeypatch.setattr(rug_analyzer, "analyze_token_contract", fake_analyze)
        resp = _run(rug_analyzer.scan_and_rank(5))
        assert len(resp.ranked_tokens) == 2
        assert resp.ranked_tokens[0].composite_score >= resp.ranked_tokens[1].composite_score

    def test_excluded_tokens_still_scored(self, monkeypatch):
        """Excluded tokens should still have alpha_score and dimension scores."""
        _stub_discovery(monkeypatch, [{"address_hash": "0xrug", "name": "Rug", "holders_count": 10}])
        _stub_analysis(monkeypatch, risk=95)  # proven rug → excluded
        resp = _run(rug_analyzer.scan_and_rank(5))
        assert len(resp.excluded_tokens) == 1
        t = resp.excluded_tokens[0]
        assert t.alpha_score is not None
        assert t.security_score is not None
        assert t.composite_score is not None


class TestNoLiquidityClassification:
    def test_no_pair_is_high_risk_not_excluded(self, monkeypatch):
        _stub_discovery(monkeypatch, [{"address_hash": "0xnopair", "name": "NoPair", "holders_count": 10}])

        async def fake_analyze(address, include_lore=False):
            return TokenAnalysisResponse(
                contract_address=address, chain="Robinhood Chain",
                status="ok", message="stub",
                holders=HolderDistribution(holder_count=50),
                market_data=TokenMarketData(pair_address=None, price_usd=None, liquidity=None),
                analysis=RugAnalysis(
                    risk_score=85, risk_level="high",
                    signals=[], data_sources=[], limitations=[],
                ),
                watchlist_hits=[],
            )

        monkeypatch.setattr(rug_analyzer, "analyze_token_contract", fake_analyze)
        resp = _run(rug_analyzer.scan_and_rank(5))
        assert len(resp.ranked_tokens) == 1
        assert resp.ranked_tokens[0].qualification_level == "high_risk"
        assert len(resp.excluded_tokens) == 0
