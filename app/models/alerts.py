from __future__ import annotations

"""Domain models for Watchlist Alerts & Intelligent Notifications (M27).

M27 adds NO new intelligence. It connects the events other services already
produce — token-monitor change events (M24), KOL follow events (M23) — to
configurable alert rules, and delivers the ones that pass through the EXISTING
notification providers (M23-H/M26). These models are the config + carrier shapes:

  - `ALERT_TYPES` — the fixed vocabulary of alertable conditions (each maps to an
    existing event type; see `alert_engine.EVENT_TO_ALERT`).
  - `SEVERITY_LEVELS` — ranked severities used for filtering + display.
  - `AlertRule` — per-alert-type config (enabled, severity, cooldown).
  - `AlertConfig` — global defaults + per-token overrides, with `rule_for(...)`.
  - `Alert` — one rendered, ready-to-deliver alert (message + dedup identity).

Conventions follow `models/monitor.py` / `models/kol.py`: pydantic `BaseModel`,
string "enums" enforced by a `field_validator`, ISO-8601 string timestamps.
"""

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Controlled vocabularies -------------------------------------------------

# The ten alertable conditions. Each is sourced from an event some existing
# service already emits (never a new computation) — the mapping lives in
# `alert_engine.EVENT_TO_ALERT`.
ALERT_TYPES: frozenset[str] = frozenset({
    "new_kol_follow",          # a tracked KOL followed a new account (FollowEvent new_follow)
    "kol_cluster",             # multiple KOLs converged on one project (kol_cluster_detected)
    "risk_change",             # a watched token's rug risk score/level moved (risk_changed)
    "alpha_change",            # a watched token's alpha score moved (alpha_changed)
    "liquidity_drop",          # a watched token's pool liquidity dropped (liquidity_changed)
    "concentration_change",    # top-10 holder concentration moved (concentration_changed)
    "smart_wallet_activity",   # smart/insider wallet count on the token moved (smart_wallet_changed)
    "new_watchlist_token",     # a token was added to the monitoring watchlist (watchlist_updated)
    "honeypot_change",         # honeypot/sell-tax status flipped (honeypot_changed)
    "privilege_change",        # contract privilege/authority signature moved (privilege_changed)
})

# Severity, strongest first. `severity_rank` compares them; a rule's severity is
# the level an alert of that type is emitted at, and `alerts_min_severity` gates
# which levels are delivered.
SEVERITY_LEVELS: tuple[str, ...] = ("critical", "high", "medium", "low", "info")


def severity_rank(level: str) -> int:
    """Rank a severity so levels are comparable (LOWER index = stronger)."""
    try:
        return SEVERITY_LEVELS.index(level)
    except ValueError:
        return len(SEVERITY_LEVELS)


# Sensible default severity per alert type (config can override per rule).
_DEFAULT_SEVERITY: dict[str, str] = {
    "new_kol_follow": "info",
    "kol_cluster": "high",
    "risk_change": "high",
    "alpha_change": "medium",
    "liquidity_drop": "critical",
    "concentration_change": "medium",
    "smart_wallet_activity": "medium",
    "new_watchlist_token": "info",
    "honeypot_change": "critical",
    "privilege_change": "high",
}


# --- Rule config -------------------------------------------------------------


class AlertRule(BaseModel):
    """Config for one alert type. All fields optional so a partial override (e.g.
    just `enabled=False` for one token) leaves the rest at the global default."""

    enabled: bool = True
    # Emitted severity for alerts of this type (one of SEVERITY_LEVELS).
    severity: str = "medium"
    # Per-rule cooldown (seconds) between two alerts with the same dedup identity.
    # None => fall back to the config-level `alerts_cooldown_seconds`.
    cooldown_seconds: int | None = None

    @field_validator("severity")
    @classmethod
    def _validate_severity(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in SEVERITY_LEVELS:
            raise ValueError(f"unknown severity {v!r}; expected one of {list(SEVERITY_LEVELS)}")
        return v


class AlertConfig(BaseModel):
    """The resolved alert configuration: global default rules keyed by alert type,
    plus per-token (lowercased contract address) overrides. `rule_for` merges an
    override onto the global default so precedence is: per-token > global > built-in."""

    default_cooldown_seconds: int = 3600
    rules: dict[str, AlertRule] = Field(default_factory=dict)
    token_overrides: dict[str, dict[str, AlertRule]] = Field(default_factory=dict)

    def rule_for(self, alert_type: str, token: str | None = None) -> AlertRule:
        """The effective rule for `alert_type` on `token` (per-token override beats
        the global rule beats the built-in default severity)."""
        base = self.rules.get(alert_type) or AlertRule(
            severity=_DEFAULT_SEVERITY.get(alert_type, "medium")
        )
        if token:
            override = (self.token_overrides.get(token.lower()) or {}).get(alert_type)
            if override is not None:
                # Merge: overridden fields win, unspecified fall back to the base.
                merged = base.model_dump()
                for k, v in override.model_dump(exclude_unset=True).items():
                    merged[k] = v
                return AlertRule(**merged)
        return base


# --- Rendered alert ----------------------------------------------------------


class Alert(BaseModel):
    """One evaluated, ready-to-deliver alert. Carries its own dedup identity
    (`dedup_key`) and a human-readable `title`/`message`; `payload` passes the
    source event's self-describing dict straight through for structured sinks."""

    alert_type: str
    severity: str
    # Stable dedupe identity, also the notification `event_key`. Same condition on
    # the same subject within the cooldown collides and is suppressed.
    dedup_key: str
    # Subject identity (contract address or platform:account) for the delivery log.
    platform: str = "monitor"
    subject: str = ""
    title: str
    message: str
    payload: dict = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)

    @field_validator("alert_type")
    @classmethod
    def _validate_alert_type(cls, v: str) -> str:
        if v not in ALERT_TYPES:
            raise ValueError(f"unknown alert type {v!r}; expected one of {sorted(ALERT_TYPES)}")
        return v

    @field_validator("severity")
    @classmethod
    def _validate_severity(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in SEVERITY_LEVELS:
            raise ValueError(f"unknown severity {v!r}; expected one of {list(SEVERITY_LEVELS)}")
        return v
