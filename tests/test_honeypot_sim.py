"""M10-B: honeypot simulator — pure classification, encoding, and inert-gate paths."""

import asyncio

import pytest

from app.models.token import TokenMarketData
from app.services import honeypot_sim
from app.core.config import settings


@pytest.fixture(autouse=True)
def _clear_sim_cache():
    honeypot_sim._sim_cache.clear()


def _run(coro):
    return asyncio.run(coro)


# --- pure encode/decode ---

def test_enc_uint_and_addr_widths():
    assert honeypot_sim._enc_uint(42) == "0" * 62 + "2a"
    assert honeypot_sim._enc_addr("0xABCD") == "abcd".rjust(64, "0")


def test_dec_uint_handles_missing_and_short():
    assert honeypot_sim._dec_uint(None) is None
    assert honeypot_sim._dec_uint("0x2a") is None  # < 32 bytes
    assert honeypot_sim._dec_uint("0x" + "00" * 31 + "2a") == 42


# --- pure classification (acceptance-criteria core) ---

def test_classify_honeypot_when_sell_reverts():
    assert honeypot_sim.classify(spent=1000, bought=500, sold_back=None).status == "honeypot"


def test_classify_honeypot_when_sell_zero():
    assert honeypot_sim.classify(spent=1000, bought=500, sold_back=0).status == "honeypot"


def test_classify_unknown_when_buy_empty():
    assert honeypot_sim.classify(spent=1000, bought=0, sold_back=None).status == "unknown"


def test_classify_high_tax_over_threshold():
    r = honeypot_sim.classify(spent=1000, bought=900, sold_back=400)  # 60% loss
    assert r.status == "high_tax"
    assert r.sell_tax_percentage == 60.0


def test_classify_sellable_low_loss():
    r = honeypot_sim.classify(spent=1000, bought=990, sold_back=970)  # 3% loss
    assert r.status == "sellable"


# --- inert-gate degradation ---

def test_simulate_unknown_when_no_router_mapped(monkeypatch):
    monkeypatch.setattr(settings, "dex_routers", {})
    market = TokenMarketData(dex_id="somedex")
    r = _run(honeypot_sim.simulate("0xtoken", market))
    assert r.status == "unknown"


def test_simulate_unknown_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "honeypot_sim_enabled", False)
    r = _run(honeypot_sim.simulate("0xtoken", TokenMarketData(dex_id="d")))
    assert r.status == "unknown"


def test_roundtrip_unknown_when_artifacts_missing(monkeypatch):
    # Router mapped, but no WETH / prober bytecode -> honest "unknown", no RPC.
    monkeypatch.setattr(settings, "dex_routers", {"d": "0xrouter"})
    monkeypatch.setattr(settings, "honeypot_weth_address", None)
    r = _run(honeypot_sim.simulate("0xtoken", TokenMarketData(dex_id="d")))
    assert r.status == "unknown"


def _wire_prober(monkeypatch, ret):
    monkeypatch.setattr(settings, "dex_routers", {"d": "0xrouter"})
    monkeypatch.setattr(settings, "honeypot_weth_address", "0xweth")
    monkeypatch.setattr(settings, "honeypot_prober_code", "0x6001")
    monkeypatch.setattr(settings, "honeypot_sim_buy_wei", 1000)

    async def fake_eth_call(to, data, block="latest", state_override=None):
        fake_eth_call.seen = (to, data, state_override)
        return ret

    monkeypatch.setattr(honeypot_sim.rpc_client, "eth_call", fake_eth_call)
    return fake_eth_call


def test_roundtrip_decodes_two_words_and_classifies_honeypot(monkeypatch):
    # bought=500, soldBack=0 -> honeypot. Also assert the prober code is injected.
    ret = "0x" + honeypot_sim._enc_uint(500) + honeypot_sim._enc_uint(0)
    spy = _wire_prober(monkeypatch, ret)
    r = _run(honeypot_sim.simulate("0xtoken", TokenMarketData(dex_id="d")))
    assert r.status == "honeypot"
    assert spy.seen[2][honeypot_sim.SYNTHETIC_BUYER]["code"] == "0x6001"


def test_roundtrip_unknown_when_call_fails(monkeypatch):
    _wire_prober(monkeypatch, None)  # rpc_client already returns None on failure
    r = _run(honeypot_sim.simulate("0xtoken", TokenMarketData(dex_id="d")))
    assert r.status == "unknown"


def test_roundtrip_sellable_low_loss(monkeypatch):
    ret = "0x" + honeypot_sim._enc_uint(990) + honeypot_sim._enc_uint(970)  # 3% loss
    _wire_prober(monkeypatch, ret)
    r = _run(honeypot_sim.simulate("0xtoken", TokenMarketData(dex_id="d")))
    assert r.status == "sellable"


def test_executed_verdict_is_cached_unknown_is_not(monkeypatch):
    honeypot_sim._sim_cache.clear()
    calls = {"n": 0}
    ret = "0x" + honeypot_sim._enc_uint(990) + honeypot_sim._enc_uint(970)
    spy = _wire_prober(monkeypatch, ret)

    async def counting(to, data, block="latest", state_override=None):
        calls["n"] += 1
        return ret

    monkeypatch.setattr(honeypot_sim.rpc_client, "eth_call", counting)
    _run(honeypot_sim.simulate("0xTok", TokenMarketData(dex_id="d")))
    _run(honeypot_sim.simulate("0xTok", TokenMarketData(dex_id="d")))
    assert calls["n"] == 1  # second analyze served from cache

    # An "unknown" (call failed) is not cached -> retried.
    honeypot_sim._sim_cache.clear()
    calls["n"] = 0

    async def failing(to, data, block="latest", state_override=None):
        calls["n"] += 1
        return None

    monkeypatch.setattr(honeypot_sim.rpc_client, "eth_call", failing)
    _run(honeypot_sim.simulate("0xTok2", TokenMarketData(dex_id="d")))
    _run(honeypot_sim.simulate("0xTok2", TokenMarketData(dex_id="d")))
    assert calls["n"] == 2  # unknown retried, never cached
