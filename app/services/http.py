from __future__ import annotations

"""One shared httpx.AsyncClient for all outbound calls.

Reusing a single client (and its connection pool) avoids tearing down and
rebuilding a pool on every request. The pool's `max_connections` limit doubles
as a global rate cap: it bounds total concurrent outbound requests across the
entire nested scan fan-out, so a scan cannot exhaust the free API budget.
"""

import httpx

from app.core.config import settings

_CLIENT: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None or _CLIENT.is_closed:
        _CLIENT = httpx.AsyncClient(
            timeout=settings.http_timeout,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=settings.http_max_connections),
        )
    return _CLIENT


async def aclose() -> None:
    global _CLIENT
    if _CLIENT is not None and not _CLIENT.is_closed:
        await _CLIENT.aclose()
    _CLIENT = None
