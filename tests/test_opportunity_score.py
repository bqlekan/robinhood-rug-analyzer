"""Tests for the Opportunity Score (Alpha) engine."""

import pytest

from app.models.token import (
    ContractIntel,
    HolderDistribution,
    HoneypotResult,
    LiquiditySnapshot,
    OpportunitySignal,
    RugAnalysis,
    TokenAge,
    TokenAnalysisResponse,
    TokenMarketData,
    WatchlistHit,
)
from app.services.opportunity_score import (
    _score_freshness,
    _score_holder_quality,
    _score_honeypot,
    _score_liquidity,
    _score_risk,
    _score_smart_wallets,
    _score_verified,
    score_opportunity,
)


def _base_result(**overrides) -> TokenAnalysisResponse:
    defaults = dict(
        contract_address="0x" + "a" * 40,
        chain="robinhood",
        status="success",
        message="ok",
        analysis=RugAnalysis(
            risk_score=30,
            risk_level="low",
            signals=[],
            data_sources=[],
            limitations=[],
        ),
        watchlist_hits=[],
    )
    defaults.update(overrides)
    return TokenAnalysisResponse(**defaults)


# --- Individual scorers ---


class TestScoreRisk:
    def test_low_risk_positive(self):
        r = _base_result(analysis=RugAnalysis(risk_score=20, risk_level="low", signals=[], data_sources=[], limitations=[]))
        s = _score_risk(r)
        assert s.value == 80
        assert s.positive is True
        assert s.name == "risk"

    def test_high_risk_negative(self):
        r = _base_result(analysis=RugAnalysis(risk_score=80, risk_level="critical", signals=[], data_sources=[], limitations=[]))
        s = _score_risk(r)
        assert s.value == 20
        assert s.positive is False

    def test_boundary_at_60(self):
        r = _base_result(analysis=RugAnalysis(risk_score=40, risk_level="medium", signals=[], data_sources=[], limitations=[]))
        s = _score_risk(r)
        assert s.value == 60
        assert s.positive is True

    def test_clamps_to_zero(self):
        r = _base_result(analysis=RugAnalysis(risk_score=100, risk_level="critical", signals=[], data_sources=[], limitations=[]))
        s = _score_risk(r)
        assert s.value == 0


class TestScoreFreshness:
    def test_fresh_launch(self, monkeypatch):
        monkeypatch.setattr("app.services.opportunity_score.settings.scan_max_launch_age_days", 3.0)
        r = _base_result(token_age=TokenAge(age_hours=1.0, age_days=1 / 24))
        s = _score_freshness(r)
        assert s is not None
        assert s.value > 90
        assert s.positive is True

    def test_old_launch(self, monkeypatch):
        monkeypatch.setattr("app.services.opportunity_score.settings.scan_max_launch_age_days", 3.0)
        r = _base_result(token_age=TokenAge(age_hours=70.0, age_days=70 / 24))
        s = _score_freshness(r)
        assert s is not None
        assert s.value < 5

    def test_none_when_no_age(self):
        r = _base_result(token_age=None)
        assert _score_freshness(r) is None

    def test_normalized_against_config(self, monkeypatch):
        monkeypatch.setattr("app.services.opportunity_score.settings.scan_max_launch_age_days", 10.0)
        r = _base_result(token_age=TokenAge(age_hours=120.0, age_days=5.0))
        s = _score_freshness(r)
        assert s is not None
        assert s.value == 50


class TestScoreLiquidity:
    def test_high_liquidity(self):
        r = _base_result(market_data=TokenMarketData(liquidity=LiquiditySnapshot(usd=50_000.0)))
        s = _score_liquidity(r)
        assert s is not None
        assert s.value > 90

    def test_low_liquidity(self):
        r = _base_result(market_data=TokenMarketData(liquidity=LiquiditySnapshot(usd=10.0)))
        s = _score_liquidity(r)
        assert s is not None
        assert s.positive is False

    def test_none_when_no_market_data(self):
        r = _base_result(market_data=None)
        assert _score_liquidity(r) is None

    def test_none_when_no_liquidity(self):
        r = _base_result(market_data=TokenMarketData(liquidity=None))
        assert _score_liquidity(r) is None


class TestScoreSmartWallets:
    def test_no_smart_wallets(self):
        r = _base_result(watchlist_hits=[])
        s = _score_smart_wallets(r)
        assert s.value == 0
        assert s.positive is False

    def test_one_smart_wallet(self):
        r = _base_result(watchlist_hits=[WatchlistHit(address="0x1", kind="smart")])
        s = _score_smart_wallets(r)
        assert s.value == 33
        assert s.positive is True

    def test_three_caps_at_99(self):
        hits = [WatchlistHit(address=f"0x{i}", kind="smart") for i in range(3)]
        r = _base_result(watchlist_hits=hits)
        s = _score_smart_wallets(r)
        assert s.value == 99

    def test_ignores_insider_wallets(self):
        r = _base_result(watchlist_hits=[WatchlistHit(address="0x1", kind="insider")])
        s = _score_smart_wallets(r)
        assert s.value == 0


class TestScoreHolderQuality:
    def test_good_distribution(self):
        r = _base_result(holders=HolderDistribution(top10_percentage=30.0))
        s = _score_holder_quality(r)
        assert s.value == 70
        assert s.positive is True

    def test_concentrated(self):
        r = _base_result(holders=HolderDistribution(top10_percentage=80.0))
        s = _score_holder_quality(r)
        assert s.value == 20
        assert s.positive is False

    def test_none_when_no_holders(self):
        r = _base_result(holders=None)
        assert _score_holder_quality(r) is None


class TestScoreHoneypot:
    def test_sellable(self):
        r = _base_result(honeypot=HoneypotResult(status="sellable"))
        s = _score_honeypot(r)
        assert s.value == 100
        assert s.positive is True

    def test_honeypot(self):
        r = _base_result(honeypot=HoneypotResult(status="honeypot"))
        s = _score_honeypot(r)
        assert s.value == 0
        assert s.positive is False

    def test_none_when_missing(self):
        r = _base_result(honeypot=None)
        assert _score_honeypot(r) is None


class TestScoreVerified:
    def test_verified(self):
        r = _base_result(contract_intel=ContractIntel(verified=True))
        s = _score_verified(r)
        assert s.value == 100
        assert s.positive is True

    def test_not_verified(self):
        r = _base_result(contract_intel=ContractIntel(verified=False))
        s = _score_verified(r)
        assert s.value == 0
        assert s.positive is False

    def test_none_when_missing(self):
        r = _base_result(contract_intel=None)
        assert _score_verified(r) is None


# --- Aggregation ---


class TestScoreOpportunity:
    def test_produces_result_with_all_data(self, monkeypatch):
        monkeypatch.setattr("app.services.opportunity_score.settings.scan_max_launch_age_days", 3.0)
        r = _base_result(
            analysis=RugAnalysis(risk_score=20, risk_level="low", signals=[], data_sources=[], limitations=[]),
            token_age=TokenAge(age_hours=1.0, age_days=1 / 24),
            market_data=TokenMarketData(liquidity=LiquiditySnapshot(usd=50_000.0)),
            holders=HolderDistribution(top10_percentage=25.0),
            honeypot=HoneypotResult(status="sellable"),
            contract_intel=ContractIntel(verified=True),
            watchlist_hits=[WatchlistHit(address="0x1", kind="smart")],
        )
        opp = score_opportunity(r)
        assert 0 <= opp.alpha_score <= 100
        assert opp.alpha_level in ("low", "medium", "high", "excellent")
        assert len(opp.signals) == 7

    def test_missing_data_skips_signals(self):
        r = _base_result()
        opp = score_opportunity(r)
        assert len(opp.signals) >= 1  # at least risk
        assert len(opp.signals) < 7  # some skipped

    def test_deterministic(self, monkeypatch):
        monkeypatch.setattr("app.services.opportunity_score.settings.scan_max_launch_age_days", 3.0)
        r = _base_result(
            token_age=TokenAge(age_hours=10.0, age_days=10 / 24),
            market_data=TokenMarketData(liquidity=LiquiditySnapshot(usd=5_000.0)),
            honeypot=HoneypotResult(status="sellable"),
        )
        a = score_opportunity(r)
        b = score_opportunity(r)
        assert a.alpha_score == b.alpha_score
        assert [s.name for s in a.signals] == [s.name for s in b.signals]

    def test_configurable_weights(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.opportunity_score.settings.opportunity_score_weights",
            {"risk": 100, "freshness": 0, "liquidity": 0, "smart_wallets": 0, "holder_quality": 0, "honeypot": 0, "verified": 0},
        )
        r = _base_result(
            analysis=RugAnalysis(risk_score=10, risk_level="low", signals=[], data_sources=[], limitations=[]),
            honeypot=HoneypotResult(status="sellable"),
        )
        opp = score_opportunity(r)
        assert opp.alpha_score == 90  # 100 - 10

    def test_alpha_levels(self):
        # smart_wallets always fires (value=0 on empty hits), so both risk + smart_wallets
        # contribute.  Compute expected levels accordingly.
        for risk, expected in [(90, "low"), (60, "medium"), (30, "medium"), (10, "high")]:
            r = _base_result(analysis=RugAnalysis(risk_score=risk, risk_level="low", signals=[], data_sources=[], limitations=[]))
            opp = score_opportunity(r)
            assert opp.alpha_level == expected, f"risk={risk} → {opp.alpha_level}, expected {expected}"

    def test_signals_carry_explanations(self):
        r = _base_result(honeypot=HoneypotResult(status="sellable"))
        opp = score_opportunity(r)
        details = [s.detail for s in opp.signals]
        assert any("sellable" in d.lower() or "honeypot" in d.lower() for d in details)

    def test_empty_no_crash(self):
        r = _base_result()
        opp = score_opportunity(r)
        assert opp.alpha_score >= 0
