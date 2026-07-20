"""Unit tests for M19 historical snapshots & trend detection.

Three layers:
- snapshot_store — append/prune/latest round-trip with configurable retention (defensive).
- analyzers.analyze_trend — the pure delta math (first sighting -> no trend; adverse moves flag).
- scoring.score_token — the trend risk signal (only an adverse, threshold-crossing trend scores).
"""

import tempfile
from pathlib import Path

import pytest

from app.core.config import settings
from app.services import analyzers, snapshot_store
from app.services.scoring import score_token
from app.models.token import TokenTrend
from tests.test_scoring import _clean_kwargs


@pytest.fixture(autouse=True)
def _temp_db():
    tmp = Path(tempfile.mkdtemp()) / "snapshots.db"
    snapshot_store.reset_for_tests(str(tmp))
    yield
    snapshot_store.reset_for_tests()


TOKEN = "0x" + "ab" * 20


# --- snapshot_store round-trip + retention ---


def test_record_and_latest_round_trip():
    snapshot_store.record_snapshot(TOKEN, risk_score=40, liquidity_usd=10_000.0,
                                   top10_percentage=30.0, holder_count=100)
    latest = snapshot_store.latest_snapshot(TOKEN)
    assert latest is not None
    assert latest["risk_score"] == 40
    assert latest["liquidity_usd"] == 10_000.0
    assert latest["top10_percentage"] == 30.0
    assert latest["holder_count"] == 100


def test_latest_returns_most_recent():
    snapshot_store.record_snapshot(TOKEN, risk_score=10, liquidity_usd=1.0)
    snapshot_store.record_snapshot(TOKEN, risk_score=20, liquidity_usd=2.0)
    assert snapshot_store.latest_snapshot(TOKEN)["risk_score"] == 20


def test_latest_missing_returns_none():
    assert snapshot_store.latest_snapshot("0xnever") is None
    assert snapshot_store.latest_snapshot("") is None


def test_retention_prunes_old_rows(monkeypatch):
    monkeypatch.setattr(settings, "snapshot_history_retain", 3)
    for i in range(6):
        snapshot_store.record_snapshot(TOKEN, risk_score=i)
    rows = snapshot_store.list_snapshots(TOKEN, limit=100)
    assert len(rows) == 3
    # The 3 most recent (risk_score 5,4,3) survive, newest first.
    assert [r["risk_score"] for r in rows] == [5, 4, 3]


# --- analyze_trend (pure) ---


def test_first_sighting_has_no_trend():
    trend = analyzers.analyze_trend(
        None, current_liquidity_usd=1000.0, current_top10_percentage=20.0, current_holder_count=50
    )
    assert trend.has_prior is False
    assert trend.signals == []
    assert trend.liquidity_change_pct is None


def test_liquidity_drop_flags_downward_trend():
    prior = {"captured_at": "t0", "risk_score": 30, "liquidity_usd": 10_000.0,
             "top10_percentage": 20.0, "holder_count": 100}
    trend = analyzers.analyze_trend(
        prior, current_liquidity_usd=4_000.0, current_top10_percentage=20.0,
        current_holder_count=100, liquidity_drop_pct=40.0,
    )
    assert trend.has_prior is True
    assert trend.liquidity_change_pct == -60.0
    assert any("liquidity fell" in s.lower() for s in trend.signals)


def test_liquidity_recovery_is_not_a_signal():
    prior = {"captured_at": "t0", "liquidity_usd": 1_000.0}
    trend = analyzers.analyze_trend(
        prior, current_liquidity_usd=5_000.0, current_top10_percentage=None,
        current_holder_count=None, liquidity_drop_pct=40.0,
    )
    assert trend.liquidity_change_pct == 400.0
    assert trend.signals == []  # a rise is reassuring, never risk


def test_concentration_rise_flags_trend():
    prior = {"captured_at": "t0", "top10_percentage": 30.0}
    trend = analyzers.analyze_trend(
        prior, current_liquidity_usd=None, current_top10_percentage=50.0,
        current_holder_count=None, concentration_rise_pct=15.0,
    )
    assert trend.concentration_change_pct == 20.0
    assert any("concentration rose" in s.lower() for s in trend.signals)


def test_small_moves_below_threshold_do_not_flag():
    prior = {"captured_at": "t0", "liquidity_usd": 1_000.0, "top10_percentage": 30.0}
    trend = analyzers.analyze_trend(
        prior, current_liquidity_usd=900.0, current_top10_percentage=33.0,
        current_holder_count=None, liquidity_drop_pct=40.0, concentration_rise_pct=15.0,
    )
    # -10% liquidity and +3pts concentration are both under threshold.
    assert trend.signals == []


# --- scoring integration ---


def test_liquidity_drop_trend_scores_signal():
    kwargs = _clean_kwargs()
    kwargs["trend"] = TokenTrend(has_prior=True, liquidity_change_pct=-60.0,
                                 signals=["Liquidity fell 60.0% since the previous snapshot"],
                                 detail="drop")
    result = score_token(**kwargs)
    assert any(s.name == "Liquidity trending down" for s in result.signals)


def test_concentration_rise_trend_scores_signal():
    kwargs = _clean_kwargs()
    kwargs["trend"] = TokenTrend(has_prior=True, concentration_change_pct=20.0,
                                 signals=["Top-10 holder concentration rose 20.0 points"],
                                 detail="rise")
    result = score_token(**kwargs)
    assert any(s.name == "Concentration trending up" for s in result.signals)


def test_no_prior_trend_adds_no_signal():
    kwargs = _clean_kwargs()
    kwargs["trend"] = TokenTrend(has_prior=False, liquidity_change_pct=-99.0)
    result = score_token(**kwargs)
    assert not any(s.name in ("Liquidity trending down", "Concentration trending up") for s in result.signals)


def test_no_trend_arg_is_backward_compatible():
    base = score_token(**_clean_kwargs())
    assert not any(s.name in ("Liquidity trending down", "Concentration trending up") for s in base.signals)
