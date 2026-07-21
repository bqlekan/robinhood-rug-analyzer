from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core import chains
from app.services.http import get_client

logger = logging.getLogger(__name__)

DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"


async def fetch_token_pairs(address: str) -> list[dict[str, Any]]:
    """Fetch public pair data from DexScreener, filtered to Robinhood Chain only."""
    url = DEXSCREENER_TOKEN_URL.format(address=address)
    try:
        response = await get_client().get(url, headers={"Accept": "application/json"})
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        logger.warning("DexScreener request failed for %s: %s", address, exc)
        return []
    except ValueError as exc:
        logger.warning("DexScreener returned invalid JSON for %s: %s", address, exc)
        return []

    pairs = payload.get("pairs") or []
    if not isinstance(pairs, list):
        return []
    # Enforce single-chain scope: only keep the active chain's pairs.
    return [p for p in pairs if (p.get("chainId") or "").lower() == chains.active().dexscreener_chain]


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
