"""M10-B: Uniswap v3 route discovery — pool/liquidity verification and route selection.

The chain is faked by a scripted `eth_call` that answers factory `getPool` and pool
`liquidity()` reads from an in-memory pool book, so these tests are deterministic and
offline while exercising the real selectors, path encoding, and quote-asset preference.
"""

import asyncio

import pytest

from app.core.config import settings
from app.services import route_discovery as rd


WETH = "0x" + "e0" * 20
USDG = "0x" + "d5" * 20
TOKEN = "0x" + "70" * 20
FACTORY = "0x" + "fa" * 20


def _run(coro):
    return asyncio.run(coro)


def _word(hexval: str) -> str:
    return hexval.lower().replace("0x", "").rjust(64, "0")


class FakeChain:
    """Answers getPool / balanceOf from a pool book keyed by (sorted pair, fee).

    pools: {(tokenA_lower, tokenB_lower, fee): (pool_addr, reserve_int)}. Order-insensitive.
    `reserve_int` is the pool's balance of BOTH tokens in this fake (enough for the
    quote-side reserve check the discovery service performs via balanceOf(quote, pool)).
    """

    def __init__(self, pools):
        self.pools = {}
        for (a, b, fee), (addr, reserve) in pools.items():
            self.pools[(frozenset({a.lower(), b.lower()}), fee)] = (addr, reserve)
        self.calls = 0

    async def eth_call(self, to, data, block="latest", state_override=None):
        self.calls += 1
        body = data.replace("0x", "")
        sel = "0x" + body[:8]
        if sel == rd._SEL_GET_POOL:
            a = "0x" + body[8 + 24: 8 + 64]
            b = "0x" + body[8 + 64 + 24: 8 + 128]
            fee = int(body[8 + 128: 8 + 192], 16)
            hit = self.pools.get((frozenset({a.lower(), b.lower()}), fee))
            addr = hit[0] if hit else "0x" + "00" * 20
            return "0x" + _word(addr)
        if sel == rd._SEL_BALANCE_OF:
            # balanceOf(pool) on some token -> the pool's reserve (any pool holding it).
            holder = "0x" + body[8 + 24: 8 + 64]
            for (_pair, _fee), (addr, reserve) in self.pools.items():
                if addr.lower() == holder.lower():
                    return "0x" + f"{reserve:064x}"
            return "0x" + "00" * 32
        raise AssertionError(f"unexpected selector {sel}")


@pytest.fixture(autouse=True)
def _chain_config(monkeypatch):
    monkeypatch.setattr(settings, "honeypot_weth_address", WETH)
    monkeypatch.setattr(settings, "honeypot_v3_factory", FACTORY)
    monkeypatch.setattr(settings, "honeypot_quote_assets", [WETH, USDG])
    monkeypatch.setattr(settings, "honeypot_fee_tiers", [500, 3000, 10000, 100])
    monkeypatch.setattr(settings, "honeypot_min_quote_reserve", {"*": 1})


def _wire(monkeypatch, chain):
    monkeypatch.setattr(rd.rpc_client, "eth_call", chain.eth_call)


# --- pure path encoding ---

def test_encode_path_single_hop():
    p = rd.encode_path([WETH, TOKEN], [3000])
    assert p == "0x" + "e0" * 20 + "000bb8" + "70" * 20


def test_encode_path_multi_hop():
    p = rd.encode_path([WETH, USDG, TOKEN], [500, 10000])
    assert p == "0x" + "e0" * 20 + "0001f4" + "d5" * 20 + "002710" + "70" * 20


def test_encode_path_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        rd.encode_path([WETH, TOKEN], [500, 3000])


# --- WETH-direct route ---

def test_direct_weth_route_selected(monkeypatch):
    chain = FakeChain({(WETH, TOKEN, 3000): ("0x" + "a1" * 20, 5_000)})
    _wire(monkeypatch, chain)
    route = _run(rd.discover_route(TOKEN))
    assert route is not None
    assert route.quote_asset == WETH
    assert route.hops == [(WETH.lower(), 3000, TOKEN.lower())]
    # buy WETH->token, sell token->WETH
    assert route.buy_path == rd.encode_path([WETH, TOKEN], [3000])
    assert route.sell_path == rd.encode_path([TOKEN, WETH], [3000])


def test_direct_route_picks_first_liquid_fee_tier(monkeypatch):
    # 500 tier exists but is too thin; 3000 is liquid -> 3000 chosen.
    chain = FakeChain({
        (WETH, TOKEN, 500): ("0x" + "b0" * 20, 0),
        (WETH, TOKEN, 3000): ("0x" + "b1" * 20, 9_999),
    })
    _wire(monkeypatch, chain)
    route = _run(rd.discover_route(TOKEN))
    assert route.hops[0][1] == 3000


# --- USDG quote-hop route ---

def test_usdg_two_hop_when_no_direct_weth_pool(monkeypatch):
    # No WETH/token pool; WETH/USDG and USDG/token both liquid -> 2-hop via USDG.
    chain = FakeChain({
        (WETH, USDG, 500): ("0x" + "c0" * 20, 100_000),
        (USDG, TOKEN, 3000): ("0x" + "c1" * 20, 50_000),
    })
    _wire(monkeypatch, chain)
    route = _run(rd.discover_route(TOKEN))
    assert route is not None
    assert route.quote_asset == USDG
    assert route.hops == [(WETH.lower(), 500, USDG.lower()), (USDG.lower(), 3000, TOKEN.lower())]
    assert route.buy_path == rd.encode_path([WETH, USDG, TOKEN], [500, 3000])
    assert route.sell_path == rd.encode_path([TOKEN, USDG, WETH], [3000, 500])


def test_weth_preferred_over_usdg_when_both_exist(monkeypatch):
    # Config order is [WETH, USDG]; a direct WETH pool wins even if a USDG path also exists.
    chain = FakeChain({
        (WETH, TOKEN, 10000): ("0x" + "a1" * 20, 5_000),
        (WETH, USDG, 500): ("0x" + "c0" * 20, 100_000),
        (USDG, TOKEN, 3000): ("0x" + "c1" * 20, 50_000),
    })
    _wire(monkeypatch, chain)
    route = _run(rd.discover_route(TOKEN))
    assert route.quote_asset == WETH
    assert len(route.hops) == 1


def test_two_hop_needs_both_legs(monkeypatch):
    # WETH/USDG liquid but USDG/token missing -> no route.
    chain = FakeChain({(WETH, USDG, 500): ("0x" + "c0" * 20, 100_000)})
    _wire(monkeypatch, chain)
    assert _run(rd.discover_route(TOKEN)) is None


# --- no / invalid liquidity ---

def test_no_route_when_no_pools(monkeypatch):
    _wire(monkeypatch, FakeChain({}))
    assert _run(rd.discover_route(TOKEN)) is None


def test_pool_below_reserve_floor_is_skipped(monkeypatch):
    # A funded-but-dust WETH pool (below the WETH floor) must be rejected, so the sim
    # never picks a dust pool and misreads the near-zero round-trip as a honeypot.
    monkeypatch.setattr(settings, "honeypot_min_quote_reserve", {WETH.lower(): 1_000, "*": 1})
    chain = FakeChain({(WETH, TOKEN, 3000): ("0x" + "a1" * 20, 999)})  # under floor
    _wire(monkeypatch, chain)
    assert _run(rd.discover_route(TOKEN)) is None


def test_per_asset_floor_uses_each_assets_decimals(monkeypatch):
    # WETH pool is dust (6 wei, below 1e16 WETH floor); USDG pool is liquid (>1 USDG).
    # The route must fall through WETH and pick the USDG 2-hop -- proving per-asset floors.
    monkeypatch.setattr(settings, "honeypot_min_quote_reserve",
                        {WETH.lower(): 10**16, USDG.lower(): 10**6, "*": 1})
    chain = FakeChain({
        (WETH, TOKEN, 3000): ("0x" + "a1" * 20, 6),          # dust WETH
        (WETH, USDG, 500): ("0x" + "c0" * 20, 10**20),       # deep WETH/USDG
        (USDG, TOKEN, 3000): ("0x" + "c1" * 20, 5 * 10**6),  # 5 USDG
    })
    _wire(monkeypatch, chain)
    route = _run(rd.discover_route(TOKEN))
    assert route is not None and route.quote_asset == USDG


def test_zero_pool_address_is_not_a_route(monkeypatch):
    # getPool returns the zero address (pair never created) -> treated as absent.
    chain = FakeChain({(WETH, TOKEN, 3000): ("0x" + "00" * 20, 10_000)})
    _wire(monkeypatch, chain)
    assert _run(rd.discover_route(TOKEN)) is None


def test_no_route_when_factory_unset(monkeypatch):
    monkeypatch.setattr(settings, "honeypot_v3_factory", None)
    chain = FakeChain({(WETH, TOKEN, 3000): ("0x" + "a1" * 20, 5_000)})
    _wire(monkeypatch, chain)
    assert _run(rd.discover_route(TOKEN)) is None


def test_rpc_error_degrades_to_no_route(monkeypatch):
    async def boom(to, data, block="latest", state_override=None):
        return None  # rpc_client surfaces errors as None

    monkeypatch.setattr(rd.rpc_client, "eth_call", boom)
    assert _run(rd.discover_route(TOKEN)) is None


# --- extension: a new quote asset needs config only ---

def test_new_quote_asset_via_config(monkeypatch):
    other = "0x" + "0a" * 20
    monkeypatch.setattr(settings, "honeypot_quote_assets", [WETH, other])
    chain = FakeChain({
        (WETH, other, 3000): ("0x" + "e1" * 20, 100_000),
        (other, TOKEN, 500): ("0x" + "e2" * 20, 100_000),
    })
    _wire(monkeypatch, chain)
    route = _run(rd.discover_route(TOKEN))
    assert route.quote_asset == other
    assert route.hops[0][0] == WETH.lower() and route.hops[-1][2] == TOKEN.lower()
