"""Tests for the Developer Reputation Intelligence engine."""

import asyncio

import pytest

from app.models.token import (
    DeveloperReputationResult,
    DevProfile,
    LaunchedToken,
    LaunchpadInfo,
    RugAnalysis,
    TokenAnalysisResponse,
)
from app.services import developer_reputation
from app.services.developer_reputation import (
    OnChainProvider,
    _cache,
    _compute_score,
    evaluate,
)
from app.services.opportunity_score import _score_dev_reputation, score_opportunity


def _run(coro):
    return asyncio.run(coro)


def _base_result(**overrides) -> TokenAnalysisResponse:
    defaults = dict(
        contract_address="0x" + "a" * 40,
        chain="robinhood",
        status="success",
        message="ok",
        analysis=RugAnalysis(
            risk_score=30,
            risk_level="low",
            signals=[],
            data_sources=[],
            limitations=[],
        ),
        watchlist_hits=[],
    )
    defaults.update(overrides)
    return TokenAnalysisResponse(**defaults)


def _dev(**overrides) -> DevProfile:
    defaults = dict(
        creator_address="0x" + "de" * 20,
        creation_tx="0x" + "ff" * 32,
    )
    defaults.update(overrides)
    return DevProfile(**defaults)


# --- New developer (no history) ---


class TestNewDeveloper:
    def test_no_deployer_returns_low_score(self):
        result = _base_result(dev=None)
        rep = _run(evaluate(result))
        assert rep is not None
        assert rep.score < 50
        assert rep.deployer is None

    def test_first_deployment_penalized(self):
        rep = _compute_score(
            dev=_dev(tokens_launched=None, launched_tokens=[]),
            evidence={"wallet_age_days": 1, "total_contracts_deployed": 0},
            launchpad_name=None,
        )
        assert rep.score < 40
        assert any("First deployment" in e for e in rep.evidence)
        assert any("recently" in e or "days old" in e for e in rep.evidence)

    def test_brand_new_wallet_heavy_penalty(self):
        rep = _compute_score(
            dev=_dev(tokens_launched=None, launched_tokens=[]),
            evidence={"wallet_age_days": 0.5, "total_contracts_deployed": 0},
            launchpad_name=None,
        )
        assert rep.score <= 20


# --- Established developer ---


class TestEstablishedDeveloper:
    def test_clean_history_high_score(self):
        tokens = [
            LaunchedToken(address=f"0x{i:040x}", name=f"T{i}", symbol=f"T{i}",
                          liquidity_usd=5000.0, outcome="alive")
            for i in range(5)
        ]
        rep = _compute_score(
            dev=_dev(
                tokens_launched=5, tokens_rugged=0, tokens_alive=5,
                launched_tokens=tokens,
            ),
            evidence={
                "wallet_age_days": 420,
                "total_contracts_deployed": 8,
                "verified_contracts": 6,
                "wallet_transaction_count": 200,
                "holder_counts": {f"0x{i:040x}": 100 for i in range(5)},
            },
            launchpad_name=None,
        )
        assert rep.score >= 80
        assert any("420 days" in e for e in rep.evidence)
        assert any("verified" in e for e in rep.evidence)
        assert any("No known abandoned" in e for e in rep.evidence)

    def test_with_launchpad_bonus(self):
        rep_no_lp = _compute_score(
            dev=_dev(tokens_launched=1, tokens_alive=1, launched_tokens=[]),
            evidence={"wallet_age_days": 100, "total_contracts_deployed": 2, "verified_contracts": 1},
            launchpad_name=None,
        )
        rep_lp = _compute_score(
            dev=_dev(tokens_launched=1, tokens_alive=1, launched_tokens=[]),
            evidence={"wallet_age_days": 100, "total_contracts_deployed": 2, "verified_contracts": 1},
            launchpad_name="NOXA Fun",
        )
        assert rep_lp.score > rep_no_lp.score
        assert any("launchpad" in e for e in rep_lp.evidence)


# --- Multiple previous launches ---


class TestMultipleLaunches:
    def test_serial_rugger_low_score(self):
        tokens = [
            LaunchedToken(address=f"0x{i:040x}", name=f"R{i}", symbol=f"R{i}",
                          liquidity_usd=0.0, outcome="likely_rugged")
            for i in range(5)
        ]
        rep = _compute_score(
            dev=_dev(
                tokens_launched=5, tokens_rugged=5, tokens_alive=0,
                launched_tokens=tokens,
            ),
            evidence={
                "wallet_age_days": 10,
                "total_contracts_deployed": 5,
                "verified_contracts": 0,
            },
            launchpad_name=None,
        )
        assert rep.score < 25
        assert rep.abandoned_launches == 5
        assert any("abandoned" in e for e in rep.evidence)

    def test_mixed_history(self):
        tokens = [
            LaunchedToken(address="0x01", name="A", symbol="A",
                          liquidity_usd=5000.0, outcome="alive"),
            LaunchedToken(address="0x02", name="B", symbol="B",
                          liquidity_usd=0.0, outcome="likely_rugged"),
        ]
        rep = _compute_score(
            dev=_dev(tokens_launched=2, tokens_rugged=1, tokens_alive=1,
                     launched_tokens=tokens),
            evidence={
                "wallet_age_days": 90,
                "total_contracts_deployed": 3,
                "verified_contracts": 1,
            },
            launchpad_name=None,
        )
        assert 30 <= rep.score <= 70

    def test_healthy_liquidity_counted(self):
        tokens = [
            LaunchedToken(address=f"0x{i:040x}", name=f"H{i}", symbol=f"H{i}",
                          liquidity_usd=2000.0 * (i + 1), outcome="alive")
            for i in range(4)
        ]
        rep = _compute_score(
            dev=_dev(tokens_launched=4, tokens_rugged=0, tokens_alive=4,
                     launched_tokens=tokens),
            evidence={"wallet_age_days": 200, "total_contracts_deployed": 4,
                      "verified_contracts": 2},
            launchpad_name=None,
        )
        assert rep.healthy_liquidity_launches == 4
        assert any("healthy liquidity" in e for e in rep.evidence)


# --- Cache behaviour ---


class TestCacheBehaviour:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        _cache.clear()
        yield
        _cache.clear()

    def test_cache_hit_skips_provider(self, monkeypatch):
        deployer = "0x" + "de" * 20
        cached_rep = DeveloperReputationResult(
            score=75, evidence=["+ cached"], deployer=deployer,
        )
        _cache.set(f"dev_rep:{deployer.lower()}", cached_rep)

        boom_called = {"n": 0}
        original_gather = OnChainProvider.gather

        async def boom(self, *a, **k):
            boom_called["n"] += 1
            raise AssertionError("should not call provider on cache hit")

        monkeypatch.setattr(OnChainProvider, "gather", boom)

        result = _base_result(dev=_dev(creator_address=deployer))
        rep = _run(evaluate(result))
        assert rep.score == 75
        assert boom_called["n"] == 0

    def test_cache_miss_calls_provider(self, monkeypatch):
        called = {"n": 0}

        async def fake_gather(self, deployer, dev):
            called["n"] += 1
            return {"wallet_age_days": 100, "total_contracts_deployed": 2,
                    "verified_contracts": 1}

        monkeypatch.setattr(OnChainProvider, "gather", fake_gather)

        result = _base_result(dev=_dev())
        _run(evaluate(result))
        assert called["n"] == 1

    def test_second_call_uses_cache(self, monkeypatch):
        called = {"n": 0}

        async def fake_gather(self, deployer, dev):
            called["n"] += 1
            return {"wallet_age_days": 100, "total_contracts_deployed": 2}

        monkeypatch.setattr(OnChainProvider, "gather", fake_gather)

        result = _base_result(dev=_dev())
        _run(evaluate(result))
        _run(evaluate(result))
        assert called["n"] == 1


# --- Explanation generation ---


class TestExplanationGeneration:
    def test_positive_evidence_prefixed_with_plus(self):
        rep = _compute_score(
            dev=_dev(tokens_launched=3, tokens_rugged=0, tokens_alive=3,
                     launched_tokens=[]),
            evidence={"wallet_age_days": 500, "total_contracts_deployed": 5,
                      "verified_contracts": 3},
            launchpad_name=None,
        )
        positives = [e for e in rep.evidence if e.startswith("+")]
        assert len(positives) >= 3

    def test_negative_evidence_prefixed_with_minus(self):
        rep = _compute_score(
            dev=_dev(tokens_launched=2, tokens_rugged=2, launched_tokens=[]),
            evidence={"wallet_age_days": 1, "total_contracts_deployed": 2,
                      "verified_contracts": 0},
            launchpad_name=None,
        )
        negatives = [e for e in rep.evidence if e.startswith("-")]
        assert len(negatives) >= 2

    def test_evidence_is_human_readable(self):
        rep = _compute_score(
            dev=_dev(tokens_launched=1, tokens_alive=1, launched_tokens=[]),
            evidence={"wallet_age_days": 200, "total_contracts_deployed": 3,
                      "verified_contracts": 2},
            launchpad_name=None,
        )
        for line in rep.evidence:
            assert line.startswith("+") or line.startswith("-")
            assert len(line) < 200


# --- Signal registration in Opportunity Score ---


class TestSignalRegistration:
    def test_scorer_registered(self):
        from app.services.opportunity_score import SCORERS
        scorer_names = []
        for s in SCORERS:
            r = _base_result(developer_reputation=DeveloperReputationResult(score=50))
            sr = s(r)
            if sr:
                scorer_names.append(sr.name)
        assert "dev_reputation" in scorer_names

    def test_score_dev_reputation_none_when_missing(self):
        r = _base_result()
        assert _score_dev_reputation(r) is None

    def test_score_dev_reputation_positive(self):
        r = _base_result(
            developer_reputation=DeveloperReputationResult(score=80),
        )
        sr = _score_dev_reputation(r)
        assert sr is not None
        assert sr.value == 80
        assert sr.positive is True
        assert sr.name == "dev_reputation"

    def test_score_dev_reputation_negative(self):
        r = _base_result(
            developer_reputation=DeveloperReputationResult(score=20),
        )
        sr = _score_dev_reputation(r)
        assert sr is not None
        assert sr.value == 20
        assert sr.positive is False

    def test_feeds_into_opportunity_aggregate(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.opportunity_score.settings.opportunity_score_weights",
            {"dev_reputation": 100},
        )
        r = _base_result(
            developer_reputation=DeveloperReputationResult(score=90),
        )
        opp = score_opportunity(r)
        assert opp.alpha_score == 90
        assert any(s.name == "dev_reputation" for s in opp.signals)


# --- Deterministic output ---


class TestDeterministicOutput:
    def test_same_input_same_output(self):
        dev = _dev(tokens_launched=3, tokens_rugged=1, tokens_alive=2,
                   launched_tokens=[
                       LaunchedToken(address="0x01", liquidity_usd=5000.0, outcome="alive"),
                       LaunchedToken(address="0x02", liquidity_usd=0.0, outcome="likely_rugged"),
                       LaunchedToken(address="0x03", liquidity_usd=3000.0, outcome="alive"),
                   ])
        evidence = {
            "wallet_age_days": 150.0,
            "total_contracts_deployed": 5,
            "verified_contracts": 2,
            "wallet_transaction_count": 100,
            "holder_counts": {"0x01": 200, "0x03": 80},
        }
        rep1 = _compute_score(dev, evidence, "NOXA Fun")
        rep2 = _compute_score(dev, evidence, "NOXA Fun")
        assert rep1.score == rep2.score
        assert rep1.evidence == rep2.evidence
        assert rep1.healthy_liquidity_launches == rep2.healthy_liquidity_launches
        assert rep1.meaningful_holder_launches == rep2.meaningful_holder_launches

    def test_score_clamped_0_100(self):
        rep_low = _compute_score(
            dev=_dev(tokens_launched=10, tokens_rugged=10, launched_tokens=[]),
            evidence={"wallet_age_days": 0.1, "total_contracts_deployed": 10,
                      "verified_contracts": 0},
            launchpad_name=None,
        )
        assert 0 <= rep_low.score <= 100

        rep_high = _compute_score(
            dev=_dev(tokens_launched=10, tokens_rugged=0, tokens_alive=10,
                     launched_tokens=[
                         LaunchedToken(address=f"0x{i:040x}", liquidity_usd=50000.0,
                                       outcome="alive")
                         for i in range(10)
                     ]),
            evidence={
                "wallet_age_days": 1000,
                "total_contracts_deployed": 15,
                "verified_contracts": 10,
                "holder_counts": {f"0x{i:040x}": 500 for i in range(10)},
            },
            launchpad_name="NOXA Fun",
        )
        assert 0 <= rep_high.score <= 100
