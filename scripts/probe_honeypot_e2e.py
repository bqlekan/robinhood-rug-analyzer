"""Throwaway E2E check: run the full honeypot simulate() against live Robinhood Chain.

Uses the app's own rpc_client (httpx, correct headers) exactly as production would.
Injects the pinned prober bytecode via eth_call state override and does a real
buy->sell round-trip against the verified SwapRouter02. No keys, no funds, no tx:
read-only eth_call with an ephemeral state override.

Run: python -m scripts.probe_honeypot_e2e
"""

import asyncio
import logging

from app.models.token import TokenMarketData
from app.services import honeypot_sim, http, route_discovery

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# Robinhood stock tokens (verified canonical addresses from the chain docs). These are
# real, liquid tokens on the chain's own Uniswap deployment -- a legitimate sellability
# check should classify them "sellable", proving the whole path executes end-to-end.
TOKENS = {
    "TSLA": "0x322F0929c4625eD5bAd873c95208D54E1c003b2d",   # deep WETH pool -> direct route
    "CASHCAT": "0x020bfC650A365f8BB26819deAAbF3E21291018b4",  # WETH-liquid meme -> direct route
    "KARMA": "0xB47f4702DEB124cb4eB6286bE83c9d84277C6239",    # WETH dust, USDG-liquid -> USDG hop
    "AAPL": "0xaF3D76f1834A1d425780943C99Ea8A608f8a93f9",    # dust on both -> unknown (no route)
}


async def main() -> None:
    # DexScreener labels this chain's Uniswap as dexId "uniswap"; that maps to the
    # verified SwapRouter02 in settings.dex_routers.
    market = TokenMarketData(dex_id="uniswap")
    try:
        for sym, addr in TOKENS.items():
            route = await route_discovery.discover_route(addr)
            desc = route.describe() if route else "no liquid route"
            r = await honeypot_sim.simulate(addr, market)
            tax = "" if r.sell_tax_percentage is None else f"  round-trip loss ~{r.sell_tax_percentage}%"
            print(f"{sym:5} {addr}\n      route: {desc}\n      -> {r.status.upper()}{tax}\n         {r.detail}")
    finally:
        await http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
