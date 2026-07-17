from __future__ import annotations

"""Uniswap v3 swap-route discovery for the honeypot simulator (M10-B).

The prober executes opaque v3 `path` bytes; deciding *which* path — which quote
asset, which fee tiers, direct vs multi-hop — lives here so the pinned bytecode
never needs a recompile to gain a new route.

How a route is chosen (all on-chain, read-only `eth_call`s to the v3 factory + pools):
  - The buy leg always starts from wrapped-native (WETH), because the synthetic buyer
    is funded with native balance the prober wraps.
  - Quote assets are tried in `settings.honeypot_quote_assets` order (WETH first, then
    stables like USDG). The FIRST quote that yields a liquid path wins — order encodes
    preference, not a hardcoded WETH/USDG special case.
  - WETH quote  -> direct pool WETH/token (one hop).
  - Other quote -> WETH/quote pool + quote/token pool (two hops); both must be liquid.
  - A pool counts only if `getPool` returns a non-zero address AND its quote-side token
    reserve (balanceOf the pool) clears `settings.honeypot_min_quote_reserve` (skips
    dead/dust pools). Reserves are used, NOT the pool's `liquidity()` — that returns only
    in-range active-tick liquidity, so a concentrated-liquidity pool with out-of-range
    positions reads 0 yet still holds swappable reserves.

Adding a future quote asset is a config edit (append its address to
`honeypot_quote_assets`); no code or contract change. Everything degrades to "no route"
(the caller reports `unknown`), never a crash or a false path.
"""

import logging
from dataclasses import dataclass

from app.core.config import settings
from app.services import rpc_client

logger = logging.getLogger(__name__)

# Standard Uniswap v3 / ERC-20 selectors (keccak256(sig)[:4]).
_SEL_GET_POOL = "0x1698ee82"   # getPool(address,address,uint24)
_SEL_BALANCE_OF = "0x70a08231"  # balanceOf(address)

_ZERO_ADDR = "0x0000000000000000000000000000000000000000"


@dataclass(frozen=True)
class Route:
    """A discovered, liquidity-verified swap route for one token.

    `buy_path`/`sell_path` are v3 `exactInput` path bytes (0x-hex) ready to hand to the
    prober; `quote_asset` and `hops` are for diagnostics/logging only.
    """
    quote_asset: str
    buy_path: str
    sell_path: str
    hops: list[tuple[str, int, str]]  # (tokenIn, fee, tokenOut) per hop, in buy order

    def describe(self) -> str:
        legs = " -> ".join(
            [self.hops[0][0]] + [f"[{fee}] {out}" for _, fee, out in self.hops]
        )
        return f"via {self.quote_asset[:10]}… ({legs})"


def _enc_addr_word(address: str) -> str:
    """address -> 32-byte left-padded ABI word (no 0x)."""
    return address.lower().replace("0x", "").rjust(64, "0")


def _enc_uint_word(value: int) -> str:
    return f"{value:064x}"


def encode_path(tokens: list[str], fees: list[int]) -> str:
    """v3 path bytes: token0 (20B) + fee0 (3B) + token1 (20B) [+ fee1 + token2 ...].

    len(fees) must be len(tokens) - 1. Pure/deterministic.
    """
    if len(tokens) < 2 or len(fees) != len(tokens) - 1:
        raise ValueError("path needs n tokens and n-1 fees")
    out = tokens[0].lower().replace("0x", "").rjust(40, "0")
    for fee, tok in zip(fees, tokens[1:]):
        out += f"{fee:06x}" + tok.lower().replace("0x", "").rjust(40, "0")
    return "0x" + out


async def _get_pool(token_a: str, token_b: str, fee: int) -> str | None:
    """Factory getPool; returns the pool address or None if unset/zero/error."""
    factory = settings.honeypot_v3_factory
    if not factory:
        return None
    data = _SEL_GET_POOL + _enc_addr_word(token_a) + _enc_addr_word(token_b) + _enc_uint_word(fee)
    ret = await rpc_client.eth_call(factory, data)
    if not ret:
        return None
    body = ret.replace("0x", "")
    if len(body) < 64:
        return None
    pool = "0x" + body[-40:]
    return None if pool.lower() == _ZERO_ADDR else pool


async def _token_balance(token: str, holder: str) -> int | None:
    """ERC-20 balanceOf(holder) as int, or None on error."""
    ret = await rpc_client.eth_call(token, _SEL_BALANCE_OF + _enc_addr_word(holder))
    if not ret:
        return None
    try:
        return int(ret.replace("0x", "") or "0", 16)
    except ValueError:
        return None


async def _best_fee_tier(quote_asset: str, other: str) -> int | None:
    """First fee tier (in config order) whose `quote_asset`/`other` pool holds usable
    reserves of the quote asset.

    `quote_asset` is the value/known-decimals side of this hop (WETH, or a quote like
    USDG); its floor comes from `settings.honeypot_min_quote_reserve` keyed by that
    asset, with a "*" fallback. We check the pool's `balanceOf(quote_asset)` reserve
    rather than `liquidity()`: a concentrated-liquidity pool can report zero in-range
    liquidity yet still hold swappable balances. Returns the fee, or None when no
    sufficiently funded pool exists for the pair.
    """
    floors = settings.honeypot_min_quote_reserve
    floor = floors.get(quote_asset.lower(), floors.get("*", 1))
    for fee in settings.honeypot_fee_tiers:
        pool = await _get_pool(quote_asset, other, fee)
        if not pool:
            continue
        reserve = await _token_balance(quote_asset, pool)
        if reserve is not None and reserve >= floor:
            return fee
    return None


async def discover_route(token_address: str) -> Route | None:
    """Find the preferred liquidity-verified buy/sell route for `token_address`.

    Tries each configured quote asset in order; returns the first liquid route, else None
    (caller reports "unknown"). WETH quote = direct pool; any other quote = 2-hop via WETH.
    """
    weth = settings.honeypot_weth_address
    if not weth:
        return None
    token = token_address.lower()

    for quote in settings.honeypot_quote_assets:
        q = quote.lower()
        if q == weth.lower():
            fee = await _best_fee_tier(weth, token)
            if fee is None:
                continue
            hops = [(weth, fee, token)]
        else:
            # WETH -> quote, then quote -> token; both legs must be liquid.
            fee_in = await _best_fee_tier(weth, q)
            if fee_in is None:
                continue
            fee_out = await _best_fee_tier(q, token)
            if fee_out is None:
                continue
            hops = [(weth, fee_in, q), (q, fee_out, token)]

        tokens = [hops[0][0]] + [h[2] for h in hops]
        fees = [h[1] for h in hops]
        buy_path = encode_path(tokens, fees)
        sell_path = encode_path(list(reversed(tokens)), list(reversed(fees)))
        route = Route(quote_asset=quote, buy_path=buy_path, sell_path=sell_path, hops=hops)
        logger.info("Honeypot route for %s: %s", token_address, route.describe())
        return route

    logger.info("Honeypot route for %s: none (no liquid quote-asset path)", token_address)
    return None
