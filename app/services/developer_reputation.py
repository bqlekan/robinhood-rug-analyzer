"""Developer Reputation Intelligence — on-chain evidence gathering + scoring.

Consumes a TokenAnalysisResponse (for the deployer address and existing DevProfile),
augments with additional Blockscout reads (wallet age, total contracts, verification
status), and produces a DeveloperReputationResult.  Cached per deployer so tokens from
the same creator avoid re-gathering.

Provider pattern: OnChainProvider is the only implementation today.  Future providers
(GitHub, ENS, social) implement the same Protocol and register in _PROVIDERS — the
evaluate() loop scores them additively, no interface change needed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from app.core.config import settings
from app.models.token import (
    DeveloperReputationResult,
    DevProfile,
    TokenAnalysisResponse,
)
from app.services import blockscout_client
from app.services.cache import TTLCache, cached_call

logger = logging.getLogger(__name__)

_cache = TTLCache(
    ttl=settings.deployer_reputation_ttl_hours * 3600,
    max_size=settings.http_cache_max_size,
)

HEALTHY_LIQUIDITY_USD = 1_000.0
MEANINGFUL_HOLDER_COUNT = 50


@runtime_checkable
class ReputationProvider(Protocol):
    """Interface for pluggable reputation data sources."""

    async def gather(self, deployer: str, dev: DevProfile | None) -> dict[str, Any]:
        """Return evidence dict.  Keys are signal names, values are raw data."""
        ...


class OnChainProvider:
    """Blockscout + RPC evidence gatherer."""

    async def gather(self, deployer: str, dev: DevProfile | None) -> dict[str, Any]:
        addr_info_task = blockscout_client.get_address_info(deployer)
        txs_task = blockscout_client.get_address_transactions_paged(
            deployer, pages=settings.transfer_scan_pages,
        )
        addr_info, txs = await asyncio.gather(addr_info_task, txs_task)

        created_addrs: list[str] = []
        earliest_ts: str | None = None
        for tx in txs:
            cc = tx.get("created_contract") or {}
            addr = cc.get("hash")
            if addr:
                created_addrs.append(addr)
            ts = tx.get("timestamp")
            if ts:
                earliest_ts = ts

        unique_created: list[str] = []
        seen: set[str] = set()
        for a in created_addrs:
            k = a.lower()
            if k not in seen:
                seen.add(k)
                unique_created.append(a)
        unique_created = unique_created[:15]

        verified_count = 0
        holder_counts: dict[str, int] = {}
        if unique_created:
            sc_tasks = [blockscout_client.get_smart_contract(a) for a in unique_created]
            sc_results = await asyncio.gather(*sc_tasks, return_exceptions=True)
            for r in sc_results:
                if isinstance(r, dict) and r.get("is_verified"):
                    verified_count += 1

            tc_tasks = [blockscout_client.get_token_counters(a) for a in unique_created]
            tc_results = await asyncio.gather(*tc_tasks, return_exceptions=True)
            for addr, r in zip(unique_created, tc_results):
                if isinstance(r, dict):
                    try:
                        holder_counts[addr.lower()] = int(r.get("token_holders_count", 0))
                    except (TypeError, ValueError):
                        pass

        wallet_age_days: float | None = None
        if earliest_ts:
            try:
                dt = datetime.fromisoformat(earliest_ts.replace("Z", "+00:00"))
                delta = datetime.now(timezone.utc) - dt
                wallet_age_days = round(delta.total_seconds() / 86400, 1)
            except (ValueError, TypeError):
                pass

        funding_source: str | None = None
        incoming = [
            t for t in txs
            if ((t.get("to") or {}).get("hash") or "").lower() == deployer.lower()
            and t.get("value") and t["value"] != "0"
        ]
        if incoming:
            funder = (incoming[-1].get("from") or {}).get("hash")
            if funder:
                funding_source = funder

        return {
            "wallet_age_days": wallet_age_days,
            "total_contracts_deployed": len(unique_created),
            "verified_contracts": verified_count,
            "wallet_transaction_count": len(txs),
            "funding_source": funding_source,
            "holder_counts": holder_counts,
        }


_PROVIDERS: list[ReputationProvider] = [OnChainProvider()]


def _compute_score(
    dev: DevProfile | None,
    evidence: dict[str, Any],
    launchpad_name: str | None,
) -> DeveloperReputationResult:
    """Pure, deterministic scoring from gathered evidence + existing DevProfile."""
    tokens_launched = (dev.tokens_launched or 0) if dev else 0
    tokens_rugged = (dev.tokens_rugged or 0) if dev else 0
    tokens_alive = (dev.tokens_alive or 0) if dev else 0
    launched_tokens = (dev.launched_tokens or []) if dev else []
    deployer = dev.creator_address if dev else None

    healthy_liq = sum(
        1 for t in launched_tokens
        if t.liquidity_usd is not None and t.liquidity_usd >= HEALTHY_LIQUIDITY_USD
    )

    holder_counts: dict[str, int] = evidence.get("holder_counts", {})
    meaningful_holders = sum(
        1 for t in launched_tokens
        if holder_counts.get(t.address.lower(), 0) >= MEANINGFUL_HOLDER_COUNT
    )

    wallet_age_days = evidence.get("wallet_age_days")
    total_contracts = evidence.get("total_contracts_deployed", 0)
    verified = evidence.get("verified_contracts", 0)
    tx_count = evidence.get("wallet_transaction_count")
    funding_source = evidence.get("funding_source")

    launchpad_deployments = 0
    if launchpad_name and launchpad_name not in ("Unknown",):
        launchpad_deployments = 1

    score = 50
    lines: list[str] = []

    # Wallet age
    if wallet_age_days is not None:
        if wallet_age_days >= 365:
            score += 15
            lines.append(f"+ Wallet active for {int(wallet_age_days)} days")
        elif wallet_age_days >= 90:
            score += 10
            lines.append(f"+ Wallet active for {int(wallet_age_days)} days")
        elif wallet_age_days >= 30:
            score += 5
            lines.append(f"+ Wallet active for {int(wallet_age_days)} days")
        elif wallet_age_days < 2:
            score -= 20
            lines.append("- Wallet created recently")
        else:
            score -= 5
            lines.append(f"- Wallet only {int(wallet_age_days)} days old")

    # Deployment history
    if total_contracts == 0 and tokens_launched == 0:
        score -= 10
        lines.append("- First deployment")
    elif total_contracts >= 5:
        score += 8
        lines.append(f"+ {total_contracts} previous deployments")
    elif total_contracts >= 1:
        score += 3
        lines.append(f"+ {total_contracts} previous deployment(s)")

    # Verified contracts
    if verified >= 3:
        score += 12
        lines.append(f"+ {verified} verified contracts")
    elif verified >= 1:
        score += 5
        lines.append(f"+ {verified} verified contract(s)")
    elif total_contracts > 0:
        score -= 5
        lines.append("- No verified contracts")

    # Abandoned launches
    if tokens_rugged == 0 and tokens_launched > 0:
        score += 10
        lines.append("+ No known abandoned launches")
    elif tokens_rugged == 0 and tokens_launched == 0:
        pass
    elif tokens_rugged >= 3:
        score -= 25
        lines.append(f"- {tokens_rugged} abandoned/rugged launches")
    elif tokens_rugged >= 1:
        score -= 10
        lines.append(f"- {tokens_rugged} abandoned/rugged launch(es)")

    # Healthy liquidity
    if healthy_liq >= 3:
        score += 8
        lines.append(f"+ {healthy_liq} previous tokens with healthy liquidity")
    elif healthy_liq >= 1:
        score += 4
        lines.append(f"+ {healthy_liq} previous token(s) with healthy liquidity")

    # Meaningful holders
    if meaningful_holders >= 2:
        score += 5
        lines.append(f"+ {meaningful_holders} previous tokens reached meaningful holder counts")
    elif meaningful_holders == 1:
        score += 2
        lines.append("+ 1 previous token reached meaningful holder count")

    # Surviving contracts
    if tokens_alive >= 3:
        score += 5
        lines.append(f"+ {tokens_alive} previous tokens still alive")
    elif tokens_alive >= 1:
        score += 2
        lines.append(f"+ {tokens_alive} previous token(s) still alive")

    # Launchpad
    if launchpad_deployments > 0:
        score += 3
        lines.append(f"+ Deployed via launchpad ({launchpad_name})")

    score = max(0, min(100, score))

    return DeveloperReputationResult(
        score=score,
        evidence=lines,
        deployer=deployer,
        wallet_age_days=wallet_age_days,
        total_contracts_deployed=total_contracts,
        token_contracts_deployed=tokens_launched,
        verified_contracts=verified,
        launchpad_deployments=launchpad_deployments,
        abandoned_launches=tokens_rugged,
        healthy_liquidity_launches=healthy_liq,
        meaningful_holder_launches=meaningful_holders,
        surviving_contracts=tokens_alive,
        wallet_transaction_count=tx_count,
        funding_source=funding_source,
    )


async def evaluate(result: TokenAnalysisResponse) -> DeveloperReputationResult | None:
    """Public interface: TokenAnalysisResponse in, DeveloperReputationResult out."""
    dev = result.dev
    deployer = dev.creator_address if dev else None
    if not deployer:
        return _compute_score(dev, {}, None)

    cache_key = f"dev_rep:{deployer.lower()}"
    hit = _cache.get(cache_key)
    from app.services.cache import MISS
    if hit is not MISS:
        return hit

    merged: dict[str, Any] = {}
    tasks = [p.gather(deployer, dev) for p in _PROVIDERS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, dict):
            merged.update(r)
        elif isinstance(r, Exception):
            logger.warning("Reputation provider failed: %s", r)

    launchpad_name = result.launchpad.name if result.launchpad else None
    rep = _compute_score(dev, merged, launchpad_name)
    _cache.set(cache_key, rep)
    return rep
