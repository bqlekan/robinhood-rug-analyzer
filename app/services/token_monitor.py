from __future__ import annotations

"""Token Watchlist & Monitoring Engine (M24).

Continuously re-runs the EXISTING intelligence pipeline against a watchlist of
contract addresses and records only what changed. The cardinal rule of this
module: it reuses, it never reimplements. Specifically —

  * contract analysis, route discovery, honeypot simulation, and rug/risk
    scoring all come from a single call to
    `rug_analyzer.analyze_token_contract`, which already chains them;
  * KOL Intelligence Score + cluster size (when a token is linked to a KOL
    project account) come from `kol_store.get_project_intelligence`, the object
    the M23 correlation engine already computed.

There is NO analysis logic here — only orchestration (scheduling, concurrency,
retries, timeouts), change detection over the reused scalars, and persistence of
the resulting deltas + internal events. It also implements no new notification /
delivery logic; emitting an internal `MonitorEvent` is where its responsibility
ends (delivery is the KOL domain's M23 Deliverable H).

Every public entry point is failure-isolated: a single token's analysis blowing
up or hanging can never sink the cycle, and a cycle can never kill the
scheduler loop that drives it.
"""

import asyncio
import logging

from app.core.config import settings
from app.models.monitor import (
    MonitorCycleReport,
    MonitorEvent,
    MonitorOptions,
    MonitorResult,
    MonitorSnapshot,
    TokenWatchEntry,
)
from app.models.token import is_valid_address
from app.services import alert_engine, kol_store, rug_analyzer, token_monitor_store

logger = logging.getLogger(__name__)


# --- Watchlist management (thin wrappers over the store) ---------------------


def add_token(
    contract_address: str,
    *,
    label: str | None = None,
    enabled: bool = True,
    options: MonitorOptions | dict | None = None,
) -> TokenWatchEntry:
    """Add a token to the monitoring watchlist (or update one with the same
    address). Returns the stored entry. Raises ValueError on an invalid address."""
    address = (contract_address or "").strip()
    if not is_valid_address(address):
        raise ValueError(f"Invalid contract address: {contract_address!r}")
    if isinstance(options, dict):
        options = MonitorOptions(**options)
    entry = TokenWatchEntry(
        contract_address=address,
        label=label,
        enabled=enabled,
        options=options or MonitorOptions(),
        status="pending" if enabled else "paused",
    )
    token_monitor_store.upsert_entry(entry)
    token_monitor_store.save_events(
        [_event("watchlist_updated", entry.contract_address, {"action": "added", "enabled": enabled})]
    )
    logger.info("token monitor: added/updated %s (enabled=%s)", entry.contract_address, enabled)
    # Return the stored entry so the caller sees preserved fields (e.g. the
    # original date_added on a re-add), not the transient object we just built.
    return token_monitor_store.get_entry(entry.contract_address) or entry


def remove_token(contract_address: str) -> bool:
    """Remove a token and all its monitoring data. True if something was removed."""
    address = (contract_address or "").strip().lower()
    removed = token_monitor_store.delete_entry(address)
    if removed:
        token_monitor_store.save_events(
            [_event("watchlist_updated", address, {"action": "removed"})]
        )
        logger.info("token monitor: removed %s", address)
    return removed


def set_enabled(contract_address: str, enabled: bool) -> TokenWatchEntry:
    """Enable or disable monitoring for a token without removing it."""
    entry = _require(contract_address)
    if enabled != entry.enabled:
        entry.enabled = enabled
        # Flipping the toggle moves lifecycle status, unless mid-error (leave the
        # error visible until the next cycle re-evaluates it).
        if not enabled:
            entry.status = "paused"
        elif entry.status == "paused":
            entry.status = "pending"
        token_monitor_store.upsert_entry(entry)
        token_monitor_store.save_events(
            [_event("watchlist_updated", entry.contract_address, {"action": "enabled" if enabled else "disabled"})]
        )
    return entry


def update_options(contract_address: str, options: MonitorOptions | dict) -> TokenWatchEntry:
    """Retune a token's monitoring options (thresholds, lore, KOL linkage)."""
    entry = _require(contract_address)
    entry.options = MonitorOptions(**options) if isinstance(options, dict) else options
    token_monitor_store.upsert_entry(entry)
    token_monitor_store.save_events(
        [_event("watchlist_updated", entry.contract_address, {"action": "options_updated"})]
    )
    return entry


def get_token(contract_address: str) -> TokenWatchEntry | None:
    return token_monitor_store.get_entry((contract_address or "").strip().lower())


def list_tokens(*, enabled_only: bool = False) -> list[TokenWatchEntry]:
    return token_monitor_store.list_entries(enabled_only=enabled_only)


def _require(contract_address: str) -> TokenWatchEntry:
    entry = get_token(contract_address)
    if entry is None:
        raise KeyError(f"token {contract_address!r} is not on the monitoring watchlist")
    return entry


# --- Config-driven seed reconciliation ---------------------------------------


def sync_from_config(seeds: list | None = None) -> dict[str, int]:
    """Reconcile the config seed watchlist into the store on startup. Each seed is
    a bare address string or a dict {address, label?, enabled?, options?}. Only
    ever ADDS missing tokens or refreshes provided fields; never auto-deletes."""
    seeds = seeds if seeds is not None else list(settings.token_monitor_seed or [])
    added = 0
    skipped = 0
    for seed in seeds:
        if isinstance(seed, str):
            seed = {"contract_address": seed}
        address = (seed.get("contract_address") or seed.get("address") or "").strip()
        if not is_valid_address(address):
            logger.warning("token monitor seed skipped (invalid address): %r", seed)
            skipped += 1
            continue
        try:
            add_token(
                address,
                label=seed.get("label"),
                enabled=bool(seed.get("enabled", True)),
                options=seed.get("options"),
            )
            added += 1
        except ValueError:
            skipped += 1
    logger.info("token monitor: synced %d seed(s) from config (%d skipped)", added, skipped)
    return {"added": added, "skipped": skipped}


# --- Snapshot construction (pure REUSE of existing outputs) ------------------


def _privilege_signature(priv) -> str | None:
    """Compact, comparable signature of a contract's retained privileges, or None
    when they couldn't be read (unverified / no ABI). Reuses the M11
    `ContractPrivileges` verbatim — no new analysis, just a stable string so a flip
    in what the dev can still do is detectable as a change."""
    if priv is None or not getattr(priv, "analyzed", False):
        return None
    return (
        f"mint={int(priv.can_mint)},pause={int(priv.can_pause)},"
        f"blacklist={int(priv.can_blacklist)},fees={int(priv.can_set_fees)},"
        f"renounced={priv.ownership_renounced}"
    )


async def _build_snapshot(entry: TokenWatchEntry) -> MonitorSnapshot:
    """Run the reused pipeline once and copy its scalars into a snapshot.

    This is the ONLY place monitoring touches the analyzer, and it does so
    through the same public entry point the API + scanner use — no private
    analysis internals, no recomputation."""
    analysis = await rug_analyzer.analyze_token_contract(
        entry.contract_address, include_lore=entry.options.include_lore
    )

    liquidity_usd = None
    if analysis.market_data and analysis.market_data.liquidity:
        liquidity_usd = analysis.market_data.liquidity.usd

    snapshot = MonitorSnapshot(
        contract_address=entry.contract_address,
        risk_score=analysis.analysis.risk_score,
        risk_level=analysis.analysis.risk_level,
        honeypot_status=analysis.honeypot.status if analysis.honeypot else None,
        liquidity_usd=liquidity_usd,
        # M27 alert sources — verbatim copies from the reused analysis, no recompute.
        top10_concentration=analysis.holders.top10_percentage if analysis.holders else None,
        smart_wallet_count=len(analysis.watchlist_hits),
        privilege_signature=_privilege_signature(analysis.contract_privileges),
    )

    # OPTIONAL KOL linkage: reuse the already-correlated ProjectIntelligence. No
    # recompute — a plain store read of what M23 produced.
    opts = entry.options
    if opts.kol_platform and opts.kol_account_key:
        intel = kol_store.get_project_intelligence(opts.kol_platform, opts.kol_account_key)
        if intel is not None:
            snapshot.kol_score = intel.score
            snapshot.cluster_size = intel.cluster.kol_count if intel.cluster else None
            alpha = (intel.correlation or {}).get("alpha_score")
            snapshot.alpha_score = int(alpha) if alpha is not None else None

    return snapshot


# --- Change detection --------------------------------------------------------

# Which event each tracked field raises when it moves. `project_changed` is added
# as the umbrella whenever any of these fire.
_FIELD_EVENT = {
    "risk_score": "risk_changed",
    "risk_level": "risk_changed",
    "honeypot_status": "honeypot_changed",
    "liquidity_usd": "liquidity_changed",
    "kol_score": "kol_changed",
    "cluster_size": "cluster_changed",
    "alpha_score": "alpha_changed",
    "top10_concentration": "concentration_changed",
    "smart_wallet_count": "smart_wallet_changed",
    "privilege_signature": "privilege_changed",
}


def _is_meaningful_change(field: str, prev, curr, opts: MonitorOptions) -> bool:
    """Whether a field's move clears its configured noise threshold. Appearance or
    disappearance of a value (None <-> value) always counts."""
    if prev is None or curr is None:
        return prev != curr
    if field in ("risk_score", "alpha_score"):
        return abs(int(curr) - int(prev)) >= int(opts.min_risk_delta)
    if field == "kol_score":
        return abs(int(curr) - int(prev)) >= int(opts.min_kol_delta)
    if field == "liquidity_usd":
        if prev == 0:
            return curr != 0
        return abs(float(curr) - float(prev)) / abs(float(prev)) >= float(opts.min_liquidity_change_pct)
    if field == "top10_concentration":
        # Percentage POINTS move (top10 is already a 0..100 percentage), guarded by
        # the same knob as risk so tiny holder jitter doesn't alert every cycle.
        return abs(float(curr) - float(prev)) >= float(opts.min_concentration_delta)
    # risk_level, honeypot_status, cluster_size, smart_wallet_count,
    # privilege_signature: any difference counts.
    return prev != curr


def _detect_changes(
    snapshot: MonitorSnapshot, previous_values: dict | None, opts: MonitorOptions
) -> tuple[list[str], list[MonitorEvent]]:
    """Diff the snapshot against the stored baseline. Returns (changed_fields,
    events). First sighting (no baseline) records the baseline but raises no
    change events — there's nothing to compare against yet."""
    current = snapshot.tracked_values()
    if previous_values is None:
        return [], []

    changed: list[str] = []
    event_types: set[str] = set()
    for field, curr in current.items():
        prev = previous_values.get(field)
        if _is_meaningful_change(field, prev, curr, opts):
            changed.append(field)
            event_types.add(_FIELD_EVENT[field])

    if not changed:
        return [], []

    events = [
        _event(
            etype,
            snapshot.contract_address,
            {
                "changed_fields": [f for f in changed if _FIELD_EVENT[f] == etype],
                "previous": {f: previous_values.get(f) for f in changed if _FIELD_EVENT[f] == etype},
                "current": {f: current.get(f) for f in changed if _FIELD_EVENT[f] == etype},
            },
        )
        for etype in sorted(event_types)
    ]
    # Umbrella event last so the timeline reads specific-then-summary.
    events.append(
        _event(
            "project_changed",
            snapshot.contract_address,
            {"changed_fields": changed, "captured_at": snapshot.captured_at},
        )
    )
    return changed, events


def _event(event_type: str, contract_address: str, payload: dict) -> MonitorEvent:
    return MonitorEvent(event_type=event_type, contract_address=contract_address, payload=payload)


# --- Single-token monitor (with timeout + retry, fully isolated) -------------


async def monitor_once(entry: TokenWatchEntry) -> MonitorResult:
    """Monitor one token: reuse-analyze, diff, persist history + events. Retries a
    failed/timed-out analysis per the configured policy. NEVER raises — a failure
    is captured in the returned result and stamped on the entry."""
    attempts = max(1, int(settings.token_monitor_retry_attempts))
    backoff = max(0.0, float(settings.token_monitor_retry_backoff_seconds))
    timeout = max(1, int(settings.token_monitor_timeout_seconds))

    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            snapshot = await asyncio.wait_for(_build_snapshot(entry), timeout=timeout)
        except asyncio.TimeoutError:
            last_error = f"analysis timed out after {timeout}s"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # analysis failure is isolated, never propagated
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            previous = token_monitor_store.get_latest_values(entry.contract_address)
            first_seen = previous is None
            changed, events = _detect_changes(snapshot, previous, entry.options)
            token_monitor_store.save_history_if_changed(snapshot, previous, changed)
            if events:
                token_monitor_store.save_events(events)
            token_monitor_store.set_last_checked(entry.contract_address, "active")
            outcome = "first_seen" if first_seen else ("changed" if changed else "unchanged")
            return MonitorResult(
                contract_address=entry.contract_address,
                outcome=outcome,
                changed_fields=changed,
                events=events,
                attempts=attempt,
            )
        if attempt < attempts and backoff:
            await asyncio.sleep(backoff * attempt)

    # All attempts failed.
    token_monitor_store.set_last_checked(entry.contract_address, "error")
    logger.warning("token monitor: %s failed after %d attempt(s): %s",
                   entry.contract_address, attempts, last_error)
    return MonitorResult(
        contract_address=entry.contract_address,
        outcome="failed",
        error=last_error,
        attempts=attempts,
    )


# --- Full cycle over the watchlist (bounded concurrency, isolated) -----------


async def run_cycle() -> MonitorCycleReport:
    """Monitor the whole enabled watchlist once, with bounded concurrency. Each
    token is isolated: one failing/hanging token affects only its own result.
    NEVER raises — safe to call directly from the scheduler loop."""
    report = MonitorCycleReport()
    entries = list_tokens(enabled_only=True)
    if not entries:
        report.finished_at = report.started_at
        return report

    concurrency = max(1, int(settings.token_monitor_concurrency))
    sem = asyncio.Semaphore(concurrency)

    async def _guarded(entry: TokenWatchEntry) -> MonitorResult:
        async with sem:
            try:
                result = await monitor_once(entry)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # defensive: monitor_once shouldn't raise
                logger.exception("token monitor: unexpected error for %s", entry.contract_address)
                return MonitorResult(
                    contract_address=entry.contract_address,
                    outcome="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            # M27: connect this token's change events to the configurable alert
            # engine. Additive + fully isolated (process_monitor_result never
            # raises) and a no-op unless `alerts_enabled`; monitoring is unaffected.
            alert_engine.process_monitor_result(result, entry)
            return result

    results = await asyncio.gather(*(_guarded(e) for e in entries))
    report.results = list(results)
    report.processed = len(results)
    report.changed = sum(1 for r in results if r.outcome in ("changed", "first_seen"))
    report.unchanged = sum(1 for r in results if r.outcome == "unchanged")
    report.failed = sum(1 for r in results if r.outcome == "failed")
    from app.models.monitor import utc_now_iso
    report.finished_at = utc_now_iso()
    logger.info(
        "token monitor cycle: processed=%d changed=%d unchanged=%d failed=%d",
        report.processed, report.changed, report.unchanged, report.failed,
    )
    return report
