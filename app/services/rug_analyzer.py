from __future__ import annotations

import asyncio
import logging
import math

from app.core.config import settings
from app.models.token import (
    DiscoveryDiagnostics,
    EnrichmentField,
    EnrichmentReport,
    LaunchedToken,
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
from app.core import chains
from app.services import alpha_timeline, analyzers, blockscout_client, candidate_discovery, contract_intel, contract_privileges, developer_network, developer_reputation, eligibility, honeypot_sim, launchpad_registry, rpc_client, smart_wallet_reputation, snapshot_store, wallet_intel, watchlist_store
from app.services.analyzers import to_float, to_int
from app.services.dexscreener_client import choose_best_pair, fetch_token_pairs
from app.services.lore_client import build_lore
from app.services.opportunity_score import score_opportunity
from app.services.scoring import LIMITATIONS, score_token, score_token_light

logger = logging.getLogger(__name__)


def _enrichment_status(report: "EnrichmentReport | None") -> str:
    """Summarise enrichment completeness: complete / partial / minimal."""
    if not report:
        return "minimal"
    dc = report.data_confidence
    if dc >= 80:
        return "complete"
    if dc >= 40:
        return "partial"
    return "minimal"


def _build_market_data(pair: dict | None) -> TokenMarketData | None:
    if not pair:
        return None

    base_token = pair.get("baseToken") or {}
    quote_token = pair.get("quoteToken") or {}
    liquidity = pair.get("liquidity") or {}
    volume = pair.get("volume") or {}
    txns_h24 = ((pair.get("txns") or {}).get("h24") or {})
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
        market_cap=mc if (mc := to_float(pair.get("marketCap"))) is not None else to_float(pair.get("fdv")),
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
            buys=txns_h24.get("buys"),
            sells=txns_h24.get("sells"),
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


async def _first_funder(addr: str) -> str | None:
    """The wallet that first sent `addr` native funds (approximates its funder)."""
    txs = await blockscout_client.get_address_transactions(addr)
    incoming = [t for t in txs if ((t.get("to") or {}).get("hash") or "").lower() == addr.lower()]
    if not incoming:
        return None
    earliest = incoming[-1]  # Blockscout returns newest-first.
    funder = (earliest.get("from") or {}).get("hash")
    return funder.lower() if funder else None


async def _trace_funders(holder_addresses: list[str]) -> tuple[dict[str, str | None], dict[str, list[str]]]:
    """Trace each holder's funding chain back up to `funder_max_hops` hops (M14).

    Single-hop only found `holder -> funder`; the sybil-launch pattern is
    `funder -> intermediary -> fresh wallet`, invisible at one hop. Tracing deeper lets
    two holders funded by the same wallet *anywhere* along their chains unify.

    Returns `(immediate_funders, chains)`:
      - immediate_funders: {holder: first-hop funder} — the legacy single-hop map.
      - chains: {holder: [hop1, hop2, ...]} — the funders walked, nearest hop first.

    Bounded and best-effort: hops are capped by config and each newly-seen funder is
    looked up once (memoized), so cost is O(distinct wallets), not O(holders x hops).
    """
    max_hops = max(1, settings.funder_max_hops)
    funder_cache: dict[str, str | None] = {}

    async def cached_funder(addr: str) -> str | None:
        key = addr.lower()
        if key not in funder_cache:
            funder_cache[key] = await _first_funder(key)
        return funder_cache[key]

    async def chain_for(holder: str) -> tuple[str, list[str]]:
        chain: list[str] = []
        seen = {holder.lower()}
        current = holder
        for _ in range(max_hops):
            funder = await cached_funder(current)
            if not funder or funder in seen:  # dead end or a funding loop
                break
            chain.append(funder)
            seen.add(funder)
            current = funder
        return holder, chain

    results = await asyncio.gather(*(chain_for(a) for a in holder_addresses), return_exceptions=True)
    immediate: dict[str, str | None] = {}
    chains: dict[str, list[str]] = {}
    for res in results:
        if isinstance(res, tuple):
            holder, chain = res
            chains[holder] = chain
            immediate[holder] = chain[0] if chain else None
    return immediate, chains


async def _scan_creator_launches(creator: str | None, this_token: str) -> tuple[list, bool]:
    """Find other tokens this deployer created and classify each as alive/rugged.

    M18: returns `(launched_tokens, from_cache)`. A fresh deployer reputation persisted
    within `deployer_reputation_ttl_hours` is reused as-is — the live per-token scan is
    skipped entirely (the expensive part: a creator-tx scan + a token-info/pairs fetch per
    launch). On a miss/stale entry, the live scan runs; the caller persists the result.

    Bounded and best-effort: reads a couple of pages of the creator's transactions,
    picks contract-creation txs, and prices each created token's liquidity via DexScreener.
    """
    if not creator:
        return [], False

    # M18: cache hit -> rebuild the launch history from the store, no live scan.
    cached = watchlist_store.get_deployer(
        creator, max_age_seconds=settings.deployer_reputation_ttl_hours * 3600
    )
    if cached and cached.get("launched_tokens"):
        return [LaunchedToken(**t) for t in cached["launched_tokens"]], True

    try:
        txs = await blockscout_client.get_address_transactions_paged(
            creator, pages=settings.transfer_scan_pages
        )
    except Exception as exc:
        logger.warning("Creator scan failed for %s: %s", creator, exc)
        return [], False

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
    return analyzers.classify_created_tokens(created), False


def _watchlist_hits(holder_addresses: list[str], this_token: str | None = None) -> list[WatchlistHit]:
    """Cross-reference sampled holders against the persisted smart/insider watchlist.

    M17: each hit is enriched with `prior_tokens` — how many OTHER tokens the wallet was
    flagged active on — so a wallet's cross-token reputation surfaces on the next token.
    """
    try:
        known = watchlist_store.known_addresses()
    except Exception as exc:
        logger.warning("Watchlist lookup failed: %s", exc)
        return []
    hit_addrs = [addr for addr in holder_addresses if known.get(addr.lower())]
    try:
        prior = watchlist_store.prior_token_counts(hit_addrs, exclude_token=this_token)
    except Exception as exc:  # cross-token memory is best-effort; never break analysis
        logger.warning("Prior-token lookup failed: %s", exc)
        prior = {}
    hits: list[WatchlistHit] = []
    for addr in hit_addrs:
        info = known[addr.lower()]
        hits.append(
            WatchlistHit(
                address=addr,
                kind=info["kind"],
                proxy_score=info.get("proxy_score"),
                prior_tokens=prior.get(addr.lower(), 0),
            )
        )
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
    # M12: page the holders endpoint (bounded) so concentration/clusters see more than
    # ~50 rows, and read /counters for the true holder count (holders_count from the token
    # payload can be a sampled/partial figure).
    holders_task = blockscout_client.get_token_holders_paged(normalized, pages=settings.holder_scan_pages)
    counters_task = blockscout_client.get_token_counters(normalized)
    # Fetch the verified contract payload once; both source-intel (M9) and privilege
    # reads (M11) derive from it, so no second Blockscout request fires.
    contract_task = blockscout_client.get_smart_contract(normalized)

    pairs, token_info, address_info, holders_raw, counters, contract_payload = await asyncio.gather(
        pairs_task, token_info_task, address_info_task, holders_task, counters_task, contract_task
    )
    ctr_intel = contract_intel.infer_from_contract(contract_payload)

    best_pair = choose_best_pair(pairs)
    market_data = _build_market_data(best_pair)

    if market_data:
        _missing = [f for f, v in [
            ("market_cap", market_data.market_cap),
            ("fdv", market_data.fdv),
            ("price_usd", market_data.price_usd),
            ("volume_h24", market_data.volume.h24 if market_data.volume else None),
            ("price_change_h24", market_data.price_change.h24 if market_data.price_change else None),
            ("liquidity_usd", market_data.liquidity.usd if market_data.liquidity else None),
            ("buys", market_data.volume.buys if market_data.volume else None),
            ("sells", market_data.volume.sells if market_data.volume else None),
        ] if v is None]
        if _missing:
            logger.debug("market_data missing fields for %s: %s", normalized, _missing)

    data_sources: list[str] = ["DexScreener"] if market_data else []
    if token_info or address_info or holders_raw:
        data_sources.append("Blockscout (Robinhood Chain)")

    # Kick off the independent tail stages NOW so their I/O overlaps the sequential
    # on-chain work below (transfers -> funder trace -> creator scan). Each depends
    # only on data already fetched above, computes nothing the later stages feed, and
    # is awaited at its original position — so output is identical, just concurrent:
    #   - lore: slow DuckDuckGo web search (only when include_lore)
    #   - honeypot: sell-tax simulation over the already-chosen market pair
    #   - privileges: owner()/paused() reads over the already-fetched contract payload
    lore_task = None
    if include_lore:
        _lore_name = (token_info or {}).get("name") or (market_data.base_token_name if market_data else None)
        _lore_symbol = (token_info or {}).get("symbol") or (market_data.base_token_symbol if market_data else None)
        lore_task = asyncio.ensure_future(
            build_lore(
                _lore_name,
                _lore_symbol,
                market_data.socials if market_data else [],
                market_data.websites if market_data else [],
            )
        )
    honeypot_task = asyncio.ensure_future(honeypot_sim.simulate(normalized, market_data))
    privileges_task = asyncio.ensure_future(
        contract_privileges.fetch_privileges(normalized, contract_payload)
    )

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
    # M12: prefer the /counters holder count (true total); fall back to the token payload.
    holder_count = to_int((counters or {}).get("token_holders_count")) or to_int(
        (token_info or {}).get("holders_count")
    )
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
    funders, funder_chains = (
        await _trace_funders(cluster_addresses) if cluster_addresses else ({}, {})
    )
    holder_pcts = {e.address: e.percentage for e in holder_distribution.top_holders}
    sampled_holder_set = {e.address for e in holder_distribution.top_holders if e.address}
    mutual = analyzers.extract_mutual_transfers(transfers, sampled_holder_set)
    clusters = analyzers.analyze_clusters(
        funders, holder_pcts, mutual_transfers=mutual, funder_chains=funder_chains
    )
    # M14: grade the bundler / sybil-launch pattern from the clustering just computed
    # (additive metadata; does not alter cluster/holder scoring). creator is resolved above.
    bundle = analyzers.analyze_bundle(clusters, creator, funder_chains)

    # Dev / creator: holdings, outgoing transfers, and prior launches.
    supply_units = analyzers._supply_units(total_supply, decimals)
    dev_holding = _dev_holding_pct(creator, holder_distribution)
    dev_transfers, dev_moved_pct = analyzers.analyze_dev_transfers(transfers, creator, supply_units)
    launched_tokens, dev_from_cache = await _scan_creator_launches(creator, normalized)
    dev = analyzers.analyze_dev(
        creator,
        creation_tx,
        dev_holding,
        launched_tokens=launched_tokens,
        dev_transfers=dev_transfers,
        transferred_out_percentage=dev_moved_pct or None,
    )
    # M18: persist a freshly-scanned deployer with real launch history so a serial
    # rugger stays flagged cheaply and the next analyze skips the live scan within TTL.
    # Only persist non-cache, non-empty results: an empty scan is ambiguous (no launches
    # vs. a transient fetch failure), so caching it would freeze a transient failure.
    if creator and not dev_from_cache and launched_tokens:
        watchlist_store.upsert_deployer(
            creator,
            reputation=dev.reputation or "unknown",
            tokens_launched=dev.tokens_launched,
            tokens_rugged=dev.tokens_rugged,
            tokens_alive=dev.tokens_alive,
            launched_tokens=[t.model_dump() for t in launched_tokens],
        )

    # Wallet intelligence: insiders + smart-wallet proxies (persists to watchlist).
    # Known contracts (LP pair + any sampled holder flagged is_contract) are excluded
    # from insider detection so the AMM pair is not mislabeled "buyer #1". Built from
    # data already on hand — no extra API calls.
    known_contracts = {e.address.lower() for e in holder_distribution.top_holders if e.is_contract and e.address}
    if lp_addr:
        known_contracts.add(lp_addr.lower())
    # M15: same-block / within-seconds-of-launch buy coordination, from the transfers
    # already fetched (no extra call). Excludes mint/creator/LP/contracts so a normal
    # launch is not read as a cohort. Additive metadata, complements funder clusters.
    buy_timing = analyzers.analyze_buy_timing(transfers, creator=creator, known_contracts=known_contracts)
    insiders, _smart = await wallet_intel.profile_token_wallets(
        normalized,
        creator,
        holder_pcts,
        symbol=(token_info or {}).get("symbol"),
        transfers=transfers,  # reuse the already-fetched transfers; no second network call
        known_contracts=known_contracts,
    )
    watchlist_hits = _watchlist_hits(list(sampled_holder_set), this_token=normalized)

    # Liquidity lock: inspect LP token holders of the pair.
    liquidity_lock = None
    if best_pair and best_pair.get("pairAddress"):
        # lp_addr was already resolved above (holders/known-contracts use it); reuse it.
        lp_info, lp_holders = await asyncio.gather(
            blockscout_client.get_token_info(lp_addr),
            blockscout_client.get_token_holders(lp_addr, settings.holder_sample_size),
        )
        liquidity_lock = analyzers.analyze_liquidity_lock(
            lp_holders, (lp_info or {}).get("total_supply"), (lp_info or {}).get("decimals")
        )
        # M13: if a registry-verified locker holds the LP and declares how to read its
        # unlock time, do one eth_call and fold the schedule in. Burn addresses and
        # spec-less lockers return no spec, so this is a no-op there (presence-only).
        unlock_spec = launchpad_registry.locker_unlock_spec(liquidity_lock.locker_address)
        if unlock_spec:
            raw = await rpc_client.eth_call(liquidity_lock.locker_address, unlock_spec["selector"])
            unlock_ts = analyzers.decode_unlock_timestamp(raw, unlock_spec["word_index"])
            liquidity_lock = analyzers.apply_unlock_schedule(liquidity_lock, unlock_ts)

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

    # Lore (started earlier, overlapping the on-chain work; awaited here so the
    # response assembles in the same order as before).
    lore: TokenLore | None = None
    if lore_task is not None:
        lore = await lore_task
        if lore.sources:
            data_sources.append("Web search (DuckDuckGo)")

    # M10: honeypot / sell-tax simulation (started earlier). Reuses the already-fetched
    # market pair (no extra discovery calls); inert unless a router is mapped for this
    # DEX. Its own module caches an executed verdict, so this stays one sim per analyze.
    honeypot = await honeypot_task

    # M11: live contract-privilege / authority reads (started earlier). Reuses the
    # already-fetched verified contract payload (no extra Blockscout call) and fires at
    # most two eth_calls for owner()/paused(). Unverified/no-ABI contracts degrade to
    # analyzed=False (never a false clean); a confirmed renounce is what silences the
    # retained-power signals.
    privileges = await privileges_task

    # M19: read the prior snapshot and diff it BEFORE scoring, so a slow-rug trend
    # (liquidity draining, concentration rising) can feed a risk signal. The metrics it
    # diffs (liquidity, top-10 %, holder count) are all already computed above — no extra
    # fetch. First-ever analyze has no prior -> has_prior=False, no trend signal.
    trend = None
    if settings.snapshot_enabled:
        cur_liq = market_data.liquidity.usd if market_data and market_data.liquidity else None
        prior_snapshot = snapshot_store.latest_snapshot(normalized)
        trend = analyzers.analyze_trend(
            prior_snapshot,
            current_liquidity_usd=cur_liq,
            current_top10_percentage=holder_distribution.top10_percentage,
            current_holder_count=holder_distribution.holder_count,
        )

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
        honeypot=honeypot,
        privileges=privileges,
        bundle=bundle,
        buy_timing=buy_timing,
        watchlist_hits=watchlist_hits,
        trend=trend,
    )

    # M19: persist this analysis as the new snapshot (after scoring, so the final
    # risk_score is captured). Bounded by snapshot_history_retain. Best-effort.
    if settings.snapshot_enabled:
        cur_liq = market_data.liquidity.usd if market_data and market_data.liquidity else None
        snapshot_store.record_snapshot(
            normalized,
            risk_score=analysis.risk_score,
            liquidity_usd=cur_liq,
            top10_percentage=holder_distribution.top10_percentage,
            holder_count=holder_distribution.holder_count,
        )

    # ── Build enrichment report ──────────────────────────────────────
    enrichment = EnrichmentReport()

    if market_data and market_data.pair_address:
        enrichment.pair = EnrichmentField(status="known", source="dexscreener", confidence="high")
    elif pairs is not None:
        enrichment.pair = EnrichmentField(status="unknown", source="dexscreener")
    # else: not_analysed (default)

    if market_data and market_data.price_usd:
        enrichment.price = EnrichmentField(status="known", source="dexscreener", confidence="high")
    elif market_data:
        enrichment.price = EnrichmentField(status="unknown", source="dexscreener")

    if market_data and market_data.liquidity and market_data.liquidity.usd is not None:
        enrichment.liquidity = EnrichmentField(status="known", source="dexscreener", confidence="high")
    elif market_data:
        enrichment.liquidity = EnrichmentField(status="unknown", source="dexscreener")

    if market_data and market_data.fdv is not None:
        enrichment.fdv = EnrichmentField(status="known", source="dexscreener", confidence="high")
    elif market_data:
        enrichment.fdv = EnrichmentField(status="unknown", source="dexscreener")

    if market_data and market_data.market_cap is not None:
        enrichment.market_cap = EnrichmentField(status="known", source="dexscreener", confidence="high")
    elif market_data:
        enrichment.market_cap = EnrichmentField(status="unknown", source="dexscreener")

    if market_data and market_data.volume and market_data.volume.h24 is not None:
        enrichment.volume_h24 = EnrichmentField(status="known", source="dexscreener", confidence="high")
    elif market_data:
        enrichment.volume_h24 = EnrichmentField(status="unknown", source="dexscreener")

    if holder_distribution and holder_distribution.holder_count:
        enrichment.holders = EnrichmentField(status="known", source="blockscout", confidence="high")
    elif holders_raw is not None:
        enrichment.holders = EnrichmentField(status="unknown", source="blockscout")

    if ctr_intel and ctr_intel.verified:
        enrichment.verification = EnrichmentField(status="known", source="blockscout", confidence="high")
    elif contract_payload is not None:
        enrichment.verification = EnrichmentField(status="known", source="blockscout", confidence="medium")
    # else: not_analysed

    enrichment.launchpad = EnrichmentField(
        status="known" if launchpad and launchpad.name else "unknown",
        source="on_chain",
    )

    enrichment.smart_wallets = EnrichmentField(
        status="known", source="watchlist", confidence="medium",
    )

    enrichment.compute_data_confidence()

    result = TokenAnalysisResponse(
        contract_address=normalized,
        chain=chains.active().chain_name,
        status="analysis_completed",
        message="Rug-risk analysis completed for Robinhood Chain token using free public data sources.",
        token_age=age,
        market_data=market_data,
        holders=holder_distribution,
        clusters=clusters,
        dev=dev,
        liquidity_lock=liquidity_lock,
        launchpad=launchpad,
        honeypot=honeypot,
        lore=lore,
        insiders=insiders,
        watchlist_hits=watchlist_hits,
        analysis=analysis,
        contract_intel=ctr_intel,
        contract_privileges=privileges,
        bundle=bundle,
        buy_timing=buy_timing,
        trend=trend,
        enrichment=enrichment,
    )

    result.developer_reputation = await developer_reputation.evaluate(result)

    # Developer network intelligence: ecosystem-level analysis
    result.developer_network = await developer_network.evaluate(result)

    # Update enrichment with dev data now that it's available.
    if enrichment:
        if result.developer_reputation:
            enrichment.developer = EnrichmentField(status="known", source="on_chain", confidence="high")
        else:
            enrichment.developer = EnrichmentField(status="unknown", source="on_chain")
        enrichment.compute_data_confidence()

    # Evaluate smart wallet reputations for watchlist hits
    smart_hits = [h for h in watchlist_hits if h.kind == "smart"]
    if smart_hits:
        rep_tasks = [smart_wallet_reputation.evaluate(h.address) for h in smart_hits]
        result.wallet_reputations = await asyncio.gather(*rep_tasks)

    # Alpha Timeline: convert all analysis outputs into a chronological story.
    result.timeline = alpha_timeline.build_timeline(result)

    # Opportunity Score: compute from analysis outputs (single source of truth).
    opp = score_opportunity(result)
    result.alpha_score = opp.alpha_score
    result.alpha_level = opp.alpha_level
    result.alpha_signals = opp.signals

    return result


def _pair_age_ms(_created_ms: int) -> tuple[int, int]:
    """Return (created_ms, now_ms). Split out so `now` can be stubbed in tests."""
    import time

    return _created_ms, int(time.time() * 1000)


async def scan_and_rank(limit: int, include_lore: bool = False) -> ScanResponse:
    """Pull recent Robinhood Chain token launches, analyze each, and rank by risk score."""
    limit = min(limit, settings.scan_max_tokens)

    # D2: multi-source candidate discovery with diagnostics.
    discovered, discovery_diag = await candidate_discovery.discover_candidates(limit)

    # Convert DiscoveredCandidate -> dict for scan_one (keeps existing deep_one interface)
    tokens = [
        {
            "address_hash": c.address_hash,
            "name": c.name,
            "symbol": c.symbol,
            "holders_count": c.holder_count,
            "source": c.source,
        }
        for c in discovered
    ]

    if not tokens:
        return ScanResponse(
            chain=chains.active().chain_name,
            status="no_tokens",
            message="No recent launches found within the configured window.",
            analyzed=0,
            ranked_tokens=[],
            limitations=LIMITATIONS,
            discovery=discovery_diag,
        )

    # Bound concurrent deep analyses so escalation cannot exhaust the API budget.
    deep_sem = asyncio.Semaphore(max(1, settings.scan_max_deep_analyses))
    _hist: dict[str, int] = {
        "age_too_old": 0, "no_dex_pair": 0, "timeout": 0,
        "no_liquidity": 0, "no_market_data": 0, "api_failure": 0,
        "other_exclusion": 0, "passed": 0,
    }

    async def deep_one(token: dict, address: str) -> RankedToken | None:
        logger.info(
            "AUDIT_ENTRY contract=%s symbol=%s source=%s holders=%s",
            address, token.get("symbol"), token.get("source"), token.get("holders_count"),
        )
        async with deep_sem:
            try:
                result = await analyze_token_contract(address, include_lore=include_lore)
            except Exception as exc:
                _err = str(exc).lower()
                _bucket = "timeout" if ("timeout" in _err or "timed out" in _err) else "api_failure"
                _hist[_bucket] += 1
                logger.info("AUDIT_REJECT contract=%s bucket=%s error=%s", address, _bucket, exc)
                return None
        _md = result.market_data
        _age = result.token_age
        logger.info(
            "AUDIT_STEP contract=%s has_pair=%s pair_addr=%s age_days=%s liq_usd=%s vol_h24=%s mc=%s quote=%s",
            address,
            bool(_md and _md.pair_address),
            _md.pair_address if _md else None,
            _age.age_days if _age else None,
            _md.liquidity.usd if _md and _md.liquidity else None,
            _md.volume.h24 if _md and _md.volume else None,
            _md.market_cap if _md else None,
            _md.quote_token_symbol if _md else None,
        )
        top_signal = max(result.analysis.signals, key=lambda s: s.points).name if result.analysis.signals else None
        opp = score_opportunity(result)
        qual = eligibility.evaluate(result)
        is_excluded = qual.qualification_level == "excluded"

        # ── AUDIT: classify rejection reason ──────────────────────────────
        if is_excluded:
            _reasons = " | ".join(qual.rejection_reasons) if qual.rejection_reasons else "unknown"
            _rl = _reasons.lower()
            if "no market data" in _rl:
                _hist["no_market_data"] += 1
            elif "zero liquidity" in _rl:
                _hist["no_liquidity"] += 1
            elif "honeypot" in _rl or "sell tax" in _rl:
                _hist["other_exclusion"] += 1
            elif "risk score" in _rl:
                _hist["other_exclusion"] += 1
            else:
                _hist["other_exclusion"] += 1
            logger.info("AUDIT_REJECT contract=%s bucket=excluded reasons=%s", address, _reasons)
        else:
            _hist["passed"] += 1
            logger.info("AUDIT_PASS contract=%s qual=%s confidence=%s", address, qual.qualification_level, qual.confidence_score)
        # ── END AUDIT ─────────────────────────────────────────────────────

        lock = result.liquidity_lock

        # Dimension scores — every token gets scored, even excluded ones.
        risk = result.analysis.risk_score
        security = max(0, 100 - risk)

        liq_usd = result.market_data.liquidity.usd if result.market_data and result.market_data.liquidity else None
        if liq_usd is not None and liq_usd > 0:
            liquidity_s: int | None = min(100, int(math.log10(max(liq_usd, 1)) / math.log10(100_000) * 100))
        elif liq_usd is not None:
            liquidity_s = 0  # Known zero liquidity
        else:
            liquidity_s = None  # Unknown — not penalised

        dev_rep = result.developer_reputation
        dev_rep_s: int | None = max(0, min(100, dev_rep.score)) if dev_rep else None

        dev_net = result.developer_network
        dev_net_s: int | None = max(0, min(100, dev_net.score)) if dev_net else None

        smart_count = sum(1 for h in result.watchlist_hits if h.kind == "smart")
        smart_s = min(100, smart_count * 25)

        if result.holders and result.holders.top10_percentage is not None:
            top10 = result.holders.top10_percentage
            holder_q: int | None = max(0, min(100, round(100 - top10)))
        else:
            holder_q = None  # Unknown — not penalised

        vol = result.market_data.volume.h24 if result.market_data and result.market_data.volume else None
        pc = result.market_data.price_change.h24 if result.market_data and result.market_data.price_change else None
        if vol is not None or pc is not None:
            momentum: int | None = 0
            if vol and vol > 0:
                momentum = min(50, int(math.log10(max(vol, 1)) * 10))
            if pc is not None and pc > 0:
                momentum = min(100, momentum + min(50, int(pc)))
        else:
            momentum = None  # No market activity data at all

        # Composite — weighted average of known dimensions only (None = unknown, skipped).
        w = settings.ranking_weights
        parts = [
            ("opportunity", opp.alpha_score),
            ("security", security),
            ("liquidity", liquidity_s),
            ("dev_reputation", dev_rep_s),
            ("dev_network", dev_net_s),
            ("smart_wallet", smart_s),
            ("confidence", qual.confidence_score),
        ]
        known_parts = [(k, v) for k, v in parts if v is not None]
        total_w = sum(w.get(k, 0) for k, _ in known_parts)
        composite: int | None = int(sum(v * w.get(k, 0) for k, v in known_parts) / total_w) if total_w > 0 else None
        if composite is not None:
            composite = max(0, min(100, composite))

        return RankedToken(
            contract_address=address,
            name=token.get("name"),
            symbol=token.get("symbol"),
            risk_score=risk,
            risk_level=result.analysis.risk_level,
            holder_count=result.holders.holder_count if result.holders else None,
            liquidity_usd=liq_usd,
            market_cap=result.market_data.market_cap if result.market_data else None,
            fdv=result.market_data.fdv if result.market_data else None,
            volume_h24=vol,
            price_usd=result.market_data.price_usd if result.market_data else None,
            price_change_h24=pc,
            age_hours=result.token_age.age_hours if result.token_age else None,
            age_days=result.token_age.age_days if result.token_age else None,
            top_signal=top_signal,
            flagged_by=result.watchlist_hits,
            alpha_score=opp.alpha_score,
            alpha_level=opp.alpha_level,
            alpha_signals=opp.signals,
            qualification_level=qual.qualification_level,
            confidence_score=qual.confidence_score,
            eligible=not is_excluded,
            excluded_from_ranking=is_excluded,
            rejection_reasons=qual.rejection_reasons,
            eligibility_evidence=qual.evidence,
            eligibility_warnings=qual.warnings,
            security_score=security,
            liquidity_score=liquidity_s,
            dev_reputation_score=dev_rep_s,
            dev_network_score=dev_net_s,
            smart_wallet_score=smart_s,
            holder_quality_score=holder_q,
            momentum_score=momentum,
            composite_score=composite,
            lock_status=lock.status if lock else "unknown",
            lock_percentage=lock.locked_percentage if lock else None,
            lock_provider=lock.locker_label if lock else None,
            data_confidence=result.enrichment.data_confidence if result.enrichment else None,
            enrichment_status=_enrichment_status(result.enrichment) if result.enrichment else None,
        )

    def _light_ranked(token: dict, address: str, light) -> RankedToken:
        """Lightweight result for a token the pre-screen skipped (no deep fetches)."""
        security = max(0, 100 - light.risk_score)
        hc = to_int(token.get("holders_count") or token.get("holders"))
        holder_q = max(0, min(100, 100 - 30)) if hc and hc >= 500 else None  # conservative estimate
        return RankedToken(
            contract_address=address,
            name=token.get("name"),
            symbol=token.get("symbol"),
            risk_score=light.risk_score,
            risk_level=light.risk_level,
            holder_count=hc,
            top_signal="Deep analysis skipped: low-risk on cheap pre-screen (high holder count).",
            alpha_score=0,
            alpha_level="low",
            qualification_level="good",
            confidence_score=60,
            security_score=security,
            holder_quality_score=holder_q,
            composite_score=security,
            eligibility_evidence=["High holder count", "Low risk on pre-screen"],
            eligibility_warnings=["Deep analysis skipped"],
        )

    async def scan_one(token: dict) -> RankedToken | None:
        address = token.get("address_hash")
        if not address:
            return None
        if not settings.scan_tiering_enabled:
            return await deep_one(token, address)
        holder_count = to_int(token.get("holders_count") or token.get("holders"))
        light = score_token_light(holder_count)
        confidently_safe = (
            holder_count is not None
            and holder_count >= settings.scan_established_holder_floor
            and light.risk_score < settings.scan_light_promote_threshold
        )
        if not confidently_safe:
            return await deep_one(token, address)
        return _light_ranked(token, address, light)

    results = await asyncio.gather(*(scan_one(t) for t in tokens))

    # ── AUDIT: enrichment rejection histogram ─────────────────────────
    logger.info("=" * 72)
    logger.info("ENRICHMENT AUDIT HISTOGRAM  (candidates entered: %d)", len(tokens))
    for _k, _v in _hist.items():
        logger.info("  %-22s : %d", _k, _v)
    _accounted = sum(_hist.values())
    logger.info("  %-22s : %d", "TOTAL accounted", _accounted)
    logger.info("  %-22s : %d", "scan_one returned None", sum(1 for r in results if r is None))
    logger.info("=" * 72)
    # ── END AUDIT ─────────────────────────────────────────────────────

    all_tokens = [r for r in results if r is not None]
    ranked = [r for r in all_tokens if r.qualification_level != "excluded"]
    excluded = [r for r in all_tokens if r.qualification_level == "excluded"]
    ranked.sort(key=lambda r: (-(r.composite_score or 0), -(r.alpha_score or 0), r.risk_score))

    # Finalize diagnostics
    discovery_diag.reached_qualification = len(all_tokens)
    discovery_diag.reached_ranking = len(ranked)
    discovery_diag.excluded = len(excluded)

    return ScanResponse(
        chain=chains.active().chain_name,
        status="scan_completed",
        message=f"Analyzed and ranked {len(ranked)} Robinhood Chain tokens by rug risk.",
        analyzed=len(all_tokens),
        ranked_tokens=ranked,
        excluded_tokens=excluded,
        limitations=LIMITATIONS,
        discovery=discovery_diag,
    )
