from __future__ import annotations

"""Per-dimension analysis helpers for Robinhood Chain tokens.

Each function turns raw API payloads into a typed model. They are deliberately
pure and defensive so they can be unit tested with mocked payloads and never raise
on partial/missing data.
"""

from datetime import datetime, timezone

from app.core.config import settings
from app.models.token import (
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
) -> ClusterAnalysis:
    """Group holders that are coordinated, from two independent link types.

    - shared_funder: holders whose first funding wallet is the same.
    - mutual_transfer: holders who have transferred the token to each other.

    Both are merged with union-find so a wallet linked by either signal lands in
    one cluster. Each cluster is annotated with the link type(s) that formed it.
    """
    # Normalize percentage lookups to lowercase so they match the lowercased
    # holder keys used throughout clustering.
    pct_by_addr = {(a or "").lower(): p for a, p in holder_percentages.items()}
    members_set = set(pct_by_addr)

    # 1) shared-funder links
    funder_groups: dict[str, list[str]] = {}
    for holder, funder in holder_funders.items():
        if not funder:
            continue
        funder_groups.setdefault(funder.lower(), []).append(holder.lower())
        members_set.add(holder.lower())

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

    if best_label and locked_pct > 0:
        status = "burned" if best_label == "Burn address" else "locked"
        return LiquidityLock(
            status=status,
            locked_percentage=round(locked_pct, 2),
            locker_label=best_label,
            detail=f"{round(locked_pct, 2)}% of LP tokens held by {best_label}.",
        )

    return LiquidityLock(
        status="unlocked",
        locked_percentage=0.0,
        locker_label=None,
        detail="No known locker or burn address holds a meaningful share of LP tokens.",
    )


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
