"""Tests for the Smart Wallet Reputation engine."""

import asyncio

import pytest

from app.models.token import (
    RugAnalysis,
    SmartWalletReputationResult,
    TokenAnalysisResponse,
)
from app.services.smart_wallet_reputation import (
    OnChainWalletProvider,
    _cache,
    _compute_score,
    evaluate,
)
from app.services.opportunity_score import _score_wallet_reputation, score_opportunity


def _run(coro):
    return asyncio.run(coro)


def _base_result(**overrides) -> TokenAnalysisResponse:
    defaults = dict(
        contract_address="0x" + "a" * 40,
        chain="robinhood",
        status="success",
        message="ok",
        analysis=RugAnalysis(
            risk_score=30, risk_level="low", signals=[],
            data_sources=[], limitations=[],
        ),
        watchlist_hits=[],
    )
    defaults.update(overrides)
    return TokenAnalysisResponse(**defaults)


def _evidence(**overrides) -> dict:
    defaults = dict(
        wallet_age_days=None,
        total_transactions=0,
        token_interactions=0,
        launches_entered=0,
        entry_timings=[],
        surviving_projects=0,
        dormant_days=None,
        raw_transfers=[],
        holdings=[],
        address="0x" + "bb" * 20,
    )
    defaults.update(overrides)
    return defaults


# --- New wallet (no history) ---


class TestNewWallet:
    def test_brand_new_wallet_low_score(self):
        ev = _evidence(wallet_age_days=1, total_transactions=2)
        rep = _compute_score("0x" + "bb" * 20, ev)
        assert rep.score < 40
        assert any("New wallet" in e or "days old" in e for e in rep.evidence)

    def test_no_transactions_penalized(self):
        ev = _evidence(wallet_age_days=5, total_transactions=2)
        rep = _compute_score("0x" + "bb" * 20, ev)
        assert rep.score < 50
        assert any("Little activity" in e or "days old" in e for e in rep.evidence)

    def test_empty_evidence_gives_baseline(self):
        ev = _evidence()
        rep = _compute_score("0x" + "bb" * 20, ev)
        assert 0 <= rep.score <= 100
        assert rep.confidence == "low"


# --- Veteran wallet ---


class TestVeteranWallet:
    def test_old_active_wallet_high_score(self):
        ev = _evidence(
            wallet_age_days=800,
            total_transactions=150,
            token_interactions=15,
            launches_entered=12,
            surviving_projects=6,
            dormant_days=0.5,
        )
        rep = _compute_score("0x" + "cc" * 20, ev)
        assert rep.score >= 70
        assert rep.confidence == "high"
        assert any("Active for" in e for e in rep.evidence)

    def test_moderate_wallet(self):
        ev = _evidence(
            wallet_age_days=120,
            total_transactions=30,
            token_interactions=5,
            launches_entered=4,
            surviving_projects=2,
            dormant_days=1,
        )
        rep = _compute_score("0x" + "dd" * 20, ev)
        assert 50 <= rep.score <= 80
        assert rep.confidence in ("medium", "high")


# --- Successful trader wallet ---


class TestSuccessfulWallet:
    def test_surviving_projects_boost(self):
        ev_no = _evidence(wallet_age_days=200, total_transactions=50,
                          token_interactions=10, surviving_projects=0)
        ev_yes = _evidence(wallet_age_days=200, total_transactions=50,
                           token_interactions=10, surviving_projects=6)
        rep_no = _compute_score("0x" + "ee" * 20, ev_no)
        rep_yes = _compute_score("0x" + "ee" * 20, ev_yes)
        assert rep_yes.score > rep_no.score

    def test_consistent_activity_boost(self):
        transfers = [
            {"token": {"address": f"0x{i:040x}"}, "to": {"hash": "0x" + "ee" * 20},
             "from": {"hash": "0x" + "00" * 20}, "timestamp": f"2025-{(i % 12) + 1:02d}-15T00:00:00Z"}
            for i in range(12)
        ]
        ev = _evidence(
            wallet_age_days=400, total_transactions=80,
            token_interactions=12, raw_transfers=transfers,
        )
        rep = _compute_score("0x" + "ee" * 20, ev)
        assert rep.consistency_score is not None
        assert rep.consistency_score > 0

    def test_long_holding_period_boost(self):
        addr = "0x" + "ff" * 20
        transfers = [
            {"token": {"address": "0x" + "01" * 20}, "to": {"hash": addr},
             "from": {"hash": "0x" + "00" * 20}, "timestamp": "2024-01-01T00:00:00Z"},
            {"token": {"address": "0x" + "01" * 20}, "from": {"hash": addr},
             "to": {"hash": "0x" + "00" * 20}, "timestamp": "2024-06-01T00:00:00Z"},
        ]
        ev = _evidence(wallet_age_days=500, total_transactions=40,
                       raw_transfers=transfers)
        rep = _compute_score(addr, ev)
        assert rep.avg_holding_period_days is not None
        assert rep.avg_holding_period_days > 100


# --- Rug-heavy wallet ---


class TestRugHeavyWallet:
    def test_rugs_from_holdings(self):
        addr = "0x" + "aa" * 20
        transfers = [
            {"token": {"address": f"0x{i:040x}", "address_hash": f"0x{i:040x}"},
             "to": {"hash": addr}, "from": {"hash": "0x" + "00" * 20},
             "timestamp": f"2025-01-{i + 1:02d}T00:00:00Z"}
            for i in range(6)
        ]
        holdings = [
            {"token": {"type": "ERC-20", "address_hash": f"0x{i:040x}",
                       "holders_count": 3}, "value": "1000"}
            for i in range(6)
        ]
        ev = _evidence(
            wallet_age_days=30, total_transactions=20,
            raw_transfers=transfers, holdings=holdings,
            address=addr,
        )
        rep = _compute_score(addr, ev)
        assert rep.rugs_entered >= 5
        assert rep.score < 45
        assert any("rugged" in e or "abandoned" in e for e in rep.evidence)

    def test_dormant_wallet_penalized(self):
        ev = _evidence(wallet_age_days=200, total_transactions=50,
                       dormant_days=60)
        rep = _compute_score("0x" + "aa" * 20, ev)
        assert rep.active is False
        assert any("Dormant" in e for e in rep.evidence)

    def test_quick_flipper_penalized(self):
        addr = "0x" + "ab" * 20
        transfers = [
            {"token": {"address": "0x" + "01" * 20}, "to": {"hash": addr},
             "from": {"hash": "0x" + "00" * 20}, "timestamp": "2025-01-01T00:00:00Z"},
            {"token": {"address": "0x" + "01" * 20}, "from": {"hash": addr},
             "to": {"hash": "0x" + "00" * 20}, "timestamp": "2025-01-01T12:00:00Z"},
        ]
        ev = _evidence(wallet_age_days=100, total_transactions=30,
                       raw_transfers=transfers)
        rep = _compute_score(addr, ev)
        assert rep.avg_holding_period_days is not None
        assert rep.avg_holding_period_days < 1
        assert any("flipper" in e.lower() for e in rep.evidence)


# --- Cache behaviour ---


class TestCacheBehaviour:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        _cache.clear()
        yield
        _cache.clear()

    def test_cache_hit_skips_provider(self, monkeypatch):
        address = "0x" + "ca" * 20
        cached = SmartWalletReputationResult(
            score=75, evidence=["+ cached"], address=address,
        )
        _cache.set(f"wallet_rep:{address.lower()}", cached)

        called = {"n": 0}

        async def boom(self, addr):
            called["n"] += 1
            raise AssertionError("should not call provider on cache hit")

        monkeypatch.setattr(OnChainWalletProvider, "gather", boom)
        rep = _run(evaluate(address))
        assert rep.score == 75
        assert called["n"] == 0

    def test_cache_miss_calls_provider(self, monkeypatch):
        called = {"n": 0}

        async def fake_gather(self, addr):
            called["n"] += 1
            return _evidence(wallet_age_days=100, total_transactions=20)

        monkeypatch.setattr(OnChainWalletProvider, "gather", fake_gather)
        _run(evaluate("0x" + "cb" * 20))
        assert called["n"] == 1

    def test_second_call_uses_cache(self, monkeypatch):
        called = {"n": 0}

        async def fake_gather(self, addr):
            called["n"] += 1
            return _evidence(wallet_age_days=100, total_transactions=20)

        monkeypatch.setattr(OnChainWalletProvider, "gather", fake_gather)
        addr = "0x" + "cc" * 20
        _run(evaluate(addr))
        _run(evaluate(addr))
        assert called["n"] == 1


# --- Explanation generation ---


class TestExplanationGeneration:
    def test_positive_evidence_prefixed_with_plus(self):
        ev = _evidence(wallet_age_days=500, total_transactions=150,
                       token_interactions=15, surviving_projects=6, dormant_days=0.5)
        rep = _compute_score("0x" + "ee" * 20, ev)
        positives = [e for e in rep.evidence if e.startswith("+")]
        assert len(positives) >= 3

    def test_negative_evidence_prefixed_with_minus(self):
        ev = _evidence(wallet_age_days=2, total_transactions=2, dormant_days=60)
        rep = _compute_score("0x" + "ee" * 20, ev)
        negatives = [e for e in rep.evidence if e.startswith("-")]
        assert len(negatives) >= 2

    def test_evidence_is_human_readable(self):
        ev = _evidence(wallet_age_days=200, total_transactions=50,
                       token_interactions=8, surviving_projects=3)
        rep = _compute_score("0x" + "ee" * 20, ev)
        for line in rep.evidence:
            assert line.startswith("+") or line.startswith("-")
            assert len(line) < 200


# --- Signal registration in Opportunity Score ---


class TestSignalRegistration:
    def test_scorer_registered(self):
        from app.services.opportunity_score import SCORERS
        scorer_names = []
        rep = SmartWalletReputationResult(score=60, address="0x" + "aa" * 20)
        r = _base_result(wallet_reputations=[rep])
        for s in SCORERS:
            sr = s(r)
            if sr:
                scorer_names.append(sr.name)
        assert "wallet_reputation" in scorer_names

    def test_score_wallet_reputation_none_when_missing(self):
        r = _base_result()
        assert _score_wallet_reputation(r) is None

    def test_score_wallet_reputation_positive(self):
        rep = SmartWalletReputationResult(score=80, address="0x" + "aa" * 20)
        r = _base_result(wallet_reputations=[rep])
        sr = _score_wallet_reputation(r)
        assert sr is not None
        assert sr.value == 80
        assert sr.positive is True
        assert sr.name == "wallet_reputation"

    def test_score_wallet_reputation_negative(self):
        rep = SmartWalletReputationResult(score=20, address="0x" + "aa" * 20)
        r = _base_result(wallet_reputations=[rep])
        sr = _score_wallet_reputation(r)
        assert sr is not None
        assert sr.value == 20
        assert sr.positive is False

    def test_averages_multiple_wallets(self):
        reps = [
            SmartWalletReputationResult(score=80, address="0x" + "aa" * 20),
            SmartWalletReputationResult(score=40, address="0x" + "bb" * 20),
        ]
        r = _base_result(wallet_reputations=reps)
        sr = _score_wallet_reputation(r)
        assert sr is not None
        assert sr.value == 60

    def test_feeds_into_opportunity_aggregate(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.opportunity_score.settings.opportunity_score_weights",
            {"wallet_reputation": 100},
        )
        rep = SmartWalletReputationResult(score=90, address="0x" + "aa" * 20)
        r = _base_result(wallet_reputations=[rep])
        opp = score_opportunity(r)
        assert opp.alpha_score == 90
        assert any(s.name == "wallet_reputation" for s in opp.signals)


# --- Deterministic output ---


class TestDeterministicOutput:
    def test_same_input_same_output(self):
        ev = _evidence(
            wallet_age_days=150, total_transactions=60,
            token_interactions=10, launches_entered=5,
            surviving_projects=3, dormant_days=1,
        )
        addr = "0x" + "dd" * 20
        rep1 = _compute_score(addr, ev)
        rep2 = _compute_score(addr, ev)
        assert rep1.score == rep2.score
        assert rep1.evidence == rep2.evidence
        assert rep1.surviving_projects == rep2.surviving_projects
        assert rep1.rugs_entered == rep2.rugs_entered

    def test_score_clamped_0_100(self):
        ev_low = _evidence(wallet_age_days=0.5, total_transactions=1,
                           dormant_days=90)
        rep_low = _compute_score("0x" + "11" * 20, ev_low)
        assert 0 <= rep_low.score <= 100

        ev_high = _evidence(wallet_age_days=1000, total_transactions=500,
                            token_interactions=50, launches_entered=30,
                            surviving_projects=15, dormant_days=0)
        rep_high = _compute_score("0x" + "22" * 20, ev_high)
        assert 0 <= rep_high.score <= 100

    def test_address_normalized(self):
        ev = _evidence(wallet_age_days=100, total_transactions=30)
        addr = "0x" + "AB" * 20
        rep = _compute_score(addr, ev)
        assert rep.address == addr.lower()
