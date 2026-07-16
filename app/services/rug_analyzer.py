from __future__ import annotations

import asyncio
import logging

from app.core.config import settings
from app.models.token import (
    LiquiditySnapshot,
    PriceChangeSnapshot,
    RankedToken,
    ScanResponse,
    TokenAnalysisResponse,
    TokenLore,
    TokenMarketData,
    VolumeSnapshot,
)
from app.models.token import WatchlistHit
from app.models.token import is_valid_address
from app.services import analyzers, blockscout_client, contract_intel, launchpad_registry, rpc_client, wallet_intel, watchlist_store
from app.services.analyzers import to_float, to_int
from app.services.dexscreener_client import choose_best_pair, fetch_token_pairs
from app.services.lore_client import build_lore
from app.services.scoring import LIMITATIONS, score_token, score_token_light

logger = logging.getLogger(__name__)


def _build_market_data(pair: dict | None) -> TokenMarketData | None:
    if not pair:
        return None

    base_token = pair.get("baseToken") or {}
    quote_token = pair.get("quoteToken") or {}
    liquidity = pair.get("liquidity") or {}
    volume = pair.get("volume") or {}
    price_change = pair.get("priceChange") or {}
    info = pair.get("info") or {}

    websites = [w.get("url") for w in (info.get("websites") or []) if isinstance(w, dict) and w.get("url")]
    socials = [
        {"type": s.get("type", ""), "url": s.get("url", "")}
        for s in (info.get("socials") or [])
        if isinstance(s, dict) and s.get("url")
    ]

    return TokenMarketData(
        chain_id=pair.get("chainId"),
        dex_id=pair.get("dexId"),
        pair_address=pair.get("pairAddress"),
        base_token_name=base_token.get("name"),
        base_token_symbol=base_token.get("symbol"),
        quote_token_symbol=quote_token.get("symbol"),
        price_usd=pair.get("priceUsd"),
        market_cap=to_float(pair.get("marketCap")),
        fdv=to_float(pair.get("fdv")),
        liquidity=LiquiditySnapshot(
            usd=to_float(liquidity.get("usd")),
            base=to_float(liquidity.get("base")),
            quote=to_float(liquidity.get("quote")),
        ),
        volume=VolumeSnapshot(
            h24=to_float(volume.get("h24")),
            h6=to_float(volume.get("h6")),
            h1=to_float(volume.get("h1")),
            m5=to_float(volume.get("m5")),
        ),
        price_change=PriceChangeSnapshot(
            h24=to_float(price_change.get("h24")),
            h6=to_float(price_change.get("h6")),
            h1=to_float(price_change.get("h1")),
            m5=to_float(price_change.get("m5")),
        ),
        pair_created_at=pair.get("pairCreatedAt"),
        url=pair.get("url"),
        websites=websites,
        socials=socials,
    )


def _dev_holding_pct(creator: str | None, holder_distribution) -> float | None:
    """Find the deployer's holding percentage from the sampled holders, if present."""
    if not creator or not holder_distribution:
        return None
    creator_l = creator.lower()
    for entry in holder_distribution.top_holders:
        if entry.address and entry.address.lower() == creator_l:
            return entry.percentage
    return None


async def _trace_funders(holder_addresses: list[str]) -> dict[str, str | None]:
    """For each holder, find the wallet that first sent it native funds (for clustering).

    Bounded and best-effort: runs a handful of concurrent Blockscout lookups.
    """
    async def first_funder(addr: str) -> tuple[str, str | None]:
        txs = await blockscout_client.get_address_transactions(addr)
        # Earliest incoming tx approximates the funding wallet.
        incoming = [t for t in txs if ((t.get("to") or {}).get("hash") or "").lower() == addr.lower()]
        if not incoming:
            return addr, None
        earliest = incoming[-1]  # Blockscout returns newest-first.
        return addr, (earliest.get("from") or {}).get("hash")

    results = await asyncio.gather(*(first_funder(a) for a in holder_addresses), return_exceptions=True)
    funders: dict[str, str | None] = {}
    for res in results:
        if isinstance(res, tuple):
            funders[res[0]] = res[1]
    return funders


async def _scan_creator_launches(creator: str | None, this_token: str) -> list:
    """Find other tokens this deployer created and classify each as alive/rugged.

    Bounded and best-effort: reads a couple of pages of the creator's transactions,
    picks contract-creation txs, and prices each created token's liquidity via DexScreener.
    """
    if not creator:
        return []
    try:
        txs = await blockscout_client.get_address_transactions_paged(
            creator, pages=settings.transfer_scan_pages
        )
    except Exception as exc:
        logger.warning("Creator scan failed for %s: %s", creator, exc)
        return []

    created_addresses: list[str] = []
    for tx in txs:
        cc = tx.get("created_contract") or {}
        addr = cc.get("hash")
        if addr and addr.lower() != this_token.lower():
            created_addresses.append(addr)
    # De-dup, cap to keep the scan cheap.
    seen: set[str] = set()
    unique = []
    for a in created_addresses:
        if a.lower() not in seen:
            seen.add(a.lower())
            unique.append(a)
    unique = unique[:10]

    async def classify(addr: str) -> dict:
        info, pairs = await asyncio.gather(
            blockscout_client.get_token_info(addr),
            fetch_token_pairs(addr),
        )
        best = choose_best_pair(pairs)
        liq = None
        if best:
            liq = to_float((best.get("liquidity") or {}).get("usd"))
        return {"address": addr, "info": info, "liquidity_usd": liq}

    results = await asyncio.gather(*(classify(a) for a in unique), return_exceptions=True)
    created = [r for r in results if isinstance(r, dict)]
    return analyzers.classify_created_tokens(created)


def _watchlist_hits(holder_addresses: list[str]) -> list[WatchlistHit]:
    """Cross-reference sampled holders against the persisted smart/insider watchlist."""
    try:
        known = watchlist_store.known_addresses()
    except Exception as exc:
        logger.warning("Watchlist lookup failed: %s", exc)
        return []
    hits: list[WatchlistHit] = []
    for addr in holder_addresses:
        info = known.get(addr.lower())
        if info:
            hits.append(WatchlistHit(address=addr, kind=info["kind"], proxy_score=info.get("proxy_score")))
    return hits


async def _fetch_creation_evidence(creation_tx: str) -> tuple[str | None, list[str] | None]:
    """Return (factory `to`, log topics) for a creation tx, preferring RPC over Blockscout.

    M10-C: try raw JSON-RPC (`eth_getTransactionByHash` + `eth_getTransactionReceipt`)
    first; fall back to the Blockscout reads when RPC is unavailable or errors. The
    downstream `match_creation_evidence` is source-agnostic (it normalizes both), so
    only the field shapes differ:
      - RPC tx `to` is a plain hex string; Blockscout tx `to` is `{"hash": ...}`.
      - RPC receipt logs live under `logs`; Blockscout logs come from a separate call.
    """
    tx, receipt = await asyncio.gather(
        rpc_client.get_transaction_by_hash(creation_tx),
        rpc_client.get_transaction_receipt(creation_tx),
    )
    if tx is not None or receipt is not None:
        factory = (tx or {}).get("to")
        logs = (receipt or {}).get("logs") or []
        topics = [t for log in logs for t in (log.get("topics") or []) if t]
        return factory, topics

    # RPC gave us nothing usable — fall back to Blockscout.
    tx_data, logs = await asyncio.gather(
        blockscout_client.get_transaction(creation_tx),
        blockscout_client.get_transaction_logs(creation_tx),
    )
    factory = ((tx_data or {}).get("to") or {}).get("hash")
    topics = [t for log in logs for t in (log.get("topics") or []) if t]
    return factory, topics


async def analyze_token_contract(contract_address: str, include_lore: bool = True) -> TokenAnalysisResponse:
    normalized = contract_address.strip()
    # Guard the real outbound boundary: /scan reaches here directly with chain-sourced
    # addresses, bypassing the request model's validator.
    if not is_valid_address(normalized):
        raise ValueError(f"Invalid contract address: {contract_address!r}")

    # Fetch market + token info + address info + verified contract source concurrently.
    pairs_task = fetch_token_pairs(normalized)
    token_info_task = blockscout_client.get_token_info(normalized)
    address_info_task = blockscout_client.get_address_info(normalized)
    holders_task = blockscout_client.get_token_holders(normalized, settings.holder_sample_size)
    contract_task = contract_intel.fetch_contract_intel(normalized)

    pairs, token_info, address_info, holders_raw, ctr_intel = await asyncio.gather(
        pairs_task, token_info_task, address_info_task, holders_task, contract_task
    )

    best_pair = choose_best_pair(pairs)
    market_data = _build_market_data(best_pair)

    data_sources: list[str] = ["DexScreener"] if market_data else []
    if token_info or address_info or holders_raw:
        data_sources.append("Blockscout (Robinhood Chain)")

    # Age. Prefer the DexScreener pair timestamp; when absent (pre-liquidity tokens),
    # fall back to the contract's creation-tx timestamp so brand-new launches are not
    # scored "unknown age". The creation tx is immutable, so this read is cached.
    creation_tx_hash = (address_info or {}).get("creation_transaction_hash")
    contract_created_iso = None
    if not (best_pair and best_pair.get("pairCreatedAt")) and creation_tx_hash:
        contract_created_iso = await blockscout_client.get_transaction_timestamp(creation_tx_hash)
    age = analyzers.analyze_age(
        best_pair.get("pairCreatedAt") if best_pair else None,
        contract_created_iso,
    )

    # Holders + distribution. Exclude the DEX pair address so top10/top1 reflect
    # real wallets, not the AMM pool itself.
    total_supply = (token_info or {}).get("total_supply")
    decimals = (token_info or {}).get("decimals")
    holder_count = to_int((token_info or {}).get("holders_count"))
    lp_addr = best_pair.get("pairAddress") if best_pair else None
    holder_distribution = analyzers.analyze_holders(
        holders_raw, holder_count, total_supply, decimals, lp_address=lp_addr
    )

    creator = (address_info or {}).get("creator_address_hash")
    creation_tx = (address_info or {}).get("creation_transaction_hash")

    # Pull the token's transfer history once; reuse for clusters, dev outflow,
    # insiders, and smart-wallet proxies.
    raw_transfers = await blockscout_client.get_token_transfers(
        normalized, pages=settings.transfer_scan_pages
    )
    transfers = wallet_intel.normalize_transfers(raw_transfers)

    # Clusters: shared-funder + mutual-transfer, merged.
    cluster_addresses = [
        e.address for e in holder_distribution.top_holders if e.address and not e.is_contract
    ][:12]
    funders = await _trace_funders(cluster_addresses) if cluster_addresses else {}
    holder_pcts = {e.address: e.percentage for e in holder_distribution.top_holders}
    sampled_holder_set = {e.address for e in holder_distribution.top_holders if e.address}
    mutual = analyzers.extract_mutual_transfers(transfers, sampled_holder_set)
    clusters = analyzers.analyze_clusters(funders, holder_pcts, mutual_transfers=mutual)

    # Dev / creator: holdings, outgoing transfers, and prior launches.
    supply_units = analyzers._supply_units(total_supply, decimals)
    dev_holding = _dev_holding_pct(creator, holder_distribution)
    dev_transfers, dev_moved_pct = analyzers.analyze_dev_transfers(transfers, creator, supply_units)
    launched_tokens = await _scan_creator_launches(creator, normalized)
    dev = analyzers.analyze_dev(
        creator,
        creation_tx,
        dev_holding,
        launched_tokens=launched_tokens,
        dev_transfers=dev_transfers,
        transferred_out_percentage=dev_moved_pct or None,
    )

    # Wallet intelligence: insiders + smart-wallet proxies (persists to watchlist).
    # Known contracts (LP pair + any sampled holder flagged is_contract) are excluded
    # from insider detection so the AMM pair is not mislabeled "buyer #1". Built from
    # data already on hand — no extra API calls.
    known_contracts = {e.address.lower() for e in holder_distribution.top_holders if e.is_contract and e.address}
    if lp_addr:
        known_contracts.add(lp_addr.lower())
    insiders, _smart = await wallet_intel.profile_token_wallets(
        normalized,
        creator,
        holder_pcts,
        symbol=(token_info or {}).get("symbol"),
        transfers=transfers,  # reuse the already-fetched transfers; no second network call
        known_contracts=known_contracts,
    )
    watchlist_hits = _watchlist_hits(list(sampled_holder_set))

    # Liquidity lock: inspect LP token holders of the pair.
    liquidity_lock = None
    if best_pair and best_pair.get("pairAddress"):
        lp_addr = best_pair["pairAddress"]
        lp_info, lp_holders = await asyncio.gather(
            blockscout_client.get_token_info(lp_addr),
            blockscout_client.get_token_holders(lp_addr, settings.holder_sample_size),
        )
        liquidity_lock = analyzers.analyze_liquidity_lock(
            lp_holders, (lp_info or {}).get("total_supply"), (lp_info or {}).get("decimals")
        )

    # Launchpad. Include the contract intel's template as an extra name hint so
    # OpenZeppelin/Uniswap/CCIP contracts surface even without a deployer match.
    contract_name = (token_info or {}).get("name")
    tags = [t.get("name", "") for t in ((address_info or {}).get("public_tags") or []) if isinstance(t, dict)]
    if ctr_intel and ctr_intel.template and ctr_intel.template not in {"unknown", "custom"}:
        tags = tags + [ctr_intel.template]
    if ctr_intel and ctr_intel.protocol:
        tags = tags + [ctr_intel.protocol]

    # M9: on-chain creation evidence (verified factory `to` = HIGH, verified factory
    # event = MEDIUM). Gated on a non-empty registry so no extra fetches fire in
    # production (empty registry) — the machinery activates only with sourced entries.
    # M10-C: retrieval now prefers raw JSON-RPC and falls back to Blockscout; the
    # evidence-matching below is source-agnostic (see _fetch_creation_evidence).
    creation_factory: str | None = None
    creation_log_topics: list[str] | None = None
    if creation_tx and launchpad_registry.has_enabled_launchpads():
        creation_factory, creation_log_topics = await _fetch_creation_evidence(creation_tx)
    launchpad = analyzers.analyze_launchpad(
        creator,
        contract_name,
        tags,
        creation_factory=creation_factory,
        creation_log_topics=creation_log_topics,
    )

    # Lore.
    lore: TokenLore | None = None
    if include_lore:
        name = (token_info or {}).get("name") or (market_data.base_token_name if market_data else None)
        symbol = (token_info or {}).get("symbol") or (market_data.base_token_symbol if market_data else None)
        lore = await build_lore(
            name,
            symbol,
            market_data.socials if market_data else [],
            market_data.websites if market_data else [],
        )
        if lore.sources:
            data_sources.append("Web search (DuckDuckGo)")

    analysis = score_token(
        age=age,
        market=market_data,
        holders=holder_distribution,
        clusters=clusters,
        dev=dev,
        liquidity_lock=liquidity_lock,
        launchpad=launchpad,
        lore=lore,
        data_sources=data_sources or ["none"],
    )

    return TokenAnalysisResponse(
        contract_address=normalized,
        chain=settings.chain_name,
        status="analysis_completed",
        message="Rug-risk analysis completed for Robinhood Chain token using free public data sources.",
        token_age=age,
        market_data=market_data,
        holders=holder_distribution,
        clusters=clusters,
        dev=dev,
        liquidity_lock=liquidity_lock,
        launchpad=launchpad,
        lore=lore,
        insiders=insiders,
        watchlist_hits=watchlist_hits,
        analysis=analysis,
        contract_intel=ctr_intel,
    )


async def scan_and_rank(limit: int, include_lore: bool = False) -> ScanResponse:
    """Pull active Robinhood Chain tokens, analyze each, and rank by risk score."""
    limit = min(limit, settings.scan_max_tokens)
    # Pull extra to leave headroom for the established-coin filter (USDT, WETH, etc.).
    tokens = await blockscout_client.list_tokens(limit=limit * 3)
    # Skip well-known assets so the scanner focuses on newer creations.
    tokens = [
        t for t in tokens
        if not launchpad_registry.is_established_token(t.get("symbol"), t.get("name"))
    ][:limit]

    if not tokens:
        return ScanResponse(
            chain=settings.chain_name,
            status="no_tokens",
            message="Could not retrieve token list from Blockscout.",
            analyzed=0,
            ranked_tokens=[],
            limitations=LIMITATIONS,
        )

    # Bound concurrent deep analyses so escalation cannot exhaust the API budget.
    deep_sem = asyncio.Semaphore(max(1, settings.scan_max_deep_analyses))

    async def deep_one(token: dict, address: str) -> RankedToken | None:
        async with deep_sem:
            try:
                result = await analyze_token_contract(address, include_lore=include_lore)
            except Exception as exc:  # keep the scan resilient to a single bad token
                logger.warning("Scan: analysis failed for %s: %s", address, exc)
                return None
        top_signal = max(result.analysis.signals, key=lambda s: s.points).name if result.analysis.signals else None
        return RankedToken(
            contract_address=address,
            name=token.get("name"),
            symbol=token.get("symbol"),
            risk_score=result.analysis.risk_score,
            risk_level=result.analysis.risk_level,
            holder_count=result.holders.holder_count if result.holders else None,
            liquidity_usd=result.market_data.liquidity.usd if result.market_data and result.market_data.liquidity else None,
            market_cap=result.market_data.market_cap if result.market_data else None,
            age_hours=result.token_age.age_hours if result.token_age else None,
            age_days=result.token_age.age_days if result.token_age else None,
            top_signal=top_signal,
            flagged_by=result.watchlist_hits,
        )

    def _light_ranked(token: dict, address: str, light) -> RankedToken:
        """Lightweight result for a token the pre-screen skipped (no deep fetches)."""
        return RankedToken(
            contract_address=address,
            name=token.get("name"),
            symbol=token.get("symbol"),
            risk_score=light.risk_score,
            risk_level=light.risk_level,
            holder_count=to_int(token.get("holders_count") or token.get("holders")),
            top_signal="Deep analysis skipped: low-risk on cheap pre-screen (high holder count).",
        )

    async def scan_one(token: dict) -> RankedToken | None:
        address = token.get("address_hash")
        if not address:
            return None
        if not settings.scan_tiering_enabled:
            return await deep_one(token, address)
        # Light tier: holder count from list_tokens only — no extra requests.
        holder_count = to_int(token.get("holders_count") or token.get("holders"))
        light = score_token_light(holder_count)
        # Promote on uncertainty. A token is skipped ONLY when it is confidently
        # low-risk: a KNOWN holder count at/above the floor AND a light score below
        # threshold. Unknown holder count, too few holders, or any light-score hit
        # all promote to deep analysis — so nothing suspicious is ever skipped.
        confidently_safe = (
            holder_count is not None
            and holder_count >= settings.scan_established_holder_floor
            and light.risk_score < settings.scan_light_promote_threshold
        )
        if not confidently_safe:
            return await deep_one(token, address)
        return _light_ranked(token, address, light)

    results = await asyncio.gather(*(scan_one(t) for t in tokens))
    ranked = [r for r in results if r is not None]
    ranked.sort(key=lambda r: r.risk_score, reverse=True)

    return ScanResponse(
        chain=settings.chain_name,
        status="scan_completed",
        message=f"Analyzed and ranked {len(ranked)} Robinhood Chain tokens by rug risk.",
        analyzed=len(ranked),
        ranked_tokens=ranked,
        limitations=LIMITATIONS,
    )
