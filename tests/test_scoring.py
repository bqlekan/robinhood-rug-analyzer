"""Unit tests for the risk scoring engine."""

from app.models.token import (
    ClusterAnalysis,
    ContractPrivileges,
    DevProfile,
    HolderCluster,
    HolderDistribution,
    HoneypotResult,
    LaunchpadInfo,
    LiquidityLock,
    LiquiditySnapshot,
    TokenAge,
    TokenMarketData,
    VolumeSnapshot,
)
from app.services.scoring import score_token


def _clean_kwargs():
    """A clean-token baseline so a honeypot result's contribution is isolated."""
    return dict(
        age=TokenAge(age_hours=2400, age_days=100, source="pair_created_at"),
        market=_healthy_market(),
        holders=HolderDistribution(holder_count=5000, top10_percentage=25, top1_percentage=5),
        clusters=ClusterAnalysis(clusters=[], clustered_percentage=0),
        dev=DevProfile(dev_holding_percentage=1, reputation="clean"),
        liquidity_lock=LiquidityLock(status="locked", locked_percentage=100),
        launchpad=LaunchpadInfo(name="NOXA Fun", confidence="high"),
        lore=None,
        data_sources=["test"],
    )


def test_honeypot_status_scores_critical():
    base = score_token(**_clean_kwargs())
    hp = score_token(**_clean_kwargs(), honeypot=HoneypotResult(status="honeypot"))
    # +40 points, surfaced as a critical-severity signal (overall level depends on
    # the other dimensions; on a clean baseline 40 alone is "medium").
    assert hp.risk_score - base.risk_score == 40
    assert any(s.category == "honeypot" and s.severity == "critical" for s in hp.signals)


def test_high_tax_scores_high_signal():
    hp = score_token(**_clean_kwargs(), honeypot=HoneypotResult(status="high_tax", sell_tax_percentage=55.0))
    assert any(s.category == "honeypot" and s.severity == "high" and s.points == 20 for s in hp.signals)


def test_sellable_and_unknown_add_no_signal_and_no_confidence_change():
    base = score_token(**_clean_kwargs())
    for status in ("sellable", "unknown"):
        r = score_token(**_clean_kwargs(), honeypot=HoneypotResult(status=status))
        assert not any(s.category == "honeypot" for s in r.signals)
        assert r.risk_score == base.risk_score
        # Honeypot is deliberately absent from confidence weights: unchanged either way.
        assert r.confidence == base.confidence


def test_retained_powers_score_when_owner_not_renounced():
    base = score_token(**_clean_kwargs())
    priv = ContractPrivileges(
        analyzed=True, ownership_renounced=False, can_mint=True, can_blacklist=True,
        can_pause=True, can_set_fees=True,
    )
    r = score_token(**_clean_kwargs(), privileges=priv)
    cats = [s for s in r.signals if s.category == "privileges"]
    assert {s.name for s in cats} == {
        "Mintable supply", "Blacklist/denylist power", "Pausable transfers", "Mutable fees/tax",
    }
    assert r.risk_score - base.risk_score == 18 + 18 + 15 + 10


def test_renounced_owner_silences_power_signals():
    priv = ContractPrivileges(
        analyzed=True, ownership_renounced=True, can_mint=True, can_blacklist=True,
        can_pause=True, can_set_fees=True,
    )
    r = score_token(**_clean_kwargs(), privileges=priv)
    assert not any(s.category == "privileges" and s.name != "Trading currently paused" for s in r.signals)


def test_unconfirmed_ownership_still_flags_powers():
    # None ownership (couldn't confirm) must NOT be treated as renounced.
    priv = ContractPrivileges(analyzed=True, ownership_renounced=None, can_mint=True)
    r = score_token(**_clean_kwargs(), privileges=priv)
    assert any(s.name == "Mintable supply" for s in r.signals)


def test_live_paused_scores_critical_regardless_of_ownership():
    priv = ContractPrivileges(analyzed=True, ownership_renounced=True, can_pause=True, is_paused=True)
    r = score_token(**_clean_kwargs(), privileges=priv)
    assert any(s.name == "Trading currently paused" and s.severity == "critical" for s in r.signals)


def test_unanalyzed_privileges_add_nothing():
    base = score_token(**_clean_kwargs())
    r = score_token(**_clean_kwargs(), privileges=ContractPrivileges(analyzed=False))
    assert not any(s.category == "privileges" for s in r.signals)
    assert r.risk_score == base.risk_score
    assert r.confidence == base.confidence


def _cluster(pct: float) -> HolderCluster:
    return HolderCluster(funder_address="0xf", member_addresses=["0xa", "0xb"], combined_percentage=pct)


def _healthy_market():
    return TokenMarketData(
        liquidity=LiquiditySnapshot(usd=100_000),
        volume=VolumeSnapshot(h24=50_000),
    )


def test_clean_token_scores_low():
    analysis = score_token(
        age=TokenAge(age_hours=2400, age_days=100, source="pair_created_at"),
        market=_healthy_market(),
        holders=HolderDistribution(holder_count=5000, top10_percentage=25, top1_percentage=5),
        clusters=ClusterAnalysis(clusters=[], clustered_percentage=0),
        dev=DevProfile(dev_holding_percentage=1, reputation="clean"),
        liquidity_lock=LiquidityLock(status="locked", locked_percentage=100),
        launchpad=LaunchpadInfo(name="NOXA Fun", confidence="high"),
        lore=None,
        data_sources=["test"],
    )
    assert analysis.risk_level == "low"
    assert analysis.risk_score < 25


def test_obvious_rug_scores_critical():
    analysis = score_token(
        age=TokenAge(age_hours=2, age_days=0.08, source="pair_created_at"),
        market=TokenMarketData(liquidity=LiquiditySnapshot(usd=500), volume=VolumeSnapshot(h24=100)),
        holders=HolderDistribution(holder_count=12, top10_percentage=95, top1_percentage=60),
        clusters=ClusterAnalysis(clusters=[_cluster(40)], clustered_percentage=40),
        dev=DevProfile(dev_holding_percentage=40, reputation="serial_rugger", tokens_rugged=5),
        liquidity_lock=LiquidityLock(status="unlocked"),
        launchpad=LaunchpadInfo(name="Unknown", confidence="low"),
        lore=None,
        data_sources=["test"],
    )
    assert analysis.risk_level == "critical"
    assert analysis.risk_score >= 75


def test_missing_market_is_penalized():
    analysis = score_token(
        age=TokenAge(age_hours=None, age_days=None, source=None),
        market=None,
        holders=None,
        clusters=None,
        dev=None,
        liquidity_lock=None,
        launchpad=None,
        lore=None,
        data_sources=["test"],
    )
    names = {s.name for s in analysis.signals}
    assert "No market pair found" in names


def test_score_is_capped_at_100():
    analysis = score_token(
        age=TokenAge(age_hours=1, age_days=0.04, source="pair_created_at"),
        market=TokenMarketData(liquidity=LiquiditySnapshot(usd=10), volume=VolumeSnapshot(h24=0)),
        holders=HolderDistribution(holder_count=5, top10_percentage=99, top1_percentage=90),
        clusters=ClusterAnalysis(clusters=[_cluster(40), _cluster(40)], clustered_percentage=80),
        dev=DevProfile(dev_holding_percentage=50, reputation="serial_rugger", tokens_rugged=10),
        liquidity_lock=LiquidityLock(status="unlocked"),
        launchpad=LaunchpadInfo(name="Unknown", confidence="low"),
        lore=None,
        data_sources=["test"],
    )
    assert analysis.risk_score == 100


def test_signals_have_categories_and_points():
    analysis = score_token(
        age=TokenAge(age_hours=10, age_days=0.4, source="pair_created_at"),
        market=_healthy_market(),
        holders=HolderDistribution(holder_count=5000, top10_percentage=20, top1_percentage=3),
        clusters=None,
        dev=None,
        liquidity_lock=LiquidityLock(status="locked"),
        launchpad=LaunchpadInfo(name="NOXA Fun", confidence="high"),
        lore=None,
        data_sources=["test"],
    )
    for signal in analysis.signals:
        assert signal.points > 0
        assert signal.category
        assert signal.severity in {"low", "medium", "high", "critical"}


def test_confidence_high_with_full_data():
    # M7: every core input present -> high confidence, independent of risk_score.
    analysis = score_token(
        age=TokenAge(age_hours=2400, age_days=100, source="pair_created_at"),
        market=_healthy_market(),
        holders=HolderDistribution(holder_count=5000, top10_percentage=25, top1_percentage=5),
        clusters=ClusterAnalysis(clusters=[], clustered_percentage=0),
        dev=DevProfile(creator_address="0xdev", reputation="clean"),
        liquidity_lock=LiquidityLock(status="locked", locked_percentage=100),
        launchpad=LaunchpadInfo(name="NOXA Fun", confidence="high"),
        lore=None,
        data_sources=["test"],
    )
    assert analysis.confidence == 100
    assert analysis.confidence_level == "high"


def test_confidence_low_when_sources_missing():
    # M7: nothing available -> low confidence, so a low risk_score is not read as "safe".
    analysis = score_token(
        age=TokenAge(age_hours=None, age_days=None, source=None),
        market=None,
        holders=None,
        clusters=None,
        dev=None,
        liquidity_lock=None,
        launchpad=None,
        lore=None,
        data_sources=["test"],
    )
    assert analysis.confidence < 40
    assert analysis.confidence_level == "low"


def test_confidence_does_not_affect_risk_score():
    # M7: confidence is additive metadata; the clean-token risk assertions still hold.
    analysis = score_token(
        age=TokenAge(age_hours=2400, age_days=100, source="pair_created_at"),
        market=_healthy_market(),
        holders=HolderDistribution(holder_count=5000, top10_percentage=25, top1_percentage=5),
        clusters=ClusterAnalysis(clusters=[], clustered_percentage=0),
        dev=DevProfile(dev_holding_percentage=1, reputation="clean"),
        liquidity_lock=LiquidityLock(status="locked", locked_percentage=100),
        launchpad=LaunchpadInfo(name="NOXA Fun", confidence="high"),
        lore=None,
        data_sources=["test"],
    )
    assert analysis.risk_level == "low"
    assert analysis.risk_score < 25
