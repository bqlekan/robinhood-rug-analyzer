from __future__ import annotations

"""X Following-page scraper (M23 Deliverable B).

Pure page-driven DOM logic, deliberately separated from session/auth
(`x_session.py`) and from the provider facade (`x_provider.py`). Everything here
operates on an injected Playwright-`page`-shaped object, so the scroll/extraction/
classification logic is fully unit-testable against a fake page with no browser
and no live X.

Responsibilities:
  - classify_profile_state : read the profile page and decide whether we can scrape
    (ok) or must raise a typed error (private / suspended / not-found-or-renamed).
  - scroll_and_collect     : drive X's virtualized, infinitely-scrolling Following
    list, extracting handles as rows render, de-duplicating, and stopping when the
    list end is reached (N stable rounds) or a safety cap trips.
  - scrape_following       : orchestrate the two above and return SocialAccounts.

Scope guard (Deliverable B): this only *reads and returns* the current following
set. No diffing, no follow detection, no scoring — those are later deliverables.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.models.kol import SocialAccount
from app.services.social.errors import (
    AccountPrivateError,
    AccountUnavailableError,
    RateLimitedError,
    TransientNetworkError,
)

logger = logging.getLogger(__name__)

# Each following entry renders as a UserCell; the profile link inside carries the
# handle in its href (/<handle>). These are stable, long-lived X test ids.
_USER_CELL = '[data-testid="UserCell"]'
_USER_CELL_LINK = '[data-testid="UserCell"] a[href^="/"][role="link"]'

# Empty-state / error markers on a profile or following page.
_EMPTY_STATE = '[data-testid="emptyState"]'

# Substrings X shows for various unavailable states (checked case-insensitively
# against visible page text as a fallback to structured markers).
_SUSPENDED_MARKERS = ("account suspended", "has been suspended")
_NOT_FOUND_MARKERS = ("this account doesn't exist", "these posts are protected but "
                      "the account", "try searching for another")
_PRIVATE_MARKERS = ("these posts are protected", "posts are protected", "this account's "
                    "posts are protected")
_RATELIMIT_MARKERS = ("rate limit", "try again later", "something went wrong. try "
                      "reloading")

# href paths that are never real followed accounts (X UI/nav chrome).
_NON_HANDLE_PATHS = {
    "home", "explore", "notifications", "messages", "i", "settings", "compose",
    "search", "hashtag", "intent", "login", "signup", "tos", "privacy",
}


@dataclass
class ScrapeResult:
    """Outcome of a following scrape."""
    handles: list[dict] = field(default_factory=list)  # {handle, display_name, profile_url}
    complete: bool = True   # False if a cap/limit cut the scroll short
    rounds: int = 0


def _handle_from_href(href: str) -> str | None:
    """Extract a clean handle from a profile href like '/cobie' or 'https://x.com/cobie'.
    Returns None for non-profile links (nav chrome, status links, etc.)."""
    if not href:
        return None
    path = href.split("x.com", 1)[-1] if "x.com" in href else href
    path = path.strip().lstrip("/")
    # A real follow link is a bare handle: strip any query, then reject anything
    # with extra path segments ('/cobie/status/123', '/i/lists/...') or empty.
    segment = path.split("?")[0].rstrip("/")
    if not segment or "/" in segment:
        return None
    if segment.lower() in _NON_HANDLE_PATHS:
        return None
    return segment


async def _visible_text(page: Any) -> str:
    """Lowercased body text, best-effort. Used only as a fallback signal."""
    try:
        txt = await page.inner_text("body")
        return (txt or "").lower()
    except Exception:  # pragma: no cover - defensive
        return ""


async def classify_profile_state(page: Any, handle: str) -> None:
    """Inspect an already-navigated profile/following page and raise a typed error
    if it cannot be scraped. Returns None when the page looks scrapeable.

    Order matters: suspended/not-found/rate-limit are checked before private, since
    a suspended account can also lack a visible following list."""
    text = await _visible_text(page)

    if any(m in text for m in _RATELIMIT_MARKERS):
        raise RateLimitedError()

    if any(m in text for m in _SUSPENDED_MARKERS):
        raise AccountUnavailableError(handle, reason=AccountUnavailableError.SUSPENDED)

    if any(m in text for m in _NOT_FOUND_MARKERS):
        # A username change lands here too: the old handle no longer resolves.
        raise AccountUnavailableError(handle, reason=AccountUnavailableError.NOT_FOUND)

    if any(m in text for m in _PRIVATE_MARKERS):
        raise AccountPrivateError(handle)


async def scroll_and_collect(page: Any) -> ScrapeResult:
    """Drive the virtualized Following list: scroll, extract rendered handles,
    de-dupe, and stop at list end or safety cap.

    X only keeps a window of rows in the DOM, so we must read handles on every
    scroll step and accumulate — we can't scroll to the bottom and read once.
    Stops when `x_scroll_stable_rounds` consecutive scrolls add nothing new (end
    reached) or when `x_scroll_max_rounds` / `x_following_max` caps trip.
    """
    seen: dict[str, dict] = {}
    stable = 0
    rounds = 0
    complete = True

    for rounds in range(1, settings.x_scroll_max_rounds + 1):
        added = await _collect_visible(page, seen)

        if len(seen) >= settings.x_following_max:
            logger.info("X following scrape hit safety cap of %s", settings.x_following_max)
            complete = False
            break

        stable = stable + 1 if added == 0 else 0
        if stable >= settings.x_scroll_stable_rounds:
            # No new rows for several rounds -> we've reached the end of the list.
            break

        await _scroll_step(page)
        await _pause(page)

    else:
        # Loop exhausted max rounds without hitting the stable-end condition.
        complete = False
        logger.info("X following scrape stopped at max rounds (%s)", rounds)

    return ScrapeResult(handles=list(seen.values()), complete=complete, rounds=rounds)


async def _collect_visible(page: Any, seen: dict[str, dict]) -> int:
    """Read currently-rendered UserCells into `seen`. Returns count of NEW handles."""
    try:
        links = await page.query_selector_all(_USER_CELL_LINK)
    except Exception as exc:  # pragma: no cover - defensive
        raise TransientNetworkError(f"failed reading following rows: {exc}") from exc

    added = 0
    for link in links or []:
        try:
            href = await link.get_attribute("href")
        except Exception:
            continue
        handle = _handle_from_href(href or "")
        if not handle:
            continue
        key = handle.lower()
        if key in seen:
            continue
        display_name = None
        try:
            display_name = (await link.inner_text() or "").strip() or None
        except Exception:
            display_name = None
        seen[key] = {
            "handle": handle,
            "display_name": display_name,
            "profile_url": f"https://x.com/{handle}",
        }
        added += 1
    return added


async def _scroll_step(page: Any) -> None:
    try:
        await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
    except Exception as exc:  # pragma: no cover - defensive
        raise TransientNetworkError(f"scroll failed: {exc}") from exc


async def _pause(page: Any) -> None:
    # Prefer Playwright's own wait; fall back to nothing if unavailable (fakes).
    wait = getattr(page, "wait_for_timeout", None)
    if callable(wait):
        try:
            await wait(settings.x_scroll_pause_ms)
        except Exception:  # pragma: no cover - defensive
            pass


async def scrape_following(page: Any, handle: str) -> ScrapeResult:
    """Navigate to a handle's Following page, verify it's scrapeable, and collect
    the full following set. `handle` must already be normalized by the caller."""
    url = f"https://x.com/{handle}/following"
    try:
        await page.goto(url, timeout=settings.x_nav_timeout_ms, wait_until="domcontentloaded")
    except Exception as exc:
        raise TransientNetworkError(f"could not open {url}: {exc}") from exc

    # Raise a typed error for private/suspended/not-found before scrolling.
    await classify_profile_state(page, handle)

    result = await scroll_and_collect(page)
    logger.info("Scraped %s following handles for @%s (complete=%s, rounds=%s)",
                len(result.handles), handle, result.complete, result.rounds)
    return result


def to_accounts(result: ScrapeResult) -> list[SocialAccount]:
    """Convert raw scrape rows into normalized SocialAccounts."""
    return [
        SocialAccount(
            platform="x",
            handle=row["handle"].lower(),
            display_name=row.get("display_name"),
            profile_url=row.get("profile_url"),
        )
        for row in result.handles
    ]
