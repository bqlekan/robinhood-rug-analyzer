from __future__ import annotations

"""KOL Intelligence capture scheduler (M25).

Automates the M23 pipeline. It owns NO intelligence logic of its own: a cycle
simply iterates the enabled KOL watchlist and calls
`kol_watchlist.capture_following` per KOL — which already chains fetch (X
provider) -> snapshot -> diff (kol_monitor) -> crypto detection -> KOL scoring ->
cluster detection -> event pipeline. The scheduler adds only orchestration:
bounded concurrency, per-KOL timeout + retry/backoff, failure isolation,
duplicate-run prevention, progress logging, and graceful shutdown.

Reuse, never reimplement:
  * capture + persistence + diff + crypto + scoring + clustering + events all
    come from the single `capture_following` entry point (unchanged);
  * resume-after-restart is free — all state (snapshots, sync_meta, followed
    accounts) lives in `kol_store`, so a fresh process just resumes iterating
    the persisted watchlist and diffs against the last persisted snapshot.

Opt-in like every other engine here: the loop only runs when
`kol_scheduler_enabled` is set. Every entry point is failure-isolated — one KOL
blowing up or hanging can never sink the cycle, and a cycle can never kill the
loop that drives it.
"""

import asyncio
import logging
from dataclasses import dataclass, field

from app.core.config import settings
from app.models.kol import KolEntry, utc_now_iso
from app.services import kol_watchlist
from app.services.social import get_provider
from app.services.social.base import ProviderError

logger = logging.getLogger(__name__)


@dataclass
class KolCaptureResult:
    """Outcome of capturing one KOL. `outcome` is one of:
    captured (complete snapshot persisted), incomplete (partial pull; prior state
    kept, retryable), skipped (no usable provider), failed (all attempts exhausted)."""

    platform: str
    handle: str
    outcome: str
    attempts: int = 0
    error: str | None = None


@dataclass
class KolCycleReport:
    """Aggregate of one scheduler cycle. `skipped_cycle` is set when a prior cycle
    was still running and this tick was declined (duplicate-run prevention)."""

    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str | None = None
    processed: int = 0
    captured: int = 0
    incomplete: int = 0
    failed: int = 0
    skipped: int = 0
    skipped_cycle: bool = False
    results: list[KolCaptureResult] = field(default_factory=list)


# Duplicate-run guard: a cycle that overruns the interval must never overlap the
# next tick (that would double the scrape load and race the store). One lock per
# process; a tick that finds it held declines rather than queues.
_cycle_lock = asyncio.Lock()


async def capture_one(entry: KolEntry) -> KolCaptureResult:
    """Capture one KOL with timeout + retry/backoff. NEVER raises — every failure
    is captured in the returned result. Reuses `capture_following` wholesale.

    A missing/incapable provider is a permanent skip (no retry). A `ProviderError`
    marked non-retryable (e.g. account suspended) stops early; retryable ones and
    timeouts back off and retry. An incomplete snapshot means the provider returned
    a partial list — `capture_following` preserved the prior baseline, so we treat
    it as a retryable non-success.
    """
    platform, handle = entry.platform, entry.handle

    # Cheap permanent-skip check up front, so a platform with no live provider
    # doesn't burn retry attempts every cycle.
    provider = get_provider(platform)
    if provider is None or not provider.capabilities().can_fetch_following:
        return KolCaptureResult(platform, handle, "skipped",
                                error="no capture-capable provider for platform")

    attempts = max(1, int(settings.kol_scheduler_retry_attempts))
    backoff = max(0.0, float(settings.kol_scheduler_retry_backoff_seconds))
    timeout = max(1, int(settings.kol_scheduler_timeout_seconds))

    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            snapshot = await asyncio.wait_for(
                kol_watchlist.capture_following(handle, platform), timeout=timeout
            )
        except asyncio.TimeoutError:
            last_error = f"capture timed out after {timeout}s"
        except asyncio.CancelledError:
            raise  # shutdown — propagate so the loop can cancel cleanly
        except ProviderError as exc:
            last_error = str(exc)
            if not exc.retryable:  # permanent (suspended/private): don't retry
                break
        except Exception as exc:  # noqa: BLE001 — one KOL never sinks the cycle
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if snapshot.complete:
                return KolCaptureResult(platform, handle, "captured", attempts=attempt)
            # Partial pull: capture_following kept the prior snapshot; retry.
            last_error = "incomplete snapshot; prior state preserved"
        if attempt < attempts and backoff:
            await asyncio.sleep(backoff * attempt)

    # Distinguish an exhausted-but-partial pull from a hard failure.
    outcome = "incomplete" if last_error and "incomplete snapshot" in last_error else "failed"
    logger.info("kol scheduler: %s:%s %s after %d attempt(s): %s",
                platform, handle, outcome, attempts, last_error)
    return KolCaptureResult(platform, handle, outcome, attempts=attempts, error=last_error)


async def run_cycle() -> KolCycleReport:
    """Capture the whole enabled watchlist once, with bounded concurrency. Each
    KOL is isolated; one failure or hang affects only its own result. Declines
    (does not queue) if a prior cycle is still running. NEVER raises — safe to
    call directly from the scheduler loop."""
    if _cycle_lock.locked():
        logger.info("kol scheduler: previous cycle still running; skipping this tick")
        return KolCycleReport(finished_at=utc_now_iso(), skipped_cycle=True)

    async with _cycle_lock:
        report = KolCycleReport()
        entries = kol_watchlist.list_kols(enabled_only=True)
        if not entries:
            report.finished_at = report.started_at
            return report

        concurrency = max(1, int(settings.kol_scheduler_concurrency))
        sem = asyncio.Semaphore(concurrency)

        async def _guarded(entry: KolEntry) -> KolCaptureResult:
            async with sem:
                try:
                    return await capture_one(entry)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # defensive: capture_one shouldn't raise
                    logger.exception("kol scheduler: unexpected error for %s:%s",
                                     entry.platform, entry.handle)
                    return KolCaptureResult(entry.platform, entry.handle, "failed",
                                            error=f"{type(exc).__name__}: {exc}")

        results = await asyncio.gather(*(_guarded(e) for e in entries))
        report.results = list(results)
        report.processed = len(results)
        report.captured = sum(1 for r in results if r.outcome == "captured")
        report.incomplete = sum(1 for r in results if r.outcome == "incomplete")
        report.failed = sum(1 for r in results if r.outcome == "failed")
        report.skipped = sum(1 for r in results if r.outcome == "skipped")
        report.finished_at = utc_now_iso()
        logger.info(
            "kol scheduler cycle: processed=%d captured=%d incomplete=%d failed=%d skipped=%d",
            report.processed, report.captured, report.incomplete, report.failed, report.skipped,
        )
        return report
