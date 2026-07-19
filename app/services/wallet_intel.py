from __future__ import annotations

"""Wallet intelligence: insider detection and a smart-wallet heuristic proxy.

IMPORTANT HONESTY NOTE
----------------------
Free, keyless public APIs (Blockscout, DexScreener) do not expose trade-level
profit/loss, so a *verified* ROI (e.g. ">70%") cannot be computed here. The
"smart wallet" score below is a HEURISTIC PROXY derived from observable on-chain
behavior (early entries, reducing into strength, surviving holdings). It is
surfaced to the UI as an explicit estimate, never as a confirmed ROI figure.

The transfer-parsing and scoring helpers are pure so they can be unit tested with
mocked payloads; the async functions orchestrate fetching and persistence.
"""

import asyncio
import logging

from app.core.config import settings
from app.models.token import InsiderWallet, SmartWallet, WalletActivity
from app.services import blockscout_client, watchlist_store
from app.services.analyzers import to_float, to_int

logger = logging.getLogger(__name__)

ZERO = "0x0000000000000000000000000000000000000000"


def normalize_transfers(raw: list[dict]) -> list[dict]:
    """Flatten Blockscout token transfers into simple oldest-first records.

    Each record: {from, to, value(int units, decimals-adjusted), ts, method}.
    Blockscout returns newest-first, so we reverse to get chronological order.
    """
    records: list[dict] = []
    for it in raw:
        frm = ((it.get("from") or {}).get("hash") or "").lower()
        to = ((it.get("to") or {}).get("hash") or "").lower()
        total = it.get("total") or {}
        raw_val = to_float(total.get("value"))
        dec = to_int(total.get("decimals")) or 0
        value = (raw_val / (10 ** dec)) if (raw_val is not None and dec) else raw_val
        records.append(
            {
                "from": frm,
                "to": to,
                "value": value,
                "ts": it.get("timestamp"),
                "method": it.get("method"),
                # M15: block number for same-block coordination detection. Blockscout v2
                # token transfers carry it as `block_number`; None when absent.
                "block": to_int(it.get("block_number")),
            }
        )
    # Reverse into chronological (oldest first) using timestamp when present.
    records.reverse()
    return records


def detect_insiders(
    transfers: list[dict],
    creator: str | None,
    holder_percentages: dict[str, float | None],
    *,
    early_count: int | None = None,
    known_contracts: set[str] | None = None,
) -> list[InsiderWallet]:
    """Identify insider wallets from a token's transfer history.

    Three signals, all from free data:
      - early_buyer: among the first wallets to receive the token after launch.
      - dev_recipient: received the token directly from the deployer.
      - dev_funded: sent tokens back to / round-tripped with the deployer.

    `known_contracts` (LP pair, router, other contracts) are excluded so the AMM
    pair — usually the first post-launch recipient — is never mislabeled a "buyer".
    """
    early_count = early_count or settings.insider_early_buyer_count
    creator_l = (creator or "").lower()
    insiders: dict[str, InsiderWallet] = {}

    # Contracts/mint/zero and the creator itself are not "buyers".
    skip = {ZERO, "", creator_l}
    skip.update((c or "").lower() for c in (known_contracts or set()))

    # Early buyers: first distinct recipients in chronological order.
    rank = 0
    for rec in transfers:
        to = rec["to"]
        if to in skip or to in insiders:
            continue
        # A mint/launch often shows from == zero or from == creator; still, the
        # recipient is an early holder. Rank them in arrival order.
        rank += 1
        if rank > early_count:
            break
        insiders[to] = InsiderWallet(
            address=to,
            reason="early_buyer",
            holding_percentage=holder_percentages.get(to),
            buy_rank=rank,
            note="Among the earliest wallets to receive this token after launch.",
        )

    # Dev recipients: anyone the creator sent tokens to directly.
    if creator_l:
        for rec in transfers:
            if rec["from"] == creator_l and rec["to"] not in skip:
                to = rec["to"]
                if to in insiders:
                    if insiders[to].reason == "early_buyer":
                        insiders[to].reason = "dev_recipient"
                        insiders[to].note = "Early holder that received tokens directly from the deployer."
                    continue
                insiders[to] = InsiderWallet(
                    address=to,
                    reason="dev_recipient",
                    holding_percentage=holder_percentages.get(to),
                    note="Received tokens directly from the deployer wallet.",
                )

    # Order: dev recipients first, then early buyers by rank.
    ordered = sorted(
        insiders.values(),
        key=lambda w: (0 if w.reason == "dev_recipient" else 1, w.buy_rank or 9_999),
    )
    return ordered


def smart_wallet_proxy(
    address: str,
    transfers: list[dict],
    *,
    surviving_tokens: int | None = None,
) -> SmartWallet:
    """Heuristic 'smart money' proxy score for a wallet, from free on-chain signals.

    This is NOT a verified ROI. Signals (each additive, capped at 100):
      - Entered early relative to the token's transfer history.
      - Held most of its position after entering (dumping >=50% is flagged as exit
        risk instead and earns no smart credit).
      - Holds/held multiple surviving tokens (passed in from cross-token context).
    """
    addr = address.lower()
    signals: list[str] = []
    score = 0

    received = [r for r in transfers if r["to"] == addr]
    sent = [r for r in transfers if r["from"] == addr]

    if received:
        # Position in the arrival order of all recipients.
        recipients_seen: list[str] = []
        for r in transfers:
            if r["to"] and r["to"] not in recipients_seen:
                recipients_seen.append(r["to"])
        if addr in recipients_seen:
            idx = recipients_seen.index(addr)
            if idx < max(5, len(recipients_seen) // 10):
                score += 35
                signals.append("Entered among the earliest holders")

    if received:
        got = sum(r["value"] or 0 for r in received)
        gave = sum(r["value"] or 0 for r in sent)
        # M6: reward HOLDING, not dumping. A wallet that entered and kept most of
        # its position is "smart"; one that offloaded >=50% is exit/insider risk and
        # earns no smart credit (only a flag). The old code rewarded the dump case.
        if got > 0:
            if gave < got * 0.5:
                score += 30
                signals.append("Held most of its position after entering")
            else:
                signals.append("Dumped >=50% of its position after entering (exit risk, not smart)")

    if surviving_tokens and surviving_tokens >= 2:
        score += min(35, 15 + 10 * surviving_tokens)
        signals.append(f"Holds/entered {surviving_tokens} surviving tokens")

    score = min(score, 100)
    return SmartWallet(address=addr, proxy_score=score, signals=signals, surviving_tokens=surviving_tokens)


# --- Async orchestration (fetch + persist) ---


async def _count_surviving_tokens(wallets: list[str], *, exclude_token: str | None = None) -> dict[str, int]:
    """Count each wallet's other surviving ERC-20 holdings (M16), bounded and concurrent.

    "Surviving" = the wallet still holds a positive balance of an ERC-20 (excluding the
    token under analysis). A wallet early on several tokens it still holds is a stronger
    smart-money signal than one early on a single token. One `/addresses/{addr}/tokens`
    call per wallet; the caller caps how many wallets reach here, so volume stays bounded.
    Any failed lookup degrades to 0 (never raises, never a false-high count).
    """
    if not wallets:
        return {}
    exclude = (exclude_token or "").lower()

    async def count_one(wallet: str) -> tuple[str, int]:
        try:
            holdings = await blockscout_client.get_address_token_holdings(wallet)
        except Exception as exc:  # a bad lookup must not break profiling
            logger.warning("Survival lookup failed for %s: %s", wallet, exc)
            return wallet, 0
        min_holders = settings.smart_wallet_survival_min_holders
        seen: set[str] = set()
        for h in holdings:
            token = h.get("token") or {}
            if token.get("type") != "ERC-20":
                continue
            addr = (token.get("address_hash") or token.get("address") or "").lower()
            if not addr or addr == exclude:
                continue
            if (to_float(h.get("value")) or 0) <= 0:
                continue
            # A rugged/dead token often collapses to a few wallets; require a holder
            # floor so "surviving" means "still a live token", not a worthless bag.
            # holders_count absent -> don't over-filter (count it).
            hc = to_int(token.get("holders_count"))
            if hc is not None and hc < min_holders:
                continue
            seen.add(addr)
        return wallet, len(seen)

    results = await asyncio.gather(*(count_one(w) for w in wallets), return_exceptions=True)
    return {w: n for res in results if isinstance(res, tuple) for w, n in [res]}


async def profile_token_wallets(
    token_address: str,
    creator: str | None,
    holder_percentages: dict[str, float | None],
    symbol: str | None = None,
    transfers: list[dict] | None = None,
    known_contracts: set[str] | None = None,
) -> tuple[list[InsiderWallet], list[SmartWallet]]:
    """Detect insiders, score smart-wallet proxies, and persist flags.

    `transfers` may be passed in already normalized (by the orchestrator, which
    fetches them once for clusters/dev/insiders). When omitted, they are fetched
    here so the function still works standalone.

    `known_contracts` (LP pair, router, sampled-holder contracts) are excluded
    from insider detection so the AMM pair is not flagged as an early buyer.
    """
    if transfers is None:
        raw = await blockscout_client.get_token_transfers(token_address, pages=settings.transfer_scan_pages)
        transfers = normalize_transfers(raw)
    if not transfers:
        return [], []

    insiders = detect_insiders(transfers, creator, holder_percentages, known_contracts=known_contracts)

    # Score smart-wallet proxies for the distinct non-contract participants.
    # M16: the single-token signals (early entry +35, held-position +30) top out at 65,
    # below the threshold (70). The deciding signal is cross-token survival — how many
    # OTHER tokens the wallet still holds. We compute it for the strongest on-token
    # candidates only (bounded by settings.smart_wallet_survival_candidates) so request
    # volume stays capped, then re-score those with surviving_tokens folded in.
    contracts_skip = {(c or "").lower() for c in (known_contracts or set())}
    contracts_skip.add((creator or "").lower())
    candidates = {r["to"] for r in transfers if r["to"] and r["to"] != ZERO and r["to"] not in contracts_skip}

    # Pre-score on single-token signals; only the near-threshold wallets are worth a
    # survival lookup (survival can add up to +35, so anything within that gap can promote).
    prescored = [(addr, smart_wallet_proxy(addr, transfers)) for addr in candidates]
    gap = 35  # max survival contribution; a wallet further than this below can't reach threshold
    survival_pool = [
        addr for addr, sw in prescored
        if sw.proxy_score >= settings.smart_wallet_min_proxy_score - gap
    ]
    # Cap the survival lookups (each is one Blockscout call), strongest candidates first.
    survival_pool.sort(key=lambda a: next(sw.proxy_score for x, sw in prescored if x == a), reverse=True)
    survival_pool = survival_pool[: settings.smart_wallet_survival_candidates]
    surviving_counts = await _count_surviving_tokens(survival_pool, exclude_token=token_address)

    smart: list[SmartWallet] = []
    for addr, sw in prescored:
        if addr in surviving_counts:
            sw = smart_wallet_proxy(addr, transfers, surviving_tokens=surviving_counts[addr])
        if sw.proxy_score >= settings.smart_wallet_min_proxy_score:
            smart.append(sw)

    # Persist flags + this token as recent activity (best-effort; never block the response).
    try:
        for ins in insiders:
            watchlist_store.upsert_wallet(ins.address, "insider", label=ins.reason, evidence=[ins.note or ins.reason])
            watchlist_store.record_activity(
                ins.address,
                [WalletActivity(token_address=token_address, symbol=symbol, direction="buy")],
            )
        for sw in smart:
            watchlist_store.upsert_wallet(sw.address, "smart", proxy_score=sw.proxy_score, evidence=sw.signals)
            watchlist_store.record_activity(
                sw.address,
                [WalletActivity(token_address=token_address, symbol=symbol, direction="buy")],
            )
    except Exception as exc:  # store issues must not break analysis
        logger.warning("Watchlist persist failed for %s: %s", token_address, exc)

    smart.sort(key=lambda s: s.proxy_score, reverse=True)
    return insiders, smart


async def refresh_watchlisted(batch: int) -> int:
    """Background refresh: re-pull recent buys for the oldest-refreshed watchlisted wallets."""
    addresses = watchlist_store.refresh_addresses(batch)
    if not addresses:
        return 0

    async def refresh_one(addr: str) -> None:
        raw = await blockscout_client.get_address_token_transfers(addr)
        activities: list[WalletActivity] = []
        for it in raw[:25]:
            to = ((it.get("to") or {}).get("hash") or "").lower()
            if to != addr.lower():
                continue  # only count acquisitions ("buys")
            token = it.get("token") or {}
            activities.append(
                WalletActivity(
                    token_address=(token.get("address") or token.get("address_hash") or ""),
                    symbol=token.get("symbol"),
                    direction="buy",
                    amount=(it.get("total") or {}).get("value"),
                    timestamp=it.get("timestamp"),
                )
            )
        if activities:
            watchlist_store.record_activity(addr, activities)

    await asyncio.gather(*(refresh_one(a) for a in addresses), return_exceptions=True)
    return len(addresses)
