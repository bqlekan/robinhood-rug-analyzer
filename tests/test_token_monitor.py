"""M24: Token Watchlist & Monitoring Engine.

Covers watchlist management, the scheduler cycle, REUSE of the existing analyzer
(the engine must never reimplement analysis), per-field change detection, the
no-change dedupe rule, persistence, and failure isolation/recovery. The analyzer
is stubbed everywhere so no test touches the network; the stub doubles as the
assertion that monitoring reaches analysis exactly through the shared entry point.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from app.core.config import settings
from app.models.kol import ClusterInfo, ProjectIntelligence
from app.models.monitor import MonitorOptions
from app.models.token import (
    HoneypotResult,
    LiquiditySnapshot,
    RugAnalysis,
    TokenAnalysisResponse,
    TokenMarketData,
)
from app.services import token_monitor, token_monitor_store

EVM = "0x" + "ab" * 20
EVM2 = "0x" + "cd" * 20


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _temp_db():
    tmp = Path(tempfile.mkdtemp()) / "token_monitor.db"
    token_monitor_store.reset_for_tests(str(tmp))
    yield
    token_monitor_store.reset_for_tests()


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    # Keep retry/timeout behavior deterministic and quick in tests.
    monkeypatch.setattr(settings, "token_monitor_retry_attempts", 2)
    monkeypatch.setattr(settings, "token_monitor_retry_backoff_seconds", 0.0)
    monkeypatch.setattr(settings, "token_monitor_timeout_seconds", 5)
    monkeypatch.setattr(settings, "token_monitor_concurrency", 3)
    monkeypatch.setattr(settings, "token_monitor_history_retain", 200)


# --- analyzer stub (the reuse seam) ------------------------------------------


def _response(address, *, risk=42, level="medium", honeypot="ok", liquidity=1000.0):
    return TokenAnalysisResponse(
        contract_address=address,
        chain="Robinhood Chain",
        status="analysis_completed",
        message="ok",
        analysis=RugAnalysis(
            risk_score=risk, risk_level=level, signals=[],
            data_sources=["stub"], limitations=[],
        ),
        honeypot=HoneypotResult(status=honeypot) if honeypot else None,
        market_data=TokenMarketData(
            liquidity=LiquiditySnapshot(usd=liquidity)
        ) if liquidity is not None else None,
    )


def _stub_analyzer(monkeypatch, *, responses=None, calls=None, factory=None):
    """Patch the shared analyzer entry point. `responses` maps address->response
    (or a single response for any address); `factory(address, include_lore)` gives
    full control. Records every call into `calls` when provided."""
    async def fake_analyze(address, include_lore=True):
        if calls is not None:
            calls.append((address, include_lore))
        if factory is not None:
            return factory(address, include_lore)
        if isinstance(responses, dict):
            return responses[address]
        return responses if responses is not None else _response(address)
    monkeypatch.setattr(token_monitor.rug_analyzer, "analyze_token_contract", fake_analyze)


# --- watchlist management -----------------------------------------------------


def test_add_lists_and_gets_token():
    entry = token_monitor.add_token(EVM, label="Moon")
    assert entry.contract_address == EVM  # normalized lowercase
    assert entry.status == "pending"
    fetched = token_monitor.get_token(EVM.upper())
    assert fetched is not None and fetched.label == "Moon"
    assert [e.contract_address for e in token_monitor.list_tokens()] == [EVM]


def test_add_rejects_invalid_address():
    with pytest.raises(ValueError):
        token_monitor.add_token("not-an-address")


def test_add_is_idempotent_and_preserves_date_added():
    first = token_monitor.add_token(EVM, label="A")
    again = token_monitor.add_token(EVM, label="B")
    assert again.date_added == first.date_added
    assert token_monitor.get_token(EVM).label == "B"
    assert len(token_monitor.list_tokens()) == 1


def test_remove_token_and_data():
    token_monitor.add_token(EVM)
    assert token_monitor.remove_token(EVM) is True
    assert token_monitor.get_token(EVM) is None
    assert token_monitor.remove_token(EVM) is False


def test_set_enabled_toggles_status():
    token_monitor.add_token(EVM)
    token_monitor.set_enabled(EVM, False)
    assert token_monitor.get_token(EVM).status == "paused"
    assert token_monitor.list_tokens(enabled_only=True) == []
    token_monitor.set_enabled(EVM, True)
    assert token_monitor.get_token(EVM).status == "pending"


def test_update_options_persists():
    token_monitor.add_token(EVM)
    token_monitor.update_options(EVM, {"min_risk_delta": 25, "include_lore": True})
    opts = token_monitor.get_token(EVM).options
    assert opts.min_risk_delta == 25 and opts.include_lore is True


def test_management_requires_existing_token():
    with pytest.raises(KeyError):
        token_monitor.set_enabled(EVM, False)


def test_watchlist_updates_emit_events():
    token_monitor.add_token(EVM)
    token_monitor.set_enabled(EVM, False)
    events = token_monitor_store.list_events(EVM, event_type="watchlist_updated")
    actions = {e.payload.get("action") for e in events}
    assert {"added", "disabled"} <= actions


# --- config-driven seed reconciliation ---------------------------------------


def test_sync_from_config_seeds_watchlist():
    result = token_monitor.sync_from_config(
        [EVM, {"contract_address": EVM2, "label": "Two", "enabled": False}, "garbage"]
    )
    assert result == {"added": 2, "skipped": 1}
    assert {e.contract_address for e in token_monitor.list_tokens()} == {EVM, EVM2}
    assert token_monitor.get_token(EVM2).enabled is False


# --- analyzer reuse -----------------------------------------------------------


def test_monitor_reuses_shared_analyzer_entry_point(monkeypatch):
    calls = []
    _stub_analyzer(monkeypatch, calls=calls)
    entry = token_monitor.add_token(EVM)
    _run(token_monitor.monitor_once(entry))
    # Analysis reached exactly through the one shared call, with the address.
    assert calls == [(EVM, False)]


def test_include_lore_option_flows_into_reused_analyzer(monkeypatch):
    calls = []
    _stub_analyzer(monkeypatch, calls=calls)
    entry = token_monitor.add_token(EVM, options={"include_lore": True})
    _run(token_monitor.monitor_once(entry))
    assert calls == [(EVM, True)]


def test_snapshot_copies_reused_scalars_verbatim(monkeypatch):
    _stub_analyzer(monkeypatch, responses=_response(
        EVM, risk=77, level="high", honeypot="sellable", liquidity=5000.0))
    entry = token_monitor.add_token(EVM)
    _run(token_monitor.monitor_once(entry))
    latest = token_monitor_store.get_latest_values(EVM)
    assert latest["risk_score"] == 77
    assert latest["risk_level"] == "high"
    assert latest["honeypot_status"] == "sellable"
    assert latest["liquidity_usd"] == 5000.0


def test_kol_linkage_reuses_project_intelligence(monkeypatch):
    _stub_analyzer(monkeypatch)

    def fake_intel(platform, account_key):
        return ProjectIntelligence(
            platform=platform, account_key=account_key, project_handle="moon",
            score=88, confidence="high", kol_count=4,
            cluster=ClusterInfo(
                platform=platform, account_key=account_key, project_handle="moon",
                is_cluster=True, cluster_types=["tier_1"], kol_count=4,
            ),
            correlation={"alpha_score": 61},
        )
    monkeypatch.setattr(token_monitor.kol_store, "get_project_intelligence", fake_intel)

    entry = token_monitor.add_token(
        EVM, options={"kol_platform": "x", "kol_account_key": "moon"})
    _run(token_monitor.monitor_once(entry))
    latest = token_monitor_store.get_latest_values(EVM)
    assert latest["kol_score"] == 88
    assert latest["cluster_size"] == 4
    assert latest["alpha_score"] == 61


def test_no_kol_linkage_leaves_kol_signals_none(monkeypatch):
    _stub_analyzer(monkeypatch)
    entry = token_monitor.add_token(EVM)
    _run(token_monitor.monitor_once(entry))
    latest = token_monitor_store.get_latest_values(EVM)
    assert latest["kol_score"] is None
    assert latest["cluster_size"] is None


# --- change detection ---------------------------------------------------------


def test_first_sighting_records_baseline_but_no_change_events(monkeypatch):
    _stub_analyzer(monkeypatch)
    entry = token_monitor.add_token(EVM)
    result = _run(token_monitor.monitor_once(entry))
    assert result.outcome == "first_seen"
    assert result.changed_fields == []
    assert token_monitor_store.list_history(EVM) == []
    # Only the management event exists; no change events on first sight.
    assert token_monitor_store.list_events(EVM, event_type="project_changed") == []


def test_risk_change_detected_and_recorded(monkeypatch):
    resp = {"v": _response(EVM, risk=10)}
    _stub_analyzer(monkeypatch, factory=lambda a, l: resp["v"])
    entry = token_monitor.add_token(EVM)
    _run(token_monitor.monitor_once(entry))          # baseline risk=10
    resp["v"] = _response(EVM, risk=80, level="high")
    result = _run(token_monitor.monitor_once(entry))  # risk jumps
    assert result.outcome == "changed"
    assert "risk_score" in result.changed_fields
    etypes = {e.event_type for e in result.events}
    assert "risk_changed" in etypes and "project_changed" in etypes
    history = token_monitor_store.list_history(EVM)
    assert len(history) == 1
    assert history[0].previous_values["risk_score"] == 10
    assert history[0].current_values["risk_score"] == 80


def test_honeypot_flip_detected(monkeypatch):
    resp = {"v": _response(EVM, honeypot="sellable")}
    _stub_analyzer(monkeypatch, factory=lambda a, l: resp["v"])
    entry = token_monitor.add_token(EVM)
    _run(token_monitor.monitor_once(entry))
    resp["v"] = _response(EVM, honeypot="honeypot")
    result = _run(token_monitor.monitor_once(entry))
    assert "honeypot_status" in result.changed_fields
    assert any(e.event_type == "honeypot_changed" for e in result.events)


def test_sub_threshold_change_is_ignored(monkeypatch):
    resp = {"v": _response(EVM, risk=50)}
    _stub_analyzer(monkeypatch, factory=lambda a, l: resp["v"])
    entry = token_monitor.add_token(EVM, options={"min_risk_delta": 10})
    _run(token_monitor.monitor_once(entry))
    resp["v"] = _response(EVM, risk=54)  # +4, under the 10-point threshold
    result = _run(token_monitor.monitor_once(entry))
    assert result.outcome == "unchanged"
    assert token_monitor_store.list_history(EVM) == []


def test_liquidity_change_uses_fractional_threshold(monkeypatch):
    resp = {"v": _response(EVM, liquidity=1000.0)}
    _stub_analyzer(monkeypatch, factory=lambda a, l: resp["v"])
    entry = token_monitor.add_token(EVM, options={"min_liquidity_change_pct": 0.20})
    _run(token_monitor.monitor_once(entry))
    resp["v"] = _response(EVM, liquidity=1100.0)  # +10%, under 20%
    assert _run(token_monitor.monitor_once(entry)).outcome == "unchanged"
    resp["v"] = _response(EVM, liquidity=1500.0)  # +50%, over 20%
    result = _run(token_monitor.monitor_once(entry))
    assert "liquidity_usd" in result.changed_fields


def test_no_change_cycle_writes_no_duplicate_history_or_events(monkeypatch):
    _stub_analyzer(monkeypatch)  # identical response every call
    entry = token_monitor.add_token(EVM)
    _run(token_monitor.monitor_once(entry))  # first_seen
    for _ in range(3):
        assert _run(token_monitor.monitor_once(entry)).outcome == "unchanged"
    assert token_monitor_store.list_history(EVM) == []
    assert token_monitor_store.list_events(EVM, event_type="project_changed") == []


# --- persistence & retention --------------------------------------------------


def test_last_checked_and_status_persist(monkeypatch):
    _stub_analyzer(monkeypatch)
    entry = token_monitor.add_token(EVM)
    _run(token_monitor.monitor_once(entry))
    stored = token_monitor.get_token(EVM)
    assert stored.status == "active"
    assert stored.last_checked is not None


def test_history_retention_prunes(monkeypatch):
    monkeypatch.setattr(settings, "token_monitor_history_retain", 3)
    resp = {"v": _response(EVM, risk=0)}
    _stub_analyzer(monkeypatch, factory=lambda a, l: resp["v"])
    entry = token_monitor.add_token(EVM, options={"min_risk_delta": 1})
    _run(token_monitor.monitor_once(entry))  # baseline
    for r in range(1, 8):
        resp["v"] = _response(EVM, risk=r)
        _run(token_monitor.monitor_once(entry))
    history = token_monitor_store.list_history(EVM)
    assert len(history) == 3  # only the most recent retained
    assert history[0].current_values["risk_score"] == 7


# --- failure isolation & recovery ---------------------------------------------


def test_analysis_failure_is_isolated_and_retried(monkeypatch):
    attempts = {"n": 0}

    async def flaky(address, include_lore=True):
        attempts["n"] += 1
        raise RuntimeError("boom")
    monkeypatch.setattr(token_monitor.rug_analyzer, "analyze_token_contract", flaky)

    entry = token_monitor.add_token(EVM)
    result = _run(token_monitor.monitor_once(entry))
    assert result.outcome == "failed"
    assert "boom" in result.error
    assert attempts["n"] == 2  # retried per config
    assert token_monitor.get_token(EVM).status == "error"


def test_timeout_is_treated_as_failure(monkeypatch):
    monkeypatch.setattr(settings, "token_monitor_timeout_seconds", 1)
    monkeypatch.setattr(settings, "token_monitor_retry_attempts", 1)

    async def slow(address, include_lore=True):
        await asyncio.sleep(5)
    monkeypatch.setattr(token_monitor.rug_analyzer, "analyze_token_contract", slow)

    entry = token_monitor.add_token(EVM)
    result = _run(token_monitor.monitor_once(entry))
    assert result.outcome == "failed"
    assert "timed out" in result.error


def test_recovers_on_next_attempt_after_transient_failure(monkeypatch):
    state = {"fail": True}

    async def sometimes(address, include_lore=True):
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("transient")
        return _response(address)
    monkeypatch.setattr(token_monitor.rug_analyzer, "analyze_token_contract", sometimes)

    entry = token_monitor.add_token(EVM)
    result = _run(token_monitor.monitor_once(entry))  # fails then succeeds within retries
    assert result.outcome == "first_seen"
    assert result.attempts == 2


# --- full cycle over the watchlist -------------------------------------------


def test_cycle_processes_only_enabled_tokens(monkeypatch):
    _stub_analyzer(monkeypatch)
    token_monitor.add_token(EVM)
    token_monitor.add_token(EVM2, enabled=False)
    report = _run(token_monitor.run_cycle())
    assert report.processed == 1
    addrs = {r.contract_address for r in report.results}
    assert addrs == {EVM}


def test_cycle_isolates_one_failing_token_from_others(monkeypatch):
    async def selective(address, include_lore=True):
        if address == EVM:
            raise RuntimeError("only EVM breaks")
        return _response(address)
    monkeypatch.setattr(token_monitor.rug_analyzer, "analyze_token_contract", selective)

    token_monitor.add_token(EVM)
    token_monitor.add_token(EVM2)
    report = _run(token_monitor.run_cycle())
    assert report.processed == 2
    by_addr = {r.contract_address: r.outcome for r in report.results}
    assert by_addr[EVM] == "failed"
    assert by_addr[EVM2] == "first_seen"  # the healthy token still processed


def test_cycle_never_raises_even_if_a_token_errors_unexpectedly(monkeypatch):
    # Force monitor_once itself to blow up to prove run_cycle's outer guard.
    async def explode(entry):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(token_monitor, "monitor_once", explode)
    token_monitor.add_token(EVM)
    report = _run(token_monitor.run_cycle())  # must not raise
    assert report.failed == 1


def test_empty_watchlist_cycle_is_a_noop():
    report = _run(token_monitor.run_cycle())
    assert report.processed == 0
    assert report.finished_at is not None
