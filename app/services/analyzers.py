from __future__ import annotations

"""Per-dimension analysis helpers for Robinhood Chain tokens.

Each function turns raw API payloads into a typed model. They are deliberately
pure and defensive so they can be unit tested with mocked payloads and never raise
on partial/missing data.
"""

from datetime import datetime, timezone

from app.core.config import settings
from app.models.token import (
    BundleAnalysis,
    BuyTimingAnalysis,
    ClusterAnalysis,
    DevProfile,
    DevTransfer,
    HolderCluster,
    HolderDistribution,
    HolderEntry,
    LaunchpadInfo,
    LaunchedToken,
    LiquidityLock,
    TokenAge,
    TokenTrend,
)
from app.services import launchpad_registry

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value: object) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


# --- Age ---


def analyze_age(pair_created_at_ms: int | None, contract_created_iso: str | None) -> TokenAge:
    """Derive token age from the DexScreener pair timestamp (preferred) or contract creation."""
    now = datetime.now(timezone.utc)
    created: datetime | None = None
    source: str | None = None

    if pair_created_at_ms:
        try:
            created = datetime.fromtimestamp(pair_created_at_ms / 1000, tz=timezone.utc)
            source = "pair_created_at"
        except (OverflowError, OSError, ValueError):
            created = None

    if created is None and contract_created_iso:
        try:
            created = datetime.fromisoformat(contract_created_iso.replace("Z", "+00:00"))
            source = "contract_creation"
        except ValueError:
            created = None

    if created is None:
        return TokenAge(created_at_iso=None, age_hours=None, age_days=None, source=None)

    delta = now - created
    hours = delta.total_seconds() / 3600
    return TokenAge(
        created_at_iso=created.isoformat(),
        age_hours=round(hours, 2),
        age_days=round(hours / 24, 2),
        source=source,
    )


# --- Holders & distribution ---


def _supply_units(total_supply: object, decimals: object) -> float | None:
    raw = to_float(total_supply)
    dec = to_int(decimals)
    if raw is None or raw <= 0:
        return None
    if dec:
        return raw / (10 ** dec)
    return raw


def analyze_holders(
    holders: list[dict],
    holder_count: int | None,
    total_supply: object,
    decimals: object,
    lp_address: str | None = None,
) -> HolderDistribution:
    """Compute top-holder concentration from a sampled holders page.

    The DEX liquidity pool for a token usually IS the largest holder, but it is not
    a "whale" in the risk sense — it's the market itself. When `lp_address` is
    provided, the pool is pulled out and reported separately so top10/top1 and the
    concentration index reflect real wallets, not the AMM pair.
    """
    supply = _supply_units(total_supply, decimals)
    dec = to_int(decimals) or 0
    lp_addr_l = (lp_address or "").lower()

    entries: list[HolderEntry] = []
    percentages: list[float] = []
    lp_pct: float | None = None
    for item in holders:
        addr_obj = item.get("address") or {}
        addr_hash = addr_obj.get("hash", "")
        raw_value = to_float(item.get("value"))
        pct: float | None = None
        if raw_value is not None and supply:
            units = raw_value / (10 ** dec) if dec else raw_value
            pct = round((units / supply) * 100, 4)
        # Peel the LP pair out so it doesn't skew the "top holder" narrative.
        if lp_addr_l and addr_hash.lower() == lp_addr_l:
            lp_pct = pct
            continue
        if pct is not None:
            percentages.append(pct)
        entries.append(
            HolderEntry(
                address=addr_hash,
                percentage=pct,
                value=item.get("value"),
                is_contract=bool(addr_obj.get("is_contract")),
                label=addr_obj.get("name"),
                is_scam=bool(addr_obj.get("is_scam")),
            )
        )

    top10 = round(sum(sorted(percentages, reverse=True)[:10]), 4) if percentages else None
    top1 = round(max(percentages), 4) if percentages else None
    # Concentration index: share of the sampled percentages held by the top 10%.
    concentration = None
    if percentages:
        ordered = sorted(percentages, reverse=True)
        cutoff = max(1, len(ordered) // 10)
        concentration = round(sum(ordered[:cutoff]) / sum(ordered), 4) if sum(ordered) else None

    return HolderDistribution(
        holder_count=holder_count,
        top10_percentage=top10,
        top1_percentage=top1,
        concentration_index=concentration,
        sampled_holders=len(entries),
        top_holders=entries[:20],
        lp_address=lp_address if lp_pct is not None else None,
        lp_percentage=lp_pct,
    )


# --- Clusters ---


class _UnionFind:
    """Minimal union-find to merge holders linked by any relationship."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def analyze_clusters(
    holder_funders: dict[str, str | None],
    holder_percentages: dict[str, float | None],
    mutual_transfers: list[tuple[str, str]] | None = None,
    funder_chains: dict[str, list[str]] | None = None,
) -> ClusterAnalysis:
    """Group holders that are coordinated, from two independent link types.

    - shared_funder: holders who share a funding wallet. With `funder_chains` (M14)
      the link is multi-hop — two holders funded by the same wallet ANYWHERE along
      their traced chain (funder -> intermediary -> fresh wallet) unify, not just at
      the immediate hop. `holder_funders` (immediate hop) is the single-hop fallback.
    - mutual_transfer: holders who have transferred the token to each other.

    Both are merged with union-find so a wallet linked by either signal lands in
    one cluster. Each cluster is annotated with the link type(s) that formed it.
    """
    # Normalize percentage lookups to lowercase so they match the lowercased
    # holder keys used throughout clustering.
    pct_by_addr = {(a or "").lower(): p for a, p in holder_percentages.items()}
    members_set = set(pct_by_addr)

    # 1) shared-funder links. A chain of length 1 == the prior single-hop behaviour,
    # so single-hop callers (no funder_chains) get identical results.
    chains_by_holder = funder_chains or {
        h: ([f] if f else []) for h, f in holder_funders.items()
    }
    funder_groups: dict[str, list[str]] = {}
    for holder, chain in chains_by_holder.items():
        h = holder.lower()
        for funder in chain:
            if not funder:
                continue
            group = funder_groups.setdefault(funder.lower(), [])
            if h not in group:  # a holder appears once per funder, even across hops
                group.append(h)
            members_set.add(h)

    uf = _UnionFind()
    # Record links per NODE (stable keys), not per root: a later union can change a
    # component's root, which would orphan any root-keyed entry. Resolve to the final
    # root only at collection time, after all unions are done.
    node_link_types: dict[str, set[str]] = {}
    node_funder: dict[str, str] = {}

    for funder, members in funder_groups.items():
        if len(members) < 2:
            continue
        first = members[0]
        for m in members[1:]:
            uf.union(first, m)
        for m in members:
            node_funder[m] = funder
            node_link_types.setdefault(m, set()).add("shared_funder")

    # 2) mutual-transfer links (holder A sent the token to holder B, both sampled)
    holder_pool = set(pct_by_addr)
    for a, b in mutual_transfers or []:
        a, b = a.lower(), b.lower()
        if a in holder_pool and b in holder_pool and a != b:
            uf.union(a, b)
            members_set.update({a, b})
            node_link_types.setdefault(a, set()).add("mutual_transfer")
            node_link_types.setdefault(b, set()).add("mutual_transfer")

    # Collect components with >= 2 members, aggregating the per-node link types and
    # funders up to each component's FINAL root (so a post-union root change can't
    # drop a real cluster).
    comps: dict[str, list[str]] = {}
    root_types: dict[str, set[str]] = {}
    root_funder: dict[str, str] = {}
    for node in members_set:
        root = uf.find(node)
        comps.setdefault(root, []).append(node)
        types = node_link_types.get(node)
        if types:
            root_types.setdefault(root, set()).update(types)
        funder = node_funder.get(node)
        if funder and root not in root_funder:
            root_funder[root] = funder

    clusters: list[HolderCluster] = []
    clustered_pct = 0.0
    for root, members in comps.items():
        # Only keep components actually linked by a signal, with 2+ members.
        types = root_types.get(root, set())
        if len(members) < 2 or not types:
            continue
        combined = round(sum(pct_by_addr.get(m) or 0.0 for m in members), 4)
        clustered_pct += combined
        link_type = "mixed" if len(types) > 1 else next(iter(types))
        clusters.append(
            HolderCluster(
                funder_address=root_funder.get(root),
                member_addresses=sorted(members),
                combined_percentage=combined,
                link_type=link_type,
            )
        )

    clusters.sort(key=lambda c: c.combined_percentage or 0, reverse=True)
    note = None if clusters else "No shared-funder or mutual-transfer clusters detected in the sampled holders."
    return ClusterAnalysis(
        clusters=clusters[:10],
        clustered_percentage=round(clustered_pct, 4) if clusters else 0.0,
        note=note,
    )


def analyze_bundle(
    clusters: ClusterAnalysis,
    creator: str | None = None,
    funder_chains: dict[str, list[str]] | None = None,
    *,
    min_wallets: int | None = None,
) -> BundleAnalysis:
    """Grade the bundler / sybil-launch pattern from already-computed clustering (M14). Pure.

    A bundler funds many fresh wallets from one source so they all buy the same token,
    faking organic distribution. The strongest shared-funder cluster IS the candidate
    bundle; this scores how dangerous it looks. Additive metadata only — it never
    changes the cluster/holder scoring, it summarizes it.

    Score (0-100), each signal additive:
      - bundle size (wallets funded by one source): the core signal.
      - supply concentrated in the bundle: how much of the float it controls.
      - the creator sits on the bundle's funding chain: launch was self-funded/sybil.
    """
    min_wallets = min_wallets or settings.bundler_min_cluster_wallets
    creator_l = (creator or "").lower()

    # The bundle = largest shared-funder (or mixed) cluster. Mutual-transfer-only
    # clusters aren't a funding bundle, so they don't seed one.
    funder_clusters = [c for c in clusters.clusters if c.link_type in ("shared_funder", "mixed")]
    if not funder_clusters:
        return BundleAnalysis(detail="No shared-funder bundle detected in the sampled holders.")

    bundle = max(funder_clusters, key=lambda c: len(c.member_addresses))
    n_wallets = len(bundle.member_addresses)
    bundled_pct = bundle.combined_percentage or 0.0

    if n_wallets < min_wallets:
        return BundleAnalysis(
            bundled_wallets=n_wallets,
            bundled_percentage=bundled_pct,
            top_funder=bundle.funder_address,
            detail=f"Largest shared-funder group has {n_wallets} wallet(s); below the bundler threshold of {min_wallets}.",
        )

    # Did the creator fund this bundle (anywhere along the traced chains of its members)?
    creator_funded = False
    if creator_l and funder_chains:
        member_set = set(bundle.member_addresses)
        for holder, chain in funder_chains.items():
            if holder.lower() in member_set and creator_l in {(f or "").lower() for f in chain}:
                creator_funded = True
                break

    signals: list[str] = []
    score = 0
    # Bundle size: 3 wallets -> 30, saturating so a huge bundle can't alone max the score.
    size_pts = min(45, n_wallets * 10)
    score += size_pts
    signals.append(f"{n_wallets} wallets funded by one source ({bundle.funder_address})")
    # Supply the bundle controls.
    if bundled_pct >= 25:
        score += 35
        signals.append(f"Bundle controls ~{bundled_pct}% of supply")
    elif bundled_pct >= 10:
        score += 20
        signals.append(f"Bundle controls ~{bundled_pct}% of supply")
    elif bundled_pct > 0:
        score += 10
        signals.append(f"Bundle controls ~{bundled_pct}% of supply")
    if creator_funded:
        score += 20
        signals.append("Deployer funds the bundle (self-funded launch)")

    score = min(score, 100)
    classification = (
        "Extreme" if score >= 75 else
        "Heavy" if score >= 50 else
        "Moderate" if score >= 25 else
        "Normal"
    )
    return BundleAnalysis(
        score=score,
        classification=classification,
        bundled_wallets=n_wallets,
        bundled_percentage=bundled_pct,
        top_funder=bundle.funder_address,
        creator_funded_bundle=creator_funded,
        signals=signals,
        detail=f"{classification} bundling: {n_wallets} wallets from one funder hold ~{bundled_pct}% of supply.",
    )


def _parse_ts(value: object) -> float | None:
    """Parse a Blockscout ISO timestamp into a unix float, or None if unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def analyze_buy_timing(
    transfers: list[dict],
    *,
    known_contracts: set[str] | None = None,
    creator: str | None = None,
    window_seconds: int | None = None,
    min_cohort: int | None = None,
) -> BuyTimingAnalysis:
    """Detect same-block / within-seconds-of-launch buy coordination (M15). Pure.

    Wallets that first receive the token in the same block, or within `window_seconds`
    of the very first real buy, are coordinated independent of funding source. Reuses the
    already-normalized transfers (block + ts captured in `normalize_transfers`); no fetch.

    Excludes the mint (from == zero), the creator, and known contracts (LP/router) so a
    single organic buyer or the launch mechanics are never mistaken for a cohort. A cohort
    counts DISTINCT recipient wallets, so one wallet buying repeatedly is not a cohort.
    """
    window = settings.coordinated_buy_window_seconds if window_seconds is None else window_seconds
    min_cohort = settings.coordinated_buy_min_cohort if min_cohort is None else min_cohort
    creator_l = (creator or "").lower()
    skip = {ZERO_ADDRESS, "", creator_l}
    skip.update((c or "").lower() for c in (known_contracts or set()))

    # First real acquisition per wallet, in chronological order (transfers are oldest-first).
    first_seen: dict[str, dict] = {}
    for rec in transfers:
        to = (rec.get("to") or "").lower()
        if to in skip or to in first_seen:
            continue
        first_seen[to] = rec

    if len(first_seen) < min_cohort:
        # Too few distinct buyers to form a cohort (covers single-buyer tokens).
        return BuyTimingAnalysis(detail="Too few distinct buyers to assess buy-timing coordination.")

    # Largest same-block cohort among first acquisitions.
    by_block: dict[int, int] = {}
    for rec in first_seen.values():
        blk = rec.get("block")
        if blk is not None:
            by_block[blk] = by_block.get(blk, 0) + 1
    same_block_number = max(by_block, key=by_block.get) if by_block else None
    same_block_wallets = by_block.get(same_block_number, 0) if same_block_number is not None else 0

    # Buyers landing within `window` seconds of the first real buy.
    stamped = [(_parse_ts(r.get("ts")), a) for a, r in first_seen.items()]
    times = sorted(t for t, _ in stamped if t is not None)
    first_window_wallets = 0
    if times:
        launch = times[0]
        first_window_wallets = sum(1 for t in times if t - launch <= window)

    coordinated = same_block_wallets >= min_cohort or first_window_wallets >= min_cohort
    if coordinated:
        parts = []
        if same_block_wallets >= min_cohort:
            parts.append(f"{same_block_wallets} wallets first bought in block {same_block_number}")
        if first_window_wallets >= min_cohort:
            parts.append(f"{first_window_wallets} wallets bought within {window}s of launch")
        detail = "Coordinated buy timing: " + "; ".join(parts) + "."
    else:
        detail = "No same-block or launch-window buy cohort detected."

    return BuyTimingAnalysis(
        same_block_wallets=same_block_wallets,
        same_block_number=same_block_number,
        first_window_wallets=first_window_wallets,
        coordinated=coordinated,
        detail=detail,
    )


def analyze_trend(
    prior: dict | None,
    *,
    current_liquidity_usd: float | None,
    current_top10_percentage: float | None,
    current_holder_count: int | None,
    current_risk_score: int | None = None,
    liquidity_drop_pct: float | None = None,
    concentration_rise_pct: float | None = None,
) -> TokenTrend:
    """Diff the current analysis against the prior stored snapshot (M19). Pure.

    A single snapshot can't see a *slow rug* — liquidity draining over days or the dev
    quietly accumulating supply. This compares the current metrics to `prior` (the last
    stored snapshot, or None on a token's first-ever analyze) and flags a downward
    liquidity trend and/or a rising-concentration trend when the delta crosses a threshold.

    First sighting (`prior is None`) -> `has_prior=False`, no deltas, no signals, no raise.
    Only adverse moves raise a signal: a liquidity DROP and a concentration RISE. A liquidity
    recovery or a de-concentration is reassuring, not risk, so it never scores.
    """
    if not prior:
        return TokenTrend(has_prior=False, detail="No prior snapshot; first analysis of this token.")

    drop_threshold = settings.snapshot_liquidity_drop_pct if liquidity_drop_pct is None else liquidity_drop_pct
    rise_threshold = settings.snapshot_concentration_rise_pct if concentration_rise_pct is None else concentration_rise_pct

    signals: list[str] = []

    # Liquidity change % (signed; negative = drop).
    liq_change: float | None = None
    prior_liq = prior.get("liquidity_usd")
    if prior_liq is not None and prior_liq > 0 and current_liquidity_usd is not None:
        liq_change = round((current_liquidity_usd - prior_liq) / prior_liq * 100, 2)
        if liq_change <= -drop_threshold:
            signals.append(f"Liquidity fell {abs(liq_change)}% since the previous snapshot")

    # Top-10 concentration change in percentage POINTS (signed; positive = rising).
    conc_change: float | None = None
    prior_top10 = prior.get("top10_percentage")
    if prior_top10 is not None and current_top10_percentage is not None:
        conc_change = round(current_top10_percentage - prior_top10, 2)
        if conc_change >= rise_threshold:
            signals.append(f"Top-10 holder concentration rose {conc_change} points since the previous snapshot")

    holder_change: int | None = None
    prior_holders = prior.get("holder_count")
    if prior_holders is not None and current_holder_count is not None:
        holder_change = current_holder_count - prior_holders

    risk_change: int | None = None
    prior_risk = prior.get("risk_score")
    if prior_risk is not None and current_risk_score is not None:
        risk_change = current_risk_score - prior_risk

    detail = "; ".join(signals) if signals else "No adverse liquidity or concentration trend vs. the previous snapshot."
    return TokenTrend(
        has_prior=True,
        prior_captured_at=prior.get("captured_at"),
        liquidity_change_pct=liq_change,
        concentration_change_pct=conc_change,
        holder_count_change=holder_change,
        risk_score_change=risk_change,
        signals=signals,
        detail=detail,
    )


def extract_mutual_transfers(transfers: list[dict], holders: set[str]) -> list[tuple[str, str]]:
    """From normalized transfers, return (from, to) pairs where both are sampled holders.

    These are direct token movements between two holders of the same token, a strong
    signal of coordinated wallets (e.g. splitting a bag across addresses).
    """
    holder_pool = {h.lower() for h in holders}
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rec in transfers:
        frm, to = rec.get("from"), rec.get("to")
        if not frm or not to or frm == ZERO_ADDRESS or to == ZERO_ADDRESS:
            continue
        if frm in holder_pool and to in holder_pool and frm != to:
            key = tuple(sorted((frm, to)))
            if key not in seen:
                seen.add(key)
                pairs.append((frm, to))
    return pairs


def classify_created_tokens(created: list[dict]) -> list[LaunchedToken]:
    """Turn a list of {address, info, liquidity_usd} into classified LaunchedTokens."""
    result: list[LaunchedToken] = []
    for c in created:
        result.append(
            build_launched_token(
                c.get("address", ""),
                c.get("info"),
                c.get("liquidity_usd"),
            )
        )
    return result


# --- Dev / creator ---


def _classify_launch_outcome(liquidity_usd: float | None) -> str:
    if liquidity_usd is None:
        return "unknown"
    if liquidity_usd < 1_000:
        return "likely_rugged"
    return "alive"


def analyze_dev_transfers(
    transfers: list[dict],
    creator_address: str | None,
    total_supply_units: float | None,
) -> tuple[list[DevTransfer], float]:
    """Find tokens the deployer moved out to other wallets (distribution/dump risk).

    `transfers` are normalized records ({from,to,value,ts}) from wallet_intel.
    Returns (dev_transfers, percentage_of_supply_moved).
    """
    if not creator_address:
        return [], 0.0
    creator = creator_address.lower()
    dev_transfers: list[DevTransfer] = []
    moved_units = 0.0
    for rec in transfers:
        if rec.get("from") != creator:
            continue
        to = rec.get("to")
        if not to or to == ZERO_ADDRESS:
            continue
        value = rec.get("value") or 0.0
        moved_units += value
        pct = None
        if total_supply_units and value:
            pct = round((value / total_supply_units) * 100, 4)
        dev_transfers.append(DevTransfer(to_address=to, amount_percentage=pct, timestamp=rec.get("ts")))

    moved_pct = 0.0
    if total_supply_units and moved_units:
        moved_pct = round((moved_units / total_supply_units) * 100, 4)
    return dev_transfers[:20], moved_pct


def analyze_dev(
    creator_address: str | None,
    creation_tx: str | None,
    dev_holding_percentage: float | None,
    launched_tokens: list[LaunchedToken],
    dev_transfers: list[DevTransfer] | None = None,
    transferred_out_percentage: float | None = None,
) -> DevProfile:
    rugged = sum(1 for t in launched_tokens if t.outcome == "likely_rugged")
    alive = sum(1 for t in launched_tokens if t.outcome == "alive")
    total = len(launched_tokens)

    if total == 0:
        reputation = "unknown"
    elif rugged == 0:
        reputation = "clean"
    elif rugged >= 3 and rugged >= alive:
        reputation = "serial_rugger"
    else:
        reputation = "mixed"

    note = None
    if total == 0:
        note = "Could not confirm other token launches by this deployer from public data."

    dev_transfers = dev_transfers or []
    return DevProfile(
        creator_address=creator_address,
        creation_tx=creation_tx,
        dev_holding_percentage=dev_holding_percentage,
        tokens_launched=total or None,
        tokens_rugged=rugged if total else None,
        tokens_alive=alive if total else None,
        launched_tokens=launched_tokens,
        reputation=reputation,
        transferred_out=bool(dev_transfers),
        transfers_out_count=len(dev_transfers),
        transferred_out_percentage=transferred_out_percentage,
        dev_transfers=dev_transfers,
        note=note,
    )


def build_launched_token(address: str, info: dict | None, market_liquidity: float | None) -> LaunchedToken:
    info = info or {}
    return LaunchedToken(
        address=address,
        name=info.get("name"),
        symbol=info.get("symbol"),
        liquidity_usd=market_liquidity,
        outcome=_classify_launch_outcome(market_liquidity),
    )


# --- Liquidity lock ---


def analyze_liquidity_lock(pair_lp_holders: list[dict], total_lp_supply: object, decimals: object) -> LiquidityLock:
    """Inspect who holds the LP tokens for the pair to decide if liquidity is locked/burned."""
    supply = to_float(total_lp_supply)
    dec = to_int(decimals) or 0

    if not pair_lp_holders:
        return LiquidityLock(
            status="unknown",
            locked_percentage=None,
            locker_label=None,
            detail="Could not read LP token holders for this pair.",
        )

    locked_pct = 0.0
    best_label: str | None = None
    best_address: str | None = None
    for item in pair_lp_holders:
        addr_obj = item.get("address") or {}
        addr = addr_obj.get("hash")
        label = launchpad_registry.locker_label(addr)
        if not label:
            continue
        value = to_float(item.get("value"))
        if value is not None and supply:
            units_pct = (value / supply) * 100
            locked_pct += units_pct
            if best_label is None:
                best_label = label
                best_address = addr

    if best_label and locked_pct > 0:
        status = "burned" if best_label == "Burn address" else "locked"
        return LiquidityLock(
            status=status,
            locked_percentage=round(locked_pct, 2),
            locker_label=best_label,
            locker_address=best_address,
            detail=f"{round(locked_pct, 2)}% of LP tokens held by {best_label}.",
        )

    return LiquidityLock(
        status="unlocked",
        locked_percentage=0.0,
        locker_label=None,
        detail="No known locker or burn address holds a meaningful share of LP tokens.",
    )


def decode_unlock_timestamp(raw: str | None, word_index: int = 0) -> int | None:
    """Decode a locker's unlock timestamp from an `eth_call` return (M13). Pure.

    `raw` is hex-encoded ABI return data (0x + 32-byte words). Reads the word at
    `word_index` as a uint256 unix timestamp. Returns None on any malformed/empty
    input or an implausible value (0 / far-future overflow), so a bad read degrades
    to "no schedule" rather than a fabricated one.
    """
    if not raw or not isinstance(raw, str):
        return None
    hexstr = raw[2:] if raw.startswith(("0x", "0X")) else raw
    if len(hexstr) < (word_index + 1) * 64:
        return None
    word = hexstr[word_index * 64:(word_index + 1) * 64]
    try:
        value = int(word, 16)
    except ValueError:
        return None
    # 0 means "no lock set"; cap at a sane upper bound (year ~5138) to reject garbage.
    if value <= 0 or value > 10**11:
        return None
    return value


def apply_unlock_schedule(
    lock: LiquidityLock, unlock_timestamp: int | None, *, now: datetime | None = None
) -> LiquidityLock:
    """Fold a decoded unlock timestamp into a LiquidityLock (M13). Pure.

    Turns the binary "locked" verdict into a time-aware one: computes the horizon in
    days and appends it to the detail. A lock whose unlock time has already passed is
    downgraded to "unlocked" (the LP is now freely withdrawable). When `unlock_timestamp`
    is None the lock is returned unchanged — presence-only behaviour is preserved.
    """
    if unlock_timestamp is None or lock.status not in ("locked", "burned"):
        return lock
    ref = now or datetime.now(timezone.utc)
    unlock_dt = datetime.fromtimestamp(unlock_timestamp, tz=timezone.utc)
    days = (unlock_dt - ref).total_seconds() / 86400.0
    days = round(days, 2)
    iso = unlock_dt.date().isoformat()
    if days <= 0:
        return lock.model_copy(update={
            "status": "unlocked",
            "unlock_timestamp": unlock_timestamp,
            "unlock_in_days": days,
            "detail": (
                f"{lock.detail} Lock expired on {iso} — LP is now withdrawable."
                if lock.detail else f"Lock expired on {iso} — LP is now withdrawable."
            ),
        })
    horizon = f"unlocks {iso} (~{days:g} days)."
    return lock.model_copy(update={
        "unlock_timestamp": unlock_timestamp,
        "unlock_in_days": days,
        "detail": f"{lock.detail} {horizon}" if lock.detail else horizon.capitalize(),
    })


# --- Launchpad ---


def analyze_launchpad(
    creator_address: str | None,
    contract_name: str | None,
    tags: list[str] | None,
    *,
    creation_factory: str | None = None,
    creation_log_topics: list[str] | None = None,
) -> LaunchpadInfo:
    """Registry-driven launchpad detection.

    On-chain creation evidence (M9) wins when present: a verified factory `to`
    match (HIGH) or a verified factory event in the creation logs (MEDIUM). Falls
    back to the creator/name heuristics (detect_launchpad) otherwise.
    """
    evidence = launchpad_registry.match_creation_evidence(creation_factory, creation_log_topics)
    if evidence:
        name, confidence, detail = evidence
    else:
        name, confidence, detail = launchpad_registry.detect_launchpad(creator_address, contract_name, tags)
    return LaunchpadInfo(name=name, confidence=confidence, detail=detail)
