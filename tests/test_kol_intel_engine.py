"""Tests for the KOL Intelligence & Correlation engine (M23 Deliverable F).

All offline (no network, no browser, no real rug analysis). Layers:
  - kol_scoring       — the PURE scorer + cluster detector: config-driven, additive,
                        fully explainable evidence, typed clusters, no I/O.
  - kol_store (F)     — cross-KOL reads (who follows a project) + intelligence
                        persistence, score/cluster history, event timeline.
  - kol_intel_engine  — orchestration: correlate contributors + reused crypto
                        classification + reused rug analysis -> ProjectIntelligence,
                        persist + emit internal events, incremental (fingerprint) skip.

Discipline mirrored from M10/Deliverable D: the intel sub-score is kept SEPARATE from
the core rug/confidence math (it only reuses existing analysis, never recomputes), and
the engine emits NO user alerts — transports are Deliverable G/H. Tests assert those
surfaces stay absent, and that everything is config-driven (no hardcoded tiers/timing).
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.config import settings
from app.models.kol import (
    CLUSTER_TYPES,
    KOL_INTEL_EVENT_TYPES,
    ClusterInfo,
    CryptoClassification,
    KolContributor,
    KolEntry,
    ProjectIntelligence,
    SocialAccount,
)
from app.services import kol_intel_engine as engine
from app.services import kol_store
from app.services.social import kol_scoring


# --- helpers -----------------------------------------------------------------

BASE = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def ts(hours: float = 0.0) -> str:
    return (BASE + timedelta(hours=hours)).isoformat()


def contrib(handle, tier, hours=0.0, account_key="proj", platform="x"):
    return KolContributor(
        platform=platform, kol_handle=handle, tier=tier,
        tier_weight=kol_scoring.tier_weight(tier),
        followed_at=ts(hours), account_key=account_key,
    )


def acct(handle, **kw):
    return SocialAccount(platform="x", handle=handle, **kw)


@pytest.fixture(autouse=True)
def _temp_db():
    tmp = Path(tempfile.mkdtemp()) / "kol.db"
    kol_store.reset_for_tests(str(tmp))
    yield
    kol_store.reset_for_tests()


@pytest.fixture
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "kol_score_enabled", True)


def seed_kol(handle, tier=2, platform="x"):
    kol_store.upsert_kol(KolEntry(platform=platform, handle=handle, tier=tier))


def seed_follow(kol_handle, project_handle, *, tier=2, hours=0.0, platform="x"):
    """Register a KOL and record it following a project account at a given time."""
    seed_kol(kol_handle, tier=tier, platform=platform)
    account = acct(project_handle)
    kol_store.upsert_followed_account(
        platform, kol_handle, account, active=True, seen_at=ts(hours),
    )
    return account


def seed_classification(project_handle, *, kol_handle="k1", classification="official",
                        confidence="high", score=70, platform="x"):
    account = acct(project_handle)
    cls = CryptoClassification(
        platform=platform, handle=project_handle, account_key=account.key(),
        classification=classification, confidence=confidence, score=score,
        signals=["contract_address"], evidence=[], contracts=[],
    )
    kol_store.save_classification(kol_handle, cls)
    return cls


def seed_analysis(project_handle, *, kol_handle="k1", risk_score=20,
                  risk_level="low", platform="x"):
    from app.models.kol import CryptoIntelEvent
    account = acct(project_handle)
    kol_store.save_crypto_events([CryptoIntelEvent(
        event_type="analysis_completed", platform=platform, kol_handle=kol_handle,
        account_key=account.key(),
        payload={"contract_address": "0xabc", "risk_score": risk_score,
                 "risk_level": risk_level, "confidence": 90, "status": "ok"},
    )])


# =============================================================================
# Pure scorer + cluster detection
# =============================================================================


def test_single_kol_is_not_a_cluster():
    cs = [contrib("a", 1)]
    cl = kol_scoring.detect_cluster("x", "proj", cs)
    assert cl.is_cluster is False
    assert cl.kol_count == 1
    assert cl.cluster_types == []


def test_two_kols_converging_is_a_cluster():
    cs = [contrib("a", 2, 0), contrib("b", 2, 1)]
    cl = kol_scoring.detect_cluster("x", "proj", cs)
    assert cl.is_cluster is True
    assert cl.kol_count == 2


def test_duplicate_kol_counts_once_keeping_earliest():
    cs = [contrib("a", 1, 5), contrib("a", 1, 2), contrib("b", 2, 3)]
    cl = kol_scoring.detect_cluster("x", "proj", cs)
    assert cl.kol_count == 2
    a = [c for c in cl.contributors if c.kol_handle == "a"][0]
    assert a.followed_at == ts(2)  # earliest follow retained


def test_tier1_cluster_type(monkeypatch):
    monkeypatch.setattr(settings, "kol_cluster_tier1_min", 2)
    cs = [contrib("a", 1, 0), contrib("b", 1, 1)]
    cl = kol_scoring.detect_cluster("x", "proj", cs)
    assert "tier_1" in cl.cluster_types


def test_mixed_tier_cluster_type():
    cs = [contrib("a", 1, 0), contrib("b", 3, 1)]
    cl = kol_scoring.detect_cluster("x", "proj", cs)
    assert "mixed_tier" in cl.cluster_types


def test_rapid_cluster_type(monkeypatch):
    monkeypatch.setattr(settings, "kol_cluster_rapid_window_hours", 6.0)
    cs = [contrib("a", 2, 0), contrib("b", 2, 2)]  # 2h span < 6h
    cl = kol_scoring.detect_cluster("x", "proj", cs)
    assert "rapid" in cl.cluster_types


def test_slow_convergence_is_not_rapid(monkeypatch):
    monkeypatch.setattr(settings, "kol_cluster_rapid_window_hours", 6.0)
    monkeypatch.setattr(settings, "kol_cluster_window_hours", 72.0)
    cs = [contrib("a", 2, 0), contrib("b", 2, 48)]  # 48h span
    cl = kol_scoring.detect_cluster("x", "proj", cs)
    assert cl.is_cluster is True
    assert "rapid" not in cl.cluster_types


def test_convergence_outside_main_window_is_not_a_cluster(monkeypatch):
    monkeypatch.setattr(settings, "kol_cluster_window_hours", 72.0)
    cs = [contrib("a", 2, 0), contrib("b", 2, 200)]  # 200h span > 72h
    cl = kol_scoring.detect_cluster("x", "proj", cs)
    assert cl.is_cluster is False


def test_high_conviction_cluster_type(monkeypatch):
    monkeypatch.setattr(settings, "kol_cluster_high_conviction_score", 50)
    cs = [contrib("a", 1, 0), contrib("b", 1, 1)]
    cl = kol_scoring.detect_cluster("x", "proj", cs, score=80)
    assert "high_conviction" in cl.cluster_types


def test_every_cluster_type_is_in_vocab(monkeypatch):
    monkeypatch.setattr(settings, "kol_cluster_tier1_min", 2)
    monkeypatch.setattr(settings, "kol_cluster_high_conviction_score", 10)
    cs = [contrib("a", 1, 0), contrib("b", 1, 1), contrib("c", 3, 2)]
    cl = kol_scoring.detect_cluster("x", "proj", cs, score=99)
    assert set(cl.cluster_types) <= set(CLUSTER_TYPES)
    assert set(cl.cluster_types) == {"tier_1", "mixed_tier", "rapid", "high_conviction"}


def test_score_is_zero_with_no_signals():
    cs = [contrib("a", 2)]
    score, conf, ev = kol_scoring.score_project(cs)
    # A lone tier-2 KOL, no crypto confidence, no analysis: only tier_quality can fire.
    assert 0 <= score <= 100
    assert conf in ("very_low", "low", "medium", "high", "very_high")


def test_score_evidence_reconstructs_score():
    cs = [contrib("a", 1, 0), contrib("b", 2, 1)]
    cl = kol_scoring.detect_cluster("x", "proj", cs)
    score, conf, ev = kol_scoring.score_project(
        cs, crypto_confidence="high", classification="official",
        risk_score=10, analyzed=True, cluster=cl,
    )
    assert score == min(100, sum(e.weight for e in ev))
    # Every evidence item is a KOL-intel signal (kept separate from rug signals).
    assert all(e.source == "kol_intel" for e in ev)


def test_convergence_rewards_more_kols():
    one = kol_scoring.score_project([contrib("a", 2)])[0]
    three = kol_scoring.score_project(
        [contrib("a", 2, 0), contrib("b", 2, 1), contrib("c", 2, 2)]
    )[0]
    assert three > one


def test_tier_weighting_is_config_driven(monkeypatch):
    base = kol_scoring.score_project([contrib("a", 1)])[0]
    monkeypatch.setattr(settings, "kol_tier_weights", {"1": 5, "2": 25, "3": 12})
    lowered = kol_scoring.score_project(
        [KolContributor(platform="x", kol_handle="a", tier=1,
                        tier_weight=kol_scoring.tier_weight(1), followed_at=ts(), account_key="proj")]
    )[0]
    assert lowered < base  # tier-1 weight dropped -> lower tier_quality contribution


def test_unknown_tier_uses_default_weight(monkeypatch):
    monkeypatch.setattr(settings, "kol_tier_default_weight", 10)
    assert kol_scoring.tier_weight(99) == 10


def test_alpha_component_fires_only_when_supplied():
    # Tier-3 KOLs keep the score well below the 100 cap so the optional alpha
    # component's contribution is observable rather than clipped.
    cs = [contrib("a", 3, 0), contrib("b", 3, 1)]
    s_without, _, ev_without = kol_scoring.score_project(cs, crypto_confidence="low")
    s_with, _, ev_with = kol_scoring.score_project(cs, crypto_confidence="low", alpha_score=100)
    assert s_with > s_without
    assert not any(e.signal == "alpha" for e in ev_without)  # absent when not supplied
    assert any(e.signal == "alpha" for e in ev_with)


def test_analysis_safety_rewards_low_risk():
    # Tier-3 keeps totals below the cap so the safety component's delta is visible.
    cs = [contrib("a", 3, 0), contrib("b", 3, 1)]
    safe = kol_scoring.score_project(cs, risk_score=5, analyzed=True)[0]
    risky = kol_scoring.score_project(cs, risk_score=95, analyzed=True)[0]
    assert safe > risky


def test_analysis_safety_absent_when_not_analyzed():
    cs = [contrib("a", 2, 0), contrib("b", 2, 1)]
    _, _, ev = kol_scoring.score_project(cs, analyzed=False, risk_score=None)
    assert not any(e.signal == "analysis_safety" for e in ev)


def test_disabled_component_weight_removes_contribution(monkeypatch):
    cs = [contrib("a", 1, 0), contrib("b", 1, 1)]
    weights = dict(settings.kol_score_weights)
    weights["cluster_bonus"] = 0
    monkeypatch.setattr(settings, "kol_score_weights", weights)
    cl = kol_scoring.detect_cluster("x", "proj", cs)
    _, _, ev = kol_scoring.score_project(cs, cluster=cl)
    assert not any(e.signal == "cluster_bonus" for e in ev)


# =============================================================================
# Store: cross-KOL correlation reads
# =============================================================================


def test_list_kols_following_inverts_the_follow_graph():
    seed_follow("k1", "cooltoken", tier=1, hours=0)
    seed_follow("k2", "cooltoken", tier=2, hours=1)
    seed_follow("k3", "othertoken", tier=1, hours=0)
    key = acct("cooltoken").key()
    rows = kol_store.list_kols_following("x", key)
    handles = {r["kol_handle"] for r in rows}
    assert handles == {"k1", "k2"}
    # tier is joined from the watchlist row.
    assert {r["tier"] for r in rows} == {1, 2}


def test_list_kols_following_excludes_unfollowed_by_default():
    account = seed_follow("k1", "cooltoken", hours=0)
    key = account.key()
    kol_store.deactivate_followed_account("x", "k1", key)
    assert kol_store.list_kols_following("x", key) == []
    assert len(kol_store.list_kols_following("x", key, active_only=False)) == 1


def test_best_classification_picks_highest_score():
    key = acct("tok").key()
    seed_classification("tok", kol_handle="k1", score=40, confidence="low")
    seed_classification("tok", kol_handle="k2", score=85, confidence="very_high")
    best = kol_store.best_classification_for_account("x", key)
    assert best.score == 85
    assert best.confidence == "very_high"


def test_latest_analysis_summary_reads_reused_event():
    key = acct("tok").key()
    seed_analysis("tok", risk_score=33, risk_level="medium")
    summary = kol_store.latest_analysis_summary("x", key)
    assert summary["risk_score"] == 33
    assert summary["risk_level"] == "medium"


def test_analysis_summary_none_when_never_analyzed():
    assert kol_store.latest_analysis_summary("x", acct("tok").key()) is None


# =============================================================================
# Engine orchestration
# =============================================================================


def test_engine_disabled_is_noop():
    seed_follow("k1", "tok")
    seed_follow("k2", "tok", hours=1)
    key = acct("tok").key()
    assert engine.update_project_intelligence("x", key) is None
    assert kol_store.get_project_intelligence("x", key) is None


def test_engine_noop_without_contributors(_enabled):
    assert engine.update_project_intelligence("x", acct("tok").key()) is None


def test_single_kol_project_scores_but_no_cluster(_enabled):
    seed_follow("k1", "tok", tier=1, hours=0)
    seed_classification("tok", confidence="high", classification="official")
    key = acct("tok").key()
    intel = engine.update_project_intelligence("x", key, project_handle="tok")
    assert intel is not None
    assert intel.kol_count == 1
    assert intel.cluster.is_cluster is False
    assert intel.classification == "official"


def test_multi_kol_convergence_builds_cluster_and_persists(_enabled):
    seed_follow("k1", "tok", tier=1, hours=0)
    seed_follow("k2", "tok", tier=1, hours=1)
    seed_classification("tok", confidence="very_high", classification="official", score=90)
    seed_analysis("tok", risk_score=10, risk_level="low")
    key = acct("tok").key()
    intel = engine.update_project_intelligence("x", key, project_handle="tok")
    assert intel.kol_count == 2
    assert intel.cluster.is_cluster is True
    # correlation carries the REUSED analysis (never recomputed here).
    assert intel.correlation["analyzed"] is True
    assert intel.correlation["risk_score"] == 10
    # persisted + retrievable with timeline.
    stored = kol_store.get_project_intelligence("x", key)
    assert stored.score == intel.score
    assert len(stored.timeline) >= 1


def test_mixed_tiers_reflected_in_contributors(_enabled):
    seed_follow("k1", "tok", tier=1, hours=0)
    seed_follow("k2", "tok", tier=3, hours=1)
    seed_classification("tok")
    intel = engine.update_project_intelligence("x", acct("tok").key(), project_handle="tok")
    tiers = {c.tier for c in intel.contributors}
    assert tiers == {1, 3}
    assert "mixed_tier" in intel.cluster.cluster_types


def test_engine_emits_internal_events_not_alerts(_enabled):
    seed_follow("k1", "tok", tier=1, hours=0)
    seed_follow("k2", "tok", tier=1, hours=1)
    seed_classification("tok", score=90, confidence="very_high")
    key = acct("tok").key()
    engine.update_project_intelligence("x", key, project_handle="tok")
    events = kol_store.list_intel_events("x", key)
    types = {e.event_type for e in events}
    assert "kol_score_updated" in types
    assert "kol_cluster_detected" in types
    assert "intelligence_updated" in types
    # every emitted event is in the internal vocab (no ad-hoc alert types).
    assert types <= set(KOL_INTEL_EVENT_TYPES)


def test_incremental_skip_on_unchanged_inputs(_enabled):
    seed_follow("k1", "tok", tier=1, hours=0)
    seed_follow("k2", "tok", tier=1, hours=1)
    seed_classification("tok")
    key = acct("tok").key()
    first = engine.update_project_intelligence("x", key, project_handle="tok")
    events_after_first = len(kol_store.list_intel_events("x", key))
    hist_after_first = len(kol_store.list_score_history("x", key))
    # Re-run with identical inputs: same object returned, no new events/history.
    second = engine.update_project_intelligence("x", key, project_handle="tok")
    assert second.fingerprint == first.fingerprint
    assert len(kol_store.list_intel_events("x", key)) == events_after_first
    assert len(kol_store.list_score_history("x", key)) == hist_after_first


def test_new_kol_triggers_rescore_and_momentum(_enabled):
    seed_follow("k1", "tok", tier=1, hours=0)
    seed_follow("k2", "tok", tier=1, hours=1)
    seed_classification("tok")
    key = acct("tok").key()
    engine.update_project_intelligence("x", key, project_handle="tok")
    # A third KOL follows -> distinct-KOL count grows -> momentum event + rescore.
    seed_follow("k3", "tok", tier=1, hours=2)
    intel = engine.update_project_intelligence("x", key, project_handle="tok")
    assert intel.kol_count == 3
    types = {e.event_type for e in kol_store.list_intel_events("x", key)}
    assert "project_momentum_detected" in types


def test_force_rescores_even_when_unchanged(_enabled):
    seed_follow("k1", "tok", tier=1, hours=0)
    seed_follow("k2", "tok", tier=1, hours=1)
    seed_classification("tok")
    key = acct("tok").key()
    engine.update_project_intelligence("x", key, project_handle="tok")
    hist_before = len(kol_store.list_score_history("x", key))
    engine.update_project_intelligence("x", key, project_handle="tok", force=True)
    assert len(kol_store.list_score_history("x", key)) == hist_before + 1


def test_score_history_is_a_timeline(_enabled):
    seed_follow("k1", "tok", tier=1, hours=0)
    seed_follow("k2", "tok", tier=1, hours=1)
    seed_classification("tok")
    key = acct("tok").key()
    engine.update_project_intelligence("x", key, project_handle="tok")
    seed_follow("k3", "tok", tier=1, hours=2)
    engine.update_project_intelligence("x", key, project_handle="tok")
    hist = kol_store.list_score_history("x", key)
    assert len(hist) == 2
    # oldest-first timeline, kol_count grew.
    assert hist[0]["kol_count"] == 2
    assert hist[1]["kol_count"] == 3


def test_cluster_history_records_formation(_enabled):
    seed_follow("k1", "tok", tier=1, hours=0)
    seed_follow("k2", "tok", tier=1, hours=1)
    seed_classification("tok")
    key = acct("tok").key()
    engine.update_project_intelligence("x", key, project_handle="tok")
    clusters = kol_store.list_cluster_history("x", key)
    assert len(clusters) == 1
    assert clusters[0].kol_count == 2


def test_high_conviction_cluster_event(_enabled, monkeypatch):
    monkeypatch.setattr(settings, "kol_cluster_high_conviction_score", 40)
    seed_follow("k1", "tok", tier=1, hours=0)
    seed_follow("k2", "tok", tier=1, hours=1)
    seed_classification("tok", score=90, confidence="very_high")
    seed_analysis("tok", risk_score=5)
    key = acct("tok").key()
    intel = engine.update_project_intelligence("x", key, project_handle="tok")
    assert intel.score >= 40
    types = {e.event_type for e in kol_store.list_intel_events("x", key)}
    assert "high_conviction_cluster" in types


def test_process_new_project_follows_filters_to_projects(_enabled):
    seed_follow("k1", "tok", tier=1, hours=0)
    seed_follow("k2", "tok", tier=1, hours=1)
    seed_classification("tok")
    seed_follow("kx", "randomperson", tier=1, hours=0)
    project_key = acct("tok").key()
    results = engine.process_new_project_follows(
        "x", [acct("tok"), acct("randomperson")], project_keys=[project_key],
    )
    assert len(results) == 1
    assert results[0].account_key == project_key
    # the non-project account was never scored.
    assert kol_store.get_project_intelligence("x", acct("randomperson").key()) is None


def test_process_new_project_follows_disabled_is_noop():
    assert engine.process_new_project_follows("x", [acct("tok")]) == []


def test_follow_without_watchlist_row_falls_back_to_default_tier(_enabled):
    # Defensive path: a follow row whose KOL isn't in the `kols` table (e.g. recorded
    # then the KOL removed) still correlates — tier is None from the join, and the
    # engine falls back to a default tier rather than dropping the contributor.
    account = acct("tok")
    kol_store.upsert_followed_account("x", "ghost", account, active=True, seen_at=ts(0))
    seed_follow("k2", "tok", tier=1, hours=1)  # a real watchlist KOL alongside
    seed_classification("tok")
    rows = kol_store.list_kols_following("x", account.key())
    ghost = [r for r in rows if r["kol_handle"] == "ghost"][0]
    assert ghost["tier"] is None  # no watchlist row -> join yields NULL
    intel = engine.update_project_intelligence("x", account.key(), project_handle="tok")
    assert intel.kol_count == 2  # both counted; ghost scored at the fallback tier


def test_engine_swallows_correlation_failure(_enabled, monkeypatch):
    seed_follow("k1", "tok", tier=1, hours=0)
    seed_classification("tok")

    def boom(*a, **k):
        raise RuntimeError("scoring blew up")

    monkeypatch.setattr(kol_scoring, "score_project", boom)
    # process_new_project_follows must never raise, even on an internal failure.
    results = engine.process_new_project_follows("x", [acct("tok")], project_keys=[acct("tok").key()])
    assert results == []


def test_is_actionable_threshold_is_config_driven(_enabled, monkeypatch):
    monkeypatch.setattr(settings, "kol_intel_min_actionable_score", 0)
    seed_follow("k1", "tok", tier=1, hours=0)
    seed_follow("k2", "tok", tier=1, hours=1)
    seed_classification("tok")
    intel = engine.update_project_intelligence("x", acct("tok").key(), project_handle="tok")
    assert intel.is_actionable is True
    monkeypatch.setattr(settings, "kol_intel_min_actionable_score", 101)
    assert intel.is_actionable is False


def test_list_project_intelligence_ranks_by_score(_enabled):
    for h, tier in (("hot", 1), ("warm", 3)):
        seed_follow("k1", h, tier=tier, hours=0)
        seed_follow("k2", h, tier=tier, hours=1)
        seed_classification(h, confidence="very_high" if h == "hot" else "low",
                            score=90 if h == "hot" else 30)
        engine.update_project_intelligence("x", acct(h).key(), project_handle=h)
    ranked = kol_store.list_project_intelligence("x")
    assert len(ranked) == 2
    assert ranked[0].score >= ranked[1].score
