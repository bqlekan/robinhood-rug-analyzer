"""M25: KOL Intelligence Automation — the capture scheduler.

The scheduler owns NO intelligence logic; it orchestrates the existing
`kol_watchlist.capture_following` pipeline. So these tests stub that single reuse
seam (and the provider lookup) and assert only orchestration behaviour: enabled
filtering, per-KOL retry/backoff, timeout, failure isolation, incomplete-vs-failed
outcomes, provider-skip, and duplicate-concurrent-run prevention. Nothing here
touches the network, Playwright, or the real store.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from app.core.config import settings
from app.models.kol import FollowingSnapshot, ProviderCapabilities
from app.services import kol_scheduler, kol_store, kol_watchlist
from app.services.social.base import ProviderError


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _temp_db():
    tmp = Path(tempfile.mkdtemp()) / "kol.db"
    kol_store.reset_for_tests(str(tmp))
    yield
    kol_store.reset_for_tests()


@pytest.fixture(autouse=True)
def _fast_and_capable(monkeypatch):
    # Deterministic, quick retry/timeout in tests.
    monkeypatch.setattr(settings, "kol_scheduler_retry_attempts", 2)
    monkeypatch.setattr(settings, "kol_scheduler_retry_backoff_seconds", 0.0)
    monkeypatch.setattr(settings, "kol_scheduler_timeout_seconds", 5)
    monkeypatch.setattr(settings, "kol_scheduler_concurrency", 3)

    # By default, pretend every platform has a capture-capable provider so the
    # skip path doesn't fire; individual tests override this.
    class _CapableProvider:
        def capabilities(self):
            return ProviderCapabilities(platform="x", can_fetch_following=True)

    monkeypatch.setattr(kol_scheduler, "get_provider", lambda platform: _CapableProvider())


def _snapshot(handle, *, complete=True):
    return FollowingSnapshot(platform="x", kol_handle=handle, accounts=[], complete=complete)


# --- enabled filtering -------------------------------------------------------


def test_cycle_only_processes_enabled_kols(monkeypatch):
    kol_watchlist.add_kol("alice", tier=1)
    kol_watchlist.add_kol("bob", tier=2, enabled=False)

    seen = []

    async def fake_capture(handle, platform=None):
        seen.append(handle)
        return _snapshot(handle)

    monkeypatch.setattr(kol_scheduler.kol_watchlist, "capture_following", fake_capture)

    report = _run(kol_scheduler.run_cycle())
    assert seen == ["alice"]  # bob disabled -> skipped entirely
    assert report.processed == 1
    assert report.captured == 1


def test_empty_watchlist_is_a_noop():
    report = _run(kol_scheduler.run_cycle())
    assert report.processed == 0
    assert report.captured == 0
    assert report.finished_at is not None


# --- outcomes ----------------------------------------------------------------


def test_complete_snapshot_counts_as_captured(monkeypatch):
    kol_watchlist.add_kol("alice")

    async def fake_capture(handle, platform=None):
        return _snapshot(handle, complete=True)

    monkeypatch.setattr(kol_scheduler.kol_watchlist, "capture_following", fake_capture)
    report = _run(kol_scheduler.run_cycle())
    assert report.captured == 1
    assert report.results[0].outcome == "captured"
    assert report.results[0].attempts == 1


def test_incomplete_snapshot_retries_then_reports_incomplete(monkeypatch):
    kol_watchlist.add_kol("alice")
    calls = {"n": 0}

    async def fake_capture(handle, platform=None):
        calls["n"] += 1
        return _snapshot(handle, complete=False)  # always partial

    monkeypatch.setattr(kol_scheduler.kol_watchlist, "capture_following", fake_capture)
    report = _run(kol_scheduler.run_cycle())
    assert calls["n"] == 2  # retried up to the configured attempts
    assert report.incomplete == 1
    assert report.results[0].outcome == "incomplete"


def test_transient_error_retries_then_succeeds(monkeypatch):
    kol_watchlist.add_kol("alice")
    calls = {"n": 0}

    async def fake_capture(handle, platform=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderError("rate limited", retryable=True)
        return _snapshot(handle)

    monkeypatch.setattr(kol_scheduler.kol_watchlist, "capture_following", fake_capture)
    report = _run(kol_scheduler.run_cycle())
    assert calls["n"] == 2
    assert report.captured == 1
    assert report.results[0].attempts == 2


def test_non_retryable_error_stops_early(monkeypatch):
    kol_watchlist.add_kol("alice")
    calls = {"n": 0}

    async def fake_capture(handle, platform=None):
        calls["n"] += 1
        raise ProviderError("account suspended", retryable=False)

    monkeypatch.setattr(kol_scheduler.kol_watchlist, "capture_following", fake_capture)
    report = _run(kol_scheduler.run_cycle())
    assert calls["n"] == 1  # no retry on a permanent failure
    assert report.failed == 1
    assert report.results[0].outcome == "failed"


def test_timeout_is_isolated_and_counted(monkeypatch):
    kol_watchlist.add_kol("alice")
    monkeypatch.setattr(settings, "kol_scheduler_timeout_seconds", 1)
    monkeypatch.setattr(settings, "kol_scheduler_retry_attempts", 1)

    async def fake_capture(handle, platform=None):
        await asyncio.sleep(5)  # exceeds the 1s budget
        return _snapshot(handle)

    monkeypatch.setattr(kol_scheduler.kol_watchlist, "capture_following", fake_capture)
    report = _run(kol_scheduler.run_cycle())
    assert report.failed == 1
    assert "timed out" in (report.results[0].error or "")


# --- failure isolation -------------------------------------------------------


def test_one_kol_failure_does_not_sink_the_cycle(monkeypatch):
    kol_watchlist.add_kol("alice", tier=1)
    kol_watchlist.add_kol("bob", tier=2)

    async def fake_capture(handle, platform=None):
        if handle == "alice":
            raise RuntimeError("boom")  # unexpected, non-ProviderError
        return _snapshot(handle)

    monkeypatch.setattr(kol_scheduler.kol_watchlist, "capture_following", fake_capture)
    report = _run(kol_scheduler.run_cycle())
    assert report.processed == 2
    assert report.captured == 1  # bob still captured
    assert report.failed == 1    # alice isolated


# --- provider skip -----------------------------------------------------------


def test_missing_provider_is_skipped_without_retry(monkeypatch):
    kol_watchlist.add_kol("alice")
    monkeypatch.setattr(kol_scheduler, "get_provider", lambda platform: None)

    calls = {"n": 0}

    async def fake_capture(handle, platform=None):
        calls["n"] += 1
        return _snapshot(handle)

    monkeypatch.setattr(kol_scheduler.kol_watchlist, "capture_following", fake_capture)
    report = _run(kol_scheduler.run_cycle())
    assert calls["n"] == 0  # never even called the pipeline
    assert report.skipped == 1
    assert report.results[0].outcome == "skipped"


# --- duplicate-run prevention ------------------------------------------------


def test_overlapping_cycle_is_declined(monkeypatch):
    kol_watchlist.add_kol("alice")

    async def fake_capture(handle, platform=None):
        await asyncio.sleep(0.05)
        return _snapshot(handle)

    monkeypatch.setattr(kol_scheduler.kol_watchlist, "capture_following", fake_capture)

    async def _two_at_once():
        first = asyncio.create_task(kol_scheduler.run_cycle())
        await asyncio.sleep(0)  # let the first grab the lock
        second = await kol_scheduler.run_cycle()  # should decline immediately
        return await first, second

    first_report, second_report = _run(_two_at_once())
    assert second_report.skipped_cycle is True
    assert first_report.captured == 1


# --- concurrency bound -------------------------------------------------------


def test_concurrency_is_bounded(monkeypatch):
    for i in range(6):
        kol_watchlist.add_kol(f"kol{i}", tier=1)
    monkeypatch.setattr(settings, "kol_scheduler_concurrency", 2)

    state = {"cur": 0, "max": 0}

    async def fake_capture(handle, platform=None):
        state["cur"] += 1
        state["max"] = max(state["max"], state["cur"])
        await asyncio.sleep(0.02)
        state["cur"] -= 1
        return _snapshot(handle)

    monkeypatch.setattr(kol_scheduler.kol_watchlist, "capture_following", fake_capture)
    report = _run(kol_scheduler.run_cycle())
    assert report.captured == 6
    assert state["max"] <= 2  # never exceeded the configured cap


def test_capture_one_never_raises(monkeypatch):
    kol_watchlist.add_kol("alice")

    async def fake_capture(handle, platform=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(kol_scheduler.kol_watchlist, "capture_following", fake_capture)
    entry = kol_watchlist.list_kols(enabled_only=True)[0]
    result = _run(kol_scheduler.capture_one(entry))  # must not raise
    assert result.outcome == "failed"
    assert "boom" in (result.error or "")
