from __future__ import annotations

"""Reusable JSON-RPC client (M10 deliverable A).

Raw JSON-RPC over the configured `rpc_url`, reusing the shared bounded HTTP pool
from `http.py` (so RPC calls share the same global concurrency cap as every other
outbound request). Every failure — transport, HTTP status, malformed body, or a
JSON-RPC `error` object — degrades to `None`, matching the Blockscout client's
contract so callers never crash or read a false value.

Consumed by the rest of M10 (honeypot simulation via `eth_call`) and later
milestones needing raw RPC access (M11 privilege reads, M13 locker state).
"""

import logging
from typing import Any

import httpx

from app.core.config import settings
from app.services.cache import TTLCache, cached_call
from app.services.http import get_client

logger = logging.getLogger(__name__)

# Immutable reads (a mined tx / receipt never changes) are cache-eligible, same
# as the Blockscout client. eth_call reads live contract state and is NEVER
# cached, so simulations always see current chain state.
_static_cache = TTLCache(
    ttl=settings.http_cache_ttl_seconds,
    max_size=settings.http_cache_max_size,
)


async def _rpc(method: str, params: list[Any]) -> Any | None:
    """POST one JSON-RPC call, returning the `result` field or None on any failure.

    A JSON-RPC `error` object (e.g. reverted call, unknown method) is a failure:
    logged and returned as None, never surfaced as a value.
    """
    client: httpx.AsyncClient = get_client()
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        response = await client.post(settings.rpc_url, json=payload)
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPError as exc:
        logger.warning("RPC %s request failed: %s", method, exc)
        return None
    except ValueError as exc:
        logger.warning("RPC %s returned invalid JSON: %s", method, exc)
        return None
    if isinstance(body, dict) and body.get("error") is not None:
        logger.info("RPC %s returned error: %s", method, body["error"])
        return None
    return (body or {}).get("result") if isinstance(body, dict) else None


async def eth_call(
    to: str,
    data: str,
    block: str = "latest",
    state_override: dict[str, Any] | None = None,
) -> str | None:
    """Static call against a contract; returns hex-encoded return data or None.

    `state_override` is the geth/Nitro `eth_call` 3rd param ({address: {code|balance|
    state...}}), used by the honeypot round-trip to fund a synthetic buyer and inject
    balances without spending funds. Omitted -> a plain 2-param call. Not cached — reads
    live contract state.
    """
    params: list[Any] = [{"to": to, "data": data}, block]
    if state_override is not None:
        params.append(state_override)
    return await _rpc("eth_call", params)


async def get_transaction_by_hash(tx_hash: str) -> dict[str, Any] | None:
    """Full transaction object by hash, or None. Cached: a mined tx is immutable."""
    async def fetch() -> dict[str, Any] | None:
        return await _rpc("eth_getTransactionByHash", [tx_hash])

    if not settings.http_cache_enabled:
        return await fetch()
    return await cached_call(_static_cache, f"rpc_tx:{tx_hash.lower()}", fetch)


async def get_transaction_receipt(tx_hash: str) -> dict[str, Any] | None:
    """Transaction receipt (incl. logs) by hash, or None. Cached: immutable once mined."""
    async def fetch() -> dict[str, Any] | None:
        return await _rpc("eth_getTransactionReceipt", [tx_hash])

    if not settings.http_cache_enabled:
        return await fetch()
    return await cached_call(_static_cache, f"rpc_receipt:{tx_hash.lower()}", fetch)
