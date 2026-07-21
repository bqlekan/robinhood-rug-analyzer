"""M27: Watchlist Alerts & Intelligent Notifications — the alert engine.

The engine CONNECTS existing events to configurable rules and delivers survivors
through the existing notification providers. It generates no intelligence and no
events, so these tests feed it the real event objects the producers emit
(`MonitorEvent`, `FollowEvent`) and assert only the alert-layer behaviour:
event→alert mapping, per-alert enable/disable, per-token overrides, global
defaults, severity gate, cooldown, dedupe, aggregation, human-readable messages,
disabled-is-noop, and failure isolation. The `memory` sink is the destination;
nothing hits the network.
"""

import tempfile
from pathlib import Path

import pytest

from app.core.config import settings
from app.models.kol import FollowEvent, SocialAccount
from app.models.monitor import MonitorEvent, MonitorResult, TokenWatchEntry
from app.services import alert_engine, kol_store, notifications


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


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    """Alerts on, memory sink, permissive severity, no retry sleeps."""
    monkeypatch.setattr(settings, "alerts_enabled", True)
    monkeypatch.setattr(settings, "notify_providers", ["memory"])
    monkeypatch.setattr(settings, "alerts_min_severity", "info")
    monkeypatch.setattr(settings, "alerts_aggregate", False)
    monkeypatch.setattr(settings, "alerts_cooldown_seconds", 0)  # cooldown off unless a test sets it
    monkeypatch.setattr(settings, "alerts_rules", {})
    monkeypatch.setattr(settings, "alerts_token_overrides", {})
    monkeypatch.setattr(settings, "notify_retry_count", 1)


def _sink():
    return notifications._get_provider("memory")


EVM = "0x" + "ab" * 20


def _mevent(event_type, previous=None, current=None, **extra):
    payload = {}
    if previous is not None:
        payload["previous"] = previous
    if current is not None:
        payload["current"] = current
    payload.update(extra)
    return MonitorEvent(event_type=event_type, contract_address=EVM, payload=payload)


# --- event -> alert mapping + delivery ---------------------------------------


def test_risk_change_event_delivers_alert():
    events = [_mevent("risk_changed", {"risk_score": 40, "risk_level": "medium"},
                      {"risk_score": 80, "risk_level": "high"})]
    n = alert_engine.dispatch(alert_engine.evaluate(events, subject=EVM))
    assert n == 1
    sent = _sink().sent
    assert len(sent) == 1
    assert sent[0].event_type == "risk_change"
    assert "risk" in sent[0].body.lower()
    assert sent[0].title.startswith("[HIGH]")  # default severity for risk_change


def test_liquidity_drop_is_critical():
    events = [_mevent("liquidity_changed", {"liquidity_usd": 10000.0}, {"liquidity_usd": 500.0})]
    alerts = alert_engine.evaluate(events, subject=EVM)
    assert alerts[0].severity == "critical"


def test_honeypot_change_delivers():
    events = [_mevent("honeypot_changed", {"honeypot_status": "sellable"}, {"honeypot_status": "honeypot"})]
    assert alert_engine.dispatch(alert_engine.evaluate(events, subject=EVM)) == 1


def test_concentration_privilege_smartwallet_map_to_alerts():
    events = [
        _mevent("concentration_changed", {"top10_concentration": 40.0}, {"top10_concentration": 70.0}),
        _mevent("privilege_changed", {"privilege_signature": "a"}, {"privilege_signature": "b"}),
        _mevent("smart_wallet_changed", {"smart_wallet_count": 1}, {"smart_wallet_count": 4}),
    ]
    alerts = alert_engine.evaluate(events, subject=EVM)
    assert {a.alert_type for a in alerts} == {
        "concentration_change", "privilege_change", "smart_wallet_activity"
    }


def test_non_alertable_events_are_skipped():
    # project_changed (umbrella) and unfollow are not alertable.
    events = [_mevent("project_changed", None, None, changed_fields=["risk_score"])]
    assert alert_engine.evaluate(events, subject=EVM) == []


# --- new KOL follow (FollowEvent source) -------------------------------------


def test_new_kol_follow_alert():
    acct = SocialAccount(platform="x", handle="degenspartan")
    ev = FollowEvent(event_type="new_follow", platform="x", kol_handle="cobie",
                     account_key=acct.key(), account=acct)
    n = alert_engine.process_follow_events("x", "cobie", [ev])
    assert n == 1
    body = _sink().sent[0].body
    assert "degenspartan" in body and "cobie" in body


def test_unfollow_is_not_alerted():
    acct = SocialAccount(platform="x", handle="someone")
    ev = FollowEvent(event_type="unfollow", platform="x", kol_handle="cobie",
                     account_key=acct.key(), account=acct)
    assert alert_engine.process_follow_events("x", "cobie", [ev]) == 0


# --- per-alert enable/disable + per-token override + global default ----------


def test_global_disable_suppresses_type(monkeypatch):
    monkeypatch.setattr(settings, "alerts_rules", {"risk_change": {"enabled": False}})
    events = [_mevent("risk_changed", {"risk_score": 40}, {"risk_score": 90})]
    assert alert_engine.evaluate(events, subject=EVM) == []


def test_per_token_override_beats_global(monkeypatch):
    # Globally enabled, but disabled for THIS token.
    monkeypatch.setattr(settings, "alerts_token_overrides",
                        {EVM: {"risk_change": {"enabled": False}}})
    events = [_mevent("risk_changed", {"risk_score": 40}, {"risk_score": 90})]
    assert alert_engine.evaluate(events, subject=EVM) == []
    # A different token is unaffected.
    other = "0x" + "cd" * 20
    assert len(alert_engine.evaluate(events, subject=other)) == 1


def test_per_token_override_can_raise_severity(monkeypatch):
    monkeypatch.setattr(settings, "alerts_token_overrides",
                        {EVM: {"alpha_change": {"severity": "critical"}}})
    events = [_mevent("alpha_changed", {"alpha_score": 10}, {"alpha_score": 50})]
    alerts = alert_engine.evaluate(events, subject=EVM)
    assert alerts[0].severity == "critical"  # override; default is "medium"


# --- severity gate -----------------------------------------------------------


def test_min_severity_gate_filters_weaker(monkeypatch):
    monkeypatch.setattr(settings, "alerts_min_severity", "high")
    # alpha_change default severity is "medium" -> below "high" -> filtered.
    events = [_mevent("alpha_changed", {"alpha_score": 1}, {"alpha_score": 9})]
    assert alert_engine.evaluate(events, subject=EVM) == []
    # risk_change default "high" -> passes.
    events = [_mevent("risk_changed", {"risk_score": 1}, {"risk_score": 9})]
    assert len(alert_engine.evaluate(events, subject=EVM)) == 1


# --- dedupe + cooldown -------------------------------------------------------


def test_dedupe_same_event_not_delivered_twice():
    ev = _mevent("risk_changed", {"risk_score": 40}, {"risk_score": 80})
    alerts = alert_engine.evaluate([ev], subject=EVM)
    assert alert_engine.dispatch(alerts) == 1
    # Re-dispatch the SAME evaluated alert (same dedup_key) -> suppressed.
    assert alert_engine.dispatch(alerts) == 0
    assert len(_sink().sent) == 1


def test_cooldown_suppresses_second_distinct_alert(monkeypatch):
    monkeypatch.setattr(settings, "alerts_cooldown_seconds", 3600)
    e1 = _mevent("risk_changed", {"risk_score": 40}, {"risk_score": 80})
    assert alert_engine.dispatch(alert_engine.evaluate([e1], subject=EVM)) == 1
    # A DIFFERENT transition (distinct dedup_key) on the same subject within cooldown.
    e2 = MonitorEvent(event_type="risk_changed", contract_address=EVM,
                      payload={"previous": {"risk_score": 80}, "current": {"risk_score": 55}})
    delivered = alert_engine.dispatch(alert_engine.evaluate([e2], subject=EVM))
    assert delivered == 0  # cooldown window still open
    assert len(_sink().sent) == 1


def test_cooldown_zero_allows_repeats(monkeypatch):
    monkeypatch.setattr(settings, "alerts_cooldown_seconds", 0)
    e1 = _mevent("risk_changed", {"risk_score": 40}, {"risk_score": 80})
    e2 = MonitorEvent(event_type="risk_changed", contract_address=EVM,
                      payload={"previous": {"risk_score": 80}, "current": {"risk_score": 55}})
    alert_engine.dispatch(alert_engine.evaluate([e1], subject=EVM))
    alert_engine.dispatch(alert_engine.evaluate([e2], subject=EVM))
    assert len(_sink().sent) == 2  # distinct transitions, no cooldown


# --- aggregation -------------------------------------------------------------


def test_aggregation_collapses_multiple_alerts(monkeypatch):
    monkeypatch.setattr(settings, "alerts_aggregate", True)
    events = [
        _mevent("risk_changed", {"risk_score": 40}, {"risk_score": 80}),        # high
        _mevent("liquidity_changed", {"liquidity_usd": 9000.0}, {"liquidity_usd": 100.0}),  # critical
    ]
    alerts = alert_engine.evaluate(events, subject=EVM)
    assert len(alerts) == 1
    agg = alerts[0]
    assert agg.severity == "critical"  # strongest of the batch
    assert "2 alerts" in agg.title
    assert alert_engine.dispatch(alerts) == 1


def test_no_aggregation_emits_each(monkeypatch):
    monkeypatch.setattr(settings, "alerts_aggregate", False)
    events = [
        _mevent("risk_changed", {"risk_score": 40}, {"risk_score": 80}),
        _mevent("honeypot_changed", {"honeypot_status": "ok"}, {"honeypot_status": "honeypot"}),
    ]
    alerts = alert_engine.evaluate(events, subject=EVM)
    assert len(alerts) == 2


# --- disabled = no-op + failure isolation ------------------------------------


def test_disabled_delivers_nothing(monkeypatch):
    monkeypatch.setattr(settings, "alerts_enabled", False)
    events = [_mevent("risk_changed", {"risk_score": 1}, {"risk_score": 99})]
    # evaluate still works (pure), but dispatch is a no-op.
    assert alert_engine.dispatch(alert_engine.evaluate(events, subject=EVM)) == 0
    assert _sink().sent == []


def test_process_monitor_result_wires_events():
    events = [_mevent("risk_changed", {"risk_score": 10}, {"risk_score": 90})]
    result = MonitorResult(contract_address=EVM, outcome="changed", events=events)
    entry = TokenWatchEntry(contract_address=EVM, label="TESTTOKEN")
    n = alert_engine.process_monitor_result(result, entry)
    assert n == 1
    assert "TESTTOKEN" in _sink().sent[0].body  # label used in the message


def test_bad_provider_does_not_sink_caller(monkeypatch):
    class Boom(notifications.NotificationProvider):
        name = "memory"  # shadow the memory sink with a raising one

        def send(self, notification):
            raise RuntimeError("sink down")

    notifications.register_provider(Boom())
    events = [_mevent("risk_changed", {"risk_score": 1}, {"risk_score": 99})]
    # Must not raise; failure isolated and recorded.
    n = alert_engine.dispatch(alert_engine.evaluate(events, subject=EVM))
    assert n == 0
    rows = kol_store.list_deliveries(destination="memory")
    assert rows and rows[0]["status"] == "failed"


def test_no_providers_configured_is_noop(monkeypatch):
    monkeypatch.setattr(settings, "notify_providers", [])
    events = [_mevent("risk_changed", {"risk_score": 1}, {"risk_score": 99})]
    assert alert_engine.dispatch(alert_engine.evaluate(events, subject=EVM)) == 0
