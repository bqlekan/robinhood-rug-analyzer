"""Tests for the Eligibility Engine (pre-ranking quality gate)."""

import pytest

from app.models.token import (
    DevProfile,
    DeveloperReputationResult,
    HolderDistribution,
    HoneypotResult,
    LiquidityLock,
    LiquiditySnapshot,
    RugAnalysis,
    TokenAge,
    TokenAnalysisResponse,
    TokenMarketData,
    VolumeSnapshot,
    WatchlistHit,
)
from app.services.eligibility import evaluate


def _base_result(**overrides) -> TokenAnalysisResponse:
    defaults = dict(
        contract_address="0x" + "a" * 40,
        chain="robinhood",
        status="success",
        message="ok",
        market_data=TokenMarketData(
            pair_address="0x" + "b" * 40,
            price_usd="1.23",
            market_cap=50000.0,
            liquidity=LiquiditySnapshot(usd=5000.0),
            volume=VolumeSnapshot(h24=1000.0),
        ),
        token_age=TokenAge(age_hours=12.0, age_days=0.5),
        holders=HolderDistribution(holder_count=100),
        analysis=RugAnalysis(
            risk_score=30,
            risk_level="low",
            signals=[],
            data_sources=["DexScreener"],
            limitations=[],
            confidence=80,
        ),
        watchlist_hits=[],
    )
    defaults.update(overrides)
    return TokenAnalysisResponse(**defaults)


class TestEligibleToken:
    def test_healthy_token_eligible(self):
        r = _base_result()
        e = evaluate(r)
        assert e.eligible is True
        assert e.rejection_reasons == []
        assert len(e.evidence) > 0

    def test_evidence_includes_liquidity(self):
        r = _base_result()
        e = evaluate(r)
        assert any("liquidity" in ev.lower() for ev in e.evidence)

    def test_evidence_includes_trading(self):
        r = _base_result()
        e = evaluate(r)
        assert any("trading" in ev.lower() for ev in e.evidence)

    def test_evidence_includes_risk(self):
        r = _base_result()
        e = evaluate(r)
        assert any("risk" in ev.lower() for ev in e.evidence)


class TestRuggedToken:
    def test_high_risk_ineligible(self, monkeypatch):
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_max_risk_score", 80)
        r = _base_result(analysis=RugAnalysis(
            risk_score=90, risk_level="critical", signals=[],
            data_sources=[], limitations=[], confidence=80,
        ))
        e = evaluate(r)
        assert e.eligible is False
        assert any("risk" in reason.lower() for reason in e.rejection_reasons)

    def test_honeypot_ineligible(self):
        r = _base_result(honeypot=HoneypotResult(status="honeypot"))
        e = evaluate(r)
        assert e.eligible is False
        assert any("honeypot" in reason.lower() for reason in e.rejection_reasons)

    def test_high_tax_warning_not_rejection(self):
        r = _base_result(honeypot=HoneypotResult(status="high_tax", sell_tax_percentage=50.0))
        e = evaluate(r)
        assert e.eligible is True
        assert any("tax" in w.lower() for w in e.warnings)


class TestNoLiquidity:
    def test_zero_liquidity_ineligible(self, monkeypatch):
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_min_liquidity_usd", 500.0)
        r = _base_result(market_data=TokenMarketData(
            pair_address="0x" + "b" * 40,
            price_usd="1.0",
            liquidity=LiquiditySnapshot(usd=0.0),
        ))
        e = evaluate(r)
        assert e.eligible is False
        assert any("liquidity" in reason.lower() for reason in e.rejection_reasons)

    def test_null_liquidity_ineligible(self, monkeypatch):
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_require_liquidity", True)
        r = _base_result(market_data=TokenMarketData(
            pair_address="0x" + "b" * 40,
            price_usd="1.0",
            liquidity=LiquiditySnapshot(usd=None),
        ))
        e = evaluate(r)
        assert e.eligible is False
        assert any("liquidity" in reason.lower() for reason in e.rejection_reasons)


class TestMissingMarketCap:
    def test_no_market_cap_eligible_by_default(self):
        """Market cap not required by default (eligibility_require_market_cap=False)."""
        r = _base_result(market_data=TokenMarketData(
            pair_address="0x" + "b" * 40,
            price_usd="1.0",
            market_cap=None,
            liquidity=LiquiditySnapshot(usd=5000.0),
            volume=VolumeSnapshot(h24=1000.0),
        ))
        e = evaluate(r)
        assert e.eligible is True

    def test_no_market_cap_ineligible_when_required(self, monkeypatch):
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_require_market_cap", True)
        r = _base_result(market_data=TokenMarketData(
            pair_address="0x" + "b" * 40,
            price_usd="1.0",
            market_cap=None,
            liquidity=LiquiditySnapshot(usd=5000.0),
        ))
        e = evaluate(r)
        assert e.eligible is False

    def test_fdv_fallback_warning(self):
        r = _base_result(market_data=TokenMarketData(
            pair_address="0x" + "b" * 40,
            price_usd="1.0",
            market_cap=None,
            fdv=100000.0,
            liquidity=LiquiditySnapshot(usd=5000.0),
            volume=VolumeSnapshot(h24=1000.0),
        ))
        e = evaluate(r)
        assert any("fdv" in w.lower() for w in e.warnings)


class TestDeadTradingPair:
    def test_no_pair_ineligible(self, monkeypatch):
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_require_pair", True)
        r = _base_result(market_data=TokenMarketData(
            pair_address=None,
            price_usd=None,
            liquidity=None,
        ))
        e = evaluate(r)
        assert e.eligible is False
        assert any("pair" in reason.lower() for reason in e.rejection_reasons)

    def test_no_market_data_at_all(self):
        r = _base_result(market_data=None)
        e = evaluate(r)
        assert e.eligible is False


class TestStalePrice:
    def test_no_price_ineligible(self, monkeypatch):
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_require_price", True)
        r = _base_result(market_data=TokenMarketData(
            pair_address="0x" + "b" * 40,
            price_usd=None,
            liquidity=LiquiditySnapshot(usd=5000.0),
        ))
        e = evaluate(r)
        assert e.eligible is False
        assert any("price" in reason.lower() for reason in e.rejection_reasons)


class TestFailedAnalysis:
    def test_low_confidence_ineligible(self, monkeypatch):
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_require_analysis", True)
        r = _base_result(analysis=RugAnalysis(
            risk_score=30, risk_level="low", signals=[],
            data_sources=[], limitations=[], confidence=10,
        ))
        e = evaluate(r)
        assert e.eligible is False
        assert any("confidence" in reason.lower() for reason in e.rejection_reasons)


class TestAgeLimit:
    def test_too_old_ineligible(self, monkeypatch):
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_max_age_days", 3.0)
        r = _base_result(token_age=TokenAge(age_hours=120.0, age_days=5.0))
        e = evaluate(r)
        assert e.eligible is False
        assert any("age" in reason.lower() for reason in e.rejection_reasons)

    def test_within_age_eligible(self, monkeypatch):
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_max_age_days", 3.0)
        r = _base_result(token_age=TokenAge(age_hours=24.0, age_days=1.0))
        e = evaluate(r)
        assert e.eligible is True


class TestEdgeCases:
    def test_multiple_rejections(self, monkeypatch):
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_max_risk_score", 80)
        r = _base_result(
            market_data=None,
            honeypot=HoneypotResult(status="honeypot"),
            analysis=RugAnalysis(
                risk_score=95, risk_level="critical", signals=[],
                data_sources=[], limitations=[], confidence=80,
            ),
        )
        e = evaluate(r)
        assert e.eligible is False
        assert len(e.rejection_reasons) >= 2

    def test_all_thresholds_zero_passes_everything(self, monkeypatch):
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_min_liquidity_usd", 0)
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_min_market_cap_usd", 0)
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_max_age_days", 0)
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_min_holder_count", 0)
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_min_volume_h24_usd", 0)
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_max_risk_score", 0)
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_require_pair", False)
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_require_liquidity", False)
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_require_price", False)
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_require_market_cap", False)
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_require_analysis", False)
        r = _base_result(market_data=None, honeypot=None)
        e = evaluate(r)
        assert e.eligible is True

    def test_confidence_degrades_without_market_data(self):
        r = _base_result(market_data=None)
        e = evaluate(r)
        assert e.confidence <= 30

    def test_smart_wallet_evidence(self):
        r = _base_result(watchlist_hits=[
            WatchlistHit(address="0x" + "c" * 40, kind="smart", proxy_score=85),
        ])
        e = evaluate(r)
        assert any("smart wallet" in ev.lower() for ev in e.evidence)

    def test_dev_reputation_evidence(self):
        r = _base_result(developer_reputation=DeveloperReputationResult(
            score=75, deployer="0x" + "d" * 40,
        ))
        e = evaluate(r)
        assert any("developer" in ev.lower() for ev in e.evidence)

    def test_lock_burned_evidence(self):
        r = _base_result(liquidity_lock=LiquidityLock(status="burned"))
        e = evaluate(r)
        assert any("burned" in ev.lower() for ev in e.evidence)

    def test_lock_unlocked_warning(self):
        r = _base_result(liquidity_lock=LiquidityLock(status="unlocked"))
        e = evaluate(r)
        assert any("unlocked" in w.lower() for w in e.warnings)

    def test_below_holder_count_minimum(self, monkeypatch):
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_min_holder_count", 50)
        r = _base_result(holders=HolderDistribution(holder_count=10))
        e = evaluate(r)
        assert e.eligible is False
        assert any("holder" in reason.lower() for reason in e.rejection_reasons)

    def test_below_volume_minimum(self, monkeypatch):
        monkeypatch.setattr("app.services.eligibility.settings.eligibility_min_volume_h24_usd", 500.0)
        r = _base_result(market_data=TokenMarketData(
            pair_address="0x" + "b" * 40,
            price_usd="1.0",
            liquidity=LiquiditySnapshot(usd=5000.0),
            volume=VolumeSnapshot(h24=100.0),
        ))
        e = evaluate(r)
        assert e.eligible is False
        assert any("volume" in reason.lower() for reason in e.rejection_reasons)
