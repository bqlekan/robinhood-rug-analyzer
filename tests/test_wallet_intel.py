"""Unit tests for wallet intelligence pure functions (no network)."""

from app.services import wallet_intel

CREATOR = "0xdead00000000000000000000000000000000beef"


def _transfer(frm, to, value, decimals=0, ts="2024-01-01T00:00:00Z"):
    return {
        "from": {"hash": frm},
        "to": {"hash": to},
        "total": {"value": str(value), "decimals": str(decimals)},
        "timestamp": ts,
        "method": "transfer",
    }


def test_normalize_transfers_reverses_and_scales():
    # Blockscout returns newest-first; normalize should return oldest-first and scale by decimals.
    raw = [
        _transfer("0xA", "0xB", 2000, decimals=3, ts="2024-01-02T00:00:00Z"),
        _transfer(wallet_intel.ZERO, "0xA", 1000, decimals=3, ts="2024-01-01T00:00:00Z"),
    ]
    recs = wallet_intel.normalize_transfers(raw)
    assert recs[0]["from"] == wallet_intel.ZERO  # oldest first
    assert recs[0]["to"] == "0xa"
    assert recs[0]["value"] == 1.0  # 1000 / 10**3
    assert recs[1]["value"] == 2.0


def test_detect_insiders_flags_early_buyers_and_dev_recipients():
    transfers = [
        {"from": wallet_intel.ZERO, "to": "0xearly1", "value": 10, "ts": "t1", "method": "mint"},
        {"from": CREATOR, "to": "0xdevfriend", "value": 5, "ts": "t2", "method": "transfer"},
        {"from": "0xearly1", "to": "0xlate", "value": 1, "ts": "t3", "method": "transfer"},
    ]
    pcts = {"0xearly1": 3.0, "0xdevfriend": 2.0}
    insiders = wallet_intel.detect_insiders(transfers, CREATOR, pcts, early_count=5)
    reasons = {i.address: i.reason for i in insiders}
    assert reasons.get("0xdevfriend") == "dev_recipient"
    assert "0xearly1" in reasons
    # Dev recipients are ordered first.
    assert insiders[0].reason == "dev_recipient"


def test_detect_insiders_skips_creator_and_zero():
    transfers = [
        {"from": wallet_intel.ZERO, "to": CREATOR, "value": 100, "ts": "t1", "method": "mint"},
        {"from": wallet_intel.ZERO, "to": wallet_intel.ZERO, "value": 1, "ts": "t2", "method": "burn"},
    ]
    insiders = wallet_intel.detect_insiders(transfers, CREATOR, {}, early_count=5)
    assert insiders == []


LP = "0x1111111111111111111111111111111111111111"


def test_detect_insiders_excludes_known_contracts():
    # M4: the LP/AMM pair is usually the first post-launch recipient; it must not
    # be flagged as buyer #1. A real EOA that follows still gets detected.
    transfers = [
        {"from": wallet_intel.ZERO, "to": LP, "value": 500, "ts": "t1", "method": "mint"},
        {"from": wallet_intel.ZERO, "to": "0xrealbuyer", "value": 10, "ts": "t2", "method": "transfer"},
    ]
    insiders = wallet_intel.detect_insiders(
        transfers, CREATOR, {}, early_count=5, known_contracts={LP}
    )
    addrs = {i.address for i in insiders}
    assert LP not in addrs
    assert "0xrealbuyer" in addrs
    # The real buyer is ranked #1 now that the LP is skipped, not #2.
    assert insiders[0].address == "0xrealbuyer"
    assert insiders[0].buy_rank == 1


def test_detect_insiders_known_contracts_is_case_insensitive():
    transfers = [
        {"from": wallet_intel.ZERO, "to": LP, "value": 500, "ts": "t1", "method": "mint"},
    ]
    # Pass the address in a different case than the transfer records use.
    insiders = wallet_intel.detect_insiders(
        transfers, CREATOR, {}, early_count=5, known_contracts={LP.upper()}
    )
    assert insiders == []


def test_detect_insiders_none_known_contracts_is_backward_compatible():
    # Omitting known_contracts must behave exactly as before.
    transfers = [
        {"from": wallet_intel.ZERO, "to": "0xearly1", "value": 10, "ts": "t1", "method": "mint"},
    ]
    insiders = wallet_intel.detect_insiders(transfers, CREATOR, {}, early_count=5)
    assert {i.address for i in insiders} == {"0xearly1"}


def test_smart_wallet_proxy_rewards_early_entry_and_holding():
    # M6: 0xsmart enters early and KEEPS most of its position (sent < 50%) -> smart.
    transfers = [
        {"from": wallet_intel.ZERO, "to": "0xsmart", "value": 100, "ts": "t1", "method": "mint"},
        {"from": wallet_intel.ZERO, "to": "0xb", "value": 50, "ts": "t2", "method": "transfer"},
        {"from": wallet_intel.ZERO, "to": "0xc", "value": 50, "ts": "t3", "method": "transfer"},
        {"from": "0xsmart", "to": "0xbuyer", "value": 20, "ts": "t4", "method": "transfer"},
    ]
    sw = wallet_intel.smart_wallet_proxy("0xsmart", transfers, surviving_tokens=3)
    assert sw.proxy_score > 0
    assert any("earliest" in s.lower() for s in sw.signals)
    assert any("held most" in s.lower() for s in sw.signals)
    assert sw.surviving_tokens == 3


def test_smart_wallet_proxy_flags_dump_and_denies_smart_credit():
    # M6: an early wallet that DUMPS >=50% is exit risk, not smart. It gets the
    # early-entry credit but NOT the hold credit, and carries an exit-risk flag.
    dumper = [
        {"from": wallet_intel.ZERO, "to": "0xdumper", "value": 100, "ts": "t1", "method": "mint"},
        {"from": "0xdumper", "to": "0xbuyer", "value": 80, "ts": "t2", "method": "transfer"},
    ]
    holder = [
        {"from": wallet_intel.ZERO, "to": "0xholder", "value": 100, "ts": "t1", "method": "mint"},
        {"from": "0xholder", "to": "0xbuyer", "value": 20, "ts": "t2", "method": "transfer"},
    ]
    sw_dump = wallet_intel.smart_wallet_proxy("0xdumper", dumper)
    sw_hold = wallet_intel.smart_wallet_proxy("0xholder", holder)
    # Dumper: no "held" credit, has an exit-risk flag; holder: has "held" credit.
    assert not any("held most" in s.lower() for s in sw_dump.signals)
    assert any("exit risk" in s.lower() for s in sw_dump.signals)
    assert any("held most" in s.lower() for s in sw_hold.signals)
    # Holding is scored strictly higher than dumping for the same early entry.
    assert sw_hold.proxy_score > sw_dump.proxy_score


def test_smart_wallet_proxy_is_capped_at_100():
    transfers = [
        {"from": wallet_intel.ZERO, "to": "0xsmart", "value": 100, "ts": "t1", "method": "mint"},
        {"from": "0xsmart", "to": "0xb", "value": 100, "ts": "t2", "method": "transfer"},
    ]
    sw = wallet_intel.smart_wallet_proxy("0xsmart", transfers, surviving_tokens=10)
    assert sw.proxy_score <= 100
