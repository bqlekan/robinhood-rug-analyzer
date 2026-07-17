from __future__ import annotations

"""The `SocialGraphProvider` interface — the single seam between the KOL
Intelligence Engine and any social platform.

Design intent (M23): the engine reasons about KOLs, follows, and snapshots in
platform-neutral terms (`app/models/kol.py`). Each platform — X today; Farcaster,
Telegram, Discord, Reddit, Lens tomorrow — is a concrete subclass of this ABC that
translates its own wire format into those models. The engine calls the interface
and reads `capabilities()`; it never branches on platform name. New providers plug
in through the registry with zero engine changes.

Deliverable A ships the interface plus the first provider (X), whose network
`fetch_following` is intentionally deferred to Deliverable B. A provider that
cannot yet fetch advertises `can_fetch_following=False` and raises
`NotImplementedError` from `fetch_following`, so the engine degrades gracefully.
"""

from abc import ABC, abstractmethod

from app.models.kol import FollowingSnapshot, ProviderCapabilities, SocialAccount


class ProviderError(Exception):
    """Raised for expected, recoverable provider failures (auth expired, rate
    limited, transient network/UI error). Callers catch this and degrade to an
    explicit 'unknown' rather than crashing or inventing a false result.

    `retryable` lets the (future) scheduler distinguish a back-off-and-retry
    condition from a permanent one (e.g. account suspended).
    """

    def __init__(self, message: str, *, platform: str | None = None, retryable: bool = True) -> None:
        super().__init__(message)
        self.platform = platform
        self.retryable = retryable


class SocialGraphProvider(ABC):
    """Read-only view of one social platform's follow graph, normalized to the
    engine's domain models.

    Subclasses MUST set `platform` to a value in `models.kol.SOCIAL_PLATFORMS` and
    implement `capabilities`, `normalize_handle`, and `account_url`. `fetch_following`
    is abstract but may raise `NotImplementedError` while a provider is still a
    foundation-only stub (Deliverable A), as long as its `capabilities()` reports
    `can_fetch_following=False`.
    """

    #: Platform key this provider serves; must match `models.kol.SOCIAL_PLATFORMS`.
    platform: str = ""

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """What this provider can actually do right now. The engine reads this
        instead of hard-coding per-platform behavior."""
        raise NotImplementedError

    @abstractmethod
    def normalize_handle(self, handle: str) -> str:
        """Canonicalize a handle for this platform (strip @, case rules, etc.) so
        the same account always maps to one identity."""
        raise NotImplementedError

    @abstractmethod
    def account_url(self, handle: str) -> str:
        """Public profile URL for a handle. Pure string building; no network."""
        raise NotImplementedError

    @abstractmethod
    async def fetch_following(self, handle: str) -> FollowingSnapshot:
        """Fetch the set of accounts `handle` follows, as a `FollowingSnapshot`.

        Deferred to a later deliverable for most providers. Implementations must
        translate platform data into `SocialAccount`s, set `complete=False` on a
        partial pull, and raise `ProviderError` (not a bare exception) on failure.
        """
        raise NotImplementedError

    def build_account(self, handle: str, **fields) -> SocialAccount:
        """Helper: construct a normalized `SocialAccount` on this platform. Keeps
        handle normalization and profile-URL logic in one place for subclasses."""
        handle = self.normalize_handle(handle)
        fields.setdefault("profile_url", self.account_url(handle))
        return SocialAccount(platform=self.platform, handle=handle, **fields)
