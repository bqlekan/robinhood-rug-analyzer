"""Tests for the Developer Network Intelligence engine."""

import asyncio

import pytest

from app.models.token import (
    ContractIntel,
    DeveloperNetworkResult,
    DeveloperReputationResult,
    DevProfile,
    HolderDistribution,
    HolderEntry,
    InsiderWallet,
    LaunchedToken,
    LaunchpadInfo,
    NetworkSibling,
    RugAnalysis,
    SmartWalletReputationResult,
    TokenAnalysisResponse,
    WatchlistHit,
)
from app.services.developer_network import (
    OnChainNetworkProvider,
    _cache,
    _compute_score,
    evaluate,
)
from app.services.opportunity_score import _score_developer_network, score_opportunity


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


def _dev(**overrides) -> DevProfile:
    defaults = dict(
        creator_address="0x" + "de" * 20,
        creation_tx="0x" + "ff" * 32,
    )
    defaults.update(overrides)
    return DevProfile(**defaults)


def _launched(addr, name="T", outcome="alive", liq=5000.0):
    return LaunchedToken(
        address=addr, name=name, symbol=name, liquidity_usd=liq, outcome=outcome,
    )


def _evidence(**overrides) -> dict:
    defaults = dict(
        deployer="0x" + "de" * 20,
        sibling_tokens=[],
        sibling_data=[],
        current_holders=set(),
        current_smart=set(),
        current_insiders=set(),
        funding_wallet=None,
        current_template=None,
        current_launchpad=None,
    )
    defaults.update(overrides)
    return defaults


# --- New developer (no history) ---


class TestNewDeveloper:
    def test_no_deployer_returns_none(self):
        result = _base_result(dev=None)
        rep = _run(evaluate(result))
        assert rep is None

    def test_no_siblings_low_score(self):
        ev = _evidence(sibling_tokens=[])
        rep = _compute_score("0x" + "de" * 20, ev)
        assert rep.score < 50
        assert rep.cluster_size == 0
        assert any("No sibling" in e for e in rep.evidence)

    def test_single_token_mild_penalty(self):
        tokens = [_launched("0x01" + "00" * 19)]
        ev = _evidence(sibling_tokens=tokens)
        rep = _compute_score("0x" + "de" * 20, ev)
        assert any("Single token" in e for e in rep.evidence)


# --- Single project ---


class TestSingleProject:
    def test_one_alive_token(self):
        tokens = [_launched("0x01" + "00" * 19, outcome="alive")]
        ev = _evidence(
            sibling_tokens=tokens,
            sibling_data=[{
                "address": "0x01" + "00" * 19,
                "holders": [], "pairs": [], "contract": None, "counters": None,
            }],
        )
        rep = _compute_score("0x" + "de" * 20, ev)
        assert rep.cluster_size == 1
        assert rep.historical_success_rate == 1.0
        assert rep.historical_failure_rate == 0.0

    def test_one_rugged_token(self):
        tokens = [_launched("0x01" + "00" * 19, outcome="likely_rugged", liq=0)]
        ev = _evidence(sibling_tokens=tokens)
        rep = _compute_score("0x" + "de" * 20, ev)
        assert rep.historical_failure_rate == 1.0
        assert rep.score < 50


# --- Multiple successful launches ---


class TestMultipleSuccesses:
    def test_clean_ecosystem_high_score(self):
        tokens = [
            _launched(f"0x{i:040x}", name=f"T{i}", outcome="alive", liq=5000.0)
            for i in range(5)
        ]
        sibling_data = [
            {
                "address": f"0x{i:040x}",
                "holders": [
                    {"address": {"hash": "0xholder1"}, "value": "1000"},
                    {"address": {"hash": "0xholder2"}, "value": "500"},
                ],
                "pairs": [{
                    "liquidity": {"usd": 8000},
                    "marketCap": 50000,
                    "info": {"websites": [], "socials": []},
                }],
                "contract": {"is_verified": True, "name": "StandardToken"},
                "counters": {"token_holders_count": "200"},
            }
            for i in range(5)
        ]
        ev = _evidence(
            sibling_tokens=tokens,
            sibling_data=sibling_data,
            current_holders={"0xholder1", "0xholder2"},
            current_template="StandardToken",
        )
        rep = _compute_score("0x" + "de" * 20, ev)
        assert rep.score >= 70
        assert rep.cluster_confidence == "high"
        assert rep.historical_success_rate == 1.0
        assert rep.avg_liquidity_usd is not None
        assert rep.avg_holder_count is not None
        assert any("surviving" in e or "success" in e for e in rep.evidence)

    def test_holder_overlap_boosts_score(self):
        tokens = [_launched(f"0x{i:040x}") for i in range(3)]
        shared_addr = "0xshared"
        sibling_data = [
            {
                "address": f"0x{i:040x}",
                "holders": [{"address": {"hash": shared_addr}}],
                "pairs": [], "contract": None, "counters": None,
            }
            for i in range(3)
        ]
        ev_no = _evidence(sibling_tokens=tokens, sibling_data=sibling_data)
        ev_yes = _evidence(
            sibling_tokens=tokens, sibling_data=sibling_data,
            current_holders={shared_addr},
        )
        rep_no = _compute_score("0x" + "de" * 20, ev_no)
        rep_yes = _compute_score("0x" + "de" * 20, ev_yes)
        assert rep_yes.score >= rep_no.score


# --- Multiple rugs ---


class TestMultipleRugs:
    def test_serial_rugger_low_score(self):
        tokens = [
            _launched(f"0x{i:040x}", name=f"R{i}", outcome="likely_rugged", liq=0)
            for i in range(5)
        ]
        ev = _evidence(sibling_tokens=tokens)
        rep = _compute_score("0x" + "de" * 20, ev)
        assert rep.score < 30
        assert rep.historical_failure_rate == 1.0
        assert any("rugged" in e.lower() or "abandoned" in e.lower() for e in rep.evidence)

    def test_mixed_history(self):
        tokens = [
            _launched("0x01" + "00" * 19, outcome="alive"),
            _launched("0x02" + "00" * 19, outcome="likely_rugged", liq=0),
            _launched("0x03" + "00" * 19, outcome="alive"),
        ]
        ev = _evidence(sibling_tokens=tokens)
        rep = _compute_score("0x" + "de" * 20, ev)
        assert 30 <= rep.score <= 70
        assert rep.funding_reputation != "clean" or rep.funding_reputation is None


# --- Shared funding wallet ---


class TestSharedFundingWallet:
    def test_clean_funder_boosts(self):
        tokens = [
            _launched(f"0x{i:040x}", outcome="alive") for i in range(3)
        ]
        ev = _evidence(
            sibling_tokens=tokens,
            funding_wallet="0xfunder" + "00" * 16,
        )
        rep = _compute_score("0x" + "de" * 20, ev)
        assert rep.funding_reputation == "clean"
        assert rep.funding_wallet is not None
        assert any("Funding wallet" in e for e in rep.evidence)

    def test_rug_funder_penalizes(self):
        tokens = [
            _launched("0x01" + "00" * 19, outcome="likely_rugged", liq=0),
            _launched("0x02" + "00" * 19, outcome="likely_rugged", liq=0),
            _launched("0x03" + "00" * 19, outcome="alive"),
        ]
        ev = _evidence(
            sibling_tokens=tokens,
            funding_wallet="0xfunder" + "00" * 16,
        )
        rep = _compute_score("0x" + "de" * 20, ev)
        assert rep.funding_reputation == "rug_linked"
        assert any("rugs" in e.lower() or "rug" in e.lower() for e in rep.evidence)


# --- Shared deployers ---


class TestSharedDeployers:
    def test_deployer_recorded(self):
        ev = _evidence(sibling_tokens=[_launched("0x01" + "00" * 19)])
        rep = _compute_score("0xdeployer" + "00" * 15, ev)
        assert rep.deployer == "0xdeployer" + "00" * 15


# --- Shared liquidity wallets ---


class TestSharedLiquidityWallets:
    def test_shared_wallets_counted(self):
        tokens = [_launched(f"0x{i:040x}") for i in range(3)]
        # Same wallet holds multiple sibling tokens
        shared = "0xlp" + "00" * 18
        sibling_data = [
            {
                "address": f"0x{i:040x}",
                "holders": [{"address": {"hash": shared}}],
                "pairs": [], "contract": None, "counters": None,
            }
            for i in range(3)
        ]
        ev = _evidence(
            sibling_tokens=tokens,
            sibling_data=sibling_data,
            current_holders={shared},
        )
        rep = _compute_score("0x" + "de" * 20, ev)
        assert len([s for s in rep.siblings if s.shared_wallets > 0]) > 0


# --- Missing data ---


class TestMissingData:
    def test_no_sibling_data_still_works(self):
        tokens = [_launched(f"0x{i:040x}") for i in range(3)]
        ev = _evidence(sibling_tokens=tokens, sibling_data=[])
        rep = _compute_score("0x" + "de" * 20, ev)
        assert 0 <= rep.score <= 100
        assert rep.cluster_size == 3

    def test_empty_evidence(self):
        ev = _evidence()
        rep = _compute_score("0x" + "de" * 20, ev)
        assert 0 <= rep.score <= 100
        assert rep.cluster_confidence == "low"

    def test_null_counters(self):
        tokens = [_launched("0x01" + "00" * 19)]
        sibling_data = [{
            "address": "0x01" + "00" * 19,
            "holders": [], "pairs": [],
            "contract": None, "counters": None,
        }]
        ev = _evidence(sibling_tokens=tokens, sibling_data=sibling_data)
        rep = _compute_score("0x" + "de" * 20, ev)
        assert rep.avg_holder_count is None


# --- Partial failures ---


class TestPartialFailures:
    def test_some_siblings_missing_data(self):
        tokens = [_launched(f"0x{i:040x}") for i in range(4)]
        sibling_data = [
            {
                "address": "0x" + "00" * 19 + "0",
                "holders": [{"address": {"hash": "0xh1"}}],
                "pairs": [{"liquidity": {"usd": 5000}, "info": {}}],
                "contract": {"is_verified": True, "name": "T"},
                "counters": {"token_holders_count": "100"},
            },
            # Other siblings have no data (not in sibling_data)
        ]
        ev = _evidence(sibling_tokens=tokens, sibling_data=sibling_data)
        rep = _compute_score("0x" + "de" * 20, ev)
        assert 0 <= rep.score <= 100
        assert rep.cluster_size == 4


# --- Cache behaviour ---


class TestCacheBehaviour:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        _cache.clear()
        yield
        _cache.clear()

    def test_cache_hit_skips_provider(self, monkeypatch):
        deployer = "0x" + "de" * 20
        cached = DeveloperNetworkResult(
            score=75, evidence=["+ cached"], deployer=deployer, cluster_size=3,
        )
        _cache.set(f"dev_net:{deployer.lower()}", cached)

        called = {"n": 0}

        async def boom(self, depl, result):
            called["n"] += 1
            raise AssertionError("should not call provider on cache hit")

        monkeypatch.setattr(OnChainNetworkProvider, "gather", boom)
        result = _base_result(dev=_dev(creator_address=deployer))
        rep = _run(evaluate(result))
        assert rep.score == 75
        assert called["n"] == 0

    def test_cache_miss_calls_provider(self, monkeypatch):
        called = {"n": 0}

        async def fake_gather(self, deployer, result):
            called["n"] += 1
            return _evidence()

        monkeypatch.setattr(OnChainNetworkProvider, "gather", fake_gather)
        result = _base_result(dev=_dev())
        _run(evaluate(result))
        assert called["n"] == 1

    def test_second_call_uses_cache(self, monkeypatch):
        called = {"n": 0}

        async def fake_gather(self, deployer, result):
            called["n"] += 1
            return _evidence()

        monkeypatch.setattr(OnChainNetworkProvider, "gather", fake_gather)
        result = _base_result(dev=_dev())
        _run(evaluate(result))
        _run(evaluate(result))
        assert called["n"] == 1


# --- Parallel execution ---


class TestParallelExecution:
    def test_multiple_siblings_fetched_concurrently(self, monkeypatch):
        fetch_count = {"n": 0}

        async def fake_gather(self, deployer, result):
            fetch_count["n"] += 1
            tokens = [_launched(f"0x{i:040x}") for i in range(3)]
            return _evidence(sibling_tokens=tokens)

        monkeypatch.setattr(OnChainNetworkProvider, "gather", fake_gather)
        result = _base_result(dev=_dev())
        rep = _run(evaluate(result))
        assert rep is not None
        assert fetch_count["n"] == 1  # single provider call


# --- Score calculation ---


class TestScoreCalculation:
    def test_score_clamped_0_100(self):
        # Very bad: all rugged, no data
        tokens = [
            _launched(f"0x{i:040x}", outcome="likely_rugged", liq=0)
            for i in range(10)
        ]
        ev = _evidence(sibling_tokens=tokens)
        rep = _compute_score("0x" + "de" * 20, ev)
        assert 0 <= rep.score <= 100

        # Very good: all alive, high liquidity, lots of overlap
        tokens2 = [_launched(f"0x{i:040x}", outcome="alive") for i in range(10)]
        sibling_data = [
            {
                "address": f"0x{i:040x}",
                "holders": [{"address": {"hash": f"0xh{j}"}} for j in range(10)],
                "pairs": [{"liquidity": {"usd": 50000}, "marketCap": 500000, "info": {}}],
                "contract": {"is_verified": True, "name": "Standard"},
                "counters": {"token_holders_count": "1000"},
            }
            for i in range(10)
        ]
        ev2 = _evidence(
            sibling_tokens=tokens2, sibling_data=sibling_data,
            current_holders={f"0xh{j}" for j in range(10)},
            funding_wallet="0xfunder",
        )
        rep2 = _compute_score("0x" + "de" * 20, ev2)
        assert 0 <= rep2.score <= 100

    def test_network_risk_inverse_of_score(self):
        ev = _evidence(sibling_tokens=[_launched("0x01" + "00" * 19)])
        rep = _compute_score("0x" + "de" * 20, ev)
        assert rep.network_risk == round(100 - rep.score, 1)

    def test_network_trust_equals_score(self):
        ev = _evidence(sibling_tokens=[_launched("0x01" + "00" * 19)])
        rep = _compute_score("0x" + "de" * 20, ev)
        assert rep.network_trust == round(rep.score, 1)

    def test_project_quality_computed(self):
        tokens = [_launched(f"0x{i:040x}") for i in range(5)]
        sibling_data = [
            {
                "address": f"0x{i:040x}",
                "holders": [],
                "pairs": [{"liquidity": {"usd": 5000}, "info": {}}],
                "contract": None,
                "counters": {"token_holders_count": "200"},
            }
            for i in range(5)
        ]
        ev = _evidence(sibling_tokens=tokens, sibling_data=sibling_data)
        rep = _compute_score("0x" + "de" * 20, ev)
        assert rep.project_quality is not None
        assert 0 <= rep.project_quality <= 100


# --- Evidence generation ---


class TestEvidenceGeneration:
    def test_positive_evidence_prefixed(self):
        tokens = [_launched(f"0x{i:040x}") for i in range(5)]
        sibling_data = [
            {
                "address": f"0x{i:040x}",
                "holders": [],
                "pairs": [{"liquidity": {"usd": 10000}, "info": {}}],
                "contract": {"is_verified": True, "name": "Std"},
                "counters": {"token_holders_count": "500"},
            }
            for i in range(5)
        ]
        ev = _evidence(
            sibling_tokens=tokens, sibling_data=sibling_data,
            funding_wallet="0xfunder",
        )
        rep = _compute_score("0x" + "de" * 20, ev)
        positives = [e for e in rep.evidence if e.startswith("+")]
        assert len(positives) >= 3

    def test_negative_evidence_prefixed(self):
        tokens = [
            _launched(f"0x{i:040x}", outcome="likely_rugged", liq=0)
            for i in range(5)
        ]
        ev = _evidence(sibling_tokens=tokens, funding_wallet="0xfunder")
        rep = _compute_score("0x" + "de" * 20, ev)
        negatives = [e for e in rep.evidence if e.startswith("-")]
        assert len(negatives) >= 2

    def test_evidence_is_human_readable(self):
        tokens = [_launched(f"0x{i:040x}") for i in range(3)]
        ev = _evidence(sibling_tokens=tokens, funding_wallet="0xfunder")
        rep = _compute_score("0x" + "de" * 20, ev)
        for line in rep.evidence:
            assert line.startswith("+") or line.startswith("-")
            assert len(line) < 200


# --- API compatibility ---


class TestAPICompatibility:
    def test_result_serializable(self):
        tokens = [_launched(f"0x{i:040x}") for i in range(3)]
        ev = _evidence(sibling_tokens=tokens)
        rep = _compute_score("0x" + "de" * 20, ev)
        data = rep.model_dump()
        assert isinstance(data["score"], int)
        assert isinstance(data["siblings"], list)
        assert isinstance(data["evidence"], list)

    def test_network_on_response_model(self):
        net = DeveloperNetworkResult(score=60, deployer="0x" + "de" * 20, cluster_size=3)
        result = _base_result(
            dev=_dev(),
            developer_network=net,
        )
        assert result.developer_network is not None
        assert result.developer_network.score == 60
        dumped = result.model_dump()
        assert "developer_network" in dumped


# --- Opportunity Score integration ---


class TestOpportunityIntegration:
    def test_scorer_registered(self):
        from app.services.opportunity_score import SCORERS
        net = DeveloperNetworkResult(score=60, deployer="0x" + "de" * 20, cluster_size=3)
        r = _base_result(developer_network=net)
        scorer_names = []
        for s in SCORERS:
            sr = s(r)
            if sr:
                scorer_names.append(sr.name)
        assert "developer_network" in scorer_names

    def test_score_none_when_missing(self):
        r = _base_result()
        assert _score_developer_network(r) is None

    def test_score_positive(self):
        net = DeveloperNetworkResult(score=80, deployer="0x" + "de" * 20, cluster_size=5)
        r = _base_result(developer_network=net)
        sr = _score_developer_network(r)
        assert sr is not None
        assert sr.value == 80
        assert sr.positive is True
        assert sr.name == "developer_network"

    def test_score_negative(self):
        net = DeveloperNetworkResult(score=20, deployer="0x" + "de" * 20, cluster_size=1)
        r = _base_result(developer_network=net)
        sr = _score_developer_network(r)
        assert sr is not None
        assert sr.value == 20
        assert sr.positive is False

    def test_feeds_into_opportunity_aggregate(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.opportunity_score.settings.opportunity_score_weights",
            {"developer_network": 100},
        )
        net = DeveloperNetworkResult(score=90, deployer="0x" + "de" * 20, cluster_size=5)
        r = _base_result(developer_network=net)
        opp = score_opportunity(r)
        assert opp.alpha_score == 90
        assert any(s.name == "developer_network" for s in opp.signals)


# --- Deterministic output ---


class TestDeterministicOutput:
    def test_same_input_same_output(self):
        tokens = [
            _launched("0x01" + "00" * 19, outcome="alive"),
            _launched("0x02" + "00" * 19, outcome="likely_rugged", liq=0),
            _launched("0x03" + "00" * 19, outcome="alive"),
        ]
        sibling_data = [
            {
                "address": "0x01" + "00" * 19,
                "holders": [{"address": {"hash": "0xshared"}}],
                "pairs": [{"liquidity": {"usd": 3000}, "info": {}}],
                "contract": {"is_verified": True, "name": "MyToken"},
                "counters": {"token_holders_count": "150"},
            },
        ]
        ev = _evidence(
            sibling_tokens=tokens, sibling_data=sibling_data,
            current_holders={"0xshared"}, funding_wallet="0xfunder",
        )
        rep1 = _compute_score("0x" + "de" * 20, ev)
        rep2 = _compute_score("0x" + "de" * 20, ev)
        assert rep1.score == rep2.score
        assert rep1.evidence == rep2.evidence
        assert rep1.cluster_size == rep2.cluster_size
        assert rep1.historical_success_rate == rep2.historical_success_rate

    def test_infrastructure_reuse_detected(self):
        tokens = [_launched(f"0x{i:040x}") for i in range(3)]
        sibling_data = [
            {
                "address": f"0x{i:040x}",
                "holders": [],
                "pairs": [],
                "contract": {"is_verified": True, "name": "SharedTemplate"},
                "counters": None,
            }
            for i in range(3)
        ]
        ev = _evidence(
            sibling_tokens=tokens, sibling_data=sibling_data,
            current_template="SharedTemplate",
        )
        rep = _compute_score("0x" + "de" * 20, ev)
        assert rep.infrastructure_reuse_score is not None
        assert rep.infrastructure_reuse_score > 0

    def test_shared_social_links(self):
        tokens = [_launched(f"0x{i:040x}") for i in range(2)]
        sibling_data = [
            {
                "address": f"0x{i:040x}",
                "holders": [],
                "pairs": [{
                    "liquidity": {"usd": 1000},
                    "info": {
                        "websites": [{"url": "https://example.com"}],
                        "socials": [{"type": "twitter", "url": "https://x.com/project"}],
                    },
                }],
                "contract": None, "counters": None,
            }
            for i in range(2)
        ]
        ev = _evidence(sibling_tokens=tokens, sibling_data=sibling_data)
        rep = _compute_score("0x" + "de" * 20, ev)
        assert any("shared" in e.lower() for e in rep.evidence)
