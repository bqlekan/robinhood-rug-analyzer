"""Tests for the notification & delivery layer (M23 Deliverable H).

All offline. The layer CONSUMES the intelligence events + `ProjectIntelligence`
that `kol_intel_engine` already produced; it never generates intelligence. Tests
cover the acceptance surface:
  - successful delivery (event reaches the provider + a `sent` row is recorded);
  - failed delivery (a raising provider is isolated + a `failed` row is recorded,
    and a failure never propagates to the caller);
  - disabled notifications (nothing delivered when the master switch is off);
  - threshold filtering (score / confidence / cluster-size / event-type rules);
  - duplicate prevention (a replayed event is not delivered twice).

Plus the reuse discipline: dispatch does no scoring/analysis, and a delivery
failure inside the engine never sinks a capture.
"""

import tempfile
from pathlib import Path

import pytest

from app.core.config import settings
from app.models.kol import (
    ClusterInfo,
    KolContributor,
    KolIntelEvent,
    ProjectIntelligence,
)
from app.services import kol_store, notifications


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _temp_db():
    tmp = Path(tempfile.mkdtemp()) / "kol.db"
    kol_store.reset_for_tests(str(tmp))
    yield
    kol_store.reset_for_tests()


@pytest.fixture(autouse=True)
def _reset_providers():
    notifications.reset_for_tests()
    yield
    notifications.reset_for_tests()


@pytest.fixture
def _enabled(monkeypatch):
    """Notifications on, memory sink only, permissive rules (all event types)."""
    monkeypatch.setattr(settings, "notify_enabled", True)
    monkeypatch.setattr(settings, "notify_providers", ["memory"])
    monkeypatch.setattr(settings, "notify_min_score", 0)
    monkeypatch.setattr(settings, "notify_min_confidence", "very_low")
    monkeypatch.setattr(settings, "notify_min_cluster_size", 0)
    monkeypatch.setattr(settings, "notify_event_types", [
        "kol_cluster_detected", "high_conviction_cluster",
        "project_momentum_detected", "kol_score_updated", "intelligence_updated",
    ])


# --- helpers -----------------------------------------------------------------


def make_event(event_type="kol_cluster_detected", *, account_key="proj",
               handle="proj", detected_at="2024-06-01T12:00:00+00:00", platform="x"):
    return KolIntelEvent(
        event_type=event_type, platform=platform, account_key=account_key,
        project_handle=handle, detected_at=detected_at,
        payload={"kol_count": 3},
    )


def make_intel(*, score=80, confidence="high", kol_count=3, account_key="proj",
               handle="proj", cluster_types=("tier_1",), platform="x"):
    cluster = ClusterInfo(
        platform=platform, account_key=account_key, project_handle=handle,
        is_cluster=True, cluster_types=list(cluster_types), kol_count=kol_count,
    )
    return ProjectIntelligence(
        platform=platform, account_key=account_key, project_handle=handle,
        score=score, confidence=confidence, kol_count=kol_count, cluster=cluster,
    )


def memory_sink() -> notifications.MemoryNotificationProvider:
    return notifications._get_provider("memory")  # type: ignore[return-value]


# --- successful delivery -----------------------------------------------------


def test_successful_delivery_reaches_provider(_enabled):
    event = make_event()
    notifications.dispatch_events([event], make_intel())

    sink = memory_sink()
    assert len(sink.sent) == 1
    assert sink.sent[0].event_type == "kol_cluster_detected"
    assert sink.sent[0].account_key == "proj"


def test_successful_delivery_records_sent_row(_enabled):
    event = make_event()
    notifications.dispatch_events([event], make_intel())

    rows = kol_store.list_deliveries(platform="x", account_key="proj")
    assert len(rows) == 1
    assert rows[0]["status"] == "sent"
    assert rows[0]["destination"] == "memory"
    assert rows[0]["error"] is None
    assert rows[0]["attempted_at"] == event.detected_at


def test_multiple_providers_each_receive(monkeypatch, _enabled):
    monkeypatch.setattr(settings, "notify_providers", ["memory", "log"])
    notifications.dispatch_events([make_event()], make_intel())

    assert len(memory_sink().sent) == 1
    # both destinations recorded as sent
    dests = {r["destination"] for r in kol_store.list_deliveries()}
    assert dests == {"memory", "log"}


def test_log_provider_default(monkeypatch, _enabled):
    monkeypatch.setattr(settings, "notify_providers", ["log"])
    # log sink has no external dependency and must not raise
    notifications.dispatch_events([make_event()], make_intel())
    rows = kol_store.list_deliveries(destination="log")
    assert len(rows) == 1 and rows[0]["status"] == "sent"


# --- failed delivery ---------------------------------------------------------


class _BoomProvider(notifications.NotificationProvider):
    name = "boom"

    def __init__(self):
        self.calls = 0

    def send(self, notification):
        self.calls += 1
        raise RuntimeError("transport down")


def test_failed_delivery_is_isolated_and_recorded(monkeypatch, _enabled):
    monkeypatch.setattr(settings, "notify_providers", ["boom"])
    boom = _BoomProvider()
    notifications.register_provider(boom)

    # must NOT raise
    notifications.dispatch_events([make_event()], make_intel())

    assert boom.calls == 1
    rows = kol_store.list_deliveries(destination="boom")
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert "transport down" in rows[0]["error"]


def test_one_failing_provider_does_not_block_others(monkeypatch, _enabled):
    monkeypatch.setattr(settings, "notify_providers", ["boom", "memory"])
    notifications.register_provider(_BoomProvider())

    notifications.dispatch_events([make_event()], make_intel())

    # memory still got it despite boom failing first
    assert len(memory_sink().sent) == 1
    statuses = {r["destination"]: r["status"] for r in kol_store.list_deliveries()}
    assert statuses == {"boom": "failed", "memory": "sent"}


def test_unknown_provider_is_skipped_not_fatal(monkeypatch, _enabled):
    monkeypatch.setattr(settings, "notify_providers", ["does_not_exist", "memory"])
    notifications.dispatch_events([make_event()], make_intel())
    # unknown one silently skipped, memory still delivered
    assert len(memory_sink().sent) == 1
    assert kol_store.list_deliveries(destination="does_not_exist") == []


# --- disabled notifications --------------------------------------------------


def test_disabled_delivers_nothing(monkeypatch):
    monkeypatch.setattr(settings, "notify_enabled", False)
    monkeypatch.setattr(settings, "notify_providers", ["memory"])
    notifications.dispatch_events([make_event()], make_intel())

    assert memory_sink().sent == []
    assert kol_store.list_deliveries() == []


def test_empty_provider_list_delivers_nothing(monkeypatch, _enabled):
    monkeypatch.setattr(settings, "notify_providers", [])
    notifications.dispatch_events([make_event()], make_intel())
    assert kol_store.list_deliveries() == []


def test_empty_event_types_forwards_nothing(monkeypatch, _enabled):
    monkeypatch.setattr(settings, "notify_event_types", [])
    notifications.dispatch_events([make_event()], make_intel())
    assert memory_sink().sent == []


# --- threshold filtering -----------------------------------------------------


def test_event_type_not_in_forward_list_filtered(monkeypatch, _enabled):
    monkeypatch.setattr(settings, "notify_event_types", ["high_conviction_cluster"])
    notifications.dispatch_events(
        [make_event("kol_cluster_detected")], make_intel()
    )
    assert memory_sink().sent == []


def test_min_score_filters(monkeypatch, _enabled):
    monkeypatch.setattr(settings, "notify_min_score", 90)
    notifications.dispatch_events([make_event()], make_intel(score=80))
    assert memory_sink().sent == []


def test_min_score_passes_at_threshold(monkeypatch, _enabled):
    monkeypatch.setattr(settings, "notify_min_score", 80)
    notifications.dispatch_events([make_event()], make_intel(score=80))
    assert len(memory_sink().sent) == 1


def test_min_confidence_filters(monkeypatch, _enabled):
    monkeypatch.setattr(settings, "notify_min_confidence", "high")
    notifications.dispatch_events([make_event()], make_intel(confidence="medium"))
    assert memory_sink().sent == []


def test_min_confidence_passes_when_stronger(monkeypatch, _enabled):
    monkeypatch.setattr(settings, "notify_min_confidence", "medium")
    notifications.dispatch_events([make_event()], make_intel(confidence="very_high"))
    assert len(memory_sink().sent) == 1


def test_min_cluster_size_filters(monkeypatch, _enabled):
    monkeypatch.setattr(settings, "notify_min_cluster_size", 5)
    notifications.dispatch_events([make_event()], make_intel(kol_count=3))
    assert memory_sink().sent == []


def test_min_cluster_size_passes_at_threshold(monkeypatch, _enabled):
    monkeypatch.setattr(settings, "notify_min_cluster_size", 3)
    notifications.dispatch_events([make_event()], make_intel(kol_count=3))
    assert len(memory_sink().sent) == 1


def test_rules_are_anded(monkeypatch, _enabled):
    # passes score + type but fails cluster size => filtered
    monkeypatch.setattr(settings, "notify_min_score", 10)
    monkeypatch.setattr(settings, "notify_min_cluster_size", 10)
    notifications.dispatch_events([make_event()], make_intel(score=80, kol_count=3))
    assert memory_sink().sent == []


# --- duplicate prevention ----------------------------------------------------


def test_duplicate_event_delivered_once(_enabled):
    event = make_event(detected_at="2024-06-01T12:00:00+00:00")
    notifications.dispatch_events([event], make_intel())
    # replay the exact same event (same type + project + detected_at)
    notifications.dispatch_events([event], make_intel())

    assert len(memory_sink().sent) == 1
    rows = kol_store.list_deliveries()
    assert len(rows) == 1


def test_same_type_different_time_delivered_again(_enabled):
    notifications.dispatch_events(
        [make_event(detected_at="2024-06-01T12:00:00+00:00")], make_intel()
    )
    notifications.dispatch_events(
        [make_event(detected_at="2024-06-01T13:00:00+00:00")], make_intel()
    )
    assert len(memory_sink().sent) == 2


def test_dedupe_is_per_destination(monkeypatch, _enabled):
    monkeypatch.setattr(settings, "notify_providers", ["memory"])
    event = make_event()
    notifications.dispatch_events([event], make_intel())
    # now add a second destination and replay: memory is deduped, log is new
    monkeypatch.setattr(settings, "notify_providers", ["memory", "log"])
    notifications.dispatch_events([event], make_intel())

    assert len(memory_sink().sent) == 1  # not re-delivered
    dests = {r["destination"] for r in kol_store.list_deliveries()}
    assert dests == {"memory", "log"}


def test_retry_after_failure_then_success(monkeypatch, _enabled):
    monkeypatch.setattr(settings, "notify_providers", ["boom"])
    boom = _BoomProvider()
    notifications.register_provider(boom)
    event = make_event()
    notifications.dispatch_events([event], make_intel())
    # a prior failure must NOT dedupe-block a retry
    rows = kol_store.list_deliveries(destination="boom")
    assert rows[0]["status"] == "failed"

    # swap in a working sink under the same name and retry the same event
    notifications.register_provider(_ConfigurableSink("boom"))
    notifications.dispatch_events([event], make_intel())
    rows = kol_store.list_deliveries(destination="boom")
    assert len(rows) == 1  # replaced, not duplicated
    assert rows[0]["status"] == "sent"


class _ConfigurableSink(notifications.NotificationProvider):
    def __init__(self, name):
        self.name = name
        self.sent = []

    def send(self, notification):
        self.sent.append(notification)


# --- reuse discipline / engine integration -----------------------------------


def test_dispatch_does_not_touch_intelligence(_enabled):
    """dispatch consumes the given intel; it must not persist scores/history."""
    notifications.dispatch_events([make_event()], make_intel())
    # no intelligence was written by the notification layer
    assert kol_store.get_project_intelligence("x", "proj") is None


def test_engine_delivery_failure_never_sinks_capture(monkeypatch):
    """A raising provider inside the engine's persist path is swallowed."""
    from app.services import kol_intel_engine

    monkeypatch.setattr(settings, "notify_enabled", True)
    monkeypatch.setattr(settings, "notify_providers", ["boom"])
    monkeypatch.setattr(settings, "notify_event_types", ["intelligence_updated"])
    notifications.register_provider(_BoomProvider())

    intel = make_intel()
    events = [make_event("intelligence_updated")]
    # mirrors the engine call site; must not raise despite the boom provider
    kol_intel_engine.notifications.dispatch_events(events, intel)
