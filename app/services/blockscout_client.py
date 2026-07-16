from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import settings
from app.services.http import get_client

logger = logging.getLogger(__name__)

# Free, keyless public Blockscout REST API v2 for Robinhood Chain (chain id 4663).
API_V2 = f"{settings.blockscout_base_url}/api/v2"


async def _get(client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None) -> Any | None:
    """GET a Blockscout v2 endpoint, returning parsed JSON or None on any failure."""
    url = f"{API_V2}{path}"
    try:
        response = await client.get(url, params=params, headers={"Accept": "application/json"})
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        # 404 is expected for non-token addresses; log quietly.
        logger.info("Blockscout %s returned %s", path, exc.response.status_code)
    except httpx.HTTPError as exc:
        logger.warning("Blockscout request failed for %s: %s", path, exc)
    except ValueError as exc:
        logger.warning("Blockscout returned invalid JSON for %s: %s", path, exc)
    return None


async def get_token_info(address: str) -> dict[str, Any] | None:
    """Token metadata: name, symbol, decimals, total_supply, holders_count, market cap."""
    return await _get(get_client(), f"/tokens/{address}")


async def get_token_counters(address: str) -> dict[str, Any] | None:
    """Token holder + transfer counts."""
    return await _get(get_client(), f"/tokens/{address}/counters")


async def get_token_holders(address: str, limit: int | None = None) -> list[dict[str, Any]]:
    """Top token holders (single page; Blockscout returns ~50 per page ordered by value)."""
    payload = await _get(get_client(), f"/tokens/{address}/holders")
    items = (payload or {}).get("items") or []
    if not isinstance(items, list):
        return []
    return items if limit is None else items[:limit]


async def get_address_info(address: str) -> dict[str, Any] | None:
    """Address details including creator_address_hash and creation_transaction_hash for contracts."""
    return await _get(get_client(), f"/addresses/{address}")


async def get_address_token_transfers(address: str) -> list[dict[str, Any]]:
    """Token transfers touching an address (used to trace common funders for clustering)."""
    payload = await _get(get_client(), f"/addresses/{address}/token-transfers")
    items = (payload or {}).get("items") or []
    return items if isinstance(items, list) else []


async def get_address_transactions(address: str) -> list[dict[str, Any]]:
    """Native transactions touching an address (used to find who funded a wallet)."""
    payload = await _get(get_client(), f"/addresses/{address}/transactions")
    items = (payload or {}).get("items") or []
    return items if isinstance(items, list) else []


async def get_smart_contract(address: str) -> dict[str, Any] | None:
    """Verified contract source + metadata (name, compiler, abi, source, imports)."""
    return await _get(get_client(), f"/smart-contracts/{address}")


async def get_token_transfers(address: str, pages: int = 1) -> list[dict[str, Any]]:
    """Chronological-ish token transfers for a token (newest first per page).

    Follows Blockscout's `next_page_params` for up to `pages` pages. Used to profile
    a token's flow: earliest buyers (insiders), mutual-transfer clusters, dev outflow.
    """
    items: list[dict[str, Any]] = []
    params: dict[str, Any] | None = None
    client = get_client()
    for _ in range(max(1, pages)):
        payload = await _get(client, f"/tokens/{address}/transfers", params=params)
        page_items = (payload or {}).get("items") or []
        if isinstance(page_items, list):
            items.extend(page_items)
        next_params = (payload or {}).get("next_page_params")
        if not next_params:
            break
        params = next_params
    return items


async def get_address_transactions_paged(address: str, pages: int = 1) -> list[dict[str, Any]]:
    """Native transactions for an address across up to `pages` pages (creator scan)."""
    items: list[dict[str, Any]] = []
    params: dict[str, Any] | None = None
    client = get_client()
    for _ in range(max(1, pages)):
        payload = await _get(client, f"/addresses/{address}/transactions", params=params)
        page_items = (payload or {}).get("items") or []
        if isinstance(page_items, list):
            items.extend(page_items)
        next_params = (payload or {}).get("next_page_params")
        if not next_params:
            break
        params = next_params
    return items


async def list_tokens(token_type: str = "ERC-20", limit: int = 50) -> list[dict[str, Any]]:
    """List tokens on the chain, ordered by market cap / holders (used by the ranked scanner)."""
    payload = await _get(get_client(), "/tokens", params={"type": token_type})
    items = (payload or {}).get("items") or []
    if not isinstance(items, list):
        return []
    return items[:limit]
