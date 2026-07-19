"""Unit tests for M13 LP lock duration & unlock schedule (pure, no network)."""

from datetime import datetime, timezone

from app.models.token import LiquidityLock
from app.services import analyzers, launchpad_registry as reg
from app.services.scoring import score_token
from tests.test_scoring import _clean_kwargs


def _word(value: int) -> str:
    return "0x" + f"{value:064x}"


# --- decode_unlock_timestamp (pure) ---


def test_decode_reads_first_word_as_unix_time():
    assert analyzers.decode_unlock_timestamp(_word(1893456000)) == 1893456000


def test_decode_reads_struct_word_index():
    # Two-word return; the unlock time is the second field.
    raw = _word(0) + f"{1893456000:064x}"
    assert analyzers.decode_unlock_timestamp(raw, 1) == 1893456000


def test_decode_rejects_empty_short_and_garbage():
    assert analyzers.decode_unlock_timestamp(None) is None
    assert analyzers.decode_unlock_timestamp("0x") is None
    assert analyzers.decode_unlock_timestamp("0x1234") is None  # < one word
    assert analyzers.decode_unlock_timestamp(_word(0)) is None  # 0 == no lock set
    assert analyzers.decode_unlock_timestamp(_word(10**12)) is None  # far-future garbage


# --- apply_unlock_schedule (pure) ---


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _locked() -> LiquidityLock:
    return LiquidityLock(status="locked", locked_percentage=100.0, locker_label="UNCX",
                         detail="100.0% of LP tokens held by UNCX.")


def test_apply_far_future_keeps_locked_adds_horizon():
    ts = int(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp())
    out = analyzers.apply_unlock_schedule(_locked(), ts, now=NOW)
    assert out.status == "locked"
    assert out.unlock_in_days is not None and out.unlock_in_days > 300
    assert "unlocks" in (out.detail or "").lower()


def test_apply_past_unlock_downgrades_to_unlocked():
    ts = int(datetime(2025, 6, 1, tzinfo=timezone.utc).timestamp())
    out = analyzers.apply_unlock_schedule(_locked(), ts, now=NOW)
    assert out.status == "unlocked"
    assert out.unlock_in_days <= 0
    assert "expired" in (out.detail or "").lower()


def test_apply_none_timestamp_is_unchanged():
    lock = _locked()
    out = analyzers.apply_unlock_schedule(lock, None, now=NOW)
    assert out == lock  # presence-only behaviour preserved


def test_apply_ignores_non_locked_status():
    unl = LiquidityLock(status="unlocked")
    assert analyzers.apply_unlock_schedule(unl, 9999999999, now=NOW) == unl


# --- registry spec lookup ---


LOCKER = "0x" + "1" * 40


def test_locker_unlock_spec_present_when_declared(monkeypatch):
    monkeypatch.setattr(reg, "LP_LOCKERS", [
        {"address": LOCKER, "label": "UNCX", "enabled": True,
         "unlock_selector": "0x12345678", "unlock_word_index": 1},
    ])
    spec = reg.locker_unlock_spec(LOCKER.upper())
    assert spec == {"selector": "0x12345678", "word_index": 1}


def test_locker_unlock_spec_none_without_selector(monkeypatch):
    monkeypatch.setattr(reg, "LP_LOCKERS", [
        {"address": LOCKER, "label": "UNCX", "enabled": True},
    ])
    assert reg.locker_unlock_spec(LOCKER) is None


def test_locker_unlock_spec_none_for_burn_and_unknown():
    assert reg.locker_unlock_spec("0x000000000000000000000000000000000000dEaD") is None
    assert reg.locker_unlock_spec(LOCKER) is None  # empty prod registry


# --- scoring: near-term unlock scored higher than a long lock ---


def test_near_term_unlock_scores_high_signal():
    lock = LiquidityLock(status="locked", locked_percentage=100.0, unlock_in_days=3.0)
    kwargs = _clean_kwargs()
    kwargs["liquidity_lock"] = lock
    result = score_token(**kwargs)
    assert any(s.name == "LP lock expiring soon" and s.severity == "high" for s in result.signals)


def test_long_lock_adds_no_unlock_signal():
    lock = LiquidityLock(status="locked", locked_percentage=100.0, unlock_in_days=365.0)
    kwargs = _clean_kwargs()
    kwargs["liquidity_lock"] = lock
    result = score_token(**kwargs)
    assert not any(s.name == "LP lock expiring soon" for s in result.signals)


def test_locked_without_schedule_unchanged_from_before():
    # No unlock_in_days -> presence-only lock, no new signal (backward compatible).
    lock = LiquidityLock(status="locked", locked_percentage=100.0)
    kwargs = _clean_kwargs()
    kwargs["liquidity_lock"] = lock
    result = score_token(**kwargs)
    assert not any(s.category == "liquidity" for s in result.signals)
