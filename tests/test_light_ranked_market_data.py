"""_light_ranked must carry discovery's market data, not drop it (provenance fix)."""

import asyncio

from app.services import candidate_discovery, rug_analyzer
from app.models.token import DiscoveryDiagnostics


_PAIR = {
    "chainId": "robinhood",
    "dexId": "uniswap",
    "pairAddress": "0xpair",
    "baseToken": {"name": "NVIDIA Token", "symbol": "NVDA"},
    "priceUsd": "215.79",
    "marketCap": 4271309,
    "fdv": 4271309,
    "liquidity": {"usd": 839454.67},
    "volume": {"h24": 4008400.38},
    "priceChange": {"h24": 1.5},
    "pairCreatedAt": 1784631726000,
}


def _run_scan(monkeypatch):
    """Drive scan_and_rank with one established candidate that owns a pair."""
    cand = candidate_discovery.DiscoveredCandidate(
        address_hash="0x" + "d0" * 20,
        name="NVIDIA Token",
        symbol="NVDA",
        source="blockscout_tokens",
        holder_count=33933,  # >= scan_established_holder_floor -> light path
        pair=_PAIR,
    )

    async def fake_discover():
        return [cand], DiscoveryDiagnostics(
            sources=[], total_raw=1, total_after_dedup=1,
            total_after_filters=1, enriched=1,
        )

    monkeypatch.setattr(candidate_discovery, "discover_candidates", fake_discover)
    return asyncio.run(rug_analyzer.scan_and_rank(page=1, page_size=10))


def test_light_path_reports_market_data(monkeypatch):
    resp = _run_scan(monkeypatch)
    assert resp.ranked_tokens, "established token should still be ranked"
    t = resp.ranked_tokens[0]

    # The whole point: the light path no longer drops discovery's market data.
    assert t.price_usd == "215.79"
    assert t.liquidity_usd == 839454.67
    assert t.market_cap == 4271309.0
    assert t.volume_h24 == 4008400.38
    assert t.age_days is not None and t.age_days > 0
    assert t.name == "NVIDIA Token" and t.symbol == "NVDA"
    # Scores come from the real scoring engine on partial inputs, and are labelled.
    assert t.alpha_score is not None
    assert t.scores_estimated is True
    assert t.risk_score >= 13  # unknown LP lock (8) + unknown launchpad (5)
