from __future__ import annotations

"""X (Twitter) — the first concrete `SocialGraphProvider`.

Handle normalization and profile URLs are platform-neutral string logic
(Deliverable A). `fetch_following` (Deliverable B) drives a persistent
authenticated browser (`x_session.XSession`) and the DOM scraper (`x_scraper`) to
read the account's live Following list and return a normalized `FollowingSnapshot`.

The session is injected via `session_factory`, so tests exercise the provider with
a fake session/page and never touch Playwright or live X. The scrape itself lives
in `x_scraper`; this class only orchestrates and translates typed errors.

Everything platform-specific about X lives behind this class, so swapping the
free-scrape path for the official API later (the M23 migration path) touches only
this file plus `x_session`/`x_scraper` — the engine, store, and other providers
are unaffected.
"""

import logging
import re
from typing import Callable

from app.models.kol import FollowingSnapshot, ProviderCapabilities
from app.services.social.base import ProviderError, SocialGraphProvider
from app.services.social.errors import TransientNetworkError

logger = logging.getLogger(__name__)

# X usernames: 1–15 chars, letters/digits/underscore. Used to reject junk handles
# early (before any network call) and to keep stored identities clean.
_X_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

# Factory that yields an object usable as `async with factory() as page`. Injectable
# so tests supply a fake session with no browser. Default builds a real XSession.
SessionFactory = Callable[[], object]


class XProvider(SocialGraphProvider):
    platform = "x"

    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        # Deferred import in the default factory keeps Playwright out of the import
        # graph for callers that never scrape (and for the unit tests).
        self._session_factory = session_factory

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            platform=self.platform,
            # Deliverable B: live following scrape is implemented.
            can_fetch_following=True,
            # X exposes a stable numeric user id; captured as SocialAccount.platform_id
            # when available (the preferred diff key for later deliverables).
            provides_stable_ids=True,
            # Monitoring drives an authenticated browser session (cookies),
            # provisioned via the manual login flow. Flagged so ops/health knows.
            requires_auth_session=True,
        )

    def normalize_handle(self, handle: str) -> str:
        """Canonical X handle: no leading @, no url wrapper, lowercased.

        X handles are case-insensitive for identity, so we lowercase to guarantee
        one account maps to one key. Accepts a bare handle, an @handle, or a full
        profile URL and reduces them all to the same canonical form.
        """
        h = (handle or "").strip()
        # Allow pasting a profile URL — take the last path segment.
        if "/" in h:
            h = h.rstrip("/").split("/")[-1]
        h = h.lstrip("@").strip().lower()
        if not h:
            raise ValueError("X handle must be a non-empty username")
        if not _X_HANDLE_RE.match(h):
            raise ValueError(
                f"invalid X handle {handle!r}: expected 1-15 letters, digits or underscores"
            )
        return h

    def account_url(self, handle: str) -> str:
        return f"https://x.com/{self.normalize_handle(handle)}"

    def _make_session(self):
        if self._session_factory is not None:
            return self._session_factory()
        # Local import: the browser dependency is needed only for a real scrape.
        from app.services.social.x_session import XSession

        return XSession()

    async def fetch_following(self, handle: str) -> FollowingSnapshot:
        """Scrape `handle`'s live Following list into a normalized snapshot.

        Opens an authenticated session (raising a typed `ProviderError` subclass if
        auth is unavailable/expired), navigates and scrolls the Following page, and
        returns a `FollowingSnapshot`. `complete=False` marks a scrape cut short by
        a safety cap so downstream logic never mistakes a partial pull for the full
        set. Per Deliverable B scope this only fetches — no diffing or detection.
        """
        from app.services.social import x_scraper

        handle = self.normalize_handle(handle)
        session = self._make_session()
        try:
            async with session as page:
                result = await x_scraper.scrape_following(page, handle)
        except ProviderError:
            # Already typed (auth/private/suspended/rate-limit/network) — let it
            # propagate so callers can react precisely.
            raise
        except Exception as exc:
            # Anything unexpected from the browser degrades to a retryable network
            # error rather than crashing the caller or returning a false result.
            logger.warning("Unexpected error scraping @%s following: %s", handle, exc)
            raise TransientNetworkError(
                f"unexpected error scraping @{handle}: {exc}"
            ) from exc

        return FollowingSnapshot(
            platform=self.platform,
            kol_handle=handle,
            accounts=x_scraper.to_accounts(result),
            complete=result.complete,
        )
