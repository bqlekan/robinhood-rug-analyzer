from __future__ import annotations

"""Persistent authenticated X browser session (M23 Deliverable B).

Owns the browser lifecycle and, crucially, the *authentication state*. It uses a
Playwright **persistent context** rooted at `settings.x_user_data_dir`, so cookies
and local storage survive across runs: log in once (manually), and later scrapes
reuse that session instead of logging in every execution.

Design points:
  - Playwright is imported LAZILY (inside methods), so importing this module — and
    the whole app and the unit test suite — never requires the browser binaries.
    Only an actual live launch needs them installed.
  - Authentication is detected, never bypassed. `ensure_ready()` verifies the
    session is logged in and raises a typed error otherwise (`AuthUnavailableError`
    when no session was ever established, `SessionExpiredError` when one lapsed).
  - `login_interactive()` is the only path that opens a human-visible window to
    (re)authenticate. We never automate credential entry.
  - The context launcher is injectable (`context_factory`) so tests exercise the
    session/auth logic against a fake context with zero Playwright dependency.

The class exposes an async context manager (`async with XSession() as page`) that
yields a ready, authenticated `page`. Scrapers operate on that page and stay
ignorant of how it was produced or authenticated.
"""

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.core.config import settings
from app.services.social.errors import (
    AuthUnavailableError,
    SessionExpiredError,
    TransientNetworkError,
)

logger = logging.getLogger(__name__)

# URL used to probe auth state. The /home timeline requires a logged-in session;
# an unauthenticated hit redirects to /login or /i/flow/login.
_HOME_URL = "https://x.com/home"
_LOGIN_MARKERS = ("/login", "/i/flow/login", "/i/flow/signup", "/?logout")

# A logged-in session renders the primary nav / compose affordances. We look for
# any of these stable test ids to confirm authentication.
_AUTHED_SELECTORS = (
    '[data-testid="SideNav_AccountSwitcher_Button"]',
    '[data-testid="AppTabBar_Home_Link"]',
    '[data-testid="primaryColumn"]',
)

# Async factory that produces a Playwright *persistent context*. Injectable for
# tests. Signature: (user_data_dir, headless) -> context-with-.new_page()/.close().
ContextFactory = Callable[[str, bool], Awaitable[Any]]


class XSession:
    """Manages a persistent, authenticated X browser context."""

    platform = "x"

    def __init__(
        self,
        *,
        user_data_dir: str | None = None,
        headless: bool | None = None,
        context_factory: ContextFactory | None = None,
    ) -> None:
        self._user_data_dir = user_data_dir or settings.x_user_data_dir
        self._headless = settings.x_headless if headless is None else headless
        # Dependency seam: real launcher by default, fake in tests.
        self._context_factory = context_factory or self._default_context_factory
        self._playwright: Any = None
        self._context: Any = None

    # --- lifecycle -----------------------------------------------------------

    async def _default_context_factory(self, user_data_dir: str, headless: bool) -> Any:
        """Launch a real Playwright persistent chromium context. Imported here so
        the dependency is required only when a live browser is actually used."""
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise AuthUnavailableError(
                "playwright is not installed; run `pip install playwright` and "
                "`python -m playwright install chromium`"
            ) from exc

        Path(user_data_dir).mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": user_data_dir,
            "headless": headless,
            # A realistic UA reduces the odds of being served a degraded page.
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "viewport": {"width": 1280, "height": 2000},
        }
        if settings.x_browser_executable:
            launch_kwargs["executable_path"] = settings.x_browser_executable
        return await self._playwright.chromium.launch_persistent_context(**launch_kwargs)

    async def open(self) -> Any:
        """Launch the persistent context and return a ready page. Does NOT verify
        auth (call `ensure_ready`/use the context manager for that)."""
        if self._context is None:
            self._context = await self._context_factory(self._user_data_dir, self._headless)
        pages = getattr(self._context, "pages", None)
        if pages:
            return pages[0]
        return await self._context.new_page()

    async def close(self) -> None:
        """Tear down the context and Playwright driver. Safe to call repeatedly."""
        if self._context is not None:
            try:
                await self._context.close()
            except Exception as exc:  # pragma: no cover - defensive teardown
                logger.debug("error closing X context: %s", exc)
            self._context = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:  # pragma: no cover - defensive teardown
                logger.debug("error stopping playwright: %s", exc)
            self._playwright = None

    # --- authentication ------------------------------------------------------

    async def is_authenticated(self, page: Any) -> bool:
        """True when the current session is logged in.

        Navigates to the home timeline and checks whether X kept us there (authed)
        or bounced us to a login/flow URL (not authed). Also confirms a logged-in
        UI affordance is present so a transient redirect doesn't read as authed.
        """
        try:
            await page.goto(_HOME_URL, timeout=settings.x_nav_timeout_ms,
                            wait_until="domcontentloaded")
        except Exception as exc:
            # A navigation failure here is a network problem, not an auth verdict.
            raise TransientNetworkError(f"could not reach X to verify session: {exc}") from exc

        url = (page.url or "").lower()
        if any(marker in url for marker in _LOGIN_MARKERS):
            return False

        for selector in _AUTHED_SELECTORS:
            try:
                if await page.query_selector(selector) is not None:
                    return True
            except Exception:  # pragma: no cover - selector engine hiccup
                continue
        # Landed somewhere that isn't a login page but shows no authed chrome:
        # treat as not authenticated rather than guessing.
        return False

    async def ensure_ready(self) -> Any:
        """Open the session and confirm it is authenticated. Returns a ready page.

        Raises `AuthUnavailableError` when no session was ever established (fresh /
        empty profile) and `SessionExpiredError` when a profile exists but its
        session has lapsed. We never bypass authentication.
        """
        page = await self.open()
        if await self.is_authenticated(page):
            logger.debug("X session authenticated (reused persistent profile)")
            return page

        # Distinguish "never logged in" from "was logged in, now expired" by whether
        # the persistent profile carries prior state. This drives the caller's UX:
        # first-time setup vs. a reauth prompt.
        if self._profile_has_state():
            raise SessionExpiredError()
        raise AuthUnavailableError()

    def _profile_has_state(self) -> bool:
        """Heuristic: a used profile dir contains cookie/state files. An empty or
        missing dir means we never authenticated."""
        p = Path(self._user_data_dir)
        if not p.exists():
            return False
        # Chromium persists cookies in a SQLite DB under Default/. Any non-trivial
        # content signals a prior session.
        return any(p.iterdir())

    async def login_interactive(self, timeout_seconds: int = 300) -> bool:
        """Open a visible browser for a human to log in, then persist the session.

        This is the ONLY authentication entry point, and it is manual by design —
        we never type credentials or solve challenges programmatically. Returns
        True once an authenticated session is detected within `timeout_seconds`.

        Forces headful regardless of `x_headless`, since a human must interact.
        """
        original_headless = self._headless
        self._headless = False
        try:
            page = await self.open()
            try:
                await page.goto("https://x.com/login",
                                timeout=settings.x_nav_timeout_ms,
                                wait_until="domcontentloaded")
            except Exception as exc:
                raise TransientNetworkError(f"could not open X login page: {exc}") from exc

            logger.info("Waiting up to %ss for manual X login to complete…", timeout_seconds)
            try:
                # Wait for the authed home URL to appear after the human logs in.
                await page.wait_for_url(
                    lambda u: "/home" in (u or "") and not any(
                        m in (u or "") for m in _LOGIN_MARKERS
                    ),
                    timeout=timeout_seconds * 1000,
                )
            except Exception:
                logger.warning("Manual X login did not complete within the timeout")
                return False

            ok = await self.is_authenticated(page)
            if ok:
                logger.info("Manual X login succeeded; session persisted to %s",
                            self._user_data_dir)
            return ok
        finally:
            self._headless = original_headless

    # --- async context manager ----------------------------------------------

    async def __aenter__(self) -> Any:
        return await self.ensure_ready()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()
