from __future__ import annotations

"""Crypto intelligence orchestrator (M23 Deliverable D).

Sits between the pure analyzer (`services/social/crypto_intel`, no I/O) and the
store/rug-analyzer. For each newly-followed account it:

  1. Classifies the account (type + confidence + score + evidence + contracts).
  2. Persists the classification (upsert) and emits internal events:
       - crypto_project_detected  when the verdict is a confident crypto project
       - contract_extracted       per analyzable contract found on the profile
       - analysis_completed        when the rug analyzer returns for a contract
       - analysis_failed           when analysis errors (recorded, never raised out)
  3. For confident crypto projects only, hands each *supported* contract to the
     EXISTING rug analyzer (`rug_analyzer.analyze_token_contract`) — reusing all of
     its contract analysis, honeypot simulation, launchpad detection, and risk +
     alpha scoring. No analysis logic is duplicated here.

Scope guard: this produces and PERSISTS engine-internal facts. It emits NO user
alerts and does NO KOL scoring/clustering — those are later deliverables. The events
here are a durable audit log for them to consume.

Reuse boundary: the rug analyzer is called through a small TTL cache keyed by
contract address, so the same contract surfacing across many KOLs' follows within
the window is analyzed once. The analyzer already caches its own immutable sub-reads;
this cache dedups whole-analysis calls at the pipeline edge. Analysis is best-effort:
a single contract's failure is captured as an `analysis_failed` event and the batch
continues — one bad token never sinks a snapshot's worth of intelligence.

Enablement: everything here is gated by `settings.kol_crypto_intel_enabled`. When
off, `process_new_follow(s)` is a no-op returning empty results, so the crypto
pipeline stays fully opt-in like the rest of M23.
"""

import logging

from app.core.config import settings
from app.models.kol import (
    CryptoClassification,
    CryptoIntelEvent,
    SocialAccount,
)
from app.services import kol_store, rug_analyzer
from app.services.cache import TTLCache, cached_call
from app.services.social import crypto_intel

logger = logging.getLogger(__name__)

# Whole-analysis dedup cache, keyed by lowercased contract address. Analysis results
# are effectively stable over a short window; the analyzer's own caches cover its
# immutable sub-reads. TTL is generous because a fresh follow burst referencing the
# same contract shouldn't re-run the full analysis for each KOL.
_ANALYSIS_CACHE = TTLCache(ttl=600.0, max_size=256)


def _risk_summary(result) -> dict:
    """Compact, JSON-able summary of a rug-analysis result for the event payload.

    Deliberately small: the event log records the verdict, not the whole response.
    Tolerant of a partial/odd result object so a summary never raises."""
    analysis = getattr(result, "analysis", None)
    summary = {
        "contract_address": getattr(result, "contract_address", None),
        "chain": getattr(result, "chain", None),
        "status": getattr(result, "status", None),
    }
    if analysis is not None:
        summary.update({
            "risk_score": getattr(analysis, "risk_score", None),
            "risk_level": getattr(analysis, "risk_level", None),
            "confidence": getattr(analysis, "confidence", None),
        })
    return summary


async def _analyze_contract(address: str):
    """Run the existing rug analyzer for one contract, deduped via the TTL cache."""
    return await cached_call(
        _ANALYSIS_CACHE, address.lower(), lambda: rug_analyzer.analyze_token_contract(address)
    )


async def process_new_follow(
    platform: str,
    kol_handle: str,
    account: SocialAccount,
) -> CryptoClassification | None:
    """Classify one newly-followed account, persist it, and (if it's a confident
    crypto project) analyze its supported contracts through the rug analyzer.

    Returns the persisted `CryptoClassification`, or None when the pipeline is
    disabled. Never raises for a single account's analysis failure — failures are
    recorded as `analysis_failed` events so the audit log is complete and the caller
    (a snapshot-wide loop) keeps going."""
    if not settings.kol_crypto_intel_enabled:
        return None

    classification = crypto_intel.classify_account(account)
    kol_store.save_classification(kol_handle, classification)

    events: list[CryptoIntelEvent] = []

    def _event(event_type: str, payload: dict) -> None:
        events.append(CryptoIntelEvent(
            event_type=event_type, platform=platform, kol_handle=kol_handle,
            account_key=classification.account_key, payload=payload,
        ))

    is_actionable = (
        classification.is_crypto_project
        and classification.score >= int(settings.kol_crypto_min_score)
    )

    if is_actionable:
        _event("crypto_project_detected", {
            "classification": classification.classification,
            "confidence": classification.confidence,
            "score": classification.score,
            "signals": classification.signals,
            "handle": classification.handle,
        })

        supported = classification.supported_contracts()
        for contract in supported:
            _event("contract_extracted", {
                "address": contract.address,
                "chain": contract.chain,
                "source": contract.source,
                "evidence": contract.evidence,
            })

        # Analyze each supported contract through the existing analyzer. Persist
        # events immediately per contract so a crash mid-batch still leaves a record.
        kol_store.save_crypto_events(events)
        events = []

        for contract in supported:
            try:
                result = await _analyze_contract(contract.address)
            except Exception as exc:  # noqa: BLE001 — one bad token must not sink the batch
                logger.info(
                    "rug analysis failed for %s (from %s:%s follow %s): %s",
                    contract.address, platform, kol_handle, classification.handle, exc,
                )
                kol_store.save_crypto_events([CryptoIntelEvent(
                    event_type="analysis_failed", platform=platform, kol_handle=kol_handle,
                    account_key=classification.account_key,
                    payload={"address": contract.address, "error": str(exc)},
                )])
                continue
            kol_store.save_crypto_events([CryptoIntelEvent(
                event_type="analysis_completed", platform=platform, kol_handle=kol_handle,
                account_key=classification.account_key,
                payload=_risk_summary(result),
            )])
    else:
        # Persist any (non-project) events; today there are none, but keeping the
        # write here means future non-actionable events (e.g. "crypto_adjacent seen")
        # slot in without restructuring.
        kol_store.save_crypto_events(events)

    logger.info(
        "classified %s:%s follow %s -> %s (%s, score %s)%s",
        platform, kol_handle, classification.handle,
        classification.classification, classification.confidence, classification.score,
        " [analyzed]" if is_actionable else "",
    )
    return classification


async def process_new_follows(
    platform: str,
    kol_handle: str,
    accounts: list[SocialAccount],
) -> list[CryptoClassification]:
    """Classify + persist + analyze a batch of newly-followed accounts.

    No-op (empty list) when the pipeline is disabled. Processes sequentially so the
    analysis cache warms across the batch and the event log stays ordered; each
    account is independent, so one failure never blocks the rest."""
    if not settings.kol_crypto_intel_enabled or not accounts:
        return []
    results: list[CryptoClassification] = []
    for account in accounts:
        classification = await process_new_follow(platform, kol_handle, account)
        if classification is not None:
            results.append(classification)
    return results


def reset_cache_for_tests() -> None:
    """Clear the whole-analysis dedup cache (tests drive analysis deterministically)."""
    _ANALYSIS_CACHE.clear()
