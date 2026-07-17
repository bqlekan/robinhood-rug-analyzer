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
    monkeypatch.setattr(settings, "honeypot_weth_address", "0x" + "ee" * 20)
    monkeypatch.setattr(settings, "honeypot_prober_code", "0x6001")
    monkeypatch.setattr(settings, "honeypot_sim_buy_wei", 1000)

    # Stub route discovery so simulate() does not hit the network; return a fixed
    # direct WETH->token route (the sim only forwards its path bytes to the prober).
    async def fake_route(token):
        weth = settings.honeypot_weth_address
        path = honeypot_sim.route_discovery.encode_path([weth, "0x" + "cc" * 20], [3000])
        return honeypot_sim.route_discovery.Route(
            quote_asset=weth, buy_path=path, sell_path=path, hops=[(weth, 3000, "0x" + "cc" * 20)]
        )

    monkeypatch.setattr(honeypot_sim.route_discovery, "discover_route", fake_route)

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


# --- Robinhood Chain activation wiring (M10 option 2) ---

def test_verified_robinhood_artifact_is_pinned():
    from app.core import honeypot_artifact as art

    # Addresses cross-checked on-chain during activation; guard against silent edits.
    assert art.ROBINHOOD_WETH == "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
    assert art.ROBINHOOD_USDG == "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
    assert art.ROBINHOOD_SWAPROUTER02 == "0xCaf681a66D020601342297493863E78C959E5cb2"
    # Path-based prober: probe(address,address,address,uint256,bytes,bytes).
    assert art.PROBER_SELECTOR == "0x184f0773"
    body = art.PROBER_RUNTIME_CODE
    assert body.startswith("0x") and len(body) > 2
    assert all(c in "0123456789abcdefABCDEF" for c in body[2:])  # valid hex, no placeholders


def test_config_defaults_activate_sim_for_robinhood():
    from app.core import honeypot_artifact as art

    # The uniswap dexId must map to the verified v3 router, with WETH + prober present.
    assert settings.dex_routers.get("uniswap") == art.ROBINHOOD_SWAPROUTER02
    assert settings.honeypot_weth_address == art.ROBINHOOD_WETH
    assert settings.honeypot_prober_selector == art.PROBER_SELECTOR
    assert settings.honeypot_prober_code == art.PROBER_RUNTIME_CODE


def test_enc_bytes_args_offsets_and_lengths():
    # Two bytes args after 4 fixed head words: 6 head words total (4 values + 2 offsets).
    buy = "0x" + "ab" * 43   # 43 bytes -> 2 words padded
    sell = "0x" + "cd" * 20  # 20 bytes -> 1 word padded
    # _enc_bytes_args returns ONLY the 2 offset words + the two tails; the 4 fixed value
    # words are emitted separately by the caller. Offsets are still measured from the full
    # arg head (6 words = 0xc0), per the ABI spec.
    out = honeypot_sim._enc_bytes_args(buy, sell)
    words = [out[i:i + 64] for i in range(0, len(out), 64)]
    assert int(words[0], 16) == 6 * 32           # buy offset (0xc0)
    assert int(words[1], 16) == 6 * 32 + 3 * 32  # sell offset (after buy: len + 2 data words)
    assert int(words[2], 16) == 43               # buy length
    assert int(words[5], 16) == 20               # sell length (idx 2 + 3 words of buy tail)


def test_probe_calldata_uses_pinned_selector_and_router(monkeypatch):
    from app.core import honeypot_artifact as art

    ret = "0x" + honeypot_sim._enc_uint(990) + honeypot_sim._enc_uint(970)
    spy = _wire_prober(monkeypatch, ret)
    # Use the real router + selector rather than the fixture's placeholders.
    monkeypatch.setattr(settings, "dex_routers", {"uniswap": art.ROBINHOOD_SWAPROUTER02})
    monkeypatch.setattr(settings, "honeypot_prober_selector", art.PROBER_SELECTOR)
    _run(honeypot_sim.simulate("0xToken", TokenMarketData(dex_id="uniswap")))

    _to, data, _override = spy.seen
    assert data.startswith(art.PROBER_SELECTOR)  # probe(...) selector
    # router is the first address argument after the selector
    assert art.ROBINHOOD_SWAPROUTER02[2:].lower() in data.lower()


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
