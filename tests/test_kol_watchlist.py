"""Tests for the KOL Intelligence Engine foundation (M23, Deliverable A).

Covers, per the deliverable spec: watchlist CRUD, configuration loading/sync,
persistence, the SocialGraphProvider abstraction + registry, validation, and error
handling. Everything is offline — no scraping, no diffing, no alerts (those are
later deliverables and are asserted here to be *absent*, i.e. explicitly deferred).
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from app.core.config import settings
from app.models.kol import (
    FollowingSnapshot,
    KolEntry,
    KolSeed,
    SocialAccount,
    utc_now_iso,
)
from app.services import kol_store, kol_watchlist as w
from app.services.social import (
    ProviderError,
    available_platforms,
    get_provider,
    is_supported,
    registry,
)
from app.services.social.base import SocialGraphProvider
from app.services.social.x_provider import XProvider


@pytest.fixture(autouse=True)
def _temp_db():
    """Isolate every test on its own temp DB, mirroring the wallet-store tests."""
    tmp = Path(tempfile.mkdtemp()) / "kol.db"
    kol_store.reset_for_tests(str(tmp))
    yield
    kol_store.reset_for_tests()


# --- Watchlist CRUD ----------------------------------------------------------


def test_add_and_get_kol():
    entry = w.add_kol("@Cobie", display_name="Cobie", tier=1, notes="og")
    assert entry.handle == "cobie"  # normalized: @ stripped, lowercased
    assert entry.platform == "x"
    assert entry.tier == 1
    assert entry.status == "pending"

    fetched = w.get_kol("cobie")
    assert fetched is not None
    assert fetched.display_name == "Cobie"
    assert fetched.notes == "og"
    assert fetched.date_added  # stamped on insert


def test_list_kols_and_enabled_filter():
    w.add_kol("alice", tier=1)
    w.add_kol("bob", tier=2, enabled=False)
    all_handles = sorted(k.handle for k in w.list_kols())
    assert all_handles == ["alice", "bob"]
    enabled = [k.handle for k in w.list_kols(enabled_only=True)]
    assert enabled == ["alice"]


def test_list_sorted_by_tier_then_handle():
    w.add_kol("zed", tier=1)
    w.add_kol("amy", tier=1)
    w.add_kol("bo", tier=3)
    order = [(k.tier, k.handle) for k in w.list_kols()]
    assert order == [(1, "amy"), (1, "zed"), (3, "bo")]


def test_update_kol_patches_only_given_fields():
    w.add_kol("carol", display_name="Carol", tier=2, notes="keep")
    updated = w.update_kol("carol", tier=1)
    assert updated.tier == 1
    assert updated.display_name == "Carol"  # untouched
    assert updated.notes == "keep"  # untouched


def test_set_enabled_toggles_status():
    w.add_kol("dan")
    paused = w.set_enabled("dan", False)
    assert paused.enabled is False
    assert paused.status == "paused"
    resumed = w.set_enabled("dan", True)
    assert resumed.enabled is True
    assert resumed.status == "pending"


def test_set_tier():
    w.add_kol("erin", tier=3)
    assert w.set_tier("erin", 1).tier == 1


def test_remove_kol():
    w.add_kol("frank")
    assert w.remove_kol("frank") is True
    assert w.get_kol("frank") is None
    # Removing a missing KOL is a no-op, not an error.
    assert w.remove_kol("frank") is False


def test_add_is_idempotent_on_identity():
    """Same platform+handle (case-insensitive) is one row, not two."""
    w.add_kol("DUPE")
    w.add_kol("dupe", display_name="second")
    matches = [k for k in w.list_kols() if k.handle == "dupe"]
    assert len(matches) == 1
    assert matches[0].display_name == "second"


def test_date_added_preserved_across_updates():
    first = w.add_kol("gina")
    original = first.date_added
    w.update_kol("gina", tier=1)
    assert w.get_kol("gina").date_added == original


# --- Validation --------------------------------------------------------------


def test_invalid_handle_rejected():
    with pytest.raises(ValueError):
        w.add_kol("bad handle!!")  # spaces + punctuation invalid on X
    with pytest.raises(ValueError):
        w.add_kol("waytoolonghandlename")  # >15 chars


def test_empty_handle_rejected():
    with pytest.raises(ValueError):
        w.add_kol("   ")


def test_invalid_tier_rejected():
    with pytest.raises(ValueError):
        w.add_kol("hank", tier=5)


def test_unknown_platform_rejected():
    with pytest.raises(ValueError):
        w.add_kol("ivan", platform="myspace")


def test_declared_but_unimplemented_platform_accepted():
    """A platform the domain model knows but has no provider yet still stores —
    that's the whole point of the multi-provider design."""
    entry = w.add_kol("someone", platform="farcaster")
    assert entry.platform == "farcaster"
    st = w.get_watch_status("someone", platform="farcaster")
    assert st.provider_available is False  # no provider wired yet


# --- Error handling ----------------------------------------------------------


def test_update_missing_kol_raises_keyerror():
    with pytest.raises(KeyError):
        w.update_kol("ghost", tier=1)


def test_get_watch_status_missing_returns_none():
    assert w.get_watch_status("nobody") is None


# --- Configuration loading / sync -------------------------------------------


def test_sync_from_config_adds(monkeypatch):
    seeds = [
        {"handle": "a", "tier": 1},
        {"handle": "@b", "enabled": False, "display_name": "Bee"},
    ]
    res = w.sync_from_config(seeds)
    assert res == {"added": 2, "updated": 0, "skipped": 0}
    assert sorted(k.handle for k in w.list_kols()) == ["a", "b"]
    assert w.get_kol("b").enabled is False


def test_sync_skips_invalid_seed():
    res = w.sync_from_config([{"handle": "ok"}, {"handle": "bad handle!!"}])
    assert res["added"] == 1
    assert res["skipped"] == 1


def test_sync_overwrite_toggle(monkeypatch):
    w.add_kol("clash", tier=3, display_name="operator-set")
    # overwrite ON: config wins
    monkeypatch.setattr(settings, "kol_config_overwrites", True)
    w.sync_from_config([{"handle": "clash", "tier": 1}])
    assert w.get_kol("clash").tier == 1
    # overwrite OFF: operator edits preserved, config only adds
    w.update_kol("clash", tier=2)
    monkeypatch.setattr(settings, "kol_config_overwrites", False)
    res = w.sync_from_config([{"handle": "clash", "tier": 1}])
    assert w.get_kol("clash").tier == 2
    assert res["skipped"] == 1


def test_sync_never_deletes():
    w.add_kol("survivor")
    w.sync_from_config([{"handle": "newcomer"}])  # survivor absent from config
    assert w.get_kol("survivor") is not None


def test_sync_reads_settings_default(monkeypatch):
    monkeypatch.setattr(settings, "kol_watchlist_seed", [{"handle": "fromsettings"}])
    w.sync_from_config()  # no explicit seeds -> uses settings
    assert w.get_kol("fromsettings") is not None


# --- Persistence -------------------------------------------------------------


def test_persistence_survives_reconnect():
    w.add_kol("persist", tier=1, notes="durable")
    # Drop the in-memory connection but keep the same file; data must reload.
    db_path = settings.kol_db_path
    kol_store.reset_for_tests(db_path)
    reloaded = w.get_kol("persist")
    assert reloaded is not None
    assert reloaded.tier == 1
    assert reloaded.notes == "durable"


def test_snapshot_persistence_roundtrip():
    """The snapshot schema/reader are production-ready in Deliverable A even
    though no producer populates them yet."""
    snap = FollowingSnapshot(
        platform="x",
        kol_handle="snapper",
        accounts=[
            SocialAccount(platform="x", handle="target1", platform_id="111"),
            SocialAccount(platform="x", handle="target2"),
        ],
    )
    kol_store.save_snapshot(snap)
    latest = kol_store.latest_snapshot("x", "snapper")
    assert latest is not None
    assert latest.keys() == {"111", "target2"}  # id preferred, else handle
    assert latest.complete is True


def test_delete_cascades_snapshots_and_sync():
    w.add_kol("cascade")
    kol_store.save_snapshot(FollowingSnapshot(platform="x", kol_handle="cascade"))
    kol_store.record_sync("x", "cascade", success=True)
    w.remove_kol("cascade")
    assert kol_store.latest_snapshot("x", "cascade") is None
    assert kol_store.get_sync_meta("x", "cascade") is None


def test_sync_meta_records_success_and_error():
    w.add_kol("meta")
    kol_store.record_sync("x", "meta", success=True)
    meta = kol_store.get_sync_meta("x", "meta")
    assert meta["last_success"] is not None
    assert meta["last_error"] is None
    kol_store.record_sync("x", "meta", success=False, error="boom")
    meta = kol_store.get_sync_meta("x", "meta")
    assert meta["last_error"] == "boom"
    assert meta["last_success"] is not None  # prior success retained


def test_watch_status_reflects_snapshot():
    w.add_kol("watched")
    assert w.get_watch_status("watched").has_snapshot is False
    kol_store.save_snapshot(FollowingSnapshot(platform="x", kol_handle="watched"))
    st = w.get_watch_status("watched")
    assert st.has_snapshot is True
    assert st.last_snapshot_at is not None


# --- Provider abstraction ----------------------------------------------------


def test_x_provider_registered():
    assert is_supported("x")
    assert "x" in available_platforms()
    assert isinstance(get_provider("x"), XProvider)


def test_unwired_platform_returns_none_provider():
    assert get_provider("lens") is None
    assert is_supported("lens") is False


def test_x_capabilities_are_honest_for_deliverable_a():
    caps = get_provider("x").capabilities()
    assert caps.platform == "x"
    # Foundation only: fetching is deferred, and the provider says so.
    assert caps.can_fetch_following is False
    assert caps.provides_stable_ids is True
    assert caps.requires_auth_session is True


def test_x_handle_normalization():
    p = get_provider("x")
    assert p.normalize_handle("@Foo") == "foo"
    assert p.normalize_handle("  BAR  ") == "bar"
    assert p.normalize_handle("https://x.com/BazQux/") == "bazqux"
    with pytest.raises(ValueError):
        p.normalize_handle("has spaces")
    with pytest.raises(ValueError):
        p.normalize_handle("@")


def test_x_account_url():
    assert get_provider("x").account_url("@Foo") == "https://x.com/foo"


def test_x_build_account_sets_url_and_platform():
    acct = get_provider("x").build_account("@Neo", display_name="Neo")
    assert acct.platform == "x"
    assert acct.handle == "neo"
    assert acct.profile_url == "https://x.com/neo"


def test_x_fetch_following_is_deferred():
    """Fetching must fail loudly (deferred), never silently return an empty set."""
    with pytest.raises(NotImplementedError):
        asyncio.run(get_provider("x").fetch_following("someone"))


def test_registry_supports_custom_provider():
    """A brand-new platform can be plugged in with zero engine changes."""

    class FakeProvider(SocialGraphProvider):
        platform = "farcaster"

        def capabilities(self):
            from app.models.kol import ProviderCapabilities

            return ProviderCapabilities(platform="farcaster", can_fetch_following=True)

        def normalize_handle(self, handle):
            return handle.strip().lstrip("@").lower()

        def account_url(self, handle):
            return f"https://warpcast.com/{self.normalize_handle(handle)}"

        async def fetch_following(self, handle):
            return FollowingSnapshot(platform="farcaster", kol_handle=handle)

    try:
        registry.register_provider(FakeProvider(), replace=True)
        assert is_supported("farcaster")
        # The watchlist facade now normalizes farcaster handles via the provider.
        entry = w.add_kol("@Vitalik", platform="farcaster")
        assert entry.platform == "farcaster"
        assert entry.handle == "vitalik"
        assert w.get_watch_status("vitalik", platform="farcaster").provider_available is True
    finally:
        registry.reset_for_tests()


def test_provider_error_carries_metadata():
    err = ProviderError("rate limited", platform="x", retryable=True)
    assert err.platform == "x"
    assert err.retryable is True


# --- Model-level guards ------------------------------------------------------


def test_kol_entry_normalizes_and_validates():
    e = KolEntry(handle="@Alice")
    assert e.handle == "Alice".lstrip("@")  # @ stripped; case left to provider layer
    assert e.identity() == ("x", "alice")


def test_kol_seed_defaults():
    seed = KolSeed(handle="x")
    assert seed.platform == "x"
    assert seed.tier == 2
    assert seed.enabled is True


def test_social_account_key_prefers_id():
    assert SocialAccount(platform="x", handle="Foo", platform_id="99").key() == "99"
    assert SocialAccount(platform="x", handle="Foo").key() == "foo"
