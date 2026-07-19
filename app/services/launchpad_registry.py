from __future__ import annotations

"""Registry mapping on-chain markers to known Robinhood Chain launchpads and LP lockers.

Registry-driven and easy to extend. Each launchpad entry is a documented record so
detection can resolve an exact address match to a named platform with a confidence tier.

SECURITY: the production registry is intentionally EMPTY. Robinhood Chain (id 4663) is
new; we do not have authoritatively-verified factory/locker addresses. A wrong locker
entry would make `analyze_liquidity_lock` report a rug's liquidity as "locked" — the exact
false-negative this tool exists to prevent. So we ship no unverified addresses: detection
degrades to "Unknown" rather than making a confident, possibly-false claim. Populate ONLY
from an authoritative source, filling `source` and `verified_date`, and set `enabled: True`.
Example addresses appear in tests only, never here.

Entry schema (LAUNCHPADS):
    name:            platform name shown to the user
    factory_address: contract that deploys the platform's tokens (exact match -> HIGH)
    team_addresses:  known deployer/team wallets (exact match -> LOW heuristic)
    event_signatures: factory event topics (reserved for M9 receipt/log parsing -> MEDIUM)
    source:          URL / citation the entry was verified from
    verified_date:   ISO date the entry was confirmed
    enabled:         only enabled entries participate in detection

All addresses are compared lowercased.
"""

# Verified launchpad records. EMPTY in production by design (see module docstring).
LAUNCHPADS: list[dict] = [
    # {
    #     "name": "Example Launch",
    #     "factory_address": "0x...",
    #     "team_addresses": ["0x..."],
    #     "event_signatures": [],
    #     "source": "https://authoritative-source/...",
    #     "verified_date": "2026-01-01",
    #     "enabled": True,
    # },
]

# Substrings in a contract's name/public tags that hint at a launchpad origin. This is a
# heuristic (not an address claim), so a match yields LOW confidence only.
LAUNCHPAD_NAME_HINTS: dict[str, str] = {
    "noxa": "NOXA Fun",
    "bags": "Bags",
    "pump": "Pump.fun",
    "pleiades": "Pleiades",
    "uniswap": "Uniswap",
}

# Known burn / dead addresses that indicate LP tokens were destroyed (permanent lock).
# Chain-agnostic and safe to hardcode.
BURN_ADDRESSES: set[str] = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}

# Verified LP locker records. EMPTY in production by design (see module docstring).
#   {"address": "0x...", "label": "UNCX", "source": "...", "verified_date": "...",
#    "unlock_selector": "0x<4-byte>", "unlock_word_index": 0}
# M13 (unlock schedule): a locker MAY declare how to read its unlock timestamp so the
# binary lock signal becomes time-aware. Optional and evidence-gated — a locker without a
# spec degrades to the prior presence-only behaviour, never a fabricated schedule.
#   unlock_selector:   4-byte eth_call selector for a no-arg view returning the unlock
#                      unix timestamp (e.g. `unlockDate()` / `lockTime()`), hex "0x........".
#   unlock_word_index: which 32-byte word of the ABI-encoded return holds the timestamp
#                      (default 0). Lets a struct-returning getter point at the right slot.
LP_LOCKERS: list[dict] = []

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


def _factory_map() -> dict[str, str]:
    """Lowercased factory_address -> name, for enabled entries only."""
    out: dict[str, str] = {}
    for e in LAUNCHPADS:
        if e.get("enabled") and e.get("factory_address"):
            out[normalize(e["factory_address"])] = e["name"]
    return out


def _team_map() -> dict[str, str]:
    """Lowercased team address -> name, for enabled entries only."""
    out: dict[str, str] = {}
    for e in LAUNCHPADS:
        if not e.get("enabled"):
            continue
        for addr in e.get("team_addresses") or []:
            out[normalize(addr)] = e["name"]
    return out


def _event_map() -> dict[str, str]:
    """Lowercased event signature/topic -> name, for enabled entries only."""
    out: dict[str, str] = {}
    for e in LAUNCHPADS:
        if not e.get("enabled"):
            continue
        for sig in e.get("event_signatures") or []:
            out[normalize(sig)] = e["name"]
    return out


def has_enabled_launchpads() -> bool:
    """True if any enabled launchpad entry exists.

    The orchestrator gates its creation-tx / log fetches on this: with the
    production registry empty, no extra network calls fire and behavior is
    unchanged — the on-chain matching activates only once sourced entries exist.
    """
    return any(e.get("enabled") for e in LAUNCHPADS)


def match_creation_evidence(
    factory_to: str | None,
    log_topics: list[str] | None,
) -> tuple[str, str, str] | None:
    """Match a token's contract-creation evidence against the registry.

    - HIGH: the creation tx's `to` (factory) is a verified factory address.
    - MEDIUM: a creation log carries a verified factory event signature.

    Returns (name, confidence, detail) or None when there is no evidence. This is
    the M9 fetch-dependent path; the creator/name heuristics stay in detect_launchpad.
    """
    factory = _factory_map().get(normalize(factory_to)) if factory_to else None
    if factory:
        return factory, "high", f"Created by verified {factory} factory (creation-tx to-address match)."

    if log_topics:
        emap = _event_map()
        for topic in log_topics:
            name = emap.get(normalize(topic))
            if name:
                return name, "medium", f"Creation logs emit a verified {name} factory event."

    return None


def detect_launchpad(creator_address: str | None, contract_name: str | None, tags: list[str] | None = None) -> tuple[str, str, str | None]:
    """Return (name, confidence, detail). Registry-driven, security-first.

    Priority (most to least authoritative):
      HIGH   — creator matches a verified factory address.
      LOW    — creator matches a verified launchpad team wallet (heuristic).
      LOW    — contract name/tag references a known launchpad substring (heuristic).
      UNKNOWN — no evidence. Never a confident claim without a verified address.

    MEDIUM (verified factory *event* match) is reserved for M9, which adds the
    receipt/log reads needed to parse factory events. Not attempted here.
    """
    creator = normalize(creator_address)

    if creator:
        factory = _factory_map().get(creator)
        if factory:
            return factory, "high", f"Deployed by verified {factory} factory."
        team = _team_map().get(creator)
        if team:
            return team, "low", f"Deployer matches a known {team} team wallet (heuristic)."

    haystack_parts = [contract_name or ""]
    haystack_parts.extend(tags or [])
    haystack = " ".join(haystack_parts).lower()
    for hint, name in LAUNCHPAD_NAME_HINTS.items():
        if hint in haystack:
            return name, "low", f"Contract metadata references '{hint}' (name heuristic, not an address match)."

    return "Unknown", "low", "No known launchpad marker matched. May be a manual/custom deploy."


def _locker_map() -> dict[str, str]:
    """Lowercased locker address -> label, for enabled entries only."""
    return {
        normalize(e["address"]): e["label"]
        for e in LP_LOCKERS
        if e.get("enabled", True) and e.get("address")
    }


def locker_label(address: str | None) -> str | None:
    """Return a human label if the given address is a known LP locker or burn address."""
    addr = normalize(address)
    if addr in BURN_ADDRESSES:
        return "Burn address"
    return _locker_map().get(addr)


def is_burn_address(address: str | None) -> bool:
    return normalize(address) in BURN_ADDRESSES


def locker_unlock_spec(address: str | None) -> dict | None:
    """Return the unlock-read spec for a known enabled locker, or None (M13).

    A spec is present only when the verified locker entry declares an `unlock_selector`.
    Burn addresses have no schedule (a burn is permanent), so they return None here.
    """
    addr = normalize(address)
    if not addr or addr in BURN_ADDRESSES:
        return None
    for e in LP_LOCKERS:
        if not e.get("enabled", True) or normalize(e.get("address")) != addr:
            continue
        selector = e.get("unlock_selector")
        if not selector:
            return None
        return {"selector": selector, "word_index": int(e.get("unlock_word_index", 0))}
    return None
