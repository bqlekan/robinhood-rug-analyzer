from __future__ import annotations

"""Provider error taxonomy for social scraping (M23 Deliverable B).

All subclass the Deliverable A `ProviderError`, so existing callers that catch
`ProviderError` keep working unchanged — this module only refines it. The engine
(and the future scheduler) can either catch the base type and read `.retryable`,
or match a specific subclass when it needs to react differently (e.g. surface a
"reauthenticate" prompt for `SessionExpiredError`).

Two axes matter to callers:
  - retryable  : is it worth trying again later on its own? (network, rate limit)
  - actionable : does a human need to do something? (session expired -> re-login)

The guiding rule from Deliverable A holds: a failure degrades to an explicit,
typed error. Nothing here ever lets a scrape silently return an empty follow set
and have it be mistaken for "follows nobody".
"""

from app.services.social.base import ProviderError


class SessionExpiredError(ProviderError):
    """The persistent session is no longer authenticated (cookies expired or X
    invalidated them). Not retryable on its own — a human must reauthenticate via
    the manual login flow. Distinct from `AuthUnavailableError`: here a session
    existed and lapsed."""

    def __init__(self, message: str = "X session expired; reauthentication required",
                 *, platform: str = "x") -> None:
        super().__init__(message, platform=platform, retryable=False)


class AuthUnavailableError(ProviderError):
    """No usable authenticated session exists at all (profile dir empty / never
    logged in), and we will NOT bypass authentication. Not retryable until a human
    completes the manual login."""

    def __init__(self, message: str = "no authenticated X session available",
                 *, platform: str = "x") -> None:
        super().__init__(message, platform=platform, retryable=False)


class RateLimitedError(ProviderError):
    """X is rate-limiting us. Retryable after a back-off. `retry_after_seconds` is
    a hint for the future scheduler when X (or our heuristic) suggests one."""

    def __init__(self, message: str = "rate limited by X",
                 *, platform: str = "x", retry_after_seconds: int | None = None) -> None:
        super().__init__(message, platform=platform, retryable=True)
        self.retry_after_seconds = retry_after_seconds


class TransientNetworkError(ProviderError):
    """A temporary network/navigation failure (timeout, connection reset, blank
    page). Retryable."""

    def __init__(self, message: str = "temporary network failure",
                 *, platform: str = "x") -> None:
        super().__init__(message, platform=platform, retryable=True)


class AccountPrivateError(ProviderError):
    """The target account is protected/private, so its following list is not
    visible to this session. Not retryable — the state won't change by retrying,
    and following that account would be an out-of-scope action."""

    def __init__(self, handle: str | None = None, *, platform: str = "x") -> None:
        who = f" for @{handle}" if handle else ""
        super().__init__(f"account is private{who}", platform=platform, retryable=False)
        self.handle = handle


class AccountUnavailableError(ProviderError):
    """The target account cannot be read for a reason tied to the account itself:
    suspended, not found, deactivated, or the handle no longer resolves (a rename).
    `reason` records which. Not retryable under the same handle.

    Username changes are surfaced here rather than silently followed: the old
    handle stops resolving, we report it, and re-linking to the new handle is an
    explicit operator decision (and belongs to later deliverables), never an
    automatic guess."""

    #: Coarse cause, useful for logging/metrics without string parsing.
    SUSPENDED = "suspended"
    NOT_FOUND = "not_found"      # includes handle renames: old handle 404s
    DEACTIVATED = "deactivated"

    def __init__(self, handle: str | None = None, *, reason: str = NOT_FOUND,
                 platform: str = "x") -> None:
        who = f" @{handle}" if handle else ""
        super().__init__(f"account{who} unavailable ({reason})",
                         platform=platform, retryable=False)
        self.handle = handle
        self.reason = reason
