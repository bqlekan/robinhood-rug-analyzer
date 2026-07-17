from __future__ import annotations

"""Honeypot / sell-tax simulation (M10 deliverable B).

Detects unsellable tokens and extreme sell taxes by simulating a buy->sell round-trip
with `eth_call` state overrides (verified supported on this chain's Nitro node, see
ROADMAP M10 probe). No transactions, no keys, no funds: state overrides fund a synthetic
buyer ephemerally inside the call.

Design guarantees:
  - Inert by default: with no router mapped for the token's DEX (`settings.dex_routers`
    empty), no RPC fires and `status="unknown"` is returned. Behavior is unchanged in
    production until a router is sourced.
  - Every uncertainty (sim disabled, no router, RPC error, setup revert) resolves to
    "unknown" ("could not simulate") — never a crash and never a false "sellable".
  - Uniswap v3: the injected prober calls SwapRouter02.exactInputSingle across the
    standard fee tiers. Router + wrapped-native address + prober bytecode are config
    (defaulted to the verified Robinhood Chain artifact); other chains stay inert.

This module owns all simulation logic; `rug_analyzer` only calls `simulate()` and threads
the result into the scorer.
"""

import logging

from app.core.config import settings
from app.models.token import HoneypotResult, TokenMarketData
from app.services import rpc_client
from app.services.cache import TTLCache, MISS

logger = logging.getLogger(__name__)

# Honeypot status is a near-static contract property, so a real verdict is cached per
# token (one sim per analyze, reused within TTL). "unknown" is never cached — it means
# "could not simulate" and must stay retryable.
_sim_cache = TTLCache(ttl=settings.http_cache_ttl_seconds, max_size=settings.http_cache_max_size)

# All swap/approve/wrap logic lives in the injected prober contract
# (contracts/HoneypotProber.sol); this module only encodes its `probe(...)` call and
# decodes the (bought, soldBack) return. The selector is pinned in config.

# Fixed synthetic actor; never a real wallet. Funded only via ephemeral state override.
SYNTHETIC_BUYER = "0x00000000000000000000000000000000c0ffee00"


def _enc_uint(value: int) -> str:
    """uint256 -> 64-hex-char (32-byte) ABI word, no 0x prefix."""
    return f"{value:064x}"


def _enc_addr(address: str) -> str:
    """address -> 32-byte left-padded ABI word, no 0x prefix."""
    return address.lower().replace("0x", "").rjust(64, "0")


def _dec_uint(data: str | None) -> int | None:
    """Decode the first 32-byte word of hex return data as uint256, or None."""
    if not data:
        return None
    body = data.replace("0x", "")
    if len(body) < 64:
        return None
    try:
        return int(body[:64], 16)
    except ValueError:
        return None


def classify(spent: int, bought: int | None, sold_back: int | None) -> HoneypotResult:
    """Map round-trip amounts to a verdict. Pure — the acceptance-criteria core.

    - bought is falsy            -> unknown (buy leg produced nothing to sell)
    - sold_back is None          -> honeypot (sell reverted: bought but cannot sell)
    - sold_back ~ 0              -> honeypot (sell yields nothing)
    - round-trip loss >= high    -> high_tax
    - otherwise                  -> sellable
    """
    if not bought:
        return HoneypotResult(status="unknown", detail="Buy leg returned no tokens; could not simulate a sell.")
    if sold_back is None:
        return HoneypotResult(status="honeypot",
                              detail="Simulated buy succeeded but the sell reverted; token appears unsellable.")
    if sold_back == 0:
        return HoneypotResult(status="honeypot",
                              detail="Simulated sell returned zero; token appears unsellable.")
    # Round-trip loss vs the native token spent approximates combined buy+sell tax.
    loss_pct = round((1 - sold_back / spent) * 100, 1) if spent else 0.0
    if loss_pct >= settings.honeypot_high_tax_pct:
        return HoneypotResult(status="high_tax", sell_tax_percentage=loss_pct,
                              detail=f"Simulated buy->sell round-trip loses ~{loss_pct}% (tax/slippage).")
    return HoneypotResult(status="sellable", sell_tax_percentage=loss_pct,
                          detail=f"Simulated round-trip is sellable (~{loss_pct}% round-trip loss).")


def _resolve_router(market: TokenMarketData | None) -> str | None:
    """Router address for the token's DEX, or None when unmapped (keeps the sim inert)."""
    if not market or not market.dex_id:
        return None
    return settings.dex_routers.get(market.dex_id)


async def simulate(token_address: str, market: TokenMarketData | None) -> HoneypotResult:
    """Simulate a buy->sell round-trip. Returns "unknown" whenever it cannot run.

    Inert unless enabled AND a router is mapped for this token's DEX. The executing
    round-trip is implemented in `_run_roundtrip` (slice 2).
    """
    if not settings.honeypot_sim_enabled:
        return HoneypotResult(status="unknown", detail="Honeypot simulation is disabled.")
    router = _resolve_router(market)
    if not router:
        return HoneypotResult(status="unknown",
                              detail="No DEX router mapped for this chain/pair; simulation unavailable.")

    key = f"honeypot:{token_address.lower()}"
    if settings.http_cache_enabled:
        cached = _sim_cache.get(key)
        if cached is not MISS:
            return cached

    result = await _run_roundtrip(token_address, market, router)
    # Cache only an executed verdict; "unknown" stays retryable next analyze.
    if settings.http_cache_enabled and result.status != "unknown":
        _sim_cache.set(key, result)
    return result


async def _run_roundtrip(token_address: str, market: TokenMarketData, router: str) -> HoneypotResult:
    """Atomic buy->sell in ONE eth_call via an injected prober contract.

    Two separate eth_calls cannot share state (each runs on a fresh snapshot), so the
    round-trip must execute inside a single call. We inject a compiled prober's runtime
    bytecode at the synthetic buyer via the `code` state override and fund it with native
    balance; the prober buys then sells in one tx-context and returns (bought, soldBack),
    catching a sell revert as soldBack=0. Everything needed beyond the router — the
    wrapped-native address and the prober bytecode — is config; absent -> "unknown".
    """
    weth = settings.honeypot_weth_address
    prober = settings.honeypot_prober_code
    if not weth or not prober:
        return HoneypotResult(status="unknown",
                              detail="Simulation artifacts (wrapped-native / prober) not configured for this chain.")

    spent = settings.honeypot_sim_buy_wei
    # probe(router, weth, token, buyWei) — 4 address/uint words after the selector.
    calldata = (
        settings.honeypot_prober_selector
        + _enc_addr(router) + _enc_addr(weth) + _enc_addr(token_address) + _enc_uint(spent)
    )
    override = {
        SYNTHETIC_BUYER: {
            "code": prober,
            "balance": hex(spent * 2),  # cover the buy plus gas headroom
        }
    }
    ret = await rpc_client.eth_call(SYNTHETIC_BUYER, calldata, state_override=override)
    if ret is None:
        # RPC error / call reverted at setup: could not simulate, not a verdict.
        return HoneypotResult(status="unknown", detail="Simulation call failed; could not determine sellability.")

    body = ret.replace("0x", "")
    bought = _dec_uint("0x" + body[:64]) if len(body) >= 64 else None
    sold_back = _dec_uint("0x" + body[64:128]) if len(body) >= 128 else None
    return classify(spent, bought, sold_back)
