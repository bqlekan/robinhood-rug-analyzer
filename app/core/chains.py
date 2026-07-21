"""Chain abstraction layer (M22).

A single seam that owns *which EVM chain* the analyzer targets: chain identity
(id/name/DexScreener label), the endpoints (Blockscout REST, JSON-RPC), and the
Uniswap-v3 DEX topology the honeypot sim routes over (wrapped-native, factory,
routers, quote assets, fee tiers, reserve floors).

Purely architectural: it does NOT add multi-chain support. There is exactly one
registered chain — Robinhood Chain, the default — and its `ChainConfig` is built
**live from `settings`** on every `active()` call. So env overrides and test
monkeypatches on the underlying `settings.*` fields flow straight through, and
behaviour is byte-for-byte what it was when services read `settings` directly.

Adding a real second chain later = register another `ChainConfig` builder and let
`active()` pick by an id/slug — no service change, because services already read
their chain values from here instead of from `settings`.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.core.config import settings


class ChainConfig(BaseModel):
    """Everything chain-specific a service needs, in one place.

    Split by concern:
    - identity: `chain_id`, `chain_name`, `dexscreener_chain` (DexScreener's chainId label)
    - endpoints: `blockscout_base_url`, `rpc_url`
    - DEX topology (Uniswap v3, for the honeypot sim / route discovery):
      `weth_address`, `v3_factory`, `dex_routers` (dexId -> SwapRouter02), `quote_assets`,
      `fee_tiers`, `min_quote_reserve` (per-asset reserve floor, "*" fallback).

    Simulation *policy* (prober bytecode, buy amount, tax threshold) is deliberately
    NOT here — it is chain-agnostic and stays in `settings`.
    """

    slug: str
    chain_id: int
    chain_name: str
    dexscreener_chain: str
    blockscout_base_url: str
    rpc_url: str

    # DEX topology — optional so a chain with no mapped DEX leaves the sim inert.
    weth_address: str | None = None
    v3_factory: str | None = None
    dex_routers: dict[str, str] = {}
    quote_assets: list[str] = []
    fee_tiers: list[int] = []
    min_quote_reserve: dict[str, int] = {}


ROBINHOOD = "robinhood"


def _robinhood() -> ChainConfig:
    """Robinhood Chain, sourced live from `settings` (so overrides/monkeypatches apply)."""
    return ChainConfig(
        slug=ROBINHOOD,
        chain_id=settings.chain_id,
        chain_name=settings.chain_name,
        dexscreener_chain=settings.dexscreener_chain,
        blockscout_base_url=settings.blockscout_base_url,
        rpc_url=settings.rpc_url,
        weth_address=settings.honeypot_weth_address,
        v3_factory=settings.honeypot_v3_factory,
        dex_routers=settings.dex_routers,
        quote_assets=settings.honeypot_quote_assets,
        fee_tiers=settings.honeypot_fee_tiers,
        min_quote_reserve=settings.honeypot_min_quote_reserve,
    )


# Registry: slug -> builder. One entry today; a second chain is one more line here.
_REGISTRY: dict[str, callable] = {ROBINHOOD: _robinhood}


def active() -> ChainConfig:
    """The chain the analyzer is currently targeting (the default chain today).

    Rebuilt from `settings` on every call — never cached — so a change to the
    underlying settings (env override, or a test monkeypatch) is always reflected.
    """
    return _REGISTRY[settings.default_chain]()


def get(slug: str) -> ChainConfig:
    """Look up a registered chain by slug (KeyError if unknown)."""
    return _REGISTRY[slug]()


if __name__ == "__main__":
    # ponytail: self-check — active() reflects settings live, incl. a monkeypatch.
    c = active()
    assert c.slug == ROBINHOOD
    assert c.chain_id == settings.chain_id
    assert c.rpc_url == settings.rpc_url
    assert c.weth_address == settings.honeypot_weth_address
    _orig = settings.chain_name
    settings.chain_name = "Test Chain"
    assert active().chain_name == "Test Chain", "active() must re-read settings live"
    settings.chain_name = _orig
    print("chains self-check ok")
