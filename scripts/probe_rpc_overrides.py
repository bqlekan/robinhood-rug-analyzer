"""Throwaway probe: does Robinhood Chain's RPC support eth_call state overrides?

Drives the reusable M10 rpc_client against the live RPC. The decisive test injects
runtime bytecode returning uint256(42) at a dummy address via the eth_call state-
override object, then calls it: a node that honors overrides returns 0x..2a; one
that ignores/rejects them returns empty/null or an error. Run: python -m scripts.probe_rpc_overrides
"""

import asyncio
import logging

from app.core.config import settings
from app.services import http, rpc_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# Runtime bytecode: PUSH1 0x2a; PUSH1 0; MSTORE; PUSH1 0x20; PUSH1 0; RETURN -> returns 42.
_CODE = "0x602a60005260206000f3"
_DUMMY = "0x000000000000000000000000000000000000dEaD"
_EXPECTED = "0x" + "00" * 31 + "2a"


async def main() -> None:
    print(f"RPC URL: {settings.rpc_url}\n")

    chain_id = await rpc_client._rpc("eth_chainId", [])
    print(f"eth_chainId          -> {chain_id!r}  (expect 0x1237 = 4663)")

    client_version = await rpc_client._rpc("web3_clientVersion", [])
    print(f"web3_clientVersion   -> {client_version!r}")

    block = await rpc_client._rpc("eth_blockNumber", [])
    print(f"eth_blockNumber      -> {block!r}")

    # Baseline: eth_call to identity precompile 0x04 echoes its input.
    echo = await rpc_client._rpc(
        "eth_call", [{"to": "0x0000000000000000000000000000000000000004", "data": "0x1234"}, "latest"]
    )
    print(f"eth_call (identity)  -> {echo!r}  (expect echo of 0x1234)")

    # The decisive test: eth_call WITH a 3rd state-override param injecting code.
    override = await rpc_client._rpc(
        "eth_call",
        [{"to": _DUMMY, "data": "0x"}, "latest", {_DUMMY: {"code": _CODE}}],
    )
    print(f"eth_call (override)  -> {override!r}  (expect {_EXPECTED})")

    supported = isinstance(override, str) and override.lower().endswith("2a") and len(override) >= 4
    print(f"\nSTATE OVERRIDES SUPPORTED: {supported}")

    await http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
