"""Developer Network Intelligence — ecosystem-level analysis around a deployer.

Developer Reputation answers: "Is this developer trustworthy?"
Developer Network Intelligence answers: "What ecosystem is this token connected to?"

Given a deployer wallet, discovers all sibling tokens, cross-references holders,
wallets, funding sources, contract templates, and social links to build a network
picture.  Produces a DeveloperNetworkResult (0-100 network score) with evidence.

Provider pattern mirrors developer_reputation.  OnChainNetworkProvider is the only
implementation; future providers (GitHub, ENS, social, KOL) implement the same
Protocol and register in _PROVIDERS.

Cached per deployer address.  Reuses existing Blockscout/DexScreener caches
aggressively — most contract/counter reads are static-cache hits.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol, runtime_checkable

from app.core.config import settings
from app.models.token import (
    DeveloperNetworkResult,
    NetworkSibling,
    TokenAnalysisResponse,
)
from app.services import blockscout_client
from app.services.cache import MISS, TTLCache
from app.services.dexscreener_client import choose_best_pair, fetch_token_pairs

logger = logging.getLogger(__name__)

_cache = TTLCache(
    ttl=settings.deployer_reputation_ttl_hours * 3600,
    max_size=settings.http_cache_max_size,
)

# Cap siblings to avoid excessive API calls for prolific deployers.
_MAX_SIBLINGS = 10


@runtime_checkable
class NetworkProvider(Protocol):
    """Interface for pluggable network data sources."""

    async def gather(
        self, deployer: str, result: TokenAnalysisResponse,
    ) -> dict[str, Any]: ...


class OnChainNetworkProvider:
    """Gather network evidence from Blockscout + DexScreener for sibling tokens."""

    async def gather(
        self, deployer: str, result: TokenAnalysisResponse,
    ) -> dict[str, Any]:
        dev = result.dev
        launched = (dev.launched_tokens if dev else []) or []
        current = result.contract_address.lower()

        # Current token's wallets for cross-referencing
        current_holders: set[str] = set()
        if result.holders and result.holders.top_holders:
            current_holders = {h.address.lower() for h in result.holders.top_holders}
        current_smart = {
            h.address.lower() for h in result.watchlist_hits if h.kind == "smart"
        }
        current_insiders = {i.address.lower() for i in result.insiders}

        # Siblings: all deployed tokens except the current one
        siblings = [t for t in launched if t.address.lower() != current][:_MAX_SIBLINGS]

        async def _fetch_sibling(addr: str) -> dict[str, Any]:
            h_task = blockscout_client.get_token_holders_paged(addr, pages=1)
            p_task = fetch_token_pairs(addr)
            sc_task = blockscout_client.get_smart_contract(addr)
            ct_task = blockscout_client.get_token_counters(addr)
            results = await asyncio.gather(
                h_task, p_task, sc_task, ct_task, return_exceptions=True,
            )
            return {
                "address": addr,
                "holders": results[0] if isinstance(results[0], list) else [],
                "pairs": results[1] if isinstance(results[1], list) else [],
                "contract": results[2] if isinstance(results[2], dict) else None,
                "counters": results[3] if isinstance(results[3], dict) else None,
            }

        sibling_data: list[dict[str, Any]] = []
        if siblings:
            raw = await asyncio.gather(
                *[_fetch_sibling(s.address) for s in siblings],
                return_exceptions=True,
            )
            sibling_data = [r for r in raw if isinstance(r, dict)]

        # Funding wallet from developer reputation (already computed)
        funding_wallet = None
        if result.developer_reputation and result.developer_reputation.funding_source:
            funding_wallet = result.developer_reputation.funding_source

        return {
            "deployer": deployer,
            "sibling_tokens": launched,
            "sibling_data": sibling_data,
            "current_holders": current_holders,
            "current_smart": current_smart,
            "current_insiders": current_insiders,
            "funding_wallet": funding_wallet,
            "current_template": (
                result.contract_intel.template
                if result.contract_intel else None
            ),
            "current_launchpad": (
                result.launchpad.name if result.launchpad else None
            ),
        }


_PROVIDERS: list[NetworkProvider] = [OnChainNetworkProvider()]


def _extract_holder_addr(holder: dict) -> str:
    """Extract address string from a Blockscout holder dict."""
    addr_obj = holder.get("address") or {}
    if isinstance(addr_obj, dict):
        return (addr_obj.get("hash") or "").lower()
    return str(addr_obj).lower()


def _compute_score(
    deployer: str,
    evidence: dict[str, Any],
) -> DeveloperNetworkResult:
    """Pure, deterministic scoring from gathered evidence."""
    sibling_tokens = evidence.get("sibling_tokens", [])
    sibling_data = evidence.get("sibling_data", [])
    current_holders = evidence.get("current_holders", set())
    current_smart = evidence.get("current_smart", set())
    current_insiders = evidence.get("current_insiders", set())
    funding_wallet = evidence.get("funding_wallet")
    current_template = evidence.get("current_template")
    current_launchpad = evidence.get("current_launchpad")
    current = deployer.lower()

    # -- Sibling classification --
    total = len(sibling_tokens)
    alive = sum(1 for t in sibling_tokens if t.outcome == "alive")
    rugged = sum(1 for t in sibling_tokens if t.outcome == "likely_rugged")
    success_rate = round(alive / total, 2) if total > 0 else None
    failure_rate = round(rugged / total, 2) if total > 0 else None

    # -- Per-sibling analysis --
    liquidities: list[float] = []
    holder_counts: list[int] = []
    templates: list[str] = []
    social_urls: dict[str, list[str]] = {}  # url -> [token symbols]
    all_sib_holders: dict[str, int] = {}  # addr -> count of sibling tokens held
    network_siblings: list[NetworkSibling] = []

    # Map sibling address -> sibling data for lookup
    sdata_map: dict[str, dict] = {
        d["address"].lower(): d for d in sibling_data
    }

    for token in sibling_tokens:
        addr_l = token.address.lower()
        if addr_l == deployer.lower():
            continue  # skip if somehow the deployer address is listed
        sdata = sdata_map.get(addr_l, {})

        # Holders for overlap analysis
        sib_holder_addrs: set[str] = set()
        for h in sdata.get("holders", []):
            ha = _extract_holder_addr(h)
            if ha:
                sib_holder_addrs.add(ha)
                all_sib_holders[ha] = all_sib_holders.get(ha, 0) + 1

        shared_with_current = len(current_holders & sib_holder_addrs)

        # Market data from DexScreener
        best = choose_best_pair(sdata.get("pairs") or [])
        liq: float | None = None
        mc: float | None = None
        if best:
            liq_obj = best.get("liquidity") or {}
            liq = _to_float(liq_obj.get("usd"))
            mc = _to_float(best.get("marketCap") or best.get("fdv"))
            # Shared social links
            info = best.get("info") or {}
            sym = token.symbol or token.address[:10]
            for w in info.get("websites") or []:
                url = w.get("url") if isinstance(w, dict) else None
                if url:
                    social_urls.setdefault(url, []).append(sym)
            for s in info.get("socials") or []:
                url = s.get("url") if isinstance(s, dict) else None
                if url:
                    social_urls.setdefault(url, []).append(sym)

        # Fallback liquidity from DevProfile
        if liq is None:
            liq = token.liquidity_usd
        if liq is not None:
            liquidities.append(liq)
        if mc is not None:
            holder_counts  # placeholder; market_cap tracked below

        # Holder count from counters
        counters = sdata.get("counters")
        hc = _to_int(counters.get("token_holders_count")) if counters else None
        if hc is not None:
            holder_counts.append(hc)

        # Contract template
        contract = sdata.get("contract")
        verified = False
        tpl_name: str | None = None
        if contract:
            verified = bool(contract.get("is_verified"))
            tpl_name = contract.get("name") or contract.get("contract_name")
            if tpl_name:
                templates.append(tpl_name)

        # Shared infrastructure for this sibling
        sib_infra: list[str] = []
        if tpl_name and current_template and tpl_name == current_template:
            sib_infra.append(f"Template: {tpl_name}")

        network_siblings.append(NetworkSibling(
            address=token.address,
            name=token.name,
            symbol=token.symbol,
            outcome=token.outcome,
            liquidity_usd=liq,
            holder_count=hc,
            market_cap=mc,
            verified=verified,
            shared_wallets=shared_with_current,
            shared_infrastructure=sib_infra,
        ))

    # -- Aggregate metrics --
    avg_liq = round(sum(liquidities) / len(liquidities), 2) if liquidities else None
    avg_hc = round(sum(holder_counts) / len(holder_counts)) if holder_counts else None

    # Wallet reuse: fraction of holder addresses appearing in 2+ sibling tokens
    reused_count = sum(1 for c in all_sib_holders.values() if c >= 2)
    wallet_reuse = (
        round(reused_count / len(all_sib_holders), 2)
        if all_sib_holders else None
    )

    # Shared wallets: current token's holders/smart/insiders that also appear in siblings
    shared_wallets_set = (current_holders | current_smart | current_insiders) & set(
        all_sib_holders.keys()
    )

    # Infrastructure reuse: template consistency across network
    template_set = set(templates)
    if current_template and current_template not in ("unknown", None):
        template_set.add(current_template)
    # Perfect reuse = all tokens use same template = 1.0
    infra_reuse: float | None = None
    if templates and total > 0:
        most_common_count = max(templates.count(t) for t in template_set) if template_set else 0
        infra_reuse = round(most_common_count / max(total, 1), 2)

    # Shared social links (urls appearing for 2+ tokens)
    shared_urls = {u for u, tokens in social_urls.items() if len(tokens) >= 2}

    # Shared infrastructure summary
    shared_infra_list: list[str] = []
    if template_set and len(template_set) == 1:
        shared_infra_list.append(f"Common template: {next(iter(template_set))}")
    if current_launchpad and current_launchpad != "Unknown":
        shared_infra_list.append(f"Launchpad: {current_launchpad}")
    for url in sorted(shared_urls)[:5]:
        shared_infra_list.append(f"Shared link: {url}")

    # Funding reputation
    funding_rep = "unknown"
    if funding_wallet:
        if rugged == 0 and alive > 0:
            funding_rep = "clean"
        elif rugged > 0 and alive > rugged:
            funding_rep = "mixed"
        elif rugged > alive:
            funding_rep = "rug_linked"

    # Launch consistency: ratio of alive to total (higher = more consistent quality)
    launch_consist: float | None = None
    if total > 0:
        launch_consist = round(alive / total, 2)

    # --- Scoring (baseline 50, additive/subtractive) ---
    score = 50
    lines: list[str] = []

    # Cluster size
    if total >= 5:
        score += 10
        lines.append(f"+ Developer launched {total} tokens (established ecosystem)")
    elif total >= 3:
        score += 5
        lines.append(f"+ Developer launched {total} tokens")
    elif total == 1:
        score -= 3
        lines.append("- Single token ecosystem")
    elif total == 0:
        score -= 10
        lines.append("- No sibling tokens found")

    # Historical success rate
    if success_rate is not None:
        if success_rate >= 0.8 and alive >= 3:
            score += 15
            lines.append(f"+ {alive}/{total} tokens surviving ({int(success_rate * 100)}% success)")
        elif success_rate >= 0.6:
            score += 10
            lines.append(f"+ {alive}/{total} tokens surviving ({int(success_rate * 100)}% success)")
        elif success_rate >= 0.4:
            score += 3
            lines.append(f"+ {alive}/{total} tokens surviving")
        elif success_rate < 0.3 and total >= 3:
            score -= 15
            lines.append(f"- Only {alive}/{total} tokens survived ({int(success_rate * 100)}%)")

    if failure_rate is not None and rugged > 0:
        if rugged >= 5:
            score -= 20
            lines.append(f"- {rugged} rugged/abandoned tokens in network")
        elif rugged >= 3:
            score -= 12
            lines.append(f"- {rugged} rugged/abandoned tokens in network")
        elif rugged >= 1:
            score -= 5
            lines.append(f"- {rugged} rugged/abandoned token(s)")

    # Average liquidity
    if avg_liq is not None:
        if avg_liq >= 10_000:
            score += 8
            lines.append(f"+ Avg network liquidity ${avg_liq:,.0f}")
        elif avg_liq >= 1_000:
            score += 4
            lines.append(f"+ Avg network liquidity ${avg_liq:,.0f}")
        elif avg_liq < 500:
            score -= 5
            lines.append(f"- Low avg network liquidity ${avg_liq:,.0f}")

    # Average holder count
    if avg_hc is not None:
        if avg_hc >= 500:
            score += 8
            lines.append(f"+ Avg {avg_hc} holders across network")
        elif avg_hc >= 100:
            score += 4
            lines.append(f"+ Avg {avg_hc} holders across network")
        elif avg_hc < 20:
            score -= 5
            lines.append(f"- Low avg holder count ({avg_hc})")

    # Holder overlap (shared wallets between current token and siblings)
    shared_wallet_count = len(shared_wallets_set)
    if shared_wallet_count >= 5:
        score += 8
        lines.append(f"+ {shared_wallet_count} wallets shared with sibling tokens")
    elif shared_wallet_count >= 2:
        score += 4
        lines.append(f"+ {shared_wallet_count} wallet(s) shared with sibling tokens")

    # Smart wallets in sibling tokens
    smart_in_siblings = current_smart & set(all_sib_holders.keys())
    if smart_in_siblings:
        score += 5
        lines.append(f"+ {len(smart_in_siblings)} smart wallet(s) also in sibling tokens")

    # Infrastructure reuse
    if infra_reuse is not None and infra_reuse >= 0.8:
        score += 5
        lines.append("+ Consistent contract infrastructure")
    elif infra_reuse is not None and infra_reuse < 0.3 and total >= 3:
        score -= 3
        lines.append("- Inconsistent contract templates across network")

    # Shared social links
    if shared_urls:
        score += 3
        lines.append(f"+ {len(shared_urls)} shared social/website link(s) across tokens")

    # Funding wallet known
    if funding_wallet:
        if funding_rep == "clean":
            score += 5
            lines.append("+ Funding wallet linked to successful launches")
        elif funding_rep == "rug_linked":
            score -= 10
            lines.append("- Funding wallet linked to more rugs than successes")
        elif funding_rep == "mixed":
            score -= 3
            lines.append("- Funding wallet has mixed launch history")

    score = max(0, min(100, score))

    # Confidence based on data completeness
    if total >= 3 and avg_liq is not None and avg_hc is not None:
        confidence = "high"
    elif total >= 1 and (avg_liq is not None or avg_hc is not None):
        confidence = "medium"
    else:
        confidence = "low"

    # Derived sub-scores (0-100 normalized)
    project_quality: float | None = None
    if success_rate is not None:
        pq = success_rate * 50  # base from success rate
        if avg_liq is not None:
            # Add up to 25 from liquidity (log scale, $10k = full)
            import math
            pq += min(25, 25 * math.log10(max(avg_liq, 1)) / math.log10(10_000))
        if avg_hc is not None:
            # Add up to 25 from holders (500 = full)
            pq += min(25, 25 * avg_hc / 500)
        project_quality = round(min(100, max(0, pq)), 1)

    network_risk = round(max(0, min(100, 100 - score)), 1)
    network_trust = round(max(0, min(100, score)), 1)

    return DeveloperNetworkResult(
        score=score,
        cluster_confidence=confidence,
        evidence=lines,
        deployer=deployer,
        funding_wallet=funding_wallet,
        cluster_size=total,
        siblings=network_siblings,
        historical_success_rate=success_rate,
        historical_failure_rate=failure_rate,
        avg_liquidity_usd=avg_liq,
        avg_holder_count=avg_hc,
        wallet_reuse_score=round(wallet_reuse * 100, 1) if wallet_reuse is not None else None,
        infrastructure_reuse_score=(
            round(infra_reuse * 100, 1) if infra_reuse is not None else None
        ),
        funding_reputation=funding_rep,
        launch_consistency=launch_consist,
        project_quality=project_quality,
        network_risk=network_risk,
        network_trust=network_trust,
    )


def _to_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _to_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


async def evaluate(result: TokenAnalysisResponse) -> DeveloperNetworkResult | None:
    """Public interface: TokenAnalysisResponse in, DeveloperNetworkResult out."""
    dev = result.dev
    deployer = dev.creator_address if dev else None
    if not deployer:
        return None

    cache_key = f"dev_net:{deployer.lower()}"
    hit = _cache.get(cache_key)
    if hit is not MISS:
        return hit

    merged: dict[str, Any] = {}
    tasks = [p.gather(deployer, result) for p in _PROVIDERS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, dict):
            merged.update(r)
        elif isinstance(r, Exception):
            logger.warning("Network provider failed for %s: %s", deployer, r)

    net = _compute_score(deployer, merged)
    _cache.set(cache_key, net)
    return net
