from __future__ import annotations

"""Public service facade for the KOL watchlist (M23, Deliverable A).

This is the ONLY module callers (API routes, the future scheduler, tests) should
touch to manage KOLs. It owns validation and business rules and delegates raw
persistence to `kol_store` and platform specifics to `SocialGraphProvider`s. The
store and provider internals are never exposed.

Public interface:
  - add_kol / remove_kol / update_kol
  - list_kols / get_kol
  - set_enabled / set_tier
  - get_watch_status
  - sync_from_config     (config-driven, no-code watchlist management)
  - capture_following    (Deliverable B: fetch + persist one following snapshot)

Handles are normalized through the platform's provider when one exists (so X
identity rules apply), and validated by the domain model regardless. Every public
call raises `ValueError` on bad input at this boundary, so malformed data never
reaches the store.
"""

import logging

from app.core.config import settings
from app.models.kol import FollowingSnapshot, KolEntry, KolSeed, WatchStatus
from app.services import kol_crypto_pipeline, kol_monitor, kol_store
from app.services.social import get_provider, is_supported
from app.services.social.base import ProviderError

logger = logging.getLogger(__name__)


def _resolve_platform(platform: str | None) -> str:
    return (platform or settings.kol_default_platform or "x").strip().lower()


def _normalize_handle(platform: str, handle: str) -> str:
    """Normalize via the platform provider when available, else a safe default.

    A platform can be declared in the domain model before its provider is wired
    (that is the whole point of the multi-provider design), so we must not require
    a provider just to store a KOL. When present, the provider's rules win.
    """
    provider = get_provider(platform)
    if provider is not None:
        return provider.normalize_handle(handle)
    normalized = (handle or "").strip().lstrip("@").strip()
    if not normalized:
        raise ValueError("handle must be a non-empty username")
    return normalized


# --- Create / update ---------------------------------------------------------


def add_kol(
    handle: str,
    *,
    platform: str | None = None,
    display_name: str | None = None,
    tier: int = 2,
    enabled: bool = True,
    notes: str | None = None,
) -> KolEntry:
    """Add a KOL to the watchlist (or update an existing one with the same
    platform+handle). Returns the stored entry."""
    platform = _resolve_platform(platform)
    handle = _normalize_handle(platform, handle)
    # The model enforces platform/tier/status validity and normalizes again.
    entry = KolEntry(
        platform=platform,
        handle=handle,
        display_name=display_name,
        tier=tier,
        enabled=enabled,
        notes=notes,
        status="pending" if enabled else "paused",
    )
    kol_store.upsert_kol(entry)
    logger.info("KOL watchlist: added/updated %s@%s (tier %s)", handle, platform, tier)
    return entry


def update_kol(
    handle: str,
    *,
    platform: str | None = None,
    display_name: str | None = None,
    tier: int | None = None,
    enabled: bool | None = None,
    notes: str | None = None,
) -> KolEntry:
    """Patch mutable fields of an existing KOL. Only provided fields change.
    Raises KeyError if the KOL is not on the watchlist."""
    platform = _resolve_platform(platform)
    handle = _normalize_handle(platform, handle)
    current = kol_store.get_kol(platform, handle)
    if current is None:
        raise KeyError(f"KOL {handle}@{platform} is not on the watchlist")

    if display_name is not None:
        current.display_name = display_name
    if notes is not None:
        current.notes = notes
    if tier is not None:
        # Re-validate through the model so an out-of-range tier is rejected here.
        current.tier = KolEntry(platform=platform, handle=handle, tier=tier).tier
    if enabled is not None and enabled != current.enabled:
        current.enabled = enabled
        # Flipping the toggle moves lifecycle status between paused and pending,
        # unless the KOL is mid-error (leave error visible until next sync).
        if not enabled:
            current.status = "paused"
        elif current.status == "paused":
            current.status = "pending"

    kol_store.upsert_kol(current)
    logger.info("KOL watchlist: updated %s@%s", handle, platform)
    return current


def set_enabled(handle: str, enabled: bool, *, platform: str | None = None) -> KolEntry:
    """Enable or disable a KOL without removing it."""
    return update_kol(handle, platform=platform, enabled=enabled)


def set_tier(handle: str, tier: int, *, platform: str | None = None) -> KolEntry:
    """Change a KOL's tier."""
    return update_kol(handle, platform=platform, tier=tier)


# --- Delete / read -----------------------------------------------------------


def remove_kol(handle: str, *, platform: str | None = None) -> bool:
    """Remove a KOL (and its snapshots/sync rows). True if something was removed."""
    platform = _resolve_platform(platform)
    handle = _normalize_handle(platform, handle)
    removed = kol_store.delete_kol(platform, handle)
    if removed:
        logger.info("KOL watchlist: removed %s@%s", handle, platform)
    return removed


def get_kol(handle: str, *, platform: str | None = None) -> KolEntry | None:
    platform = _resolve_platform(platform)
    handle = _normalize_handle(platform, handle)
    return kol_store.get_kol(platform, handle)


def list_kols(
    *, platform: str | None = None, enabled_only: bool = False
) -> list[KolEntry]:
    plat = platform.strip().lower() if platform else None
    return kol_store.list_kols(plat, enabled_only=enabled_only)


def get_watch_status(handle: str, *, platform: str | None = None) -> WatchStatus | None:
    """Read-only operational view for a KOL, or None if not watched. Combines the
    watchlist row, sync bookkeeping, and provider availability without exposing
    any of those internals."""
    platform = _resolve_platform(platform)
    handle = _normalize_handle(platform, handle)
    entry = kol_store.get_kol(platform, handle)
    if entry is None:
        return None
    snapshot = kol_store.latest_snapshot(platform, handle)
    return WatchStatus(
        platform=entry.platform,
        handle=entry.handle,
        display_name=entry.display_name,
        tier=entry.tier,
        enabled=entry.enabled,
        status=entry.status,
        date_added=entry.date_added,
        last_checked=entry.last_checked,
        has_snapshot=snapshot is not None,
        last_snapshot_at=snapshot.captured_at if snapshot else None,
        provider_available=is_supported(platform),
    )


# --- Config-driven management ------------------------------------------------


def sync_from_config(seeds: list[dict] | None = None) -> dict[str, int]:
    """Reconcile the config seed list into the store — the no-code way to manage
    the watchlist.

    Behavior (see `kol_config_overwrites`): seeds always ADD missing KOLs. When
    `kol_config_overwrites` is True, a seed also overwrites display_name/tier/
    enabled/notes on an existing row (config is source of truth); when False, it
    leaves operator edits untouched. Removing a KOL from config never auto-deletes
    it — deletion is an explicit operator action, so a config typo can't wipe a
    tracked KOL and its history.

    Returns counts {added, updated, skipped} for observability.
    """
    raw_seeds = seeds if seeds is not None else settings.kol_watchlist_seed
    added = updated = skipped = 0
    for raw in raw_seeds or []:
        try:
            seed = KolSeed(**raw)
            platform = _resolve_platform(seed.platform)
            handle = _normalize_handle(platform, seed.handle)
        except (ValueError, TypeError) as exc:
            logger.warning("KOL config seed skipped (%r): %s", raw, exc)
            skipped += 1
            continue

        existing = kol_store.get_kol(platform, handle)
        if existing is None:
            add_kol(
                handle,
                platform=platform,
                display_name=seed.display_name,
                tier=seed.tier,
                enabled=seed.enabled,
                notes=seed.notes,
            )
            added += 1
        elif settings.kol_config_overwrites:
            update_kol(
                handle,
                platform=platform,
                display_name=seed.display_name,
                tier=seed.tier,
                enabled=seed.enabled,
                notes=seed.notes,
            )
            updated += 1
        else:
            skipped += 1

    if added or updated:
        logger.info(
            "KOL watchlist config sync: %s added, %s updated, %s skipped",
            added, updated, skipped,
        )
    return {"added": added, "updated": updated, "skipped": skipped}


# --- Following capture (Deliverable B) ---------------------------------------


async def capture_following(handle: str, platform: str | None = None) -> FollowingSnapshot:
    """Fetch a KOL's current following list, then persist + diff it.

    Fetching is Deliverable B; the persist step is routed through
    `kol_monitor.process_snapshot` (Deliverable C), which stores the snapshot and
    detects/persists follow, unfollow, and profile-change events. This function
    still does NOT alert, score, cluster, or infer crypto relevance.

    Outcomes:
      - Complete capture  -> monitor persists snapshot + change events; sync marked
        successful; status `active`.
      - Incomplete capture -> monitor persists NOTHING (previous valid snapshot is
        preserved); sync marked failed so the next scheduled run retries; status
        `error`. Not raised — a partial pull isn't an exception.
      - Typed provider failure -> error recorded, status `error`, prior snapshots
        untouched, re-raised for the caller/scheduler.
    `KeyError` if the KOL isn't on the watchlist.
    """
    platform = _resolve_platform(platform)
    handle = _normalize_handle(platform, handle)

    entry = kol_store.get_kol(platform, handle)
    if entry is None:
        raise KeyError(f"{platform}:{handle} is not on the watchlist")

    provider = get_provider(platform)
    if provider is None:
        raise ValueError(f"no provider available for platform {platform!r}")
    if not provider.capabilities().can_fetch_following:
        raise ValueError(f"provider for {platform!r} cannot fetch following lists")

    try:
        snapshot = await provider.fetch_following(handle)
    except ProviderError as exc:
        # Typed, expected failure (auth/private/suspended/rate-limit/network):
        # record it and surface an error status without corrupting history.
        kol_store.record_sync(platform, handle, success=False, error=str(exc))
        kol_store.set_last_checked(platform, handle, "error")
        logger.info("capture_following failed for %s:%s — %s", platform, handle, exc)
        raise

    diff = kol_monitor.process_snapshot(snapshot)

    if diff is None:
        # Incomplete/interrupted capture: nothing persisted, previous snapshot kept.
        # Treat as a retryable non-success so the scheduler tries again.
        kol_store.record_sync(
            platform, handle, success=False, error="incomplete snapshot; not persisted"
        )
        kol_store.set_last_checked(platform, handle, "error")
        logger.info(
            "capture_following got incomplete snapshot for %s:%s — preserved prior state",
            platform, handle,
        )
        return snapshot

    kol_store.record_sync(platform, handle, success=True)
    kol_store.set_last_checked(platform, handle, "active")
    logger.info(
        "captured %s following accounts for %s:%s (%s new, %s unfollowed)",
        len(snapshot.accounts), platform, handle,
        len(diff.new_follows), len(diff.unfollows),
    )

    # Deliverable D: automatically classify each NEW follow and, for confident crypto
    # projects, run the existing rug analyzer on any contracts on their profile. Only
    # `diff.new_follows` (never a baseline's whole list — that yields no new follows),
    # so a first capture never triggers a burst of analysis. Gated + best-effort: the
    # pipeline no-ops when disabled and swallows per-account analysis failures, so it
    # can never turn a good capture into a failed sync.
    if diff.new_follows:
        try:
            await kol_crypto_pipeline.process_new_follows(platform, handle, diff.new_follows)
        except Exception:  # noqa: BLE001 — intelligence is additive; capture already succeeded
            logger.exception(
                "crypto intelligence pipeline errored for %s:%s (capture unaffected)",
                platform, handle,
            )

    return snapshot
