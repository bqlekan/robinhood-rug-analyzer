from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core import chains
from app.core.config import settings
from app.services.cache import TTLCache, cached_call
from app.services.http import get_client

logger = logging.getLogger(__name__)


def _api_v2() -> str:
    """Blockscout v2 base for the active chain (M22). Resolved per-call so a chain
    switch / settings override applies without reimport."""
    return f"{chains.active().blockscout_base_url}/api/v2"

# Cache ONLY near-static reads: verified contract source and contract creation
# facts (creator/creation tx). Both are immutable for a deployed contract.
# Holder metrics, transfers, token counters, and market data are deliberately
# NOT long-cached so scoring always sees live data.
_static_cache = TTLCache(
    ttl=settings.http_cache_ttl_seconds,
    max_size=settings.http_cache_max_size,
)

# Short-TTL cache for freshness-sensitive token/market reads. The window is small
# (`market_cache_ttl_seconds`, ~15s) so it only collapses duplicate reads inside a
# scan burst / rapid re-analysis — a single analyze never calls these twice on one
# token, so per-analyze output is unchanged — without serving stale data to a later
# separate analysis.
_market_cache = TTLCache(
    ttl=settings.market_cache_ttl_seconds,
    max_size=settings.http_cache_max_size,
)


async def _get(client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None) -> Any | None:
    """GET a Blockscout v2 endpoint, returning parsed JSON or None on any failure."""
    url = f"{_api_v2()}{path}"
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
    """Token metadata: name, symbol, decimals, total_supply, holders_count, market cap.

    Short-TTL cached (see `_market_cache`): collapses duplicate reads across a scan
    burst / rapid re-analysis; a single analyze reads this once, so its output is
    unchanged.
    """
    async def fetch() -> dict[str, Any] | None:
        return await _get(get_client(), f"/tokens/{address}")

    if not settings.http_cache_enabled:
        return await fetch()
    return await cached_call(_market_cache, f"token_info:{address.lower()}", fetch)


async def get_token_counters(address: str) -> dict[str, Any] | None:
    """Token holder + transfer counts. Short-TTL cached (see `get_token_info`)."""
    async def fetch() -> dict[str, Any] | None:
        return await _get(get_client(), f"/tokens/{address}/counters")

    if not settings.http_cache_enabled:
        return await fetch()
    return await cached_call(_market_cache, f"token_counters:{address.lower()}", fetch)


async def get_token_holders(address: str, limit: int | None = None) -> list[dict[str, Any]]:
    """Top token holders (single page; Blockscout returns ~50 per page ordered by value)."""
    payload = await _get(get_client(), f"/tokens/{address}/holders")
    items = (payload or {}).get("items") or []
    if not isinstance(items, list):
        return []
    return items if limit is None else items[:limit]


async def get_token_holders_paged(address: str, pages: int = 1) -> list[dict[str, Any]]:
    """Top token holders across up to `pages` pages (bounded full-holder set, M12).

    Follows Blockscout's `next_page_params` exactly like `get_token_transfers`. A single
    page (~50 rows) misses whales beyond rank ~50 and makes a 40-holder token look like a
    40,000-holder one at the top; paging widens the set concentration/clusters see. Not
    cached — holder balances are freshness-sensitive.
    """
    items: list[dict[str, Any]] = []
    params: dict[str, Any] | None = None
    client = get_client()
    for _ in range(max(1, pages)):
        payload = await _get(client, f"/tokens/{address}/holders", params=params)
        page_items = (payload or {}).get("items") or []
        if isinstance(page_items, list):
            items.extend(page_items)
        next_params = (payload or {}).get("next_page_params")
        if not next_params:
            break
        params = next_params
    return items


async def get_address_info(address: str) -> dict[str, Any] | None:
    """Address details including creator_address_hash and creation_transaction_hash for contracts.

    Cached: only immutable creation facts are read from this payload downstream.
    """
    async def fetch() -> dict[str, Any] | None:
        return await _get(get_client(), f"/addresses/{address}")

    if not settings.http_cache_enabled:
        return await fetch()
    return await cached_call(_static_cache, f"address_info:{address.lower()}", fetch)


async def get_address_token_transfers(address: str) -> list[dict[str, Any]]:
    """Token transfers touching an address (used to trace common funders for clustering)."""
    payload = await _get(get_client(), f"/addresses/{address}/token-transfers")
    items = (payload or {}).get("items") or []
    return items if isinstance(items, list) else []


async def get_address_transactions(address: str) -> list[dict[str, Any]]:
    """Native transactions touching an address (used to find who funded a wallet).

    Cached (static TTL): funder tracing only reads the EARLIEST incoming tx, which is
    immutable once a wallet exists; caching removes the hottest repeated Blockscout
    read across the funder-graph walk and repeat analyses without changing the result.
    """
    async def fetch() -> list[dict[str, Any]]:
        payload = await _get(get_client(), f"/addresses/{address}/transactions")
        items = (payload or {}).get("items") or []
        return items if isinstance(items, list) else []

    if not settings.http_cache_enabled:
        return await fetch()
    return await cached_call(_static_cache, f"address_txs:{address.lower()}", fetch)


async def get_address_token_holdings(address: str) -> list[dict[str, Any]]:
    """Tokens currently held by an address (single page, M16 cross-token survival).

    Each item is `{token: {address_hash, type, ...}, value, ...}`. Used to count how
    many *surviving* tokens a candidate smart wallet still holds. One page (~50 rows)
    is enough for a survival count and keeps request volume bounded. Not cached — a
    wallet's live holdings change. `[]` on any failure.
    """
    payload = await _get(get_client(), f"/addresses/{address}/tokens")
    items = (payload or {}).get("items") or []
    return items if isinstance(items, list) else []


async def get_smart_contract(address: str) -> dict[str, Any] | None:
    """Verified contract source + metadata (name, compiler, abi, source, imports).

    Cached: a deployed contract's verified source is immutable within the TTL.
    """
    async def fetch() -> dict[str, Any] | None:
        return await _get(get_client(), f"/smart-contracts/{address}")

    if not settings.http_cache_enabled:
        return await fetch()
    return await cached_call(_static_cache, f"smart_contract:{address.lower()}", fetch)


async def get_transaction_timestamp(tx_hash: str) -> str | None:
    """ISO timestamp of a transaction (used for real contract-creation age).

    Cached: a mined transaction's timestamp is immutable within the TTL.
    Returns None if the tx is missing or has no timestamp.
    """
    async def fetch() -> str | None:
        payload = await _get(get_client(), f"/transactions/{tx_hash}")
        return (payload or {}).get("timestamp")

    if not settings.http_cache_enabled:
        return await fetch()
    return await cached_call(_static_cache, f"tx_timestamp:{tx_hash.lower()}", fetch)


async def get_transaction(tx_hash: str) -> dict[str, Any] | None:
    """Full transaction payload (used for creation-tx factory detection via its `to`).

    Cached: a mined transaction is immutable within the TTL. None on any failure.
    """
    async def fetch() -> dict[str, Any] | None:
        return await _get(get_client(), f"/transactions/{tx_hash}")

    if not settings.http_cache_enabled:
        return await fetch()
    return await cached_call(_static_cache, f"tx:{tx_hash.lower()}", fetch)


async def get_transaction_logs(tx_hash: str) -> list[dict[str, Any]]:
    """Event logs emitted by a transaction (used for launchpad event-signature matching).

    Cached: a mined transaction's logs are immutable within the TTL. Empty list on failure.
    """
    async def fetch() -> list[dict[str, Any]]:
        payload = await _get(get_client(), f"/transactions/{tx_hash}/logs")
        items = (payload or {}).get("items") or []
        return items if isinstance(items, list) else []

    if not settings.http_cache_enabled:
        return await fetch()
    return await cached_call(_static_cache, f"tx_logs:{tx_hash.lower()}", fetch)


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
