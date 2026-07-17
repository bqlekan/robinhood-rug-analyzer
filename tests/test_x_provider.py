"""Tests for the X Playwright provider (M23 Deliverable B).

Everything here runs against fake, Playwright-shaped objects — no browser, no
network, no live X. A `FakePage` simulates X's virtualized, infinitely-scrolling
Following list (a moving window of rendered rows) plus the various profile states
(private / suspended / not-found / rate-limited). A `FakeSession` stands in for
the persistent authenticated browser so provider + persistence are testable in
isolation.

Scope: Deliverable B only fetches and persists snapshots. Tests assert that no
diffing/detection happens (e.g. two captures just store two snapshots).
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from app.core.config import settings
from app.models.kol import SocialAccount
from app.services import kol_store, kol_watchlist as w
from app.services.social import x_scraper
from app.services.social.errors import (
    AccountPrivateError,
    AccountUnavailableError,
    AuthUnavailableError,
    RateLimitedError,
    SessionExpiredError,
    TransientNetworkError,
)
from app.services.social.x_provider import XProvider
from app.services.social.x_session import XSession


# --- fakes -------------------------------------------------------------------


class FakeElement:
    def __init__(self, href: str, text: str = ""):
        self._href = href
        self._text = text

    async def get_attribute(self, name):
        return self._href if name == "href" else None

    async def inner_text(self):
        return self._text


class FakePage:
    """Simulates an X page: a Following list revealed a window at a time as the
    caller scrolls, plus optional profile-state text and navigation behavior."""

    def __init__(
        self,
        *,
        handles=None,
        body_text="",
        window=20,
        url="https://x.com/target/following",
        goto_error=False,
    ):
        # Full following list (each entry: handle or (handle, display_name)).
        self._all = []
        for h in handles or []:
            if isinstance(h, tuple):
                self._all.append(h)
            else:
                self._all.append((h, h))
        self._body_text = body_text
        self._window = window          # how many rows render per "viewport"
        self._revealed = window        # grows as we scroll
        self.url = url
        self._goto_error = goto_error
        self.scrolls = 0
        self.waits = 0

    async def goto(self, url, **kwargs):
        if self._goto_error:
            raise RuntimeError("net::ERR_CONNECTION_RESET")
        self.url = url
        return None

    async def inner_text(self, selector):
        return self._body_text

    async def query_selector(self, selector):
        # Used by session auth check; presence of any authed selector.
        return None

    async def query_selector_all(self, selector):
        # Return the currently-revealed window of UserCell links.
        rows = self._all[: self._revealed]
        return [FakeElement(f"/{h}", text=name) for h, name in rows]

    async def evaluate(self, script):
        # A scroll reveals one more window of rows.
        self.scrolls += 1
        self._revealed = min(self._revealed + self._window, len(self._all))
        return None

    async def wait_for_timeout(self, ms):
        self.waits += 1
        return None


class FakeSession:
    """Stands in for XSession: yields a prepared page, records close()."""

    def __init__(self, page, *, enter_error=None):
        self._page = page
        self._enter_error = enter_error
        self.closed = False

    async def __aenter__(self):
        if self._enter_error is not None:
            raise self._enter_error
        return self._page

    async def __aexit__(self, *exc):
        self.closed = True
        return None


def _provider(page=None, *, enter_error=None):
    session = FakeSession(page or FakePage(handles=["a", "b"]), enter_error=enter_error)
    return XProvider(session_factory=lambda: session), session


@pytest.fixture(autouse=True)
def _temp_db():
    tmp = Path(tempfile.mkdtemp()) / "kol.db"
    kol_store.reset_for_tests(str(tmp))
    yield
    kol_store.reset_for_tests()


# --- handle parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    "href,expected",
    [
        ("/cobie", "cobie"),
        ("https://x.com/Cobie", "Cobie"),
        ("/cobie/status/123", None),   # not a bare profile link
        ("/i/lists/9", None),
        ("/home", None),               # nav chrome
        ("/settings", None),
        ("", None),
        ("/hashtag", None),
    ],
)
def test_handle_from_href(href, expected):
    assert x_scraper._handle_from_href(href) == expected


# --- scrolling + dedup -------------------------------------------------------


def test_scroll_collects_all_with_pagination():
    # 55 handles, window of 20 -> needs several scrolls to reveal all.
    page = FakePage(handles=[f"user{i}" for i in range(55)], window=20)
    result = asyncio.run(x_scraper.scroll_and_collect(page))
    assert len(result.handles) == 55
    assert result.complete is True
    assert page.scrolls >= 2  # had to scroll to reveal beyond the first window


def test_scroll_dedupes_repeated_rows():
    # The same handles keep rendering (virtualization re-shows rows); no dupes.
    page = FakePage(handles=["a", "b", "c"], window=3)
    result = asyncio.run(x_scraper.scroll_and_collect(page))
    handles = [h["handle"] for h in result.handles]
    assert sorted(handles) == ["a", "b", "c"]
    assert len(handles) == len(set(handles))


def test_scroll_stops_on_stable_rounds():
    # Small list fully visible immediately; should stop quickly, not loop to max.
    page = FakePage(handles=["only"], window=20)
    result = asyncio.run(x_scraper.scroll_and_collect(page))
    assert result.rounds < settings.x_scroll_max_rounds
    assert result.complete is True


def test_scroll_respects_following_cap(monkeypatch):
    monkeypatch.setattr(settings, "x_following_max", 10)
    page = FakePage(handles=[f"u{i}" for i in range(50)], window=20)
    result = asyncio.run(x_scraper.scroll_and_collect(page))
    assert len(result.handles) <= 10 + 20  # capped; partial pull flagged
    assert result.complete is False


def test_scroll_respects_max_rounds(monkeypatch):
    monkeypatch.setattr(settings, "x_scroll_max_rounds", 3)
    monkeypatch.setattr(settings, "x_scroll_stable_rounds", 99)  # never "stable"
    # A page that always reveals one more row so it never naturally ends.
    page = FakePage(handles=[f"u{i}" for i in range(500)], window=1)
    result = asyncio.run(x_scraper.scroll_and_collect(page))
    assert result.rounds == 3
    assert result.complete is False


# --- profile state classification -------------------------------------------


def test_classify_private_raises():
    page = FakePage(body_text="These posts are protected")
    with pytest.raises(AccountPrivateError):
        asyncio.run(x_scraper.classify_profile_state(page, "target"))


def test_classify_suspended_raises():
    page = FakePage(body_text="Account suspended")
    with pytest.raises(AccountUnavailableError) as ei:
        asyncio.run(x_scraper.classify_profile_state(page, "target"))
    assert ei.value.reason == AccountUnavailableError.SUSPENDED


def test_classify_not_found_raises_for_rename():
    # A username change surfaces as not-found on the old handle.
    page = FakePage(body_text="This account doesn't exist")
    with pytest.raises(AccountUnavailableError) as ei:
        asyncio.run(x_scraper.classify_profile_state(page, "oldname"))
    assert ei.value.reason == AccountUnavailableError.NOT_FOUND


def test_classify_rate_limited_raises():
    page = FakePage(body_text="Something went wrong. Try reloading.")
    with pytest.raises(RateLimitedError):
        asyncio.run(x_scraper.classify_profile_state(page, "target"))


def test_classify_ok_returns_none():
    page = FakePage(body_text="Following")
    assert asyncio.run(x_scraper.classify_profile_state(page, "target")) is None


# --- provider fetch_following ------------------------------------------------


def test_fetch_following_returns_snapshot():
    page = FakePage(handles=[("Cobie", "Cobie"), ("ansem", "Ansem")])
    provider, session = _provider(page)
    snap = asyncio.run(provider.fetch_following("@Target"))
    assert snap.platform == "x"
    assert snap.kol_handle == "target"       # normalized
    assert snap.complete is True
    assert {a.handle for a in snap.accounts} == {"cobie", "ansem"}  # handles lowercased
    assert all(a.profile_url.startswith("https://x.com/") for a in snap.accounts)
    assert session.closed is True            # session always torn down


def test_fetch_following_private_propagates():
    page = FakePage(body_text="These posts are protected")
    provider, session = _provider(page)
    with pytest.raises(AccountPrivateError):
        asyncio.run(provider.fetch_following("target"))
    assert session.closed is True            # cleaned up even on error


def test_fetch_following_auth_unavailable_propagates():
    provider, _ = _provider(enter_error=AuthUnavailableError())
    with pytest.raises(AuthUnavailableError):
        asyncio.run(provider.fetch_following("target"))


def test_fetch_following_session_expired_propagates():
    provider, _ = _provider(enter_error=SessionExpiredError())
    with pytest.raises(SessionExpiredError):
        asyncio.run(provider.fetch_following("target"))


def test_fetch_following_navigation_error_becomes_transient():
    page = FakePage(handles=["a"], goto_error=True)
    provider, _ = _provider(page)
    with pytest.raises(TransientNetworkError):
        asyncio.run(provider.fetch_following("target"))


def test_fetch_following_unexpected_error_becomes_transient():
    class Boom(FakePage):
        async def query_selector_all(self, selector):
            raise ValueError("DOM exploded")

    provider, _ = _provider(Boom(handles=["a"]))
    # scraper wraps read failures as TransientNetworkError already; provider-level
    # catch-all also guarantees a typed error, never a bare exception.
    with pytest.raises(TransientNetworkError):
        asyncio.run(provider.fetch_following("target"))


def test_fetch_following_partial_flagged(monkeypatch):
    monkeypatch.setattr(settings, "x_following_max", 5)
    page = FakePage(handles=[f"u{i}" for i in range(40)], window=20)
    provider, _ = _provider(page)
    snap = asyncio.run(provider.fetch_following("target"))
    assert snap.complete is False            # partial pull never looks "full"


# --- capture_following (fetch + persist facade) ------------------------------


def _register_fake_provider(page=None, *, enter_error=None):
    from app.services.social import registry

    session = FakeSession(page or FakePage(handles=["a", "b"]), enter_error=enter_error)
    registry.register_provider(
        XProvider(session_factory=lambda: session), replace=True
    )
    return session


def test_capture_following_persists_snapshot():
    session = _register_fake_provider(FakePage(handles=["a", "b", "c"]))
    try:
        w.add_kol("target")
        snap = asyncio.run(w.capture_following("target"))
        assert len(snap.accounts) == 3
        # Stored and reloadable.
        stored = kol_store.latest_snapshot("x", "target")
        assert stored is not None
        assert stored.keys() == {"a", "b", "c"}
        # Sync + status updated.
        meta = kol_store.get_sync_meta("x", "target")
        assert meta["last_success"] is not None
        assert w.get_kol("target").status == "active"
    finally:
        _reset_registry()


def test_capture_following_records_error_on_failure():
    _register_fake_provider(FakePage(body_text="Account suspended"))
    try:
        w.add_kol("target")
        with pytest.raises(AccountUnavailableError):
            asyncio.run(w.capture_following("target"))
        meta = kol_store.get_sync_meta("x", "target")
        assert meta["last_error"] is not None
        assert w.get_kol("target").status == "error"
        # No snapshot stored on failure.
        assert kol_store.latest_snapshot("x", "target") is None
    finally:
        _reset_registry()


def test_capture_following_unknown_kol_raises():
    _register_fake_provider()
    try:
        with pytest.raises(KeyError):
            asyncio.run(w.capture_following("ghost"))
    finally:
        _reset_registry()


def test_capture_following_stores_two_snapshots_without_diffing():
    """Deliverable B must not diff: two captures yield two independent snapshots
    and nothing computes a delta."""
    session = _register_fake_provider(FakePage(handles=["a", "b"]))
    try:
        w.add_kol("target")
        asyncio.run(w.capture_following("target"))
        # Re-register with a changed following set for the second capture.
        _register_fake_provider(FakePage(handles=["a", "b", "c"]))
        snap2 = asyncio.run(w.capture_following("target"))
        assert snap2.keys() == {"a", "b", "c"}
        # latest_snapshot just returns the most recent; no diff surface exists.
        assert kol_store.latest_snapshot("x", "target").keys() == {"a", "b", "c"}
    finally:
        _reset_registry()


def _reset_registry():
    from app.services.social import registry

    registry.reset_for_tests()


# --- session management (auth, no bypass) ------------------------------------


def _fake_context_factory(page):
    async def factory(user_data_dir, headless):
        class Ctx:
            def __init__(self):
                self.pages = [page]
                self.closed = False

            async def new_page(self):
                return page

            async def close(self):
                self.closed = True

        return Ctx()

    return factory


def test_session_authenticated_when_home_loads():
    class AuthedPage(FakePage):
        async def query_selector(self, selector):
            return object()  # authed chrome present

    page = AuthedPage(url="https://x.com/home")
    sess = XSession(context_factory=_fake_context_factory(page))
    ready = asyncio.run(sess.ensure_ready())
    assert ready is page
    asyncio.run(sess.close())


def test_session_expired_when_redirected_to_login(tmp_path):
    # Profile dir has prior state -> lapsed session, not first-time.
    (tmp_path / "cookies").write_text("x")
    page = FakePage(url="https://x.com/i/flow/login")
    sess = XSession(
        user_data_dir=str(tmp_path),
        context_factory=_fake_context_factory(page),
    )
    with pytest.raises(SessionExpiredError):
        asyncio.run(sess.ensure_ready())


def test_session_auth_unavailable_when_no_profile(tmp_path):
    # Empty profile dir -> never authenticated.
    empty = tmp_path / "empty"
    page = FakePage(url="https://x.com/login")
    sess = XSession(
        user_data_dir=str(empty),
        context_factory=_fake_context_factory(page),
    )
    with pytest.raises(AuthUnavailableError):
        asyncio.run(sess.ensure_ready())


def test_session_never_bypasses_auth(tmp_path):
    """A page that isn't a login page but shows no authed chrome must NOT be
    treated as authenticated."""
    page = FakePage(url="https://x.com/home")  # query_selector returns None
    sess = XSession(
        user_data_dir=str(tmp_path),
        context_factory=_fake_context_factory(page),
    )
    assert asyncio.run(sess.is_authenticated(page)) is False


def test_session_context_manager_closes(tmp_path):
    class AuthedPage(FakePage):
        async def query_selector(self, selector):
            return object()

    page = AuthedPage(url="https://x.com/home")
    factory = _fake_context_factory(page)
    sess = XSession(user_data_dir=str(tmp_path), context_factory=factory)

    async def run():
        async with sess as p:
            assert p is page
        # After exit the context is closed.
        assert sess._context is None

    asyncio.run(run())


# --- to_accounts -------------------------------------------------------------


def test_to_accounts_normalizes():
    result = x_scraper.ScrapeResult(
        handles=[{"handle": "CoBie", "display_name": "Cobie", "profile_url": "u"}]
    )
    accts = x_scraper.to_accounts(result)
    assert accts[0] == SocialAccount(
        platform="x", handle="cobie", display_name="Cobie", profile_url="u"
    )
