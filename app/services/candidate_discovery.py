"""Multi-source candidate discovery pipeline (D2).

Gathers token candidates from multiple on-chain and off-chain sources, merges
them into a deduplicated pool, filters by age/liquidity/established-token
checks, and returns a list ready for deep analysis + DexScreener enrichment.

Architecture:
    Each *provider* is an async function ``() -> list[RawCandidate]`` registered
    in ``_PROVIDERS``.  ``discover_candidates`` runs all enabled providers
    concurrently, merges, deduplicates by lowercased address, applies the shared
    filter chain, and returns ``(candidates, diagnostics)``.

    DexScreener is deliberately NOT a discovery provider.  It is called AFTER
    discovery to enrich each candidate with market data (price, liquidity,
    volume, pair metadata).  This separation means discovery scales with on-chain
    sources while enrichment stays a bounded fan-out over accepted candidates.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from app.core.config import settings
from app.models.token import DiscoveryDiagnostics, SourceDiagnostic, is_valid_address
from app.services import blockscout_client, launchpad_registry
from app.services.analyzers import to_int
from app.services.dexscreener_client import fetch_token_pairs, choose_best_pair

logger = logging.getLogger(__name__)


@dataclass
class RawCandidate:
    address: str
    name: str | None = None
    symbol: str | None = None
    source: str = ""
    holder_count: int | None = None


@dataclass
class DiscoveredCandidate:
    """Candidate ready for deep analysis, enriched with optional market data."""
    address_hash: str
    name: str | None = None
    symbol: str | None = None
    source: str = ""
    holder_count: int | None = None
    pair: dict | None = None


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

async def _blockscout_tokens_provider() -> list[RawCandidate]:
    """Discover candidates from Blockscout ERC-20 tokens (market cap desc)."""
    pages = settings.discovery_blockscout_tokens_pages
    items = await blockscout_client.list_new_tokens(pages=pages)
    out: list[RawCandidate] = []
    for item in items:
        addr = item.get("address_hash") or (item.get("address") or {}).get("hash")
        if not addr:
            continue
        out.append(RawCandidate(
            address=addr,
            name=item.get("name"),
            symbol=item.get("symbol"),
            source="blockscout_tokens",
            holder_count=to_int(item.get("holders_count")),
        ))
    return out


async def _blockscout_contracts_provider() -> list[RawCandidate]:
    """Discover candidates from Blockscout ERC-20 tokens sorted by fiat value (price).

    Catches actively-traded tokens that the market-cap sort may rank differently.
    Unlike the old verified-contracts provider, this only returns actual ERC-20 tokens
    (not UniswapV3Pool, BeaconProxy, etc.) so every candidate is investable.
    """
    pages = settings.discovery_blockscout_contracts_pages
    items = await blockscout_client.list_tokens_by(sort="fiat_value", order="desc", pages=pages)
    out: list[RawCandidate] = []
    for item in items:
        addr = item.get("address_hash") or (item.get("address") or {}).get("hash")
        if not addr:
            continue
        out.append(RawCandidate(
            address=addr,
            name=item.get("name"),
            symbol=item.get("symbol"),
            source="blockscout_fiat_value",
            holder_count=to_int(item.get("holders_count")),
        ))
    return out


async def _launchpad_factory_provider() -> list[RawCandidate]:
    """Discover candidates from configured launchpads via the plugin engine.

    Delegates to ``launchpad_discovery.discover_all``: the engine iterates every
    enabled ``LaunchpadDefinition`` and dispatches by ``discovery_mode`` to a
    registered strategy (event / factory_scan / contract_creation_scan). Adding a
    new launchpad is a config-only change; the engine has no launchpad-specific
    branches.
    """
    from app.services import launchpad_discovery

    definitions = launchpad_registry.get_launchpad_definitions()
    raws = await launchpad_discovery.discover_all(definitions)

    out: list[RawCandidate] = []
    for r in raws:
        addr = r.get("address")
        if not addr:
            continue
        out.append(RawCandidate(
            address=addr,
            name=None,
            symbol=None,
            source=r.get("source", "launchpad:unknown"),
        ))
    return out[:settings.discovery_per_provider_limit]


async def _blockscout_holders_provider() -> list[RawCandidate]:
    """Discover candidates from Blockscout ERC-20 tokens sorted by holder count.

    Surfaces tokens with real adoption that market-cap or fiat-value sorts
    may rank differently. Chain-native: only returns tokens on Robinhood Chain.
    """
    pages = settings.discovery_blockscout_contracts_pages
    items = await blockscout_client.list_tokens_by(sort="holders_count", order="desc", pages=pages)
    out: list[RawCandidate] = []
    for item in items:
        addr = item.get("address_hash") or (item.get("address") or {}).get("hash")
        if not addr:
            continue
        out.append(RawCandidate(
            address=addr,
            name=item.get("name"),
            symbol=item.get("symbol"),
            source="blockscout_holders",
            holder_count=to_int(item.get("holders_count")),
        ))
    return out


# Provider registry: name -> (enabled_check, provider_fn)
_PROVIDERS: list[tuple[str, callable, callable]] = [
    ("blockscout_tokens", lambda: settings.discovery_blockscout_tokens_enabled, _blockscout_tokens_provider),
    ("blockscout_fiat_value", lambda: settings.discovery_blockscout_contracts_enabled, _blockscout_contracts_provider),
    ("launchpad_factories", lambda: settings.discovery_launchpad_enabled, _launchpad_factory_provider),
    ("blockscout_holders", lambda: settings.discovery_dexscreener_enabled, _blockscout_holders_provider),
]


# ---------------------------------------------------------------------------
# Shared filters
# ---------------------------------------------------------------------------

def _filter_candidates(
    candidates: list[RawCandidate],
    seen: set[str],
    diag: SourceDiagnostic,
    max_age_ms: float = 0,
    min_liq: float = 0,
) -> list[RawCandidate]:
    """Apply shared filter chain, mutating `diag` with rejection counts.

    max_age_ms and min_liq are accepted for API compatibility but unused —
    age/liquidity are now scoring inputs, not discovery gates.
    """
    accepted: list[RawCandidate] = []
    for c in candidates:
        diag.raw += 1
        if not is_valid_address(c.address):
            diag.rejected_invalid_address += 1
            continue
        addr_low = c.address.lower()
        if addr_low in seen:
            diag.rejected_duplicate += 1
            continue
        if launchpad_registry.is_established_token(c.symbol, c.name):
            diag.rejected_established += 1
            continue
        # Reject zero-holder spam (holder_count=None means unknown — allow through)
        if c.holder_count is not None and c.holder_count == 0:
            diag.rejected_zero_holders += 1
            continue
        seen.add(addr_low)
        diag.accepted += 1
        accepted.append(c)
    return accepted


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

async def _enrich_with_market_data(
    candidates: list[RawCandidate],
) -> list[DiscoveredCandidate]:
    """Fetch DexScreener pairs for each candidate. No filtering — all pass through.

    Returns enriched candidates (pair may be None if DexScreener has no data).
    """
    sem = asyncio.Semaphore(max(1, settings.scan_max_deep_analyses))

    async def _enrich_one(c: RawCandidate) -> DiscoveredCandidate:
        async with sem:
            try:
                pairs = await fetch_token_pairs(c.address)
            except Exception as exc:
                logger.debug("DexScreener lookup failed for %s: %s", c.address, exc)
                pairs = []
        pair = choose_best_pair(pairs)
        return DiscoveredCandidate(
            address_hash=c.address,
            name=c.name,
            symbol=c.symbol,
            source=c.source,
            holder_count=c.holder_count,
            pair=pair,
        )

    return await asyncio.gather(*[_enrich_one(c) for c in candidates])


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

# ponytail: _pair_age_ms split out so tests can stub time without mocking time.time
def _pair_age_ms(created_ms: int) -> tuple[int, int]:
    return created_ms, int(time.time() * 1000)


async def discover_candidates(
    limit: int,
) -> tuple[list[DiscoveredCandidate], DiscoveryDiagnostics]:
    """Run all enabled discovery providers, merge, deduplicate, filter, enrich.

    Returns (candidates, diagnostics). Candidates are capped at ``limit``.
    """
    seen: set[str] = set()
    all_filtered: list[RawCandidate] = []
    source_diags: list[SourceDiagnostic] = []

    # Run all enabled providers concurrently
    enabled = [(name, fn) for name, check, fn in _PROVIDERS if check()]
    if not enabled:
        diag = DiscoveryDiagnostics(sources=[], total_raw=0, total_after_dedup=0,
                                     total_after_filters=0, enriched=0)
        return [], diag

    async def _run_provider(name: str, fn: callable) -> tuple[str, list[RawCandidate], SourceDiagnostic]:
        sd = SourceDiagnostic(source=name)
        try:
            raw = await fn()
        except Exception as exc:
            logger.warning("Discovery provider %s failed: %s", name, exc)
            sd.error = str(exc)
            return name, [], sd
        per_limit = settings.discovery_per_provider_limit
        raw = raw[:per_limit]
        filtered = _filter_candidates(raw, seen, sd, 0, 0)
        return name, filtered, sd

    # Run sequentially to share the `seen` set correctly for cross-source dedup
    for name, fn in enabled:
        _, filtered, sd = await _run_provider(name, fn)
        all_filtered.extend(filtered)
        source_diags.append(sd)

    total_raw = sum(sd.raw for sd in source_diags)
    total_after_dedup = len(all_filtered)

    # Enrich with DexScreener market data — all candidates pass through
    pool_limit = min(limit * 4, settings.scan_candidate_pool_size, len(all_filtered))
    enriched = await _enrich_with_market_data(all_filtered[:pool_limit])

    final = enriched[:limit]

    diag = DiscoveryDiagnostics(
        sources=source_diags,
        total_raw=total_raw,
        total_after_dedup=total_after_dedup,
        total_after_filters=total_after_dedup,
        enriched=len(enriched),
        reached_qualification=len(final),
    )
    return final, diag
