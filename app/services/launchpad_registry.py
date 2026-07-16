from __future__ import annotations

"""Registry mapping on-chain markers to known Robinhood Chain launchpads and LP lockers.

Addresses on Robinhood Chain are still stabilizing, so this registry is intentionally
data-driven and easy to extend. Detection uses two complementary signals:

1. Known deployer / factory addresses (exact match on the token creator).
2. Name/tag heuristics from Blockscout address metadata and public tags.

All addresses are stored lowercased for case-insensitive matching.
"""

# creator/factory address (lowercased) -> launchpad name.
# Populate as launchpad factory addresses are confirmed on-chain.
LAUNCHPAD_DEPLOYERS: dict[str, str] = {
    # "0x...": "NOXA Fun",
    # "0x...": "Bags",
    # "0x...": "Pump.fun",
}

# Substrings found in a contract's name/public tags that imply a launchpad origin.
LAUNCHPAD_NAME_HINTS: dict[str, str] = {
    "noxa": "NOXA Fun",
    "bags": "Bags",
    "pump": "Pump.fun",
    "pleiades": "Pleiades",
    "uniswap": "Uniswap",
}

# Known burn / dead addresses that indicate LP tokens were destroyed (permanent lock).
BURN_ADDRESSES: set[str] = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}

# Known LP locker contracts. Populate with confirmed locker addresses on the chain.
# label shown to the user when the LP is held by one of these.
LP_LOCKERS: dict[str, str] = {
    # "0x...": "UNCX",
    # "0x...": "Team Finance",
}

# Established assets (stablecoins, wrapped majors, blue chips) to exclude from the
# rug scanner — the tool is meant to surface risk in newer creations, not to re-rank
# well-known coins. Matched case-insensitively against a token's symbol or name.
ESTABLISHED_TOKEN_SYMBOLS: set[str] = {
    # Stablecoins
    "usdt", "usdc", "usdg", "usde", "dai", "busd", "tusd", "usdp", "frax", "lusd",
    "gusd", "fdusd", "pyusd", "usdd", "sdai", "susds", "usds", "crvusd", "usdb",
    # Wrapped / bridged majors
    "weth", "wbtc", "wsol", "wbnb", "wmatic", "wavax", "wftm", "wsteth", "steth",
    "reth", "cbeth", "sfrxeth", "meth",
    # Blue-chip / native wrapped natives on multiple chains
    "eth", "btc", "bnb", "sol", "matic", "avax", "arb", "op", "link", "uni",
    # Robinhood-specific: known native/wrapped placeholder if applicable.
    "wrhx",
}

ESTABLISHED_TOKEN_NAME_HINTS: set[str] = {
    "tether", "usd coin", "wrapped ether", "wrapped bitcoin", "wrapped bnb",
    "wrapped solana", "dai stablecoin", "binance usd", "ethena", "paxos",
}


# Substrings that reliably indicate a stablecoin/wrapped-major derivative — used
# in addition to exact-symbol checks so wrappers like "syrupUSDG" or "sUSDe" are
# also filtered out. Kept short and conservative to avoid false positives.
ESTABLISHED_SYMBOL_SUBSTRINGS: tuple[str, ...] = (
    "usdt", "usdc", "usdg", "usde", "usdb", "usdp", "usds",
    "weth", "wbtc", "wsol", "wbnb", "wmatic", "wavax",
    "steth", "reth", "cbeth", "sfrxeth",
)


def is_established_token(symbol: str | None, name: str | None) -> bool:
    """True if the token looks like a well-known asset the scanner should skip."""
    sym = (symbol or "").strip().lower()
    if sym:
        if sym in ESTABLISHED_TOKEN_SYMBOLS:
            return True
        if any(needle in sym for needle in ESTABLISHED_SYMBOL_SUBSTRINGS):
            return True
    nm = (name or "").strip().lower()
    if nm and any(hint in nm for hint in ESTABLISHED_TOKEN_NAME_HINTS):
        return True
    return False


def normalize(address: str | None) -> str:
    return (address or "").strip().lower()


def detect_launchpad(creator_address: str | None, contract_name: str | None, tags: list[str] | None = None) -> tuple[str, str, str | None]:
    """Return (name, confidence, detail).

    confidence is "high" for an exact deployer match, "medium" for a name/tag hint,
    and "low" when nothing matches.
    """
    creator = normalize(creator_address)
    if creator and creator in LAUNCHPAD_DEPLOYERS:
        name = LAUNCHPAD_DEPLOYERS[creator]
        return name, "high", f"Deployed by known {name} factory/deployer."

    haystack_parts = [contract_name or ""]
    haystack_parts.extend(tags or [])
    haystack = " ".join(haystack_parts).lower()
    for hint, name in LAUNCHPAD_NAME_HINTS.items():
        if hint in haystack:
            return name, "medium", f"Contract metadata references '{hint}'."

    return "Unknown", "low", "No known launchpad marker matched. May be a manual/custom deploy."


def locker_label(address: str | None) -> str | None:
    """Return a human label if the given address is a known LP locker or burn address."""
    addr = normalize(address)
    if addr in BURN_ADDRESSES:
        return "Burn address"
    return LP_LOCKERS.get(addr)


def is_burn_address(address: str | None) -> bool:
    return normalize(address) in BURN_ADDRESSES
