"""Scan and analyze must not drift apart.

The light tier is a cost optimization, not a second scoring engine. It calls the
same `score_token` / `score_opportunity` the deep path calls, with fewer inputs.
These tests pin that property: same inputs -> same scores, and an estimate is
always labelled and always conservative.
"""

import asyncio

from app.core.config import settings
from app.models.token import (
    HolderDistribution,
    LaunchpadInfo,
    LiquidityLock,
    LiquiditySnapshot,
    RugAnalysis,
    TokenAnalysisResponse,
    TokenMarketData,
)
from app.services import analyzers, candidate_discovery, rug_analyzer
from app.services.scoring import score_token


_PAIR = {
    "chainId": "robinhood",
    "pairAddress": "0xpair",
    "baseToken": {"name": "Established Token", "symbol": "ESTB"},
    "priceUsd": "12.50",
    "marketCap": 8_000_000,
    "fdv": 8_000_000,
    "liquidity": {"usd": 750_000.0},
    "volume": {"h24": 400_000.0},
    "priceChange": {"h24": 2.5},
    "pairCreatedAt": 1_700_000_000_000,
}

_HOLDERS = 6000


def _scan(monkeypatch, tokens):
    from app.models.token import DiscoveryDiagnostics

    async def fake_discover():
        cands = [
            candidate_discovery.DiscoveredCandidate(
                address_hash=t["address_hash"],
                name=t.get("name"),
                symbol=t.get("symbol"),
                holder_count=t.get("holders_count"),
                source="test",
                pair=t.get("pair"),
            )
            for t in tokens
        ]
        return cands, DiscoveryDiagnostics()

    monkeypatch.setattr(candidate_discovery, "discover_candidates", fake_discover)
    return asyncio.run(rug_analyzer.scan_and_rank(page_size=10))


def test_light_estimate_uses_the_same_engine(monkeypatch):
    """The estimate equals score_token() called directly with the same partial inputs."""
    resp = _scan(monkeypatch, [
        {"address_hash": "0xestb", "holders_count": _HOLDERS, "pair": _PAIR},
    ])
    t = resp.ranked_tokens[0]
    assert t.scores_estimated is True

    # Reproduce the estimate independently through the authoritative scorer.
    expected = score_token(
        age=analyzers.analyze_age(_PAIR["pairCreatedAt"], None),
        market=rug_analyzer._build_market_data(_PAIR),
        holders=HolderDistribution(holder_count=_HOLDERS),
        clusters=None,
        dev=None,
        liquidity_lock=LiquidityLock(status="unknown"),
        launchpad=LaunchpadInfo(name="Unknown", confidence="low"),
        lore=None,
        data_sources=["DexScreener pair", "Blockscout token list"],
    )
    assert t.risk_score == expected.risk_score
    assert t.risk_level == expected.risk_level


def test_estimate_carries_unknown_penalties(monkeypatch):
    """An unverified token never scores a clean 0 — unknown lock + launchpad cost points."""
    resp = _scan(monkeypatch, [
        {"address_hash": "0xestb", "holders_count": _HOLDERS, "pair": _PAIR},
    ])
    t = resp.ranked_tokens[0]
    # 8 (LP lock unknown) + 5 (launchpad unknown) at minimum.
    assert t.risk_score >= 13, "estimate must not report a false clean"


def test_deep_path_is_not_flagged_estimated(monkeypatch):
    """A token that goes through deep analysis reports verified scores."""
    async def fake_analyze(address, include_lore=False):
        return TokenAnalysisResponse(
            contract_address=address,
            chain="Robinhood Chain",
            status="ok",
            message="stub",
            holders=HolderDistribution(holder_count=10),
            market_data=TokenMarketData(liquidity=LiquiditySnapshot(usd=1000.0)),
            analysis=RugAnalysis(
                risk_score=42, risk_level="medium", signals=[], data_sources=[], limitations=[]
            ),
        )

    monkeypatch.setattr(rug_analyzer, "analyze_token_contract", fake_analyze)
    # Low holders -> fails the gate -> deep path.
    resp = _scan(monkeypatch, [
        {"address_hash": "0xnew", "holders_count": 10, "pair": _PAIR},
    ])
    t = resp.ranked_tokens[0]
    assert t.scores_estimated is False
    assert t.risk_score == 42


def test_risky_estimate_promotes_to_deep(monkeypatch):
    """If the estimate itself scores risky, the token is analyzed rather than published."""
    promoted: set[str] = set()

    async def fake_analyze(address, include_lore=False):
        promoted.add(address)
        return TokenAnalysisResponse(
            contract_address=address,
            chain="Robinhood Chain",
            status="ok",
            message="stub",
            holders=HolderDistribution(holder_count=_HOLDERS),
            market_data=TokenMarketData(liquidity=LiquiditySnapshot(usd=750_000.0)),
            analysis=RugAnalysis(
                risk_score=55, risk_level="high", signals=[], data_sources=[], limitations=[]
            ),
        )

    monkeypatch.setattr(rug_analyzer, "analyze_token_contract", fake_analyze)
    # Threshold below the unknown-marker baseline -> every estimate promotes.
    monkeypatch.setattr(settings, "scan_light_promote_threshold", 1)
    _scan(monkeypatch, [
        {"address_hash": "0xestb", "holders_count": _HOLDERS, "pair": _PAIR},
    ])
    assert "0xestb" in promoted


def test_no_token_reports_a_hardcoded_zero(monkeypatch):
    """The NVDA symptom: scanner publishing risk_score 0 for an unverified token."""
    resp = _scan(monkeypatch, [
        {"address_hash": "0xestb", "holders_count": _HOLDERS, "pair": _PAIR},
    ])
    for t in resp.ranked_tokens:
        if t.scores_estimated:
            assert t.risk_score != 0
