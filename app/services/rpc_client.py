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

import asyncio
import logging
from typing import Any, Callable

import httpx

from app.core import chains
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
        response = await client.post(chains.active().rpc_url, json=payload)
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


# ---------------------------------------------------------------------------
# eth_getLogs — generic, reusable log-fetching primitives
# ---------------------------------------------------------------------------

async def get_logs(
    address: str | list[str] | None = None,
    topics: list[str | list[str] | None] | None = None,
    from_block: int = 0,
    to_block: int | str = "latest",
) -> list[dict[str, Any]]:
    """Raw eth_getLogs. Returns [] on RPC failure.

    Designed for general reuse — wallet monitoring, whale alerts, liquidity
    events, or any on-chain event query. Callers needing wide ranges should
    use ``get_logs_chunked`` instead of a single wide call (public RPCs
    time out on large block windows).
    """
    filt: dict[str, Any] = {
        "fromBlock": hex(from_block),
        "toBlock": to_block if isinstance(to_block, str) else hex(to_block),
    }
    if address is not None:
        filt["address"] = address
    if topics is not None:
        filt["topics"] = topics
    result = await _rpc("eth_getLogs", [filt])
    return result if isinstance(result, list) else []


async def get_logs_chunked(
    address: str | list[str] | None,
    topics: list[str | list[str] | None] | None,
    from_block: int,
    to_block: int | None = None,
    chunk_size: int = 2000,
    max_chunks: int = 20,
    retries: int = 2,
    checkpoint_cb: Callable[[int], None] | None = None,
) -> list[dict[str, Any]]:
    """Chunked eth_getLogs with retries and per-chunk checkpoints.

    Wide block ranges time out on public RPCs.  This helper splits
    ``[from_block, to_block]`` into windows of ``chunk_size`` blocks, retries
    each window up to ``retries`` times on transient failure, and invokes
    ``checkpoint_cb(last_completed_block)`` after each successful window so
    callers can persist progress and resume later.

    Returns the accumulated log entries across all windows, or as many as
    were fetched before hitting ``max_chunks``.
    """
    if to_block is None:
        head = await _rpc("eth_blockNumber", [])
        if head is None:
            logger.warning("get_logs_chunked: cannot resolve latest block")
            return []
        to_block = int(head, 16)

    all_logs: list[dict[str, Any]] = []
    cursor = from_block
    chunks_done = 0

    while cursor <= to_block and chunks_done < max_chunks:
        chunk_end = min(cursor + chunk_size - 1, to_block)
        logs: list[dict[str, Any]] | None = None

        for attempt in range(1, retries + 1):
            try:
                logs = await get_logs(
                    address=address,
                    topics=topics,
                    from_block=cursor,
                    to_block=chunk_end,
                )
                break
            except Exception:
                if attempt >= retries:
                    logger.warning(
                        "get_logs_chunked: chunk %s–%s failed after %d retries",
                        cursor, chunk_end, retries,
                    )
                else:
                    await asyncio.sleep(0.5 * attempt)

        if logs:
            all_logs.extend(logs)

        if checkpoint_cb is not None:
            checkpoint_cb(chunk_end)

        cursor = chunk_end + 1
        chunks_done += 1

    return all_logs
