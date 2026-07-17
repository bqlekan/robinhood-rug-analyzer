from __future__ import annotations

"""X (Twitter) — the first concrete `SocialGraphProvider`.

Deliverable A implements the platform-neutral, no-network surface: handle
normalization (X rules), profile-URL building, and a truthful `capabilities()`
that reports `can_fetch_following=False` because the Playwright-based scrape is a
later deliverable. `fetch_following` therefore raises `NotImplementedError` — the
engine reads `capabilities()` and skips/degrades rather than calling it blindly.

Everything platform-specific about X lives behind this class, so when the scraper
lands (Deliverable B) it changes only this file, and when the free-scrape path is
swapped for the official API later (M23 migration path) it is again only this file.
"""

import re

from app.models.kol import FollowingSnapshot, ProviderCapabilities
from app.services.social.base import SocialGraphProvider

# X usernames: 1–15 chars, letters/digits/underscore. Used to reject junk handles
# early (before any future network call) and to keep stored identities clean.
_X_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


class XProvider(SocialGraphProvider):
    platform = "x"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            platform=self.platform,
            # Deliverable A is foundation-only; the scraper is Deliverable B.
            can_fetch_following=False,
            # X exposes a stable numeric user id; the scraper will capture it as
            # SocialAccount.platform_id, the preferred diff key.
            provides_stable_ids=True,
            # Free monitoring drives an authenticated browser session (cookies),
            # provisioned out-of-band. Flagged so ops/health surfaces know.
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

    async def fetch_following(self, handle: str) -> FollowingSnapshot:  # noqa: ARG002
        # Intentionally deferred to Deliverable B (Playwright scrape). Kept as an
        # explicit, honest failure so nothing silently returns an empty follow set
        # and mistakes it for "follows nobody".
        raise NotImplementedError(
            "X follow-graph fetching is not implemented in Deliverable A "
            "(scheduled for Deliverable B: Playwright monitoring)."
        )
