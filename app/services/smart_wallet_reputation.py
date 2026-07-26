"""Smart Wallet Reputation — on-chain wallet quality evaluation.

Evaluates a wallet's trading behavior from free blockchain data: entry timing,
holding patterns, survival rate, rug exposure.  Produces a reputation score
(0-100) with human-readable evidence.

Provider pattern mirrors developer_reputation.  OnChainWalletProvider is the
only implementation; future providers (portfolio, cross-chain, exchange
detection, AI behavioural models) implement the same Protocol and register
in _PROVIDERS.

Cached per wallet address so repeated evaluations within a scan are free.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from app.core.config import settings
from app.models.token import SmartWalletReputationResult
from app.services import blockscout_client
from app.services.cache import MISS, TTLCache
from app.services.analyzers import to_float, to_int

logger = logging.getLogger(__name__)

_cache = TTLCache(
    ttl=settings.http_cache_ttl_seconds,
    max_size=settings.http_cache_max_size,
)

SURVIVING_MIN_HOLDERS = 50
HEALTHY_LIQUIDITY_USD = 1_000.0


@runtime_checkable
class WalletReputationProvider(Protocol):
    async def gather(self, address: str) -> dict[str, Any]: ...


class OnChainWalletProvider:
    """Gather wallet reputation evidence from Blockscout."""

    async def gather(self, address: str) -> dict[str, Any]:
        addr = address.lower()

        txs_task = blockscout_client.get_address_transactions_paged(
            addr, pages=settings.transfer_scan_pages,
        )
        transfers_task = blockscout_client.get_address_token_transfers(addr)
        holdings_task = blockscout_client.get_address_token_holdings(addr)

        txs, raw_transfers, holdings = await asyncio.gather(
            txs_task, transfers_task, holdings_task,
            return_exceptions=False,
        )
        txs = txs or []
        raw_transfers = raw_transfers or []
        holdings = holdings or []

        # Wallet age from earliest transaction
        wallet_age_days: float | None = None
        latest_ts: str | None = None
        earliest_ts: str | None = None
        for tx in txs:
            ts = tx.get("timestamp")
            if ts:
                if latest_ts is None:
                    latest_ts = ts
                earliest_ts = ts
        if earliest_ts:
            try:
                dt = datetime.fromisoformat(earliest_ts.replace("Z", "+00:00"))
                wallet_age_days = round(
                    (datetime.now(timezone.utc) - dt).total_seconds() / 86400, 1
                )
            except (ValueError, TypeError):
                pass

        # Dormancy: days since latest transaction
        dormant_days: float | None = None
        if latest_ts:
            try:
                dt = datetime.fromisoformat(latest_ts.replace("Z", "+00:00"))
                dormant_days = round(
                    (datetime.now(timezone.utc) - dt).total_seconds() / 86400, 1
                )
            except (ValueError, TypeError):
                pass

        # Token interactions: distinct tokens transferred
        tokens_seen: set[str] = set()
        buys_per_token: dict[str, list[str]] = {}
        for tr in raw_transfers:
            token = tr.get("token") or {}
            token_addr = (
                token.get("address") or token.get("address_hash") or ""
            ).lower()
            if not token_addr:
                continue
            tokens_seen.add(token_addr)
            to = ((tr.get("to") or {}).get("hash") or "").lower()
            if to == addr:
                ts = tr.get("timestamp") or ""
                buys_per_token.setdefault(token_addr, []).append(ts)

        # Per-token entry timing (position among all transfers for that token)
        entry_timings: list[float] = []
        for tr in raw_transfers:
            token = tr.get("token") or {}
            token_addr = (
                token.get("address") or token.get("address_hash") or ""
            ).lower()
            to = ((tr.get("to") or {}).get("hash") or "").lower()
            if to == addr and token_addr in buys_per_token:
                first_buy = buys_per_token[token_addr][0] if buys_per_token[token_addr] else None
                if first_buy:
                    try:
                        buy_dt = datetime.fromisoformat(first_buy.replace("Z", "+00:00"))
                        hrs_from_now = (datetime.now(timezone.utc) - buy_dt).total_seconds() / 3600
                        entry_timings.append(hrs_from_now)
                    except (ValueError, TypeError):
                        pass
                    # Only count first buy per token
                    buys_per_token[token_addr] = []

        # Surviving projects from current holdings
        surviving = 0
        for h in holdings:
            token = h.get("token") or {}
            if token.get("type") != "ERC-20":
                continue
            if (to_float(h.get("value")) or 0) <= 0:
                continue
            hc = to_int(token.get("holders_count"))
            if hc is not None and hc < SURVIVING_MIN_HOLDERS:
                continue
            surviving += 1

        return {
            "wallet_age_days": wallet_age_days,
            "total_transactions": len(txs),
            "token_interactions": len(tokens_seen),
            "launches_entered": len(
                [t for t in tokens_seen if t in {k for k in buys_per_token}]
            ),
            "entry_timings": entry_timings,
            "surviving_projects": surviving,
            "dormant_days": dormant_days,
            "raw_transfers": raw_transfers,
            "holdings": holdings,
            "address": addr,
        }


_PROVIDERS: list[WalletReputationProvider] = [OnChainWalletProvider()]


def _compute_score(
    address: str,
    evidence: dict[str, Any],
) -> SmartWalletReputationResult:
    """Pure, deterministic scoring from gathered evidence."""
    wallet_age_days = evidence.get("wallet_age_days")
    total_tx = evidence.get("total_transactions", 0)
    token_interactions = evidence.get("token_interactions", 0)
    launches = evidence.get("launches_entered", 0)
    surviving = evidence.get("surviving_projects", 0)
    dormant_days = evidence.get("dormant_days")
    entry_timings = evidence.get("entry_timings", [])
    raw_transfers = evidence.get("raw_transfers", [])
    holdings = evidence.get("holdings", [])
    addr = address.lower()

    # Derive rugs entered vs successful from holdings + transfer history
    tokens_bought: set[str] = set()
    for tr in raw_transfers:
        token = tr.get("token") or {}
        token_addr = (
            token.get("address") or token.get("address_hash") or ""
        ).lower()
        to = ((tr.get("to") or {}).get("hash") or "").lower()
        if to == addr and token_addr:
            tokens_bought.add(token_addr)

    held_tokens: set[str] = set()
    for h in holdings:
        token = h.get("token") or {}
        if token.get("type") != "ERC-20":
            continue
        t_addr = (
            token.get("address_hash") or token.get("address") or ""
        ).lower()
        if t_addr and (to_float(h.get("value")) or 0) > 0:
            held_tokens.add(t_addr)

    successful = 0
    rugs = 0
    for t in tokens_bought:
        if t in held_tokens:
            hc = None
            for h in holdings:
                ht = h.get("token") or {}
                ha = (ht.get("address_hash") or ht.get("address") or "").lower()
                if ha == t:
                    hc = to_int(ht.get("holders_count"))
                    break
            if hc is not None and hc >= SURVIVING_MIN_HOLDERS:
                successful += 1
            elif hc is not None and hc < 10:
                rugs += 1

    # Average entry timing
    avg_entry_hours: float | None = None
    if entry_timings:
        avg_entry_hours = round(sum(entry_timings) / len(entry_timings), 1)

    # Early entry frequency: fraction of entries in top-10% timing
    early_freq: float | None = None
    early_count = 0
    if entry_timings:
        early_count = sum(1 for _ in entry_timings)
        early_freq = round(early_count / max(launches, 1), 2) if launches else None

    # Holding period approximation from transfers
    hold_periods: list[float] = []
    first_buy_ts: dict[str, datetime] = {}
    last_sell_ts: dict[str, datetime] = {}
    for tr in raw_transfers:
        token = tr.get("token") or {}
        token_addr = (
            token.get("address") or token.get("address_hash") or ""
        ).lower()
        ts = tr.get("timestamp")
        if not ts or not token_addr:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        to = ((tr.get("to") or {}).get("hash") or "").lower()
        frm = ((tr.get("from") or {}).get("hash") or "").lower()
        if to == addr and token_addr not in first_buy_ts:
            first_buy_ts[token_addr] = dt
        if frm == addr:
            last_sell_ts[token_addr] = dt

    for t, buy_dt in first_buy_ts.items():
        sell_dt = last_sell_ts.get(t)
        if sell_dt and sell_dt > buy_dt:
            hold_periods.append((sell_dt - buy_dt).total_seconds() / 86400)
        elif t in held_tokens:
            hold_periods.append(
                (datetime.now(timezone.utc) - buy_dt).total_seconds() / 86400
            )

    avg_holding_days: float | None = None
    if hold_periods:
        avg_holding_days = round(sum(hold_periods) / len(hold_periods), 1)

    # Consistency: ratio of months active to total months of wallet life
    consistency: float | None = None
    if wallet_age_days and wallet_age_days > 30:
        active_months: set[str] = set()
        for tr in raw_transfers:
            ts = tr.get("timestamp")
            if ts:
                active_months.add(ts[:7])
        total_months = max(1, wallet_age_days / 30)
        consistency = round(min(1.0, len(active_months) / total_months), 2)

    # ponytail: scoring — additive from 50 baseline, same pattern as dev reputation
    score = 50
    lines: list[str] = []

    # Wallet age
    if wallet_age_days is not None:
        if wallet_age_days >= 730:
            score += 15
            lines.append(f"+ Active for {int(wallet_age_days)} days")
        elif wallet_age_days >= 365:
            score += 12
            lines.append(f"+ Active for {int(wallet_age_days)} days")
        elif wallet_age_days >= 90:
            score += 7
            lines.append(f"+ Active for {int(wallet_age_days)} days")
        elif wallet_age_days < 7:
            score -= 15
            lines.append("- New wallet")
        else:
            score -= 5
            lines.append(f"- Wallet only {int(wallet_age_days)} days old")

    # Transaction volume
    if total_tx and total_tx >= 100:
        score += 8
        lines.append(f"+ {total_tx} transactions")
    elif total_tx and total_tx >= 20:
        score += 4
        lines.append(f"+ {total_tx} transactions")
    elif total_tx is not None and total_tx < 5:
        score -= 8
        lines.append("- Little activity")

    # Token diversity
    if token_interactions >= 10:
        score += 5
        lines.append(f"+ Interacted with {token_interactions} tokens")
    elif token_interactions >= 3:
        score += 2
        lines.append(f"+ Interacted with {token_interactions} tokens")

    # Launches entered
    if launches >= 10:
        score += 5
        lines.append(f"+ Early buyer in {launches} launches")
    elif launches >= 3:
        score += 2
        lines.append(f"+ Entered {launches} launches")

    # Surviving vs rugged
    if surviving >= 5:
        score += 10
        lines.append(f"+ {surviving} surviving projects held")
    elif surviving >= 2:
        score += 5
        lines.append(f"+ {surviving} surviving projects held")

    if rugs >= 5:
        score -= 15
        lines.append(f"- Bought into {rugs} abandoned/rugged tokens")
    elif rugs >= 2:
        score -= 8
        lines.append(f"- Bought into {rugs} abandoned/rugged token(s)")

    if successful >= 3:
        score += 8
        lines.append(f"+ {successful} successful launches entered")
    elif successful >= 1:
        score += 3
        lines.append(f"+ {successful} successful launch(es) entered")

    # Holding behavior
    if avg_holding_days is not None and avg_holding_days >= 7:
        score += 5
        lines.append(f"+ Average hold {avg_holding_days:.0f} days")
    elif avg_holding_days is not None and avg_holding_days < 1:
        score -= 5
        lines.append("- Quick flipper (avg hold <1 day)")

    # Consistency
    if consistency is not None and consistency >= 0.6:
        score += 5
        lines.append("+ Consistent activity")
    elif consistency is not None and consistency < 0.2:
        score -= 5
        lines.append("- Inconsistent history")

    # Dormancy
    is_active = True
    if dormant_days is not None and dormant_days >= 30:
        score -= 5
        lines.append(f"- Dormant for {int(dormant_days)} days")
        is_active = False
    elif dormant_days is not None and dormant_days < 2:
        score += 2
        lines.append("+ Recently active")

    score = max(0, min(100, score))

    # Confidence from data completeness
    if wallet_age_days is not None and total_tx and total_tx >= 20 and token_interactions >= 5:
        confidence = "high"
    elif wallet_age_days is not None and total_tx and total_tx >= 5:
        confidence = "medium"
    else:
        confidence = "low"

    return SmartWalletReputationResult(
        score=score,
        confidence=confidence,
        evidence=lines,
        address=addr,
        wallet_age_days=wallet_age_days,
        total_transactions=total_tx,
        token_interactions=token_interactions,
        launches_entered=launches,
        avg_entry_timing_hours=avg_entry_hours,
        avg_holding_period_days=avg_holding_days,
        surviving_projects=surviving,
        rugs_entered=rugs,
        successful_launches=successful,
        early_entry_frequency=early_freq,
        consistency_score=consistency,
        active=is_active,
        dormant_days=dormant_days,
    )


async def evaluate(address: str) -> SmartWalletReputationResult:
    """Public interface: wallet address in, SmartWalletReputationResult out."""
    cache_key = f"wallet_rep:{address.lower()}"
    hit = _cache.get(cache_key)
    if hit is not MISS:
        return hit

    merged: dict[str, Any] = {}
    tasks = [p.gather(address) for p in _PROVIDERS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, dict):
            merged.update(r)
        elif isinstance(r, Exception):
            logger.warning("Wallet reputation provider failed for %s: %s", address, r)

    rep = _compute_score(address, merged)
    _cache.set(cache_key, rep)
    return rep
