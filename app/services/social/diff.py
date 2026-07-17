from __future__ import annotations

"""Platform-agnostic snapshot diff engine (M23 Deliverable C).

Given a previous and a current `FollowingSnapshot`, computes who was newly
followed, unfollowed, and unchanged, plus per-account profile changes (handle
rename, display-name/bio edit, verification change).

This module is deliberately provider-neutral: it operates only on the neutral
`FollowingSnapshot`/`SocialAccount` models, so every `SocialGraphProvider`
(X today; Farcaster, Telegram, ... tomorrow) reuses it unchanged. It is pure —
no I/O, no persistence, no time source beyond the models' own timestamps — which
is what makes it exhaustively unit-testable and safe to call from anywhere.

Scope guard (Deliverable C): this DETECTS changes and returns them. It does not
persist, alert, score, cluster, or interpret crypto relevance. Persistence is the
caller's job (`kol_monitor`); everything downstream is a later deliverable.

Performance: diffing is O(n_prev + n_curr). Each snapshot is indexed once into a
dict keyed by each account's stable identity (`SocialAccount.key()` — the
immutable platform id where available, else the lowercased handle), then the two
key sets are compared with set operations. No account is scanned more than once
and there is no nested iteration, so a KOL following tens of thousands of accounts
diffs in a single linear pass over each side.
"""

from app.models.kol import (
    FollowingSnapshot,
    ProfileChange,
    SnapshotDiff,
    SocialAccount,
)

# Profile attributes we track for change. Each maps a ProfileChange.field name to
# the SocialAccount attribute read for old/new values. Verification is coerced to
# a string ("true"/"false"/None) so the change record stays uniformly stringly.
_TRACKED_FIELDS: tuple[str, ...] = ("handle", "display_name", "bio", "verified")


def _as_str(value: object) -> str | None:
    """Normalize an attribute to a comparable string, preserving None as unknown."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _detect_profile_changes(
    snapshot: FollowingSnapshot,
    prev: SocialAccount,
    curr: SocialAccount,
) -> list[ProfileChange]:
    """Compare an account's tracked attributes across two snapshots.

    A field only counts as changed when BOTH sides are known and differ. A value
    appearing for the first time (None -> something) is enrichment arriving, not a
    profile *change*, so we don't record it — that avoids a flood of spurious
    'changes' the run after a provider starts capturing a new field.
    """
    changes: list[ProfileChange] = []
    for field in _TRACKED_FIELDS:
        old = _as_str(getattr(prev, field, None))
        new = _as_str(getattr(curr, field, None))
        if old is None or new is None:
            continue
        if old != new:
            changes.append(ProfileChange(
                platform=snapshot.platform,
                kol_handle=snapshot.kol_handle,
                account_key=curr.key(),
                field=field,
                old_value=old,
                new_value=new,
            ))
    return changes


def diff_snapshots(
    previous: FollowingSnapshot | None,
    current: FollowingSnapshot,
) -> SnapshotDiff:
    """Compare two snapshots and return the structured differences.

    `previous is None` means this is the first snapshot on record: the result is a
    *baseline* — every current account is 'unchanged' (established, not newly
    followed) and no follow events are implied. This is what stops the very first
    capture from reporting a KOL's entire existing following list as new follows.

    Diffing keys on `SocialAccount.key()`. Because that prefers the immutable
    platform id, an account that merely renamed its handle stays the SAME key
    across snapshots — surfacing as a `handle` ProfileChange, not an unfollow plus
    a new-follow. Providers without stable ids fall back to the handle, where a
    rename is (unavoidably) seen as unfollow+follow.
    """
    curr_index = current.by_key()

    if previous is None:
        # Baseline: record the current set as the starting point, emit no events.
        return SnapshotDiff(
            platform=current.platform,
            kol_handle=current.kol_handle,
            unchanged=list(curr_index.values()),
            is_baseline=True,
        )

    prev_index = previous.by_key()

    prev_keys = prev_index.keys()
    curr_keys = curr_index.keys()

    # Set algebra over the key sets — O(n) with no nested scans.
    new_keys = curr_keys - prev_keys
    gone_keys = prev_keys - curr_keys
    common_keys = curr_keys & prev_keys

    new_follows = [curr_index[k] for k in new_keys]
    unfollows = [prev_index[k] for k in gone_keys]

    unchanged: list[SocialAccount] = []
    profile_changes: list[ProfileChange] = []
    for k in common_keys:
        prev_acct = prev_index[k]
        curr_acct = curr_index[k]
        unchanged.append(curr_acct)
        profile_changes.extend(_detect_profile_changes(current, prev_acct, curr_acct))

    return SnapshotDiff(
        platform=current.platform,
        kol_handle=current.kol_handle,
        new_follows=new_follows,
        unfollows=unfollows,
        unchanged=unchanged,
        profile_changes=profile_changes,
        is_baseline=False,
    )
