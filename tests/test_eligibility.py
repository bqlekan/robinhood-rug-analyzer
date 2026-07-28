"""Tests for the Qualification Engine (pre-ranking classifier)."""

import pytest

from app.models.token import (
    ContractIntel,
    DeveloperReputationResult,
    HolderDistribution,
    HoneypotResult,
    LiquidityLock,
    LiquiditySnapshot,
    QualificationResult,
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
            fdv=60000.0,
            liquidity=LiquiditySnapshot(usd=5000.0),
            volume=VolumeSnapshot(h24=1000.0),
        ),
        token_age=TokenAge(age_hours=12.0, age_days=0.5),
        holders=HolderDistribution(holder_count=100, top10_percentage=40.0),
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


# ── Hard exclusions ──────────────────────────────────────────────


class TestExcluded:
    def test_honeypot_excluded(self):
        r = _base_result(honeypot=HoneypotResult(status="honeypot"))
        q = evaluate(r)
        assert q.qualification_level == "excluded"
        assert any("honeypot" in reason.lower() for reason in q.rejection_reasons)

    def test_no_pair_classified_speculative_or_high_risk(self):
        r = _base_result(
            analysis=RugAnalysis(
                risk_score=85, risk_level="critical", signals=[],
                data_sources=[], limitations=[], confidence=50,
            ),
            market_data=TokenMarketData(
                pair_address=None, price_usd=None, liquidity=None,
            ),
        )
        q = evaluate(r)
        assert q.qualification_level != "excluded"
        assert q.qualification_level in ("speculative", "high_risk")

    def test_zero_liquidity_not_excluded(self):
        r = _base_result(market_data=TokenMarketData(
            pair_address="0x" + "b" * 40,
            price_usd="1.0",
            liquidity=LiquiditySnapshot(usd=0.0),
        ))
        q = evaluate(r)
        assert q.qualification_level != "excluded"
        assert any("zero" in w.lower() and "liquidity" in w.lower() for w in q.warnings)

    def test_no_market_data_with_holders_not_excluded(self):
        """No DexScreener pair but real holders → speculative, not excluded."""
        r = _base_result(
            market_data=None,
            holders=HolderDistribution(holder_count=100, top10_percentage=40.0),
        )
        q = evaluate(r)
        assert q.qualification_level != "excluded"

    def test_no_market_data_no_holders_excluded(self):
        """No market data AND no holders → dead contract."""
        r = _base_result(
            market_data=None,
            holders=HolderDistribution(holder_count=0),
        )
        q = evaluate(r)
        assert q.qualification_level == "excluded"
        assert any("dead" in reason.lower() for reason in q.rejection_reasons)

    def test_proven_rug_excluded(self):
        r = _base_result(analysis=RugAnalysis(
            risk_score=95, risk_level="critical", signals=[],
            data_sources=[], limitations=[], confidence=80,
        ))
        q = evaluate(r)
        assert q.qualification_level == "excluded"
        assert any("proven rug" in reason.lower() for reason in q.rejection_reasons)

    def test_trading_disabled_excluded(self):
        r = _base_result(honeypot=HoneypotResult(status="high_tax", sell_tax_percentage=95.0))
        q = evaluate(r)
        assert q.qualification_level == "excluded"
        assert any("trading" in reason.lower() for reason in q.rejection_reasons)

    def test_multiple_exclusion_reasons(self):
        r = _base_result(
            market_data=None,
            holders=HolderDistribution(holder_count=0),
            honeypot=HoneypotResult(status="honeypot"),
        )
        q = evaluate(r)
        assert q.qualification_level == "excluded"
        assert len(q.rejection_reasons) >= 2
        assert any("honeypot" in r.lower() for r in q.rejection_reasons)
        assert any("dead" in r.lower() for r in q.rejection_reasons)


# ── Not excluded (the key behavioral change) ────────────────────


class TestNotExcluded:
    def test_risk_81_not_excluded(self):
        """Risk score 81 was excluded by old engine (max_risk=80). Now it should be ranked."""
        r = _base_result(analysis=RugAnalysis(
            risk_score=81, risk_level="high", signals=[],
            data_sources=["DexScreener"], limitations=[], confidence=80,
        ))
        q = evaluate(r)
        assert q.qualification_level != "excluded"
        assert q.rejection_reasons == []

    def test_low_liquidity_not_excluded(self):
        """$100 liquidity was excluded by old engine (min=500). Now ranked as speculative/high_risk."""
        r = _base_result(market_data=TokenMarketData(
            pair_address="0x" + "b" * 40,
            price_usd="0.001",
            liquidity=LiquiditySnapshot(usd=100.0),
            volume=VolumeSnapshot(h24=50.0),
        ))
        q = evaluate(r)
        assert q.qualification_level != "excluded"

    def test_no_price_not_excluded(self):
        """Missing price was excluded by old engine (require_price). Now ranked."""
        r = _base_result(market_data=TokenMarketData(
            pair_address="0x" + "b" * 40,
            price_usd=None,
            liquidity=LiquiditySnapshot(usd=5000.0),
        ))
        q = evaluate(r)
        assert q.qualification_level != "excluded"

    def test_no_pair_low_risk_is_speculative(self):
        """Low-risk token with no pair is speculative, not avoid."""
        r = _base_result(market_data=TokenMarketData(
            pair_address=None, price_usd=None, liquidity=None,
        ))
        q = evaluate(r)
        assert q.qualification_level == "speculative"

    def test_old_token_not_excluded(self):
        """Token >3d old was excluded by old engine (max_age=3). Now ranked."""
        r = _base_result(token_age=TokenAge(age_hours=120.0, age_days=5.0))
        q = evaluate(r)
        assert q.qualification_level != "excluded"

    def test_low_confidence_not_excluded(self):
        """Low analysis confidence was excluded by old engine. Now ranked."""
        r = _base_result(analysis=RugAnalysis(
            risk_score=30, risk_level="low", signals=[],
            data_sources=[], limitations=[], confidence=10,
        ))
        q = evaluate(r)
        assert q.qualification_level != "excluded"

    def test_low_holders_not_excluded(self):
        """Low holder count no longer excludes."""
        r = _base_result(holders=HolderDistribution(holder_count=5))
        q = evaluate(r)
        assert q.qualification_level != "excluded"

    def test_low_volume_not_excluded(self):
        """Low volume no longer excludes."""
        r = _base_result(market_data=TokenMarketData(
            pair_address="0x" + "b" * 40,
            price_usd="1.0",
            liquidity=LiquiditySnapshot(usd=5000.0),
            volume=VolumeSnapshot(h24=0.5),
        ))
        q = evaluate(r)
        assert q.qualification_level != "excluded"

    def test_high_tax_not_excluded_below_90(self):
        """High sell tax (under 90%) should warn, not exclude."""
        r = _base_result(honeypot=HoneypotResult(status="high_tax", sell_tax_percentage=50.0))
        q = evaluate(r)
        assert q.qualification_level != "excluded"
        assert any("tax" in w.lower() for w in q.warnings)


# ── Classification levels ────────────────────────────────────────


class TestClassification:
    def test_excellent(self):
        r = _base_result(
            holders=HolderDistribution(holder_count=100, top10_percentage=30.0),
            developer_reputation=DeveloperReputationResult(score=75, deployer="0x" + "d" * 40),
            liquidity_lock=LiquidityLock(status="locked"),
        )
        q = evaluate(r)
        assert q.qualification_level == "excellent"

    def test_good(self):
        r = _base_result(
            analysis=RugAnalysis(
                risk_score=45, risk_level="medium", signals=[],
                data_sources=["DexScreener"], limitations=[], confidence=80,
            ),
            market_data=TokenMarketData(
                pair_address="0x" + "b" * 40,
                price_usd="1.0",
                liquidity=LiquiditySnapshot(usd=2000.0),
                volume=VolumeSnapshot(h24=500.0),
            ),
        )
        q = evaluate(r)
        assert q.qualification_level == "good"

    def test_speculative(self):
        r = _base_result(
            analysis=RugAnalysis(
                risk_score=70, risk_level="high", signals=[],
                data_sources=["DexScreener"], limitations=[], confidence=60,
            ),
            market_data=TokenMarketData(
                pair_address="0x" + "b" * 40,
                price_usd="0.001",
                liquidity=LiquiditySnapshot(usd=200.0),
                volume=VolumeSnapshot(h24=50.0),
            ),
        )
        q = evaluate(r)
        assert q.qualification_level == "speculative"

    def test_high_risk(self):
        r = _base_result(
            analysis=RugAnalysis(
                risk_score=90, risk_level="critical", signals=[],
                data_sources=["DexScreener"], limitations=[], confidence=50,
            ),
            market_data=TokenMarketData(
                pair_address="0x" + "b" * 40,
                price_usd="0.001",
                liquidity=LiquiditySnapshot(usd=100.0),
                volume=VolumeSnapshot(h24=10.0),
            ),
        )
        q = evaluate(r)
        assert q.qualification_level == "high_risk"

    def test_unknown_liquidity_classified_high_risk(self):
        """Token with no liquidity data but risk>80 → high_risk (avoid tier removed)."""
        r = _base_result(
            analysis=RugAnalysis(
                risk_score=85, risk_level="critical", signals=[],
                data_sources=["DexScreener"], limitations=[], confidence=50,
            ),
            market_data=TokenMarketData(
                pair_address=None,
                price_usd=None,
                liquidity=None,
            ),
        )
        q = evaluate(r)
        assert q.qualification_level == "high_risk"


# ── Confidence score ─────────────────────────────────────────────


class TestConfidenceScore:
    def test_confidence_range(self):
        r = _base_result()
        q = evaluate(r)
        assert 0 <= q.confidence_score <= 100

    def test_confidence_factors_populated(self):
        r = _base_result()
        q = evaluate(r)
        assert len(q.confidence_factors) == 8
        assert all("/100" in f for f in q.confidence_factors)

    def test_verified_contract_boosts_confidence(self):
        r_unverified = _base_result()
        r_verified = _base_result(contract_intel=ContractIntel(verified=True))
        q_unverified = evaluate(r_unverified)
        q_verified = evaluate(r_verified)
        assert q_verified.confidence_score >= q_unverified.confidence_score

    def test_no_market_data_low_confidence(self):
        r = _base_result(market_data=None, holders=HolderDistribution(holder_count=0))
        q = evaluate(r)
        assert q.confidence_score < 40

    def test_strong_dev_reputation_boosts_confidence(self):
        r_none = _base_result()
        r_strong = _base_result(developer_reputation=DeveloperReputationResult(
            score=90, deployer="0x" + "d" * 40,
        ))
        q_none = evaluate(r_none)
        q_strong = evaluate(r_strong)
        assert q_strong.confidence_score > q_none.confidence_score

    def test_smart_wallet_boosts_confidence(self):
        r_none = _base_result()
        r_smart = _base_result(watchlist_hits=[
            WatchlistHit(address="0x" + "c" * 40, kind="smart", proxy_score=85),
            WatchlistHit(address="0x" + "d" * 40, kind="smart", proxy_score=80),
        ])
        q_none = evaluate(r_none)
        q_smart = evaluate(r_smart)
        assert q_smart.confidence_score > q_none.confidence_score


# ── Evidence & warnings ──────────────────────────────────────────


class TestEvidenceAndWarnings:
    def test_evidence_includes_liquidity(self):
        q = evaluate(_base_result())
        assert any("liquidity" in ev.lower() for ev in q.evidence)

    def test_evidence_includes_trading(self):
        q = evaluate(_base_result())
        assert any("trading" in ev.lower() for ev in q.evidence)

    def test_evidence_includes_risk(self):
        q = evaluate(_base_result())
        assert any("risk" in ev.lower() for ev in q.evidence)

    def test_dev_reputation_evidence(self):
        r = _base_result(developer_reputation=DeveloperReputationResult(
            score=75, deployer="0x" + "d" * 40,
        ))
        q = evaluate(r)
        assert any("developer" in ev.lower() for ev in q.evidence)

    def test_smart_wallet_evidence(self):
        r = _base_result(watchlist_hits=[
            WatchlistHit(address="0x" + "c" * 40, kind="smart", proxy_score=85),
        ])
        q = evaluate(r)
        assert any("smart wallet" in ev.lower() for ev in q.evidence)

    def test_lock_burned_evidence(self):
        r = _base_result(liquidity_lock=LiquidityLock(status="burned"))
        q = evaluate(r)
        assert any("burned" in ev.lower() for ev in q.evidence)

    def test_lock_unlocked_warning(self):
        r = _base_result(liquidity_lock=LiquidityLock(status="unlocked"))
        q = evaluate(r)
        assert any("unlocked" in w.lower() for w in q.warnings)

    def test_fdv_fallback_warning(self):
        r = _base_result(market_data=TokenMarketData(
            pair_address="0x" + "b" * 40,
            price_usd="1.0",
            market_cap=None,
            fdv=100000.0,
            liquidity=LiquiditySnapshot(usd=5000.0),
            volume=VolumeSnapshot(h24=1000.0),
        ))
        q = evaluate(r)
        assert any("fdv" in w.lower() for w in q.warnings)

    def test_pros_cons_populated(self):
        """A typical token should have non-empty evidence and warnings."""
        q = evaluate(_base_result())
        assert len(q.evidence) > 0
        assert len(q.warnings) > 0

    def test_zero_liq_in_warnings(self):
        r = _base_result(market_data=TokenMarketData(
            pair_address="0x" + "b" * 40,
            price_usd="1.0",
            liquidity=LiquiditySnapshot(usd=0.0),
        ))
        q = evaluate(r)
        assert any("zero" in w.lower() and "liquidity" in w.lower() for w in q.warnings)

    def test_verified_contract_in_evidence(self):
        r = _base_result(contract_intel=ContractIntel(verified=True))
        q = evaluate(r)
        assert any("verified" in ev.lower() for ev in q.evidence)

    def test_no_market_data_warnings(self):
        """No market data with holders → not excluded, but warnings present."""
        r = _base_result(market_data=None)
        q = evaluate(r)
        assert any("no market data" in w.lower() for w in q.warnings)


# ── Backward compatibility ───────────────────────────────────────


class TestBackwardCompat:
    def test_eligibility_result_alias(self):
        from app.models.token import EligibilityResult
        assert EligibilityResult is QualificationResult

    def test_excluded_has_rejection_reasons(self):
        r = _base_result(honeypot=HoneypotResult(status="honeypot"))
        q = evaluate(r)
        assert q.qualification_level == "excluded"
        assert len(q.rejection_reasons) > 0

    def test_non_excluded_has_empty_rejection_reasons(self):
        q = evaluate(_base_result())
        assert q.rejection_reasons == []
