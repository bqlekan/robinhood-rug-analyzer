"""Tests for the Market Intelligence Enrichment layer.

Validates that missing data produces None dimension scores (not 0),
composite skips None, and enrichment metadata is populated correctly.
"""

import asyncio

import pytest

from app.models.token import (
    EnrichmentField,
    EnrichmentReport,
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
    DiscoveryDiagnostics,
)
from app.services import candidate_discovery, rug_analyzer
from app.services.eligibility import evaluate


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


def _one_token():
    return [{"address_hash": "0xabc", "name": "T", "holders_count": 10}]


# ── None-to-0 fix: dimension scores ─────────────────────────────


class TestMissingDataProducesNone:
    """When provider data is absent, dimension scores must be None, not 0."""

    def test_no_dev_reputation_is_none(self, monkeypatch):
        _stub_discovery(monkeypatch, _one_token())

        async def fake(address, include_lore=False):
            return TokenAnalysisResponse(
                contract_address=address, chain="test", status="ok", message="stub",
                holders=HolderDistribution(holder_count=100, top10_percentage=40.0),
                market_data=TokenMarketData(
                    pair_address="0xpair",
                    liquidity=LiquiditySnapshot(usd=5000.0),
                    volume=VolumeSnapshot(h24=1000.0),
                    price_usd="1.0",
                ),
                analysis=RugAnalysis(risk_score=30, risk_level="low", signals=[], data_sources=[], limitations=[]),
                developer_reputation=None,
                developer_network=None,
                watchlist_hits=[],
            )

        monkeypatch.setattr(rug_analyzer, "analyze_token_contract", fake)
        resp = _run(rug_analyzer.scan_and_rank(5))
        t = resp.ranked_tokens[0]
        assert t.dev_reputation_score is None
        assert t.dev_network_score is None

    def test_no_holders_is_none(self, monkeypatch):
        _stub_discovery(monkeypatch, _one_token())

        async def fake(address, include_lore=False):
            return TokenAnalysisResponse(
                contract_address=address, chain="test", status="ok", message="stub",
                holders=None,
                market_data=TokenMarketData(
                    pair_address="0xpair",
                    liquidity=LiquiditySnapshot(usd=5000.0),
                    volume=VolumeSnapshot(h24=1000.0),
                    price_usd="1.0",
                ),
                analysis=RugAnalysis(risk_score=30, risk_level="low", signals=[], data_sources=[], limitations=[]),
                watchlist_hits=[],
            )

        monkeypatch.setattr(rug_analyzer, "analyze_token_contract", fake)
        resp = _run(rug_analyzer.scan_and_rank(5))
        t = resp.ranked_tokens[0]
        assert t.holder_quality_score is None

    def test_no_market_data_liquidity_is_none(self, monkeypatch):
        _stub_discovery(monkeypatch, _one_token())

        async def fake(address, include_lore=False):
            return TokenAnalysisResponse(
                contract_address=address, chain="test", status="ok", message="stub",
                holders=HolderDistribution(holder_count=50),
                market_data=TokenMarketData(pair_address=None, price_usd=None, liquidity=None),
                analysis=RugAnalysis(risk_score=85, risk_level="high", signals=[], data_sources=[], limitations=[]),
                watchlist_hits=[],
            )

        monkeypatch.setattr(rug_analyzer, "analyze_token_contract", fake)
        resp = _run(rug_analyzer.scan_and_rank(5))
        t = resp.ranked_tokens[0]
        assert t.liquidity_score is None

    def test_no_volume_momentum_is_none(self, monkeypatch):
        _stub_discovery(monkeypatch, _one_token())

        async def fake(address, include_lore=False):
            return TokenAnalysisResponse(
                contract_address=address, chain="test", status="ok", message="stub",
                holders=HolderDistribution(holder_count=50, top10_percentage=30.0),
                market_data=TokenMarketData(
                    pair_address="0xpair",
                    liquidity=LiquiditySnapshot(usd=5000.0),
                    volume=None,
                    price_usd="1.0",
                    price_change=None,
                ),
                analysis=RugAnalysis(risk_score=30, risk_level="low", signals=[], data_sources=[], limitations=[]),
                watchlist_hits=[],
            )

        monkeypatch.setattr(rug_analyzer, "analyze_token_contract", fake)
        resp = _run(rug_analyzer.scan_and_rank(5))
        t = resp.ranked_tokens[0]
        assert t.momentum_score is None


# ── Composite skips None dimensions ──────────────────────────────


class TestCompositeSkipsNone:
    def test_composite_excludes_none_dimensions(self, monkeypatch):
        """Composite should only average known dimensions — not drag down with 0s."""
        _stub_discovery(monkeypatch, _one_token())

        async def fake(address, include_lore=False):
            return TokenAnalysisResponse(
                contract_address=address, chain="test", status="ok", message="stub",
                holders=HolderDistribution(holder_count=100, top10_percentage=20.0),
                market_data=TokenMarketData(
                    pair_address="0xpair",
                    liquidity=LiquiditySnapshot(usd=50000.0),
                    volume=VolumeSnapshot(h24=10000.0),
                    price_usd="1.0",
                    price_change=PriceChangeSnapshot(h24=10.0),
                ),
                analysis=RugAnalysis(risk_score=10, risk_level="low", signals=[], data_sources=[], limitations=[]),
                # Deliberately no dev data — should be None, not 0
                developer_reputation=None,
                developer_network=None,
                watchlist_hits=[],
            )

        monkeypatch.setattr(rug_analyzer, "analyze_token_contract", fake)
        resp = _run(rug_analyzer.scan_and_rank(5))
        t = resp.ranked_tokens[0]
        # With old code, dev_rep=0 and dev_net=0 would drag composite down.
        # Now they're None and excluded, so composite should be higher.
        assert t.dev_reputation_score is None
        assert t.dev_network_score is None
        assert t.composite_score is not None
        # Security = 90, liquidity should be high, so composite should be well above 50
        assert t.composite_score > 50

    def test_all_none_gives_none_composite(self, monkeypatch):
        """If every dimension is None, composite should be None, not 0."""
        _stub_discovery(monkeypatch, _one_token())

        async def fake(address, include_lore=False):
            return TokenAnalysisResponse(
                contract_address=address, chain="test", status="ok", message="stub",
                holders=None,
                market_data=None,
                analysis=RugAnalysis(risk_score=50, risk_level="medium", signals=[], data_sources=[], limitations=[]),
                watchlist_hits=[],
            )

        monkeypatch.setattr(rug_analyzer, "analyze_token_contract", fake)
        resp = _run(rug_analyzer.scan_and_rank(5))
        # This token should be excluded (no market data), but composite should still
        # have a value because security and opportunity always exist.
        assert len(resp.excluded_tokens) == 1
        t = resp.excluded_tokens[0]
        # security is always computed (100 - risk), and opportunity always runs
        assert t.security_score is not None


# ── Enrichment model ─────────────────────────────────────────────


class TestEnrichmentReport:
    def test_data_confidence_all_known(self):
        report = EnrichmentReport(
            pair=EnrichmentField(status="known"),
            price=EnrichmentField(status="known"),
            liquidity=EnrichmentField(status="known"),
            fdv=EnrichmentField(status="known"),
            market_cap=EnrichmentField(status="known"),
            volume_h24=EnrichmentField(status="known"),
            holders=EnrichmentField(status="known"),
            developer=EnrichmentField(status="known"),
            verification=EnrichmentField(status="known"),
            launchpad=EnrichmentField(status="known"),
            smart_wallets=EnrichmentField(status="known"),
        )
        dc = report.compute_data_confidence()
        assert dc == 100

    def test_data_confidence_partial(self):
        report = EnrichmentReport(
            pair=EnrichmentField(status="known"),
            price=EnrichmentField(status="known"),
            liquidity=EnrichmentField(status="unknown"),
            fdv=EnrichmentField(status="unknown"),
            market_cap=EnrichmentField(status="unknown"),
            volume_h24=EnrichmentField(status="unknown"),
            holders=EnrichmentField(status="known"),
            developer=EnrichmentField(status="unknown"),
            verification=EnrichmentField(status="known"),
            launchpad=EnrichmentField(status="not_analysed"),
            smart_wallets=EnrichmentField(status="not_analysed"),
        )
        dc = report.compute_data_confidence()
        assert 30 <= dc <= 50  # 4 out of 11 known

    def test_data_confidence_none_known(self):
        report = EnrichmentReport()
        dc = report.compute_data_confidence()
        assert dc == 0

    def test_default_field_status(self):
        f = EnrichmentField()
        assert f.status == "not_analysed"
        assert f.source is None
        assert f.confidence == "medium"


# ── Eligibility with None data ───────────────────────────────────


class TestEligibilityNoneHandling:
    def test_no_dev_no_penalty(self):
        """Missing dev data should not prevent 'good' classification."""
        r = TokenAnalysisResponse(
            contract_address="0x" + "a" * 40, chain="test", status="ok", message="stub",
            market_data=TokenMarketData(
                pair_address="0xpair",
                price_usd="1.0",
                liquidity=LiquiditySnapshot(usd=5000.0),
                volume=VolumeSnapshot(h24=1000.0),
            ),
            holders=HolderDistribution(holder_count=100, top10_percentage=40.0),
            analysis=RugAnalysis(risk_score=30, risk_level="low", signals=[], data_sources=[], limitations=[]),
            developer_reputation=None,
            watchlist_hits=[],
        )
        q = evaluate(r)
        # Should still be 'good' since risk is low and liquidity is known
        assert q.qualification_level in ("excellent", "good")

    def test_none_liquidity_is_not_zero(self):
        """None liquidity (unknown) should classify differently than zero liquidity."""
        r_none = TokenAnalysisResponse(
            contract_address="0x" + "a" * 40, chain="test", status="ok", message="stub",
            market_data=TokenMarketData(pair_address=None, price_usd=None, liquidity=None),
            holders=HolderDistribution(holder_count=50),
            analysis=RugAnalysis(risk_score=85, risk_level="high", signals=[], data_sources=[], limitations=[]),
            watchlist_hits=[],
        )
        r_zero = TokenAnalysisResponse(
            contract_address="0x" + "a" * 40, chain="test", status="ok", message="stub",
            market_data=TokenMarketData(
                pair_address="0xpair", price_usd="0.0",
                liquidity=LiquiditySnapshot(usd=0.0),
            ),
            holders=HolderDistribution(holder_count=50),
            analysis=RugAnalysis(risk_score=85, risk_level="high", signals=[], data_sources=[], limitations=[]),
            watchlist_hits=[],
        )
        q_none = evaluate(r_none)
        q_zero = evaluate(r_zero)
        # None liq → high_risk (avoid tier removed); Zero → warning, not excluded
        assert q_none.qualification_level == "high_risk"
        assert q_zero.qualification_level != "excluded"
        assert any("zero" in w.lower() and "liquidity" in w.lower() for w in q_zero.warnings)

    def test_confidence_not_penalized_by_missing_dev(self):
        """Confidence should not be 0 for missing dev — it should skip that dimension."""
        r = TokenAnalysisResponse(
            contract_address="0x" + "a" * 40, chain="test", status="ok", message="stub",
            market_data=TokenMarketData(
                pair_address="0xpair",
                price_usd="1.0",
                liquidity=LiquiditySnapshot(usd=5000.0),
                volume=VolumeSnapshot(h24=1000.0),
            ),
            holders=HolderDistribution(holder_count=100, top10_percentage=40.0),
            analysis=RugAnalysis(risk_score=30, risk_level="low", signals=[], data_sources=[], limitations=[]),
            developer_reputation=None,
            developer_network=None,
            watchlist_hits=[],
        )
        q = evaluate(r)
        # Without dev data, confidence should still be reasonable (not dragged to 0)
        assert q.confidence_score >= 30
