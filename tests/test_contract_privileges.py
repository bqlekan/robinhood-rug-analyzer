"""Unit tests for M11 contract-privilege / authority reads.

Pure ABI-power detection + live-read decoding (no network); the live-read wiring is
exercised with a stubbed rpc_client.eth_call.
"""

import asyncio

from app.services import contract_privileges as cp


def _fn(name, mutability="nonpayable"):
    return {"type": "function", "name": name, "stateMutability": mutability}


def _payload(abi, verified=True):
    return {"is_verified": verified, "abi": abi}


# --- ABI power detection (pure) ---


def test_mint_pause_blacklist_fee_detected():
    abi = [
        _fn("mint"),
        _fn("pause"),
        _fn("setBlacklist"),
        _fn("setFee"),
        _fn("owner", "view"),
    ]
    p = cp.infer_privileges(_payload(abi))
    assert p.analyzed is True
    assert (p.can_mint, p.can_pause, p.can_blacklist, p.can_set_fees) == (True, True, True, True)


def test_view_named_paused_is_not_a_power():
    # A view/pure function named like a power is state, not a mutator power.
    abi = [_fn("paused", "view"), _fn("mintedAt", "view")]
    p = cp.infer_privileges(_payload(abi))
    assert p.can_pause is False
    assert p.can_mint is False


def test_fee_needs_set_or_update_prefix():
    # A `feeBalance()` getter or a `distributeFees()` action is not a fee *mutator*.
    abi = [_fn("distributeFees"), _fn("feeCollector", "view")]
    p = cp.infer_privileges(_payload(abi))
    assert p.can_set_fees is False
    p2 = cp.infer_privileges(_payload([_fn("updateTax")]))
    assert p2.can_set_fees is True


def test_no_powers_reports_clean_detail():
    p = cp.infer_privileges(_payload([_fn("transfer"), _fn("approve")]))
    assert p.analyzed is True
    assert not any([p.can_mint, p.can_pause, p.can_blacklist, p.can_set_fees])
    assert "no mint" in (p.detail or "").lower()


# --- ownership decoding ---


ZERO = "0x" + "0" * 64
OWNER = "0x" + "0" * 24 + "b" * 40  # a real (non-zero) owner word


def test_renounced_owner_zero_address():
    p = cp.infer_privileges(_payload([_fn("mint")]), owner_hex=ZERO)
    assert p.ownership_renounced is True
    assert p.owner_address is None
    assert "renounced" in (p.detail or "").lower()


def test_retained_owner_real_address():
    p = cp.infer_privileges(_payload([_fn("mint")]), owner_hex=OWNER)
    assert p.ownership_renounced is False
    assert p.owner_address == "0x" + "b" * 40
    assert "retained" in (p.detail or "").lower()


def test_unreadable_owner_stays_none_not_renounced():
    p = cp.infer_privileges(_payload([_fn("mint")]), owner_hex=None)
    assert p.ownership_renounced is None  # never a false "renounced"
    assert "could not be confirmed" in (p.detail or "").lower()


def test_paused_decoded_only_when_pausable():
    one = "0x" + "0" * 63 + "1"
    p = cp.infer_privileges(_payload([_fn("pause")]), paused_hex=one)
    assert p.is_paused is True
    # No pause power -> paused read ignored even if a blob is passed.
    p2 = cp.infer_privileges(_payload([_fn("transfer")]), paused_hex=one)
    assert p2.is_paused is None


# --- graceful degradation ---


def test_unverified_degrades_to_not_analyzed():
    p = cp.infer_privileges(_payload([_fn("mint")], verified=False))
    assert p.analyzed is False
    assert p.can_mint is False  # no false powers surfaced


def test_missing_abi_degrades():
    assert cp.infer_privileges({"is_verified": True}).analyzed is False
    assert cp.infer_privileges(None).analyzed is False


# --- live-read wiring (stubbed eth_call) ---


def test_fetch_privileges_reads_owner_and_paused(monkeypatch):
    calls = []

    async def fake_eth_call(to, data, *a, **k):
        calls.append(data)
        if data == cp._SEL_OWNER:
            return ZERO
        if data == cp._SEL_PAUSED:
            return "0x" + "0" * 63 + "1"
        return None

    monkeypatch.setattr(cp.rpc_client, "eth_call", fake_eth_call)
    abi = [_fn("owner", "view"), _fn("paused", "view"), _fn("pause"), _fn("mint")]
    p = asyncio.run(cp.fetch_privileges("0x" + "a" * 40, _payload(abi)))
    assert cp._SEL_OWNER in calls and cp._SEL_PAUSED in calls
    assert p.ownership_renounced is True
    assert p.is_paused is True
    assert p.can_mint is True


def test_fetch_privileges_no_rpc_when_reads_absent(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("eth_call should not fire when ABI exposes no owner/paused")

    monkeypatch.setattr(cp.rpc_client, "eth_call", boom)
    # mint power but no owner()/paused() reads -> no live calls, still analyzed.
    p = asyncio.run(cp.fetch_privileges("0x" + "a" * 40, _payload([_fn("mint")])))
    assert p.analyzed is True
    assert p.can_mint is True
    assert p.ownership_renounced is None


def test_fetch_privileges_unverified_fires_no_rpc(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("eth_call should not fire for unverified contracts")

    monkeypatch.setattr(cp.rpc_client, "eth_call", boom)
    p = asyncio.run(cp.fetch_privileges("0x" + "a" * 40, _payload([_fn("owner", "view")], verified=False)))
    assert p.analyzed is False
