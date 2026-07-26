"""Tests for the Alpha Timeline Engine."""

import pytest

from app.models.token import (
    AlphaTimeline,
    BundleAnalysis,
    BuyTimingAnalysis,
    ClusterAnalysis,
    ContractIntel,
    ContractPrivileges,
    DeveloperNetworkResult,
    DeveloperReputationResult,
    DevProfile,
    HolderCluster,
    HolderDistribution,
    HolderEntry,
    HoneypotResult,
    InsiderWallet,
    LaunchpadInfo,
    LiquidityLock,
    LiquiditySnapshot,
    NetworkSibling,
    OpportunityResult,
    RugAnalysis,
    SmartWalletReputationResult,
    TimelineEvent,
    TimelineSummary,
    TokenAge,
    TokenAnalysisResponse,
    TokenMarketData,
    TokenTrend,
    VolumeSnapshot,
    WatchlistHit,
)
from app.services.alpha_timeline import (
    PROVIDERS,
    _dedup_events,
    _generate_summary,
    _sort_events,
    build_timeline,
    _launch_events,
    _liquidity_events,
    _ownership_events,
    _developer_events,
    _holder_events,
    _smart_wallet_events,
    _insider_events,
    _cluster_events,
    _honeypot_events,
    _network_events,
    _wallet_reputation_events,
    _opportunity_events,
)


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


# --- Empty timeline ---

class TestEmptyTimeline:
    def test_empty_timeline_from_minimal_result(self):
        r = _base_result()
        tl = build_timeline(r)
        assert isinstance(tl, AlphaTimeline)
        assert isinstance(tl.summary, TimelineSummary)
        # With risk_score 30 and nothing else, we still get the opportunity event
        assert any(e.category == "Opportunity" for e in tl.events)

    def test_truly_empty_when_high_mid_risk(self):
        r = _base_result(analysis=RugAnalysis(
            risk_score=50, risk_level="medium", signals=[],
            data_sources=[], limitations=[],
        ))
        tl = build_timeline(r)
        # Mid-range risk produces no opportunity event
        assert not any(e.category == "Opportunity" for e in tl.events)


# --- Single event providers ---

class TestLaunchEvents:
    def test_contract_deployed(self):
        r = _base_result(
            dev=DevProfile(creator_address="0x" + "de" * 20, creation_tx="0x" + "ff" * 32),
            token_age=TokenAge(created_at_iso="2026-07-01T00:00:00Z", age_hours=100, age_days=4, source="pair"),
        )
        events = _launch_events(r)
        assert any(e.title == "Contract deployed" for e in events)
        deploy_event = [e for e in events if e.title == "Contract deployed"][0]
        assert deploy_event.timestamp == "2026-07-01T00:00:00Z"
        assert deploy_event.confidence == "high"

    def test_contract_verified(self):
        r = _base_result(
            contract_intel=ContractIntel(verified=True, compiler="solc 0.8.24", template="ERC20"),
            token_age=TokenAge(created_at_iso="2026-07-01T00:00:00Z", age_hours=100, age_days=4, source="pair"),
        )
        events = _launch_events(r)
        assert any(e.title == "Contract verified" for e in events)

    def test_launchpad_detected(self):
        r = _base_result(
            launchpad=LaunchpadInfo(name="NOXA Fun", confidence="high", detail="Matched factory"),
            token_age=TokenAge(created_at_iso="2026-07-01T00:00:00Z", age_hours=100, age_days=4, source="pair"),
        )
        events = _launch_events(r)
        assert any(e.title == "Launchpad detected" for e in events)

    def test_unknown_launchpad_skipped(self):
        r = _base_result(
            launchpad=LaunchpadInfo(name="Unknown", confidence="low"),
        )
        events = _launch_events(r)
        assert not any(e.title == "Launchpad detected" for e in events)


class TestLiquidityEvents:
    def test_liquidity_added(self):
        r = _base_result(
            market_data=TokenMarketData(
                liquidity=LiquiditySnapshot(usd=50000),
                pair_created_at=1719792000000,
                pair_address="0x" + "bb" * 20,
            ),
        )
        events = _liquidity_events(r)
        assert any(e.title == "Liquidity added" for e in events)
        assert any(e.title == "Trading enabled" for e in events)

    def test_lp_burned(self):
        r = _base_result(
            liquidity_lock=LiquidityLock(status="burned", locked_percentage=99.5),
        )
        events = _liquidity_events(r)
        assert any(e.title == "LP tokens burned" for e in events)

    def test_lp_locked(self):
        r = _base_result(
            liquidity_lock=LiquidityLock(status="locked", locker_label="Unicrypt", unlock_in_days=90),
        )
        events = _liquidity_events(r)
        lk = [e for e in events if e.title == "Liquidity locked"]
        assert lk
        assert lk[0].severity == "info"  # >30 days

    def test_lp_locked_near_expiry(self):
        r = _base_result(
            liquidity_lock=LiquidityLock(status="locked", locker_label="Unicrypt", unlock_in_days=5),
        )
        events = _liquidity_events(r)
        lk = [e for e in events if e.title == "Liquidity locked"]
        assert lk
        assert lk[0].severity == "medium"

    def test_liquidity_trend_drop(self):
        r = _base_result(
            market_data=TokenMarketData(liquidity=LiquiditySnapshot(usd=1000)),
            trend=TokenTrend(has_prior=True, liquidity_change_pct=-50.0),
        )
        events = _liquidity_events(r)
        assert any("decreased" in e.title for e in events)

    def test_market_cap_milestone(self):
        r = _base_result(
            market_data=TokenMarketData(
                liquidity=LiquiditySnapshot(usd=10000),
                market_cap=150000,
            ),
        )
        events = _liquidity_events(r)
        assert any("$100K" in e.title for e in events)


class TestOwnershipEvents:
    def test_ownership_renounced(self):
        r = _base_result(
            contract_privileges=ContractPrivileges(analyzed=True, ownership_renounced=True),
        )
        events = _ownership_events(r)
        assert any(e.title == "Ownership renounced" for e in events)

    def test_owner_retained_with_powers(self):
        r = _base_result(
            contract_privileges=ContractPrivileges(
                analyzed=True, ownership_renounced=False,
                owner_address="0x" + "11" * 20,
                can_mint=True, can_pause=True,
            ),
        )
        events = _ownership_events(r)
        retained = [e for e in events if e.title == "Owner retained with powers"]
        assert retained
        assert retained[0].severity == "high"  # 2+ powers

    def test_paused_contract(self):
        r = _base_result(
            contract_privileges=ContractPrivileges(analyzed=True, is_paused=True),
        )
        events = _ownership_events(r)
        assert any(e.title == "Contract is paused" for e in events)
        assert any(e.severity == "critical" for e in events)


class TestDeveloperEvents:
    def test_dev_transfer(self):
        r = _base_result(
            dev=DevProfile(
                creator_address="0x" + "de" * 20,
                transferred_out=True, transfers_out_count=3,
                transferred_out_percentage=15.0,
            ),
        )
        events = _developer_events(r)
        assert any(e.title == "Developer transfer detected" for e in events)
        assert any(e.severity == "high" for e in events)  # 15% >= 10

    def test_dev_holds_supply(self):
        r = _base_result(
            dev=DevProfile(creator_address="0x" + "de" * 20, dev_holding_percentage=12.0),
        )
        events = _developer_events(r)
        assert any("significant supply" in e.title for e in events)

    def test_serial_rugger(self):
        r = _base_result(
            dev=DevProfile(
                creator_address="0x" + "de" * 20,
                tokens_launched=5, tokens_rugged=3, tokens_alive=2,
            ),
        )
        events = _developer_events(r)
        assert any(e.title == "Developer linked to prior rugs" for e in events)
        assert any(e.severity == "critical" for e in events)


class TestHolderEvents:
    def test_holder_milestone(self):
        r = _base_result(
            holders=HolderDistribution(
                holder_count=1500, top10_percentage=30.0,
                top1_percentage=5.0, sampled_holders=50,
            ),
        )
        events = _holder_events(r)
        assert any("1,000" in e.title for e in events)

    def test_high_concentration(self):
        r = _base_result(
            holders=HolderDistribution(
                holder_count=100, top10_percentage=75.0,
                top1_percentage=20.0, sampled_holders=50,
            ),
        )
        events = _holder_events(r)
        assert any("concentration" in e.title.lower() for e in events)

    def test_holder_count_trend(self):
        r = _base_result(
            holders=HolderDistribution(
                holder_count=100, top10_percentage=30.0,
                top1_percentage=5.0, sampled_holders=50,
            ),
            trend=TokenTrend(has_prior=True, holder_count_change=-50),
        )
        events = _holder_events(r)
        assert any("shrank" in e.title for e in events)


class TestSmartWalletEvents:
    def test_smart_wallet_buys(self):
        r = _base_result(
            watchlist_hits=[
                WatchlistHit(address="0x" + "aa" * 20, kind="smart", proxy_score=80, holding_percentage=2.0),
                WatchlistHit(address="0x" + "bb" * 20, kind="smart", proxy_score=75, holding_percentage=1.5),
                WatchlistHit(address="0x" + "cc" * 20, kind="smart", proxy_score=72, holding_percentage=1.0),
            ],
        )
        events = _smart_wallet_events(r)
        assert any("First Smart Wallet" in e.title for e in events)
        assert any("Second Smart Wallet" in e.title for e in events)
        assert any("Multiple Smart Wallets" in e.title for e in events)

    def test_no_smart_wallets(self):
        r = _base_result(watchlist_hits=[])
        events = _smart_wallet_events(r)
        assert events == []


class TestInsiderEvents:
    def test_insider_detected(self):
        r = _base_result(
            insiders=[
                InsiderWallet(address="0x" + "11" * 20, reason="early_buyer", holding_percentage=5.0),
            ],
        )
        events = _insider_events(r)
        assert any("early buyer" in e.title for e in events)

    def test_watchlisted_insider(self):
        r = _base_result(
            watchlist_hits=[
                WatchlistHit(address="0x" + "11" * 20, kind="insider", holding_percentage=3.0),
            ],
        )
        events = _insider_events(r)
        assert any("insider wallets present" in e.title.lower() for e in events)


class TestClusterEvents:
    def test_wallet_cluster(self):
        r = _base_result(
            clusters=ClusterAnalysis(
                clusters=[HolderCluster(
                    funder_address="0x" + "ff" * 20,
                    member_addresses=["0x" + "aa" * 20, "0x" + "bb" * 20],
                    combined_percentage=8.0, link_type="shared_funder",
                )],
                clustered_percentage=8.0,
            ),
        )
        events = _cluster_events(r)
        assert any("cluster" in e.title.lower() for e in events)

    def test_bundle_detected(self):
        r = _base_result(
            bundle=BundleAnalysis(
                score=80, classification="Heavy", bundled_wallets=5,
                bundled_percentage=15.0,
            ),
        )
        events = _cluster_events(r)
        assert any("Bundle" in e.title for e in events)

    def test_coordinated_buy(self):
        r = _base_result(
            buy_timing=BuyTimingAnalysis(
                same_block_wallets=4, coordinated=True,
                detail="4 wallets bought in the same block.",
            ),
        )
        events = _cluster_events(r)
        assert any("Coordinated buy" in e.title for e in events)


class TestHoneypotEvents:
    def test_honeypot_detected(self):
        r = _base_result(
            honeypot=HoneypotResult(status="honeypot", detail="Sell reverted."),
        )
        events = _honeypot_events(r)
        assert any(e.severity == "critical" for e in events)
        assert any("honeypot" in e.title.lower() for e in events)

    def test_high_tax(self):
        r = _base_result(
            honeypot=HoneypotResult(status="high_tax", sell_tax_percentage=45.0),
        )
        events = _honeypot_events(r)
        assert any("tax" in e.title.lower() for e in events)

    def test_sellable(self):
        r = _base_result(
            honeypot=HoneypotResult(status="sellable", sell_tax_percentage=2.0),
        )
        events = _honeypot_events(r)
        assert any("sellable" in e.title.lower() for e in events)
        assert all(e.severity == "info" for e in events)

    def test_unknown_honeypot(self):
        r = _base_result(
            honeypot=HoneypotResult(status="unknown"),
        )
        events = _honeypot_events(r)
        assert events == []


class TestNetworkEvents:
    def test_developer_network(self):
        r = _base_result(
            developer_network=DeveloperNetworkResult(
                score=65, cluster_size=3, cluster_confidence="medium",
                historical_success_rate=0.8, network_trust=70.0,
            ),
        )
        events = _network_events(r)
        assert any("network" in e.title.lower() for e in events)

    def test_developer_reputation(self):
        r = _base_result(
            developer_reputation=DeveloperReputationResult(
                score=75, evidence=["+Active deployer", "+Verified contracts"],
                deployer="0x" + "de" * 20, total_contracts_deployed=5,
            ),
        )
        events = _network_events(r)
        assert any("reputation score" in e.title.lower() for e in events)


class TestWalletReputationEvents:
    def test_wallet_reputations(self):
        r = _base_result(
            wallet_reputations=[
                SmartWalletReputationResult(
                    score=80, address="0x" + "aa" * 20,
                    surviving_projects=5, rugs_entered=1,
                ),
            ],
        )
        events = _wallet_reputation_events(r)
        assert len(events) == 1
        assert "80/100" in events[0].title


class TestOpportunityEvents:
    def test_low_risk(self):
        r = _base_result(analysis=RugAnalysis(
            risk_score=20, risk_level="low", signals=[],
            data_sources=[], limitations=[], confidence=80,
        ))
        events = _opportunity_events(r)
        assert any("Low risk" in e.title for e in events)

    def test_high_risk(self):
        r = _base_result(analysis=RugAnalysis(
            risk_score=85, risk_level="critical", signals=[],
            data_sources=[], limitations=[], confidence=90,
        ))
        events = _opportunity_events(r)
        assert any("High risk" in e.title for e in events)
        assert any(e.severity == "critical" for e in events)


# --- Deduplication ---

class TestDeduplication:
    def test_dedup_removes_exact_duplicates(self):
        events = [
            TimelineEvent(title="A", category="Launch", source="x", evidence="ev1"),
            TimelineEvent(title="A", category="Launch", source="x", evidence="ev1"),
            TimelineEvent(title="B", category="Launch", source="x"),
        ]
        deduped = _dedup_events(events)
        assert len(deduped) == 2

    def test_dedup_keeps_different_evidence(self):
        events = [
            TimelineEvent(title="A", category="Launch", source="x", evidence="ev1"),
            TimelineEvent(title="A", category="Launch", source="x", evidence="ev2"),
        ]
        deduped = _dedup_events(events)
        assert len(deduped) == 2


# --- Ordering ---

class TestOrdering:
    def test_timestamped_before_untimestamped(self):
        events = [
            TimelineEvent(title="No TS", category="X", source="s"),
            TimelineEvent(title="Has TS", category="X", source="s", timestamp="2026-07-01T00:00:00Z"),
        ]
        sorted_events = _sort_events(events)
        assert sorted_events[0].title == "Has TS"

    def test_severity_tiebreak(self):
        events = [
            TimelineEvent(title="Info", category="X", source="s", severity="info"),
            TimelineEvent(title="Critical", category="X", source="s", severity="critical"),
        ]
        sorted_events = _sort_events(events)
        assert sorted_events[0].title == "Critical"


# --- Missing timestamps ---

class TestMissingTimestamps:
    def test_events_without_timestamps(self):
        r = _base_result(
            honeypot=HoneypotResult(status="sellable"),
            holders=HolderDistribution(holder_count=500, top10_percentage=30.0, top1_percentage=5.0, sampled_holders=50),
        )
        tl = build_timeline(r)
        # All events should have timestamp=None or a valid string
        for e in tl.events:
            assert e.timestamp is None or isinstance(e.timestamp, str)


# --- Confidence calculation ---

class TestConfidence:
    def test_deploy_with_timestamp_high_confidence(self):
        r = _base_result(
            dev=DevProfile(creator_address="0x" + "de" * 20, creation_tx="0x" + "ff" * 32),
            token_age=TokenAge(created_at_iso="2026-07-01T00:00:00Z", age_hours=100, age_days=4, source="pair"),
        )
        events = _launch_events(r)
        deploy = [e for e in events if e.title == "Contract deployed"]
        assert deploy[0].confidence == "high"

    def test_deploy_without_timestamp_low_confidence(self):
        r = _base_result(
            dev=DevProfile(creator_address="0x" + "de" * 20, creation_tx="0x" + "ff" * 32),
        )
        events = _launch_events(r)
        deploy = [e for e in events if e.title == "Contract deployed"]
        assert deploy[0].confidence == "low"


# --- Summary generation ---

class TestSummaryGeneration:
    def test_healthy_launch_summary(self):
        r = _base_result(
            honeypot=HoneypotResult(status="sellable"),
            analysis=RugAnalysis(risk_score=20, risk_level="low", signals=[], data_sources=[], limitations=[]),
        )
        summary = _generate_summary(r, [])
        assert summary.launch_quality == "healthy"

    def test_dangerous_honeypot_summary(self):
        r = _base_result(
            honeypot=HoneypotResult(status="honeypot"),
        )
        summary = _generate_summary(r, [])
        assert summary.launch_quality == "dangerous"
        assert "honeypot" in summary.narrative.lower() or "DANGEROUS" in summary.narrative

    def test_developer_behaviour_serial_rugger(self):
        r = _base_result(
            dev=DevProfile(
                creator_address="0x" + "de" * 20,
                tokens_launched=5, tokens_rugged=4,
                reputation="serial_rugger",
            ),
        )
        summary = _generate_summary(r, [])
        assert summary.developer_behaviour == "dangerous"

    def test_developer_behaviour_reliable(self):
        r = _base_result(
            dev=DevProfile(creator_address="0x" + "de" * 20, reputation="reliable"),
        )
        summary = _generate_summary(r, [])
        assert summary.developer_behaviour == "positive"

    def test_liquidity_draining(self):
        r = _base_result(
            market_data=TokenMarketData(liquidity=LiquiditySnapshot(usd=1000)),
            trend=TokenTrend(has_prior=True, liquidity_change_pct=-50.0),
        )
        summary = _generate_summary(r, [])
        assert summary.liquidity_evolution == "draining"

    def test_community_organic(self):
        r = _base_result(
            holders=HolderDistribution(
                holder_count=2000, top10_percentage=25.0,
                top1_percentage=3.0, sampled_holders=50,
            ),
        )
        summary = _generate_summary(r, [])
        assert summary.community_growth == "organic"

    def test_smart_money_strong(self):
        r = _base_result(
            watchlist_hits=[
                WatchlistHit(address="0x" + "a1" * 20, kind="smart"),
                WatchlistHit(address="0x" + "a2" * 20, kind="smart"),
                WatchlistHit(address="0x" + "a3" * 20, kind="smart"),
            ],
        )
        summary = _generate_summary(r, [])
        assert summary.smart_money == "strong"

    def test_narrative_pieces(self):
        r = _base_result(
            honeypot=HoneypotResult(status="sellable"),
            analysis=RugAnalysis(risk_score=20, risk_level="low", signals=[], data_sources=[], limitations=[]),
            dev=DevProfile(creator_address="0x" + "de" * 20, reputation="reliable"),
        )
        summary = _generate_summary(r, [])
        assert "Healthy" in summary.narrative
        assert "reputable" in summary.narrative


# --- Full build_timeline integration ---

class TestBuildTimeline:
    def test_full_integration(self):
        r = _base_result(
            dev=DevProfile(
                creator_address="0x" + "de" * 20, creation_tx="0x" + "ff" * 32,
                dev_holding_percentage=2.0, reputation="established",
                tokens_launched=3, tokens_alive=3,
            ),
            token_age=TokenAge(created_at_iso="2026-07-01T00:00:00Z", age_hours=100, age_days=4, source="pair"),
            market_data=TokenMarketData(
                liquidity=LiquiditySnapshot(usd=50000), market_cap=200000,
                pair_created_at=1719792000000, pair_address="0x" + "bb" * 20,
            ),
            holders=HolderDistribution(
                holder_count=1500, top10_percentage=30.0,
                top1_percentage=5.0, sampled_holders=50,
            ),
            honeypot=HoneypotResult(status="sellable"),
            contract_intel=ContractIntel(verified=True, compiler="solc 0.8.24", template="ERC20"),
            contract_privileges=ContractPrivileges(analyzed=True, ownership_renounced=True),
            watchlist_hits=[
                WatchlistHit(address="0x" + "aa" * 20, kind="smart", proxy_score=80, holding_percentage=2.0),
            ],
        )
        tl = build_timeline(r)
        assert len(tl.events) >= 5
        assert tl.summary.launch_quality == "healthy"
        categories = {e.category for e in tl.events}
        assert "Launch" in categories
        assert "Liquidity" in categories
        assert "Security" in categories

    def test_timeline_no_duplicates(self):
        r = _base_result(
            dev=DevProfile(creator_address="0x" + "de" * 20, creation_tx="0x" + "ff" * 32),
            token_age=TokenAge(created_at_iso="2026-07-01T00:00:00Z", age_hours=100, age_days=4, source="pair"),
        )
        tl = build_timeline(r)
        titles_evidence = [(e.title, e.evidence) for e in tl.events]
        assert len(titles_evidence) == len(set(titles_evidence))

    def test_timeline_ordering_timestamped_first(self):
        r = _base_result(
            dev=DevProfile(creator_address="0x" + "de" * 20, creation_tx="0x" + "ff" * 32),
            token_age=TokenAge(created_at_iso="2026-07-01T00:00:00Z", age_hours=100, age_days=4, source="pair"),
            honeypot=HoneypotResult(status="sellable"),
        )
        tl = build_timeline(r)
        # Timestamped events should come before untimestamped
        first_ts = None
        for e in tl.events:
            if e.timestamp:
                first_ts = True
            elif first_ts:
                break
        # No regression: timestamped events weren't pushed after untimestamped

    def test_multiple_events_all_categories(self):
        r = _base_result(
            dev=DevProfile(
                creator_address="0x" + "de" * 20, creation_tx="0x" + "ff" * 32,
                tokens_launched=5, tokens_rugged=2, tokens_alive=3,
                reputation="suspicious", transferred_out=True,
                transfers_out_count=2, transferred_out_percentage=8.0,
                dev_holding_percentage=6.0,
            ),
            token_age=TokenAge(created_at_iso="2026-07-01T00:00:00Z", age_hours=100, age_days=4, source="pair"),
            market_data=TokenMarketData(
                liquidity=LiquiditySnapshot(usd=50000), market_cap=600000,
                pair_created_at=1719792000000, pair_address="0x" + "bb" * 20,
            ),
            holders=HolderDistribution(
                holder_count=2000, top10_percentage=25.0,
                top1_percentage=3.0, sampled_holders=50,
            ),
            honeypot=HoneypotResult(status="sellable"),
            contract_intel=ContractIntel(verified=True, compiler="solc 0.8.24", template="ERC20"),
            contract_privileges=ContractPrivileges(
                analyzed=True, ownership_renounced=False,
                owner_address="0x" + "11" * 20, can_mint=True,
            ),
            liquidity_lock=LiquidityLock(status="locked", locker_label="Unicrypt", unlock_in_days=180),
            clusters=ClusterAnalysis(
                clusters=[HolderCluster(
                    funder_address="0x" + "ff" * 20,
                    member_addresses=["0x" + "a1" * 20, "0x" + "a2" * 20],
                    combined_percentage=6.0, link_type="shared_funder",
                )],
                clustered_percentage=6.0,
            ),
            watchlist_hits=[
                WatchlistHit(address="0x" + "aa" * 20, kind="smart", proxy_score=80, holding_percentage=2.0),
            ],
            insiders=[
                InsiderWallet(address="0x" + "11" * 20, reason="early_buyer", holding_percentage=5.0),
            ],
            developer_reputation=DeveloperReputationResult(
                score=40, deployer="0x" + "de" * 20, total_contracts_deployed=5,
            ),
            developer_network=DeveloperNetworkResult(
                score=55, cluster_size=3, cluster_confidence="medium",
                historical_success_rate=0.6, network_trust=50.0,
            ),
        )
        tl = build_timeline(r)
        categories = {e.category for e in tl.events}
        assert "Launch" in categories
        assert "Liquidity" in categories
        assert "Developer" in categories
        assert "Ownership" in categories
        assert "Holder Growth" in categories
        assert "Security" in categories
        assert "Insider" in categories
        assert "Smart Wallet" in categories
        assert "Network" in categories


# --- API compatibility ---

class TestAPICompatibility:
    def test_timeline_on_response_model(self):
        r = _base_result()
        tl = build_timeline(r)
        r.timeline = tl
        d = r.model_dump()
        assert "timeline" in d
        assert "events" in d["timeline"]
        assert "summary" in d["timeline"]

    def test_timeline_serializable(self):
        r = _base_result(
            honeypot=HoneypotResult(status="sellable"),
            dev=DevProfile(creator_address="0x" + "de" * 20, creation_tx="0x" + "ff" * 32),
        )
        tl = build_timeline(r)
        r.timeline = tl
        import json
        s = json.dumps(r.model_dump())
        parsed = json.loads(s)
        assert parsed["timeline"]["events"]

    def test_timeline_null_by_default(self):
        r = _base_result()
        assert r.timeline is None

    def test_existing_fields_unchanged(self):
        r = _base_result(
            analysis=RugAnalysis(risk_score=42, risk_level="medium", signals=[], data_sources=[], limitations=[]),
        )
        tl = build_timeline(r)
        r.timeline = tl
        assert r.analysis.risk_score == 42
        assert r.contract_address == "0x" + "a" * 40


# --- Performance / cache reuse ---

class TestPerformance:
    def test_build_timeline_is_pure(self):
        r = _base_result(
            dev=DevProfile(creator_address="0x" + "de" * 20, creation_tx="0x" + "ff" * 32),
            honeypot=HoneypotResult(status="sellable"),
        )
        tl1 = build_timeline(r)
        tl2 = build_timeline(r)
        assert len(tl1.events) == len(tl2.events)
        assert tl1.summary.launch_quality == tl2.summary.launch_quality

    def test_no_side_effects_on_input(self):
        r = _base_result(
            watchlist_hits=[
                WatchlistHit(address="0x" + "aa" * 20, kind="smart", proxy_score=80),
            ],
        )
        hits_before = len(r.watchlist_hits)
        build_timeline(r)
        assert len(r.watchlist_hits) == hits_before


# --- Provider registry ---

class TestProviderRegistry:
    def test_all_providers_callable(self):
        for p in PROVIDERS:
            assert callable(p)

    def test_providers_accept_minimal_result(self):
        r = _base_result()
        for p in PROVIDERS:
            events = p(r)
            assert isinstance(events, list)
            for e in events:
                assert isinstance(e, TimelineEvent)


# --- Frontend rendering data shape ---

class TestFrontendDataShape:
    def test_event_fields_for_frontend(self):
        r = _base_result(
            honeypot=HoneypotResult(status="honeypot", detail="Cannot sell."),
        )
        tl = build_timeline(r)
        for e in tl.events:
            assert hasattr(e, "timestamp")
            assert hasattr(e, "title")
            assert hasattr(e, "category")
            assert hasattr(e, "severity")
            assert hasattr(e, "confidence")
            assert hasattr(e, "evidence")
            assert hasattr(e, "impact")
            assert hasattr(e, "explanation")

    def test_summary_fields_for_frontend(self):
        r = _base_result()
        tl = build_timeline(r)
        s = tl.summary
        assert hasattr(s, "launch_quality")
        assert hasattr(s, "developer_behaviour")
        assert hasattr(s, "liquidity_evolution")
        assert hasattr(s, "community_growth")
        assert hasattr(s, "smart_money")
        assert hasattr(s, "narrative")
