"""Tests for the snapshot diff engine + follow-change monitor (M23 Deliverable C).

Three layers, all offline:
  - `diff.diff_snapshots` — pure comparison logic (no I/O).
  - `kol_monitor.process_snapshot` — orchestration + persistence, including the
    error-recovery rules for incomplete/corrupted snapshots.
  - `kol_watchlist.capture_following` — the fetch+diff integration, driven by a
    fake provider (no browser, no network).

Scope assertions: Deliverable C stops at persisted follow-change events. Tests
confirm the absence of alerting/scoring/clustering/crypto surfaces.
"""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from app.models.kol import (
    FollowingSnapshot,
    SnapshotDiff,
    SocialAccount,
)
from app.services import kol_monitor, kol_store, kol_watchlist as w
from app.services.social import diff as diff_mod
from app.services.social.diff import diff_snapshots


# --- helpers -----------------------------------------------------------------


def acct(handle, *, pid=None, display_name=None, bio=None, verified=None, **extra):
    return SocialAccount(
        platform="x",
        handle=handle,
        platform_id=pid,
        display_name=display_name,
        bio=bio,
        verified=verified,
        **extra,
    )


def snap(accounts, *, handle="kol", complete=True, captured_at=None):
    kwargs = dict(platform="x", kol_handle=handle, accounts=accounts, complete=complete)
    if captured_at is not None:
        kwargs["captured_at"] = captured_at
    return FollowingSnapshot(**kwargs)


@pytest.fixture(autouse=True)
def _temp_db():
    tmp = Path(tempfile.mkdtemp()) / "kol.db"
    kol_store.reset_for_tests(str(tmp))
    yield
    kol_store.reset_for_tests()


# --- pure diff: baseline -----------------------------------------------------


def test_baseline_when_no_previous_emits_no_events():
    current = snap([acct("a"), acct("b")])
    d = diff_snapshots(None, current)
    assert d.is_baseline is True
    assert {a.handle for a in d.unchanged} == {"a", "b"}
    assert d.new_follows == []
    assert d.unfollows == []
    assert d.events() == []          # first observation is not a follow event
    assert d.has_changes is False


# --- pure diff: no changes ---------------------------------------------------


def test_no_changes_between_identical_snapshots():
    prev = snap([acct("a"), acct("b")])
    curr = snap([acct("a"), acct("b")])
    d = diff_snapshots(prev, curr)
    assert d.new_follows == []
    assert d.unfollows == []
    assert {a.handle for a in d.unchanged} == {"a", "b"}
    assert d.profile_changes == []
    assert d.has_changes is False
    assert d.events() == []


# --- pure diff: new follows --------------------------------------------------


def test_new_follows_detected():
    prev = snap([acct("a")])
    curr = snap([acct("a"), acct("b"), acct("c")])
    d = diff_snapshots(prev, curr)
    assert {a.handle for a in d.new_follows} == {"b", "c"}
    assert d.unfollows == []
    evs = d.events()
    assert {e.event_type for e in evs} == {"new_follow"}
    assert {e.account.handle for e in evs} == {"b", "c"}
    assert all(e.kol_handle == "kol" and e.platform == "x" for e in evs)


# --- pure diff: unfollows ----------------------------------------------------


def test_unfollows_detected():
    prev = snap([acct("a"), acct("b"), acct("c")])
    curr = snap([acct("a")])
    d = diff_snapshots(prev, curr)
    assert {a.handle for a in d.unfollows} == {"b", "c"}
    assert d.new_follows == []
    evs = d.events()
    assert {e.event_type for e in evs} == {"unfollow"}
    assert {e.account.handle for e in evs} == {"b", "c"}


def test_simultaneous_follow_and_unfollow():
    prev = snap([acct("a"), acct("b")])
    curr = snap([acct("a"), acct("c")])
    d = diff_snapshots(prev, curr)
    assert {a.handle for a in d.new_follows} == {"c"}
    assert {a.handle for a in d.unfollows} == {"b"}
    assert {a.handle for a in d.unchanged} == {"a"}
    types = sorted(e.event_type for e in d.events())
    assert types == ["new_follow", "unfollow"]


# --- pure diff: username (handle) changes ------------------------------------


def test_username_change_with_stable_id_is_not_unfollow():
    # Same immutable id, different handle -> a profile change, not unfollow+follow.
    prev = snap([acct("oldname", pid="111")])
    curr = snap([acct("newname", pid="111")])
    d = diff_snapshots(prev, curr)
    assert d.new_follows == []
    assert d.unfollows == []
    assert len(d.unchanged) == 1
    changes = {(c.field, c.old_value, c.new_value) for c in d.profile_changes}
    assert ("handle", "oldname", "newname") in changes


def test_username_change_without_stable_id_looks_like_churn():
    # No id: the engine can only key on handle, so a rename is unavoidably seen as
    # an unfollow of the old handle + a new follow of the new one. Documented limit.
    prev = snap([acct("oldname")])
    curr = snap([acct("newname")])
    d = diff_snapshots(prev, curr)
    assert {a.handle for a in d.unfollows} == {"oldname"}
    assert {a.handle for a in d.new_follows} == {"newname"}
    assert d.profile_changes == []


def test_display_name_bio_and_verification_changes():
    prev = snap([acct("a", pid="1", display_name="Al", bio="hi", verified=False)])
    curr = snap([acct("a", pid="1", display_name="Alice", bio="hello", verified=True)])
    d = diff_snapshots(prev, curr)
    fields = {c.field: (c.old_value, c.new_value) for c in d.profile_changes}
    assert fields["display_name"] == ("Al", "Alice")
    assert fields["bio"] == ("hi", "hello")
    assert fields["verified"] == ("false", "true")
    assert d.has_changes is True


def test_newly_appearing_field_is_not_a_change():
    # None -> value is enrichment arriving, not a profile change.
    prev = snap([acct("a", pid="1", bio=None)])
    curr = snap([acct("a", pid="1", bio="now i have a bio")])
    d = diff_snapshots(prev, curr)
    assert d.profile_changes == []


# --- pure diff: duplicate entries --------------------------------------------


def test_duplicate_entries_counted_once():
    # A snapshot that lists the same account twice must not double-count it, and
    # its presence in both sides is "unchanged", not follow+unfollow.
    prev = snap([acct("a"), acct("a"), acct("b")])
    curr = snap([acct("a"), acct("b"), acct("b")])
    d = diff_snapshots(prev, curr)
    assert d.new_follows == []
    assert d.unfollows == []
    assert {a.handle for a in d.unchanged} == {"a", "b"}


def test_duplicate_new_follow_emits_single_event():
    prev = snap([acct("a")])
    curr = snap([acct("a"), acct("b"), acct("b")])
    d = diff_snapshots(prev, curr)
    assert len(d.new_follows) == 1
    assert len(d.events()) == 1


# --- monitor: persistence ----------------------------------------------------


def test_monitor_baseline_persists_snapshot_no_events():
    d = kol_monitor.process_snapshot(snap([acct("a"), acct("b")], handle="mk"))
    assert d.is_baseline is True
    assert kol_store.latest_complete_snapshot("x", "mk") is not None
    assert kol_store.list_follow_events("x", "mk") == []
    # Baseline still records the accounts as followed metadata.
    active = kol_store.list_followed_accounts("x", "mk")
    assert {a["account"].handle for a in active} == {"a", "b"}


def test_monitor_persists_follow_events_and_metadata():
    kol_monitor.process_snapshot(snap([acct("a")], handle="mk", captured_at="2024-01-01T00:00:00+00:00"))
    kol_monitor.process_snapshot(snap(
        [acct("a"), acct("b", display_name="Bee")],
        handle="mk", captured_at="2024-01-02T00:00:00+00:00",
    ))
    events = kol_store.list_follow_events("x", "mk")
    assert len(events) == 1
    assert events[0].event_type == "new_follow"
    assert events[0].account.handle == "b"
    assert events[0].account.display_name == "Bee"   # full metadata persisted
    # Metadata row exists and is active with first/last-seen.
    row = kol_store.get_followed_account("x", "mk", "b")
    assert row["active"] is True
    assert row["first_seen"] == "2024-01-02T00:00:00+00:00"


def test_monitor_unfollow_flips_active_and_logs_event():
    kol_monitor.process_snapshot(snap([acct("a"), acct("b")], handle="mk",
                                       captured_at="2024-01-01T00:00:00+00:00"))
    kol_monitor.process_snapshot(snap([acct("a")], handle="mk",
                                       captured_at="2024-01-02T00:00:00+00:00"))
    unfollows = kol_store.list_follow_events("x", "mk", event_type="unfollow")
    assert len(unfollows) == 1 and unfollows[0].account.handle == "b"
    # b retained but inactive; last_seen preserved from when it was last observed.
    row = kol_store.get_followed_account("x", "mk", "b")
    assert row["active"] is False
    assert row["last_seen"] == "2024-01-01T00:00:00+00:00"
    # active-only listing excludes it; full listing includes it.
    assert {a["account"].handle for a in kol_store.list_followed_accounts("x", "mk")} == {"a"}
    assert {a["account"].handle
            for a in kol_store.list_followed_accounts("x", "mk", active_only=False)} == {"a", "b"}


def test_monitor_persists_profile_changes():
    kol_monitor.process_snapshot(snap([acct("a", pid="1", display_name="Al")], handle="mk"))
    kol_monitor.process_snapshot(snap([acct("a", pid="1", display_name="Alice")], handle="mk"))
    changes = kol_store.list_profile_changes("x", "mk")
    assert len(changes) == 1
    assert changes[0].field == "display_name"
    assert (changes[0].old_value, changes[0].new_value) == ("Al", "Alice")


# --- monitor: error recovery -------------------------------------------------


def test_monitor_skips_incomplete_snapshot():
    # First a good baseline.
    kol_monitor.process_snapshot(snap([acct("a"), acct("b")], handle="mk",
                                       captured_at="2024-01-01T00:00:00+00:00"))
    # An incomplete capture (e.g. scroll interrupted) must not persist or diff.
    result = kol_monitor.process_snapshot(snap([acct("a")], handle="mk", complete=False,
                                               captured_at="2024-01-02T00:00:00+00:00"))
    assert result is None
    # Previous valid snapshot preserved; no spurious unfollow of b.
    baseline = kol_store.latest_complete_snapshot("x", "mk")
    assert baseline.keys() == {"a", "b"}
    assert kol_store.list_follow_events("x", "mk", event_type="unfollow") == []
    assert kol_store.get_followed_account("x", "mk", "b")["active"] is True


def test_monitor_baseline_uses_last_complete_not_incomplete():
    # Baseline snapshot, then an incomplete one is skipped, then a real change.
    kol_monitor.process_snapshot(snap([acct("a")], handle="mk",
                                       captured_at="2024-01-01T00:00:00+00:00"))
    kol_monitor.process_snapshot(snap([], handle="mk", complete=False,
                                       captured_at="2024-01-02T00:00:00+00:00"))
    d = kol_monitor.process_snapshot(snap([acct("a"), acct("c")], handle="mk",
                                          captured_at="2024-01-03T00:00:00+00:00"))
    # Diff is against the last COMPLETE snapshot (just [a]), so only c is new.
    assert {a.handle for a in d.new_follows} == {"c"}
    assert d.unfollows == []


def test_monitor_recovers_from_corrupted_snapshot():
    # A good baseline, then a corrupted snapshot row written directly to the DB.
    kol_monitor.process_snapshot(snap([acct("a"), acct("b")], handle="mk",
                                       captured_at="2024-01-01T00:00:00+00:00"))
    conn = kol_store._connect()
    conn.execute(
        "INSERT INTO following_snapshots (platform, handle, captured_at, complete, accounts) "
        "VALUES ('x', 'mk', '2024-01-02T00:00:00+00:00', 1, ?)",
        ("{ this is not valid json",),
    )
    conn.commit()
    # The corrupted (newer) row is skipped; baseline falls back to the intact one.
    baseline = kol_store.latest_complete_snapshot("x", "mk")
    assert baseline.captured_at == "2024-01-01T00:00:00+00:00"
    assert baseline.keys() == {"a", "b"}
    # A subsequent real capture diffs cleanly against the recovered baseline.
    d = kol_monitor.process_snapshot(snap([acct("a"), acct("b"), acct("c")], handle="mk",
                                          captured_at="2024-01-03T00:00:00+00:00"))
    assert {a.handle for a in d.new_follows} == {"c"}


# --- capture integration -----------------------------------------------------


class _FakeProvider:
    """Minimal SocialGraphProvider returning a scripted snapshot.

    Implements the full provider contract the watchlist facade relies on
    (`normalize_handle`/`account_url` alongside `capabilities`/`fetch_following`)
    so the fetch+diff integration exercises the same seam a real provider does.
    """

    platform = "x"

    def __init__(self, snapshot):
        self._snapshot = snapshot

    def capabilities(self):
        from app.models.kol import ProviderCapabilities
        return ProviderCapabilities(platform="x", can_fetch_following=True)

    def normalize_handle(self, handle):
        return handle.strip().lstrip("@").lower()

    def account_url(self, handle):
        return f"https://x.com/{self.normalize_handle(handle)}"

    async def fetch_following(self, handle):
        return self._snapshot


def _register(snapshot):
    from app.services.social import registry
    registry.register_provider(_FakeProvider(snapshot), replace=True)


def _reset_registry():
    from app.services.social import registry
    registry.reset_for_tests()


def test_capture_following_detects_new_follow_end_to_end():
    try:
        w.add_kol("target")
        _register(snap([acct("a")], handle="target", captured_at="2024-01-01T00:00:00+00:00"))
        asyncio.run(w.capture_following("target"))
        _register(snap([acct("a"), acct("b")], handle="target",
                       captured_at="2024-01-02T00:00:00+00:00"))
        asyncio.run(w.capture_following("target"))
        events = kol_store.list_follow_events("x", "target", event_type="new_follow")
        assert len(events) == 1 and events[0].account.handle == "b"
        assert w.get_kol("target").status == "active"
    finally:
        _reset_registry()


def test_capture_following_incomplete_preserves_state_and_marks_error():
    try:
        w.add_kol("target")
        _register(snap([acct("a"), acct("b")], handle="target",
                       captured_at="2024-01-01T00:00:00+00:00"))
        asyncio.run(w.capture_following("target"))
        # Now an incomplete capture arrives.
        _register(snap([acct("a")], handle="target", complete=False,
                       captured_at="2024-01-02T00:00:00+00:00"))
        asyncio.run(w.capture_following("target"))
        # Prior snapshot preserved, no unfollow event, KOL flagged error for retry.
        assert kol_store.latest_complete_snapshot("x", "target").keys() == {"a", "b"}
        assert kol_store.list_follow_events("x", "target", event_type="unfollow") == []
        assert w.get_kol("target").status == "error"
        meta = kol_store.get_sync_meta("x", "target")
        assert meta["last_error"] is not None
        assert meta["last_success"] is not None   # earlier success retained
    finally:
        _reset_registry()


# --- monitor: snapshot retention ---------------------------------------------


def _count_snapshots(handle):
    conn = kol_store._connect()
    return conn.execute(
        "SELECT COUNT(*) AS n FROM following_snapshots WHERE platform='x' AND handle=?",
        (handle,),
    ).fetchone()["n"]


def test_snapshot_retention_prunes_oldest(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "kol_snapshot_retain", 3)
    for day in range(1, 7):  # six captures, retain 3
        kol_monitor.process_snapshot(snap(
            [acct("a"), acct(f"n{day}")], handle="mk",
            captured_at=f"2024-01-0{day}T00:00:00+00:00",
        ))
    assert _count_snapshots("mk") == 3
    # The most recent survives and still diffs correctly.
    assert kol_store.latest_complete_snapshot("x", "mk").captured_at == \
        "2024-01-06T00:00:00+00:00"


def test_snapshot_retention_disabled_keeps_all(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "kol_snapshot_retain", 0)
    for day in range(1, 6):
        kol_monitor.process_snapshot(snap(
            [acct("a")], handle="mk", captured_at=f"2024-01-0{day}T00:00:00+00:00",
        ))
    assert _count_snapshots("mk") == 5


def test_snapshot_retention_preserves_complete_baseline(monkeypatch):
    # A complete baseline followed by many incomplete captures: pruning must keep
    # the complete one so the next real capture doesn't see a mass unfollow.
    from app.core.config import settings
    monkeypatch.setattr(settings, "kol_snapshot_retain", 2)
    kol_store.save_snapshot(snap([acct("a"), acct("b")], handle="mk",
                                  captured_at="2024-01-01T00:00:00+00:00"))
    for day in range(2, 7):
        kol_store.save_snapshot(snap([acct("a")], handle="mk", complete=False,
                                     captured_at=f"2024-01-0{day}T00:00:00+00:00"))
    baseline = kol_store.latest_complete_snapshot("x", "mk")
    assert baseline is not None
    assert baseline.captured_at == "2024-01-01T00:00:00+00:00"
    assert baseline.keys() == {"a", "b"}


# --- scope guard -------------------------------------------------------------


def test_no_alerting_scoring_or_crypto_surfaces():
    """Deliverable C stops at follow-change events. Assert later-deliverable
    surfaces are absent so scope creep is caught."""
    for banned in ("alert", "score", "cluster", "crypto", "contract"):
        assert not hasattr(kol_monitor, banned)
        assert not hasattr(diff_mod, banned)
    # The diff result exposes only change data, no scoring/alert fields.
    fields = set(SnapshotDiff.model_fields)
    assert fields == {
        "platform", "kol_handle", "new_follows", "unfollows",
        "unchanged", "profile_changes", "is_baseline",
    }
