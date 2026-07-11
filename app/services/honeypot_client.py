from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

HONEYPOT_URL = "https://api.honeypot.is/v2/IsHoneypot"
CHAIN_IDS = {
    "ethereum": 1,
    "bsc": 56,
    "polygon": 137,
    "arbitrum": 42161,
    "optimism": 10,
    "base": 8453,
    "avalanche": 43114,
}


async def fetch_honeypot_data(address: str, chain_id: str | None) -> dict[str, Any] | None:
    """Fetch honeypot simulation data for supported EVM chains."""
    if not address.startswith("0x"):
        return None

    params: dict[str, Any] = {"address": address}
    normalized_chain = (chain_id or "").lower()
    if normalized_chain in CHAIN_IDS:
        params["chainID"] = CHAIN_IDS[normalized_chain]

    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            response = await client.get(HONEYPOT_URL, params=params, headers={"Accept": "application/json"})
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("Honeypot.is returned %s for %s", exc.response.status_code, address)
    except httpx.HTTPError as exc:
        logger.warning("Honeypot.is request failed for %s: %s", address, exc)
    except ValueError as exc:
        logger.warning("Honeypot.is returned invalid JSON for %s: %s", address, exc)
    return None
