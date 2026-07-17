from __future__ import annotations

"""Follow-change monitor: turn a fresh snapshot into persisted change events (M23 Deliverable C).

This is the orchestration layer that sits between the raw diff engine
(`services/social/diff.py`, pure) and the store (`kol_store`, raw persistence).
Given a freshly captured `FollowingSnapshot`, it:

  1. Refuses to process an incomplete capture (see "Error recovery" below).
  2. Loads the previous *complete* snapshot as the diff baseline.
  3. Diffs old vs new to find new follows, unfollows, and profile changes.
  4. Persists: the snapshot itself, the follow/unfollow events, the profile
     changes, and the current per-account metadata (with first/last-seen).

It is platform-neutral — it only touches neutral models and the store, never a
concrete provider — so every `SocialGraphProvider` gets change detection for free.

Scope guard (Deliverable C): this stops at producing accurate, persisted follow
CHANGE events. It does NOT alert, score, cluster, or infer crypto relevance. The
events it writes are engine-internal facts for later deliverables to consume.

Error recovery: an incomplete snapshot (a partial/interrupted scrape, `complete=
False`) is never diffed and never overwrites the last good state. `process_snapshot`
returns None for it; the previous valid snapshot stays authoritative and the next
scheduled run retries. Corrupted historical snapshots are skipped by the store's
readers, so the baseline falls back to the last intact complete snapshot.
"""

import logging

from app.models.kol import FollowingSnapshot, SnapshotDiff
from app.services import kol_store
from app.services.social.diff import diff_snapshots

logger = logging.getLogger(__name__)


def process_snapshot(snapshot: FollowingSnapshot) -> SnapshotDiff | None:
    """Diff a freshly captured snapshot against the last complete one, persist the
    results, and return the diff. Returns None (persisting nothing) when the
    snapshot is incomplete, so an interrupted capture can't corrupt history.

    Persistence is ordered so a crash mid-way still leaves a consistent story: the
    snapshot is written first (it's the source of truth), then the derived events
    and metadata. Re-running on the same inputs is idempotent for metadata (upsert)
    though events are append-only by design (an audit log).
    """
    if not snapshot.complete:
        # An incomplete pull has gaps that would look like unfollows. Do not diff,
        # do not persist, do not overwrite the previous valid snapshot — just skip
        # and let the next scheduled run retry. (The caller still records the sync
        # attempt / error separately.)
        logger.info(
            "Skipping diff for incomplete snapshot %s:%s (retry next run)",
            snapshot.platform, snapshot.kol_handle,
        )
        return None

    previous = kol_store.latest_complete_snapshot(snapshot.platform, snapshot.kol_handle)
    diff = diff_snapshots(previous, snapshot)

    # 1. Persist the raw snapshot (the authoritative capture).
    kol_store.save_snapshot(snapshot)

    # 2. Persist derived follow/unfollow events (none on a baseline).
    events = diff.events()
    kol_store.save_follow_events(events)

    # 3. Persist detected profile changes.
    kol_store.save_profile_changes(diff.profile_changes)

    # 4. Refresh per-account metadata + first/last-seen. Present accounts (new or
    #    unchanged) are active and stamped at this capture time; unfollowed ones
    #    are flipped inactive but retained for history.
    seen_at = snapshot.captured_at
    for acct in diff.new_follows:
        kol_store.upsert_followed_account(
            snapshot.platform, snapshot.kol_handle, acct, active=True, seen_at=seen_at
        )
    for acct in diff.unchanged:
        kol_store.upsert_followed_account(
            snapshot.platform, snapshot.kol_handle, acct, active=True, seen_at=seen_at
        )
    for acct in diff.unfollows:
        kol_store.deactivate_followed_account(
            snapshot.platform, snapshot.kol_handle, acct.key()
        )

    logger.info(
        "Processed snapshot %s:%s — %s new, %s unfollowed, %s profile changes%s",
        snapshot.platform, snapshot.kol_handle,
        len(diff.new_follows), len(diff.unfollows), len(diff.profile_changes),
        " (baseline)" if diff.is_baseline else "",
    )
    return diff
