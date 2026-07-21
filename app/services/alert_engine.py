from __future__ import annotations

"""Alert engine — Watchlist Alerts & Intelligent Notifications (M27).

Connects the events the rest of the system ALREADY produces to configurable alert
rules, and delivers the ones that pass through the EXISTING notification providers.
It adds no intelligence, generates no new events, and touches no scoring:

  - **Sources (reused, unchanged):** token-monitor change events (`MonitorEvent`,
    M24), KOL follow events (`FollowEvent`, M23), KOL intel events (`KolIntelEvent`,
    M23) — each carries an `event_type` + a self-describing `payload`.
  - **Rules (M27, config-driven):** `AlertConfig` maps each of the ten alert types
    to an `AlertRule` (enabled, severity, cooldown), with per-token overrides on top
    of global defaults.
  - **Delivery (reused, unchanged):** each surviving alert becomes a `Notification`
    and is handed to `notifications.deliver` — the ONE delivery path (providers +
    retry + dedupe + audit log). No transport code is duplicated here.

Everything is opt-in (`alerts_enabled`, off by default) and fully failure-isolated:
evaluation/dispatch is wrapped so a bad rule or a dead sink can never sink the
capture/monitor cycle that produced the events.
"""

import logging
from datetime import datetime, timezone

from app.core.config import settings
from app.models.alerts import (
    ALERT_TYPES,
    Alert,
    AlertConfig,
    AlertRule,
    severity_rank,
)
from app.services import kol_store, notifications

logger = logging.getLogger(__name__)


# Which alert type each existing event maps to. An event whose type isn't here is
# simply not alertable (e.g. `unfollow`, the `*_updated` umbrellas) and is skipped.
EVENT_TO_ALERT: dict[str, str] = {
    # KOL follow events (M23) — currently produced, never delivered until now.
    "new_follow": "new_kol_follow",
    # KOL intelligence events (M23).
    "kol_cluster_detected": "kol_cluster",
    "high_conviction_cluster": "kol_cluster",
    # Token-monitor change events (M24) — produced, never delivered until now.
    "risk_changed": "risk_change",
    "alpha_changed": "alpha_change",
    "liquidity_changed": "liquidity_drop",
    "concentration_changed": "concentration_change",
    "smart_wallet_changed": "smart_wallet_activity",
    "watchlist_updated": "new_watchlist_token",
    "honeypot_changed": "honeypot_change",
    "privilege_changed": "privilege_change",
}


# --- Config ------------------------------------------------------------------


def _load_config() -> AlertConfig:
    """Build the resolved `AlertConfig` from `settings`. Cheap; called per batch so
    a settings/monkeypatch change is always reflected (mirrors the notification
    layer's live-settings discipline)."""
    rules = {
        atype: AlertRule(**spec)
        for atype, spec in (settings.alerts_rules or {}).items()
        if atype in ALERT_TYPES
    }
    overrides = {
        token.lower(): {
            atype: AlertRule(**spec)
            for atype, spec in (per_token or {}).items()
            if atype in ALERT_TYPES
        }
        for token, per_token in (settings.alerts_token_overrides or {}).items()
    }
    return AlertConfig(
        default_cooldown_seconds=int(settings.alerts_cooldown_seconds),
        rules=rules,
        token_overrides=overrides,
    )


# --- Message rendering (human-readable) --------------------------------------


def _render(alert_type: str, event_type: str, subject_label: str, payload: dict) -> tuple[str, str]:
    """A human-readable (title, message) for one alert. Pulls before/after values
    straight from the event's self-describing payload — no re-analysis."""
    prev = payload.get("previous") or {}
    curr = payload.get("current") or {}

    def _mv(field: str) -> str:
        return f"{prev.get(field)} → {curr.get(field)}"

    if alert_type == "new_kol_follow":
        who = payload.get("account", {}).get("handle") if isinstance(payload.get("account"), dict) else None
        who = who or payload.get("account_key") or "an account"
        return ("New KOL follow", f"{subject_label} started following {who}.")
    if alert_type == "kol_cluster":
        n = payload.get("kol_count") or payload.get("cluster_size") or "several"
        return ("KOL cluster forming", f"{n} KOLs have converged on {subject_label}.")
    if alert_type == "risk_change":
        return ("Risk score changed", f"{subject_label}: risk {_mv('risk_score')} (level {_mv('risk_level')}).")
    if alert_type == "alpha_change":
        return ("Alpha score changed", f"{subject_label}: alpha {_mv('alpha_score')}.")
    if alert_type == "liquidity_drop":
        return ("Liquidity moved", f"{subject_label}: liquidity {_mv('liquidity_usd')} USD.")
    if alert_type == "concentration_change":
        return ("Holder concentration changed", f"{subject_label}: top-10 {_mv('top10_concentration')}%.")
    if alert_type == "smart_wallet_activity":
        return ("Smart-wallet activity", f"{subject_label}: flagged wallets {_mv('smart_wallet_count')}.")
    if alert_type == "new_watchlist_token":
        action = payload.get("action", "updated")
        return ("Watchlist token", f"{subject_label} was {action} on the monitoring watchlist.")
    if alert_type == "honeypot_change":
        return ("Honeypot status changed", f"{subject_label}: honeypot {_mv('honeypot_status')}.")
    if alert_type == "privilege_change":
        return ("Contract privileges changed", f"{subject_label}: privileges {_mv('privilege_signature')}.")
    return (alert_type, f"{subject_label}: {event_type}")


# --- Evaluation (pure: events -> alerts) -------------------------------------


def evaluate(
    events: list,
    *,
    subject: str,
    subject_label: str | None = None,
    platform: str = "monitor",
    config: AlertConfig | None = None,
) -> list[Alert]:
    """Map a batch of already-produced events for ONE subject to the alerts that
    should fire. Pure — no delivery, no persistence. Applies per-type enable +
    severity gate + per-token overrides. When `alerts_aggregate` is on and more
    than one alert fires for the subject, they collapse into one summary alert
    (severity = strongest of the batch)."""
    cfg = config or _load_config()
    label = subject_label or subject
    min_rank = severity_rank(settings.alerts_min_severity)

    alerts: list[Alert] = []
    for event in events:
        etype = getattr(event, "event_type", None)
        atype = EVENT_TO_ALERT.get(etype)
        if atype is None:
            continue  # not an alertable event (e.g. unfollow, umbrella *_updated)
        rule = cfg.rule_for(atype, subject)
        if not rule.enabled:
            continue
        if severity_rank(rule.severity) > min_rank:
            continue  # below the delivery severity floor
        payload = dict(getattr(event, "payload", {}) or {})
        # FollowEvents carry no `payload`; surface their counterparty so the message
        # can name who was followed (still a plain read of the event, no analysis).
        acct = getattr(event, "account", None)
        if acct is not None and "account" not in payload:
            payload["account"] = {"handle": getattr(acct, "handle", None)}
            payload.setdefault("account_key", getattr(event, "account_key", None))
        title, message = _render(atype, etype, label, payload)
        detected_at = getattr(event, "detected_at", None) or _now_iso()
        alerts.append(Alert(
            alert_type=atype,
            severity=rule.severity,
            dedup_key=_dedup_key(atype, subject, payload, detected_at),
            platform=platform,
            subject=subject,
            title=title,
            message=message,
            payload=payload,
        ))

    if settings.alerts_aggregate and len(alerts) > 1:
        return [_aggregate(alerts, subject, label, platform)]
    return alerts


def _aggregate(alerts: list[Alert], subject: str, label: str, platform: str) -> Alert:
    """Collapse several alerts for one subject into a single summary alert. Severity
    is the strongest present; the message lists each constituent."""
    strongest = min(alerts, key=lambda a: severity_rank(a.severity)).severity
    lines = "; ".join(a.message for a in alerts)
    types = ",".join(sorted({a.alert_type for a in alerts}))
    keys = "|".join(sorted(a.dedup_key for a in alerts))
    return Alert(
        alert_type=alerts[0].alert_type,  # representative; full set is in payload
        severity=strongest,
        dedup_key=f"agg:{subject}:{keys}",
        platform=platform,
        subject=subject,
        title=f"{len(alerts)} alerts for {label}",
        message=lines,
        payload={"aggregated": [a.model_dump() for a in alerts], "alert_types": types},
    )


# --- Dispatch (alerts -> notification providers, with cooldown + dedupe) ------


def dispatch(alerts: list[Alert], *, config: AlertConfig | None = None) -> int:
    """Deliver `alerts` through the configured notification providers. Returns the
    count actually delivered. No-ops when alerts are disabled or no providers are
    configured. Cooldown + dedupe suppress repeats; delivery reuses
    `notifications.deliver` (providers + retry + audit) — no transport code here.
    NEVER raises."""
    if not settings.alerts_enabled or not alerts:
        return 0
    provider_names = list(settings.notify_providers)
    if not provider_names:
        return 0
    cfg = config or _load_config()

    delivered = 0
    for alert in alerts:
        try:
            if _in_cooldown(alert, cfg):
                continue
            notification = notifications.Notification(
                event_key=alert.dedup_key, event_type=alert.alert_type,
                platform=alert.platform, account_key=alert.subject,
                project_handle=None,
                title=f"[{alert.severity.upper()}] {alert.title}",
                body=alert.message, payload=alert.payload,
            )
            for name in provider_names:
                status = notifications.deliver(notification, name, when=alert.created_at)
                if status == "sent":
                    delivered += 1
        except Exception:  # noqa: BLE001 — alerting is additive; never sink the caller
            logger.exception("alert dispatch errored for %s (analysis unaffected)", alert.subject)
    return delivered


def _in_cooldown(alert: Alert, cfg: AlertConfig) -> bool:
    """Whether an alert of this type for this subject was delivered too recently.
    Reuses the notification delivery log (persisted, so cooldown survives restart):
    finds the most recent `sent` attempt for (subject, alert_type) and compares
    against the rule's cooldown (falling back to the config default)."""
    rule = cfg.rule_for(alert.alert_type, alert.subject)
    window = rule.cooldown_seconds
    if window is None:
        window = cfg.default_cooldown_seconds
    if window <= 0:
        return False
    try:
        recent = kol_store.list_deliveries(account_key=alert.subject, status="sent", limit=50)
    except Exception:  # noqa: BLE001 — a store hiccup must not block a real alert
        return False
    last: datetime | None = None
    for row in recent:
        if row.get("event_type") != alert.alert_type:
            continue
        ts = _parse_iso(row.get("attempted_at"))
        if ts is not None and (last is None or ts > last):
            last = ts
    if last is None:
        return False
    age = (_now() - last).total_seconds()
    return age < window


# --- Convenience wiring hooks (called by the producers) ----------------------


def process_monitor_result(result, entry) -> int:
    """Alert on one token's monitoring outcome (M24 `MonitorResult` + its
    `TokenWatchEntry`). Called additively from the token-monitor cycle. Never raises."""
    events = getattr(result, "events", None) or []
    if not events:
        return 0
    try:
        label = getattr(entry, "label", None) or result.contract_address
        alerts = evaluate(events, subject=result.contract_address,
                          subject_label=label, platform="monitor")
        return dispatch(alerts)
    except Exception:  # noqa: BLE001
        logger.exception("alert processing failed for %s (monitoring unaffected)",
                         getattr(result, "contract_address", "?"))
        return 0


def process_follow_events(platform: str, handle: str, events: list) -> int:
    """Alert on a KOL's newly-detected follows (M23 `FollowEvent`s). Called
    additively from the capture path. Never raises."""
    if not events:
        return 0
    try:
        alerts = evaluate(events, subject=handle, subject_label=f"{platform}:{handle}",
                          platform=platform)
        return dispatch(alerts)
    except Exception:  # noqa: BLE001
        logger.exception("alert processing failed for %s:%s (capture unaffected)", platform, handle)
        return 0


# --- helpers -----------------------------------------------------------------


def _dedup_key(alert_type: str, subject: str, payload: dict, detected_at: str) -> str:
    """Stable identity for one alert occurrence. Ties to the detection time so the
    exact same event replayed collides (suppressed), while a genuinely new
    transition at a later time is a distinct alert (subject to cooldown)."""
    return f"alert:{alert_type}:{subject.lower()}:{detected_at}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
