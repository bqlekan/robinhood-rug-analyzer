from __future__ import annotations

"""Domain models for the Token Watchlist & Monitoring Engine (M24).

The monitoring engine continuously re-runs the EXISTING intelligence pipeline
(`rug_analyzer.analyze_token_contract` + the KOL project intelligence already
correlated by M23) against a watchlist of contract addresses, and records only
what CHANGED. It never re-implements any analysis: every field in
`MonitorSnapshot` is copied verbatim from an output another service already
produced.

Conventions follow `models/token.py` and `models/kol.py`: pydantic `BaseModel`,
string-typed "enums" documented with a `# "a" | "b"` comment and enforced by a
`field_validator`, and stringly-typed ISO-8601 timestamps (matching how the
sqlite stores in this project persist time).
"""

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

# --- Controlled vocabularies -------------------------------------------------

# Lifecycle/health status of a watched token. Distinct from the enabled toggle
# (a token can be enabled but temporarily "error").
#   pending — enabled, never monitored yet (no snapshot on record)
#   active  — enabled and the last monitoring cycle succeeded
#   paused  — administratively disabled (enabled=False)
#   error   — enabled but the last monitoring cycle failed (after retries)
MONITOR_STATUSES: frozenset[str] = frozenset({"pending", "active", "paused", "error"})

# Internal monitoring events. These are engine-internal facts describing a
# detected change, NOT user notifications (M24 explicitly does not implement any
# new notification/delivery logic — that is M23 Deliverable H's job and lives in
# the KOL intelligence domain). `project_changed` is the umbrella event, emitted
# alongside the specific per-field events whenever anything meaningful moved; a
# no-change cycle emits nothing (see the engine's dedupe rule).
MONITOR_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "watchlist_updated",         # a watchlist entry was added/removed/toggled/retuned
        "project_changed",           # umbrella: one or more meaningful fields changed
        "risk_changed",              # rug risk_score / risk_level moved
        "alpha_changed",             # external alpha score moved (when one exists)
        "liquidity_changed",         # pool liquidity (USD) moved beyond the threshold
        "honeypot_changed",          # honeypot/sell-tax status flipped
        "kol_changed",               # KOL Intelligence Score moved
        "cluster_changed",           # distinct-KOL cluster size moved
        "concentration_changed",     # top-10 holder concentration moved (M27 alert source)
        "smart_wallet_changed",      # smart/insider wallet count on the token moved (M27)
        "privilege_changed",         # contract privilege/authority signature moved (M27)
    }
)


def utc_now_iso() -> str:
    """ISO-8601 UTC timestamp, matching the format the sqlite stores persist."""
    return datetime.now(timezone.utc).isoformat()


# --- Watchlist ---------------------------------------------------------------


class MonitorOptions(BaseModel):
    """Per-token monitoring options. All optional with sensible defaults so an
    entry added with just an address behaves reasonably. Change thresholds gate
    what counts as a *meaningful* move for the noisy continuous fields (liquidity
    is a float that jitters constantly; scores are ints but small wiggles aren't
    worth an event)."""

    # Include the (slower) lore/web-search step when analyzing. Off by default:
    # monitoring runs often and lore rarely changes the risk picture.
    include_lore: bool = False
    # Minimum absolute change in the 0..100 risk score to count as a move.
    min_risk_delta: int = 1
    # Minimum absolute change in the 0..100 KOL Intelligence Score to count.
    min_kol_delta: int = 1
    # Minimum FRACTIONAL change in pool liquidity (USD) to count, e.g. 0.10 = 10%.
    # Guards against constant float jitter producing an event every cycle.
    min_liquidity_change_pct: float = 0.10
    # Minimum change in top-10 holder concentration (percentage POINTS) to count
    # as a move (M27). top10 is a 0..100 percentage, so this is an absolute-points
    # threshold, guarding against holder-list jitter.
    min_concentration_delta: float = 5.0
    # OPTIONAL linkage to an already-correlated KOL project account (M23). KOL
    # intelligence is keyed by social account, not contract; there is no reverse
    # index from a contract to a project. Rather than invent one, monitoring lets
    # an operator tie a token to the project account whose `ProjectIntelligence`
    # it should track. When both are set, the engine REUSES
    # `kol_store.get_project_intelligence(platform, account_key)` to pull the KOL
    # Intelligence Score + cluster size. When unset, those signals stay None
    # (correctly "not linked") and only the on-chain signals are monitored.
    kol_platform: str | None = None
    kol_account_key: str | None = None


class TokenWatchEntry(BaseModel):
    """One monitored token. `contract_address` is the identity (lowercased)."""

    contract_address: str
    label: str | None = None
    enabled: bool = True
    options: MonitorOptions = Field(default_factory=MonitorOptions)
    date_added: str = Field(default_factory=utc_now_iso)
    last_checked: str | None = None
    status: str = "pending"

    @field_validator("contract_address")
    @classmethod
    def _normalize_address(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not v:
            raise ValueError("contract_address must not be empty")
        return v

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in MONITOR_STATUSES:
            raise ValueError(
                f"unknown status {v!r}; expected one of {sorted(MONITOR_STATUSES)}"
            )
        return v


# --- Monitoring snapshot -----------------------------------------------------


class MonitorSnapshot(BaseModel):
    """The compact set of scalars we track for change detection.

    EVERY field here is copied verbatim from an output an existing service
    already produced (`TokenAnalysisResponse` from the rug analyzer, or
    `ProjectIntelligence` from the KOL engine). Nothing here is recomputed. It
    is intentionally small: monitoring cares about *movement* in these signals,
    not about re-storing the whole analysis (which the analyzer/KOL stores
    already persist)."""

    contract_address: str
    captured_at: str = Field(default_factory=utc_now_iso)
    # From TokenAnalysisResponse.analysis (rug scoring — reused, not recomputed).
    risk_score: int | None = None
    risk_level: str | None = None
    # From TokenAnalysisResponse.honeypot (honeypot simulation — reused).
    honeypot_status: str | None = None
    # From TokenAnalysisResponse.market_data.liquidity.usd (route/market — reused).
    liquidity_usd: float | None = None
    # From KOL ProjectIntelligence (correlation engine — reused). None when the
    # token isn't linked to any watched project's intelligence.
    kol_score: int | None = None
    cluster_size: int | None = None
    # From the reused analysis correlation; None today (no alpha scorer exists),
    # but tracked so the engine detects a move the moment one is wired upstream.
    alpha_score: int | None = None
    # M27 alert sources — every field is copied VERBATIM from the reused analysis
    # (HolderDistribution / watchlist_hits / ContractPrivileges); nothing recomputed.
    # From TokenAnalysisResponse.holders.top10_percentage.
    top10_concentration: float | None = None
    # Count of smart/insider watchlisted wallets flagged on the token
    # (len(TokenAnalysisResponse.watchlist_hits)).
    smart_wallet_count: int | None = None
    # A compact, comparable signature of the contract's retained privileges
    # (mint/pause/blacklist/fees + ownership state) — so a flip in what the dev can
    # still do surfaces as a change. Built by the engine from ContractPrivileges;
    # None when the contract was unverified / privileges couldn't be read.
    privilege_signature: str | None = None

    def tracked_values(self) -> dict:
        """The comparable field map used for diffing (excludes identity/time)."""
        return {
            "risk_score": self.risk_score,
            "risk_level": self.risk_level,
            "honeypot_status": self.honeypot_status,
            "liquidity_usd": self.liquidity_usd,
            "kol_score": self.kol_score,
            "cluster_size": self.cluster_size,
            "alpha_score": self.alpha_score,
            "top10_concentration": self.top10_concentration,
            "smart_wallet_count": self.smart_wallet_count,
            "privilege_signature": self.privilege_signature,
        }


# --- Monitoring event + history + run result ---------------------------------


class MonitorEvent(BaseModel):
    """An engine-internal fact describing a detected change on a monitored token.

    NOT a user notification. `payload` is a self-describing JSON-able dict so the
    durable timeline explains itself (previous -> current) without a re-read."""

    event_type: str
    contract_address: str
    detected_at: str = Field(default_factory=utc_now_iso)
    payload: dict = Field(default_factory=dict)

    @field_validator("event_type")
    @classmethod
    def _validate_event_type(cls, v: str) -> str:
        if v not in MONITOR_EVENT_TYPES:
            raise ValueError(
                f"unknown monitor event type {v!r}; expected one of {sorted(MONITOR_EVENT_TYPES)}"
            )
        return v


class MonitorHistoryEntry(BaseModel):
    """One persisted monitoring-history row: what changed, and the before/after."""

    contract_address: str
    captured_at: str = Field(default_factory=utc_now_iso)
    changed_fields: list[str] = Field(default_factory=list)
    previous_values: dict = Field(default_factory=dict)
    current_values: dict = Field(default_factory=dict)


class MonitorResult(BaseModel):
    """Outcome of monitoring a single token in one cycle."""

    contract_address: str
    # "unchanged" | "changed" | "first_seen" | "failed" | "skipped"
    outcome: str
    changed_fields: list[str] = Field(default_factory=list)
    events: list[MonitorEvent] = Field(default_factory=list)
    error: str | None = None
    attempts: int = 1


class MonitorCycleReport(BaseModel):
    """Aggregate outcome of one full scheduler cycle over the watchlist."""

    started_at: str = Field(default_factory=utc_now_iso)
    finished_at: str | None = None
    processed: int = 0
    changed: int = 0
    unchanged: int = 0
    failed: int = 0
    results: list[MonitorResult] = Field(default_factory=list)
