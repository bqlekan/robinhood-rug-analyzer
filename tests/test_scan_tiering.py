"""Unit tests for M2 scan tiering: light pre-screen + promote-on-uncertainty.

The light tier must NEVER skip a suspicious token. A token is skipped only when it
is confidently low-risk: a known holder count at/above the floor and a light score
below threshold. Everything else promotes to deep analysis.
"""

import asyncio

import pytest

from app.core.config import settings
from app.models.token import (
    HolderDistribution,
    RugAnalysis,
    TokenAnalysisResponse,
    TokenMarketData,
    LiquiditySnapshot,
)
from app.services import rug_analyzer
from app.services.scoring import score_token_light


# --- Light scorer (pure) ---


def test_light_scorer_few_holders_scores_high():
    a = score_token_light(12)
    assert a.risk_score == 18
    assert a.signals[0].name == "Few holders"


def test_light_scorer_low_holders_scores_medium():
    a = score_token_light(150)
    assert a.risk_score == 8


def test_light_scorer_many_holders_scores_zero():
    assert score_token_light(5000).risk_score == 0


def test_light_scorer_unknown_holders_scores_zero_but_signals_empty():
    a = score_token_light(None)
    assert a.risk_score == 0
    assert a.signals == []


# --- Promotion policy (via scan_and_rank with deep analysis stubbed) ---


def _stub_deep(monkeypatch):
    """Replace analyze_token_contract with a marker; return the set of promoted addrs."""
    promoted: set[str] = set()

    async def fake_analyze(address, include_lore=False):
        promoted.add(address)
        return TokenAnalysisResponse(
            contract_address=address,
            chain="Robinhood Chain",
            status="ok",
            message="stub",
            holders=HolderDistribution(holder_count=1),
            market_data=TokenMarketData(liquidity=LiquiditySnapshot(usd=1.0)),
            analysis=RugAnalysis(
                risk_score=42, risk_level="medium", signals=[], data_sources=[], limitations=[]
            ),
        )

    monkeypatch.setattr(rug_analyzer, "analyze_token_contract", fake_analyze)
    return promoted


def _stub_list(monkeypatch, tokens):
    async def fake_list(limit=50):
        return tokens

    monkeypatch.setattr(rug_analyzer.blockscout_client, "list_tokens", fake_list)


def _run(coro):
    return asyncio.run(coro)


def test_high_holder_token_is_skipped(monkeypatch):
    promoted = _stub_deep(monkeypatch)
    _stub_list(monkeypatch, [{"address_hash": "0xsafe", "holders_count": 5000, "name": "Safe"}])
    resp = _run(rug_analyzer.scan_and_rank(1))
    assert "0xsafe" not in promoted  # skipped, no deep fetch
    assert resp.ranked_tokens[0].top_signal.startswith("Deep analysis skipped")


def test_few_holders_token_is_promoted(monkeypatch):
    promoted = _stub_deep(monkeypatch)
    _stub_list(monkeypatch, [{"address_hash": "0xrisky", "holders_count": 12, "name": "Risky"}])
    _run(rug_analyzer.scan_and_rank(1))
    assert "0xrisky" in promoted  # suspicious -> deep analysis


def test_unknown_holder_count_is_promoted(monkeypatch):
    promoted = _stub_deep(monkeypatch)
    _stub_list(monkeypatch, [{"address_hash": "0xunknown", "name": "NoHolderData"}])
    _run(rug_analyzer.scan_and_rank(1))
    assert "0xunknown" in promoted  # uncertainty -> deep analysis


def test_holder_floor_edge_cases(monkeypatch):
    floor = settings.scan_established_holder_floor
    promoted = _stub_deep(monkeypatch)
    _stub_list(
        monkeypatch,
        [
            {"address_hash": "0xat", "holders_count": floor},       # exactly floor -> skip
            {"address_hash": "0xbelow", "holders_count": floor - 1},  # below floor -> promote
        ],
    )
    _run(rug_analyzer.scan_and_rank(5))
    assert "0xat" not in promoted
    assert "0xbelow" in promoted


def test_tiering_disabled_promotes_everything(monkeypatch):
    monkeypatch.setattr(settings, "scan_tiering_enabled", False)
    promoted = _stub_deep(monkeypatch)
    _stub_list(monkeypatch, [{"address_hash": "0xsafe", "holders_count": 5000}])
    _run(rug_analyzer.scan_and_rank(1))
    assert "0xsafe" in promoted  # tiering off -> deep analysis even for safe token


def test_floor_override_changes_promotion(monkeypatch):
    # Raise the floor above the token's holder count -> it should now promote.
    monkeypatch.setattr(settings, "scan_established_holder_floor", 10_000)
    promoted = _stub_deep(monkeypatch)
    _stub_list(monkeypatch, [{"address_hash": "0xmid", "holders_count": 5000}])
    _run(rug_analyzer.scan_and_rank(1))
    assert "0xmid" in promoted
