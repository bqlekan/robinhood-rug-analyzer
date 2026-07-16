"""Unit tests for the pure per-dimension analyzers (no network)."""

from app.models.token import LaunchedToken
from app.services import analyzers, launchpad_registry


def _holder(addr: str, value: str, *, is_contract=False, name=None, is_scam=False):
    return {
        "address": {"hash": addr, "is_contract": is_contract, "name": name, "is_scam": is_scam},
        "value": value,
    }


def test_analyze_age_prefers_pair_timestamp():
    # 10 days ago in ms.
    import time

    ten_days_ago_ms = int((time.time() - 10 * 86400) * 1000)
    age = analyzers.analyze_age(ten_days_ago_ms, None)
    assert age.source == "pair_created_at"
    assert age.age_days is not None
    assert 9.5 < age.age_days < 10.5


def test_analyze_age_unknown_when_no_data():
    age = analyzers.analyze_age(None, None)
    assert age.age_days is None
    assert age.source is None


def test_analyze_age_from_contract_creation_new_token():
    # No pair timestamp; a fresh contract-creation ISO ~5 hours ago.
    from datetime import datetime, timedelta, timezone

    iso = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    age = analyzers.analyze_age(None, iso)
    assert age.source == "contract_creation"
    assert 4.5 < age.age_hours < 5.5


def test_analyze_age_from_contract_creation_old_token():
    from datetime import datetime, timedelta, timezone

    iso = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    age = analyzers.analyze_age(None, iso)
    assert age.source == "contract_creation"
    assert 199 < age.age_days < 201


def test_analyze_age_pair_timestamp_wins_over_contract_iso():
    import time
    from datetime import datetime, timedelta, timezone

    pair_ms = int((time.time() - 10 * 86400) * 1000)
    iso = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    age = analyzers.analyze_age(pair_ms, iso)
    assert age.source == "pair_created_at"
    assert 9.5 < age.age_days < 10.5


def test_analyze_age_bad_iso_falls_back_to_unknown():
    age = analyzers.analyze_age(None, "not-a-timestamp")
    assert age.source is None
    assert age.age_days is None


def test_analyze_holders_concentration():
    # 18-decimal token, total supply 1000 tokens. One whale holds 500 (50%).
    supply = "1000" + "0" * 18
    holders = [
        _holder("0xWhale", "500" + "0" * 18),
        _holder("0xB", "300" + "0" * 18),
        _holder("0xC", "200" + "0" * 18),
    ]
    dist = analyzers.analyze_holders(holders, holder_count=3, total_supply=supply, decimals="18")
    assert dist.top1_percentage == 50.0
    assert dist.top10_percentage == 100.0
    assert dist.sampled_holders == 3
    assert dist.top_holders[0].percentage == 50.0


def test_analyze_holders_handles_missing_supply():
    holders = [_holder("0xA", "100")]
    dist = analyzers.analyze_holders(holders, holder_count=1, total_supply=None, decimals=None)
    # No supply -> percentages cannot be computed, but must not raise.
    assert dist.top_holders[0].percentage is None
    assert dist.top10_percentage is None


def test_analyze_clusters_groups_shared_funder():
    funders = {"0xA": "0xFunder1", "0xB": "0xFunder1", "0xC": "0xFunder2"}
    pcts = {"0xA": 10.0, "0xB": 15.0, "0xC": 5.0}
    result = analyzers.analyze_clusters(funders, pcts)
    assert len(result.clusters) == 1  # only Funder1 has >=2 members
    cluster = result.clusters[0]
    assert cluster.funder_address == "0xfunder1"
    # Addresses are normalized to lowercase so shared-funder and mutual-transfer
    # links (which come from lowercased transfer data) unify correctly.
    assert set(cluster.member_addresses) == {"0xa", "0xb"}
    assert cluster.combined_percentage == 25.0
    assert result.clustered_percentage == 25.0


def test_analyze_clusters_none_when_no_shared_funder():
    funders = {"0xA": "0xF1", "0xB": "0xF2"}
    result = analyzers.analyze_clusters(funders, {"0xA": 1.0, "0xB": 1.0})
    assert result.clusters == []
    assert result.note is not None


def test_analyze_holders_excludes_lp_from_topline():
    # LP pool holds 80% of supply and would otherwise dominate top1/top10.
    holders = [
        _holder("0xLP", "800", is_contract=True),
        _holder("0xA", "50"),
        _holder("0xB", "40"),
        _holder("0xC", "10"),
    ]
    dist = analyzers.analyze_holders(holders, 4, "1000", 0, lp_address="0xLP")
    top_addrs = {h.address for h in dist.top_holders}
    assert "0xLP" not in top_addrs
    assert dist.top1_percentage == 5.0
    assert dist.top10_percentage == 10.0
    assert dist.lp_address == "0xLP"
    assert dist.lp_percentage == 80.0


def test_is_established_token_matches_symbols_and_names():
    assert launchpad_registry.is_established_token("USDT", None)
    assert launchpad_registry.is_established_token("weth", None)
    assert launchpad_registry.is_established_token(None, "Tether USD")
    # Wrappers and yield-bearing derivatives (e.g. syrupUSDG, sUSDe, wstETH).
    assert launchpad_registry.is_established_token("syrupUSDG", "syrupUSDG")
    assert launchpad_registry.is_established_token("wstETH", None)
    assert not launchpad_registry.is_established_token("PEPE", "Pepe Coin")


def test_analyze_clusters_merges_mutual_transfers():
    # No shared funder, but A <-> B transferred the token to each other.
    funders = {"0xA": None, "0xB": None, "0xC": "0xF1"}
    pcts = {"0xA": 4.0, "0xB": 6.0, "0xC": 3.0}
    mutual = [("0xA", "0xB")]
    result = analyzers.analyze_clusters(funders, pcts, mutual_transfers=mutual)
    assert len(result.clusters) == 1
    cluster = result.clusters[0]
    assert set(cluster.member_addresses) == {"0xa", "0xb"}
    assert cluster.link_type == "mutual_transfer"
    assert cluster.combined_percentage == 10.0


def test_analyze_clusters_mixed_link_when_funder_and_transfer_overlap():
    funders = {"0xA": "0xF1", "0xB": "0xF1"}
    pcts = {"0xA": 5.0, "0xB": 5.0}
    result = analyzers.analyze_clusters(funders, pcts, mutual_transfers=[("0xA", "0xB")])
    assert len(result.clusters) == 1
    assert result.clusters[0].link_type == "mixed"


def test_analyze_clusters_retains_link_type_after_root_change():
    # M5 regression: a shared-funder pair (A,B) whose component root is later moved
    # by a mutual transfer to a third holder C. Before re-keying, the shared_funder
    # link type and funder attribution sat on the OLD root and were lost at the new
    # root, mislabeling the cluster "mutual_transfer" with no funder. After the fix
    # both link types survive (-> "mixed") and the funder is retained.
    funders = {"0xA": "0xF1", "0xB": "0xF1", "0xC": None}
    pcts = {"0xA": 5.0, "0xB": 5.0, "0xC": 5.0}
    result = analyzers.analyze_clusters(funders, pcts, mutual_transfers=[("0xB", "0xC")])
    assert len(result.clusters) == 1
    cluster = result.clusters[0]
    assert set(cluster.member_addresses) == {"0xa", "0xb", "0xc"}
    assert cluster.link_type == "mixed"
    assert cluster.funder_address == "0xf1"
    assert cluster.combined_percentage == 15.0


def test_extract_mutual_transfers_only_between_sampled_holders():
    transfers = [
        {"from": "0xa", "to": "0xb", "value": 1.0},
        {"from": "0xa", "to": "0xoutsider", "value": 1.0},  # not a holder
        {"from": ZERO, "to": "0xa", "value": 1.0},  # mint, ignored
    ]
    pairs = analyzers.extract_mutual_transfers(transfers, {"0xA", "0xB"})
    assert pairs == [("0xa", "0xb")]


def test_analyze_dev_transfers_computes_supply_percentage():
    transfers = [
        {"from": "0xdev", "to": "0xrecipient", "value": 100.0, "ts": "t1"},
        {"from": "0xother", "to": "0xdev", "value": 50.0, "ts": "t2"},  # inbound, ignored
    ]
    dev_transfers, moved_pct = analyzers.analyze_dev_transfers(transfers, "0xDev", total_supply_units=1000.0)
    assert len(dev_transfers) == 1
    assert dev_transfers[0].to_address == "0xrecipient"
    assert dev_transfers[0].amount_percentage == 10.0
    assert moved_pct == 10.0


ZERO = "0x0000000000000000000000000000000000000000"


def test_analyze_dev_serial_rugger():
    launched = [
        LaunchedToken(address="0x1", outcome="likely_rugged"),
        LaunchedToken(address="0x2", outcome="likely_rugged"),
        LaunchedToken(address="0x3", outcome="likely_rugged"),
    ]
    dev = analyzers.analyze_dev("0xdev", "0xtx", 25.0, launched)
    assert dev.reputation == "serial_rugger"
    assert dev.tokens_rugged == 3


def test_analyze_dev_unknown_without_history():
    dev = analyzers.analyze_dev("0xdev", "0xtx", None, [])
    assert dev.reputation == "unknown"
    assert dev.tokens_launched is None


def test_analyze_liquidity_lock_burned():
    dead = "0x000000000000000000000000000000000000dEaD"
    holders = [{"address": {"hash": dead}, "value": "900"}]
    lock = analyzers.analyze_liquidity_lock(holders, total_lp_supply="1000", decimals="18")
    assert lock.status == "burned"
    assert lock.locked_percentage == 90.0


def test_analyze_liquidity_lock_unlocked():
    holders = [{"address": {"hash": "0xRandomWallet"}, "value": "900"}]
    lock = analyzers.analyze_liquidity_lock(holders, total_lp_supply="1000", decimals="18")
    assert lock.status == "unlocked"


def test_analyze_liquidity_lock_unknown_without_holders():
    lock = analyzers.analyze_liquidity_lock([], total_lp_supply="1000", decimals="18")
    assert lock.status == "unknown"


def test_launchpad_name_hint():
    name, confidence, _ = launchpad_registry.detect_launchpad(None, "NOXA Fun Token", None)
    assert name == "NOXA Fun"
    assert confidence == "medium"


def test_launchpad_unknown():
    name, confidence, _ = launchpad_registry.detect_launchpad("0xabc", "Random Token", None)
    assert name == "Unknown"
    assert confidence == "low"


def test_burn_address_detection():
    assert launchpad_registry.is_burn_address("0x0000000000000000000000000000000000000000")
    assert launchpad_registry.locker_label("0x000000000000000000000000000000000000dead") == "Burn address"
    assert not launchpad_registry.is_burn_address("0xabc")
