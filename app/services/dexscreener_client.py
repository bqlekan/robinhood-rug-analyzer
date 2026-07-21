from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core import chains
from app.core.config import settings
from app.services.cache import TTLCache, cached_call
from app.services.http import get_client

logger = logging.getLogger(__name__)

DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"

# Short-TTL cache (matches the blockscout market cache): collapses duplicate pair
# reads across a scan burst / rapid re-analysis without serving stale market data to
# a later separate analysis. A single analyze reads a token's pairs once, so its
# output is unchanged.
_market_cache = TTLCache(
    ttl=settings.market_cache_ttl_seconds,
    max_size=settings.http_cache_max_size,
)


async def fetch_token_pairs(address: str) -> list[dict[str, Any]]:
    """Fetch public pair data from DexScreener, filtered to the active chain only."""
    async def fetch() -> list[dict[str, Any]] | None:
        url = DEXSCREENER_TOKEN_URL.format(address=address)
        try:
            response = await get_client().get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            logger.warning("DexScreener request failed for %s: %s", address, exc)
            return None  # error -> not cached (retried next call), coalesced to [] below
        except ValueError as exc:
            logger.warning("DexScreener returned invalid JSON for %s: %s", address, exc)
            return None

        pairs = payload.get("pairs") or []
        if not isinstance(pairs, list):
            return []
        # Enforce single-chain scope: only keep the active chain's pairs.
        return [p for p in pairs if (p.get("chainId") or "").lower() == chains.active().dexscreener_chain]

    if not settings.http_cache_enabled:
        result = await fetch()
    else:
        result = await cached_call(_market_cache, f"ds_pairs:{address.lower()}", fetch)
    return result if result is not None else []


def choose_best_pair(pairs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer the pair with the highest USD liquidity."""
    if not pairs:
        return None

    def liquidity_usd(pair: dict[str, Any]) -> float:
        liquidity = pair.get("liquidity") or {}
        value = liquidity.get("usd") or 0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    return max(pairs, key=liquidity_usd)
