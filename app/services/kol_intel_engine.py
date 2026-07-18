from __future__ import annotations

"""KOL Intelligence & Correlation engine (M23 Deliverable F).

The correlation layer that turns raw follow events into structured intelligence.
It sits above the pure scorer (`services/social/kol_scoring`, no I/O) and the store,
and REUSES — never recomputes — the Deliverable-D crypto classification and rug
analysis. For one project account it:

  1. Correlates every watched KOL following it (with tier + follow timing), the best
     crypto classification seen for it, and the latest reused rug-analysis summary.
  2. Detects the cluster shape (typed: tier_1 / mixed_tier / rapid / high_conviction)
     and computes a fully-explainable KOL Intelligence Score.
  3. Assembles ONE `ProjectIntelligence` object carrying score, confidence, structured
     evidence, contributors, cluster, the correlation of reused analysis, and a
     timeline of prior scores — everything the future AI Trading Intelligence Engine
     needs to explain the call WITHOUT rescanning or recomputing history.
  4. Persists it (upsert + score/cluster history) and emits engine-internal events
     (kol_score_updated / kol_cluster_detected / high_conviction_cluster /
     project_momentum_detected / intelligence_updated).

Scope guard: this produces and PERSISTS engine-internal facts and a durable timeline.
It emits NO user notifications — Telegram/Discord/webhook/email/UI transports are
Deliverable G/H. The events here are the durable feed those transports will consume.

Performance: scoring is INCREMENTAL. A fingerprint of the scoring inputs is stored;
when a re-trigger produces the same fingerprint, the project is not rescored, no
history row is appended, and no duplicate event is emitted. So re-capturing an
unchanged following list does no redundant work.

Enablement + safety: everything is gated by `settings.kol_score_enabled`, and the
public entrypoints never raise — a correlation failure for one project is logged and
swallowed so it can never turn a good capture into a failed sync.
"""

import hashlib
import json
import logging

from app.core.config import settings
from app.models.kol import (
    ClusterInfo,
    KolContributor,
    KolIntelEvent,
    ProjectIntelligence,
    SocialAccount,
)
from app.services import kol_store, notifications
from app.services.social import kol_scoring

logger = logging.getLogger(__name__)


def _contributors_for(platform: str, account_key: str) -> list[KolContributor]:
    """Build the distinct-KOL contributor list for a project from the follow store.

    Tier comes from the joined watchlist row; a KOL removed from the watchlist (tier
    None) falls back to the configured default tier weight's tier (2) so history still
    scores. Follow timing uses `first_seen` — when this KOL FIRST followed the project."""
    rows = kol_store.list_kols_following(platform, account_key, active_only=True)
    contributors: list[KolContributor] = []
    for row in rows:
        tier = row.get("tier")
        tier = int(tier) if tier is not None else 2
        contributors.append(KolContributor(
            platform=row["platform"],
            kol_handle=row["kol_handle"],
            tier=tier,
            tier_weight=kol_scoring.tier_weight(tier),
            followed_at=row.get("first_seen") or row.get("last_seen") or "",
            account_key=account_key,
        ))
    return contributors


def _fingerprint(
    contributors: list[KolContributor],
    classification: str | None,
    crypto_confidence: str | None,
    analysis: dict | None,
) -> str:
    """Stable hash of the scoring inputs. Unchanged fingerprint => skip rescoring.

    Includes each distinct KOL + their follow time + tier, the crypto verdict, and the
    reused analysis' risk verdict — exactly the things that move the score. Ordering is
    normalized so the same set in a different order hashes identically."""
    kol_part = sorted(
        f"{c.platform}:{c.kol_handle.lower()}:{c.tier}:{c.followed_at}" for c in contributors
    )
    analysis_part = ""
    if analysis:
        analysis_part = f"{analysis.get('risk_score')}:{analysis.get('risk_level')}:{analysis.get('status')}"
    raw = json.dumps(
        {"kols": kol_part, "cls": classification, "conf": crypto_confidence, "an": analysis_part},
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _analysis_inputs(summary: dict | None) -> tuple[bool, int | None, int | None]:
    """Extract (analyzed, risk_score, alpha_score) from a reused analysis summary.

    Tolerant of a partial/missing summary. `alpha_score` is future-extensible: it's
    read if a summary ever carries one, else None (the component simply won't fire)."""
    if not summary:
        return False, None, None
    risk = summary.get("risk_score")
    analyzed = risk is not None
    alpha = summary.get("alpha_score")  # None today; ready for a future alpha scorer
    return analyzed, (int(risk) if risk is not None else None), (int(alpha) if alpha is not None else None)


def update_project_intelligence(
    platform: str,
    account_key: str,
    *,
    project_handle: str | None = None,
    force: bool = False,
) -> ProjectIntelligence | None:
    """Correlate + (re)score one project account, persist, and emit events.

    Returns the fresh `ProjectIntelligence`, the unchanged existing one when the
    fingerprint matched (no rescoring done), or None when disabled / no contributors.
    Set `force=True` to rescore even when the fingerprint is unchanged. Never raises.
    """
    if not settings.kol_score_enabled:
        return None

    contributors = _contributors_for(platform, account_key)
    if not contributors:
        return None

    classification = kol_store.best_classification_for_account(platform, account_key)
    cls_name = classification.classification if classification else None
    crypto_conf = classification.confidence if classification else None
    if project_handle is None and classification is not None:
        project_handle = classification.handle

    analysis_summary = kol_store.latest_analysis_summary(platform, account_key)
    analyzed, risk_score, alpha_score = _analysis_inputs(analysis_summary)

    fingerprint = _fingerprint(contributors, cls_name, crypto_conf, analysis_summary)
    previous = kol_store.get_project_intelligence(platform, account_key)

    # Incremental: identical inputs => no work, no history churn, no duplicate events.
    if not force and previous is not None and previous.fingerprint == fingerprint:
        logger.debug(
            "intel unchanged for %s:%s (fingerprint %s) — skipping rescore",
            platform, account_key, fingerprint,
        )
        return previous

    # Cluster shape first (needs no score), then score (uses the cluster), then re-tag
    # the cluster with high_conviction now that the score exists.
    cluster = kol_scoring.detect_cluster(
        platform, account_key, contributors, project_handle=project_handle,
    )
    score, confidence, evidence = kol_scoring.score_project(
        contributors,
        crypto_confidence=crypto_conf,
        classification=cls_name,
        risk_score=risk_score,
        analyzed=analyzed,
        cluster=cluster,
        alpha_score=alpha_score,
    )
    cluster = kol_scoring.detect_cluster(
        platform, account_key, contributors, project_handle=project_handle, score=score,
    )

    correlation = {
        "analyzed": analyzed,
        "risk_score": risk_score,
        "risk_level": (analysis_summary or {}).get("risk_level"),
        "analysis_confidence": (analysis_summary or {}).get("confidence"),
        "alpha_score": alpha_score,  # None today; future extensible
        "classification": cls_name,
        "crypto_confidence": crypto_conf,
    }

    intel = ProjectIntelligence(
        platform=platform,
        account_key=account_key,
        project_handle=project_handle,
        classification=cls_name,
        crypto_confidence=crypto_conf,
        score=score,
        confidence=confidence,
        evidence=evidence,
        contributors=cluster.contributors,  # distinct, de-duplicated set
        cluster=cluster,
        correlation=correlation,
        kol_count=cluster.kol_count,
        fingerprint=fingerprint,
    )

    _persist_and_emit(intel, previous)
    return intel


def _persist_and_emit(intel: ProjectIntelligence, previous: ProjectIntelligence | None) -> None:
    """Persist the intelligence object + history and emit the internal event timeline.

    Ordering mirrors the rest of the engine: the authoritative object first, then
    history, then events — a crash mid-way still leaves a consistent, readable story."""
    kol_store.save_project_intelligence(intel)
    kol_store.append_score_history(
        intel.platform, intel.account_key, intel.score, intel.confidence, intel.kol_count,
        when=intel.updated_at,
    )
    if intel.cluster is not None and intel.cluster.is_cluster:
        kol_store.append_cluster_history(intel.platform, intel.cluster, when=intel.updated_at)

    events = _build_events(intel, previous)
    kol_store.save_intel_events(events)

    # Deliverable H: hand the just-persisted events to the notification layer. It
    # consumes these events + this already-computed intelligence (no recompute),
    # applies the configured forwarding rules, and delivers to the configured sinks.
    # No-ops when disabled, and fully failure-isolated — a delivery failure can never
    # interrupt the correlation/capture that produced these events.
    notifications.dispatch_events(events, intel)

    logger.info(
        "intelligence updated %s:%s score=%s (%s) kols=%s cluster=%s",
        intel.platform, intel.account_key, intel.score, intel.confidence,
        intel.kol_count, intel.cluster.cluster_types if intel.cluster else [],
    )


def _build_events(intel: ProjectIntelligence, previous: ProjectIntelligence | None) -> list[KolIntelEvent]:
    """Derive the internal intelligence events implied by this update.

    Always emits `intelligence_updated` (umbrella) and `kol_score_updated`. Adds
    `kol_cluster_detected` / `high_conviction_cluster` when a cluster is present, and
    `project_momentum_detected` when the distinct-KOL count grew by the configured
    minimum since the last score. These are engine-internal facts, NOT user alerts."""
    def ev(event_type: str, payload: dict) -> KolIntelEvent:
        return KolIntelEvent(
            event_type=event_type, platform=intel.platform, account_key=intel.account_key,
            project_handle=intel.project_handle, payload=payload,
        )

    prev_score = previous.score if previous else None
    prev_kols = previous.kol_count if previous else 0

    events: list[KolIntelEvent] = [
        ev("kol_score_updated", {
            "score": intel.score, "previous_score": prev_score,
            "confidence": intel.confidence, "kol_count": intel.kol_count,
            "evidence": [e.model_dump() for e in intel.evidence],
        }),
    ]

    if intel.cluster is not None and intel.cluster.is_cluster:
        events.append(ev("kol_cluster_detected", {
            "cluster_types": intel.cluster.cluster_types,
            "kol_count": intel.cluster.kol_count,
            "tier_counts": intel.cluster.tier_counts,
            "window_hours": intel.cluster.window_hours,
        }))
        if "high_conviction" in intel.cluster.cluster_types:
            events.append(ev("high_conviction_cluster", {
                "score": intel.score, "confidence": intel.confidence,
                "cluster_types": intel.cluster.cluster_types,
            }))

    # Momentum: distinct-KOL count grew by the configured minimum since last time.
    min_new = int(settings.kol_momentum_min_new_kols)
    if intel.kol_count - prev_kols >= min_new and prev_kols > 0:
        events.append(ev("project_momentum_detected", {
            "kol_count": intel.kol_count, "previous_kol_count": prev_kols,
            "delta": intel.kol_count - prev_kols,
        }))

    events.append(ev("intelligence_updated", {
        "score": intel.score, "confidence": intel.confidence,
        "kol_count": intel.kol_count, "is_actionable": intel.is_actionable,
        "cluster_types": intel.cluster.cluster_types if intel.cluster else [],
    }))
    return events


def process_new_project_follows(
    platform: str,
    accounts: list[SocialAccount],
    *,
    project_keys: list[str] | None = None,
) -> list[ProjectIntelligence]:
    """Entrypoint from the capture flow: (re)score each project a new follow touched.

    `accounts` are the newly-followed accounts from a capture; `project_keys`, when
    given, restricts scoring to those account keys (e.g. only accounts the crypto
    pipeline classified as projects). Best-effort and gated: no-op when disabled, and
    a failure scoring one project is logged and swallowed so the batch — and the
    capture that triggered it — always completes."""
    if not settings.kol_score_enabled or not accounts:
        return []
    wanted = set(project_keys) if project_keys is not None else None
    results: list[ProjectIntelligence] = []
    seen: set[str] = set()
    for account in accounts:
        key = account.key()
        if key in seen or (wanted is not None and key not in wanted):
            continue
        seen.add(key)
        try:
            intel = update_project_intelligence(
                platform, key, project_handle=account.handle,
            )
        except Exception:  # noqa: BLE001 — intelligence is additive; never sink a capture
            logger.exception(
                "KOL intelligence engine errored for %s:%s (capture unaffected)",
                platform, key,
            )
            continue
        if intel is not None:
            results.append(intel)
    return results
