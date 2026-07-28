"""Plugin-based launchpad discovery engine.

Iterates over enabled ``LaunchpadDefinition`` entries and dispatches each to the
strategy matching its ``discovery_mode``.  The engine has zero launchpad-specific
branches — adding a launchpad is a configuration change; adding a discovery mode
is one new strategy class + ``register_strategy(...)``.

Strategies
----------
- ``EventLogDiscovery``          — ``eth_getLogs`` with topic0 filtering
- ``FactoryTransactionDiscovery``— Blockscout tx history for a factory contract
- ``ContractCreationDiscovery``  — Blockscout tx history for a deployer address
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, ClassVar, Protocol

from app.models.token import LaunchpadDefinition
from app.services import blockscout_client, rpc_checkpoint
from app.services import rpc_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Strategy protocol + registry
# ---------------------------------------------------------------------------

class DiscoveryStrategy(Protocol):
    """Interface for a launchpad discovery strategy."""

    mode: ClassVar[str]

    async def discover(self, defn: LaunchpadDefinition) -> list[Any]:
        """Return a list of ``RawCandidate``-like dicts with at least ``address`` and ``source``."""
        ...


_STRATEGIES: dict[str, DiscoveryStrategy] = {}


def register_strategy(strategy: DiscoveryStrategy) -> None:
    """Register *strategy* under its ``mode`` key."""
    _STRATEGIES[strategy.mode] = strategy


def get_strategy(mode: str) -> DiscoveryStrategy | None:
    return _STRATEGIES.get(mode)


# ---------------------------------------------------------------------------
# Built-in strategies
# ---------------------------------------------------------------------------

class EventLogDiscovery:
    """Discover tokens via ``eth_getLogs`` with a configurable topic0.

    Uses ``rpc_client.get_logs_chunked`` for retry, chunking, and checkpoint
    support.  The token address is extracted from ``topics[1 + defn.token_index]``.
    """

    mode: ClassVar[str] = "event"

    async def discover(self, defn: LaunchpadDefinition) -> list[dict[str, str]]:
        from app.core.config import settings

        if not defn.factory_address or not defn.topic0:
            logger.warning(
                "EventLogDiscovery: launchpad %r missing factory_address or topic0",
                defn.name,
            )
            return []

        # Resume from last checkpoint if available
        ck_key = f"launchpad:{defn.name}"
        saved = rpc_checkpoint.load_checkpoint(ck_key)
        start = (saved + 1) if saved is not None else defn.start_block

        logs = await rpc_client.get_logs_chunked(
            address=defn.factory_address,
            topics=[defn.topic0],
            from_block=start,
            chunk_size=settings.launchpad_event_scan_chunk_size,
            max_chunks=settings.launchpad_event_scan_max_chunks,
            checkpoint_cb=lambda blk: rpc_checkpoint.save_checkpoint(ck_key, blk),
        )

        out: list[dict[str, str]] = []
        topic_idx = 1 + defn.token_index
        for log in logs:
            topics = log.get("topics") or []
            if len(topics) <= topic_idx:
                continue
            raw = topics[topic_idx]
            # ABI-encoded address: 32 bytes, address is last 20 → 0x + last 40 hex chars
            addr = "0x" + raw[-40:]
            out.append({"address": addr, "source": f"launchpad:{defn.name}"})
        return out


class FactoryTransactionDiscovery:
    """Discover tokens by scanning a factory contract's transaction history.

    Extracts ``created_contract`` from each transaction sent *to* the factory.
    """

    mode: ClassVar[str] = "factory_scan"

    async def discover(self, defn: LaunchpadDefinition) -> list[dict[str, str]]:
        addr = defn.factory_address
        if not addr:
            logger.warning(
                "FactoryTransactionDiscovery: launchpad %r missing factory_address",
                defn.name,
            )
            return []

        txs = await blockscout_client.get_address_transactions_paged(addr, pages=2)
        out: list[dict[str, str]] = []
        for tx in txs:
            created = (tx.get("created_contract") or {}).get("hash")
            if not created:
                continue
            out.append({"address": created, "source": f"launchpad:{defn.name}"})
        return out


class ContractCreationDiscovery:
    """Discover tokens by scanning a deployer address's outbound transactions.

    Extracts ``created_contract`` from outbound txs where the deployer is ``from``.
    """

    mode: ClassVar[str] = "contract_creation_scan"

    async def discover(self, defn: LaunchpadDefinition) -> list[dict[str, str]]:
        addr = defn.deployer_address or defn.factory_address
        if not addr:
            logger.warning(
                "ContractCreationDiscovery: launchpad %r missing deployer_address",
                defn.name,
            )
            return []

        txs = await blockscout_client.get_address_transactions_paged(addr, pages=2)
        out: list[dict[str, str]] = []
        for tx in txs:
            created = (tx.get("created_contract") or {}).get("hash")
            if not created:
                continue
            out.append({"address": created, "source": f"launchpad:{defn.name}"})
        return out


# Register built-in strategies
register_strategy(EventLogDiscovery())
register_strategy(FactoryTransactionDiscovery())
register_strategy(ContractCreationDiscovery())


# ---------------------------------------------------------------------------
# Engine — iterate enabled definitions, dispatch by mode
# ---------------------------------------------------------------------------

async def discover_all(definitions: list[LaunchpadDefinition]) -> list[dict[str, str]]:
    """Run all enabled launchpad definitions concurrently.

    Returns a flat list of ``{"address": ..., "source": ...}`` dicts.
    The engine has no launchpad-specific branches — it dispatches solely
    via the ``_STRATEGIES`` registry.
    """
    enabled = [d for d in definitions if d.enabled]
    if not enabled:
        return []

    tasks = []
    task_defs: list[LaunchpadDefinition] = []
    for d in enabled:
        strat = _STRATEGIES.get(d.discovery_mode)
        if strat is None:
            logger.warning(
                "No strategy registered for mode %r (launchpad %s)",
                d.discovery_mode, d.name,
            )
            continue
        tasks.append(strat.discover(d))
        task_defs.append(d)

    if not tasks:
        return []

    batches = await asyncio.gather(*tasks, return_exceptions=True)
    out: list[dict[str, str]] = []
    for defn, batch in zip(task_defs, batches):
        if isinstance(batch, Exception):
            logger.warning("Launchpad %s scan failed: %s", defn.name, batch)
            continue
        out.extend(batch)
    return out
