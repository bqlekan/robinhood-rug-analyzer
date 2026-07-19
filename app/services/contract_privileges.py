from __future__ import annotations

"""Live contract-privilege / authority reads (M11 deliverable).

`contract_intel` tells us *which template* a token was built from. This module tells us
*what dangerous powers the dev still holds*: can they mint new supply, freeze trading,
blacklist wallets, or flip fees — and, crucially, whether ownership has been renounced
(which neutralizes the classic `onlyOwner` rug powers).

Two inputs, both already on hand:
  - the verified ABI from Blockscout's `/smart-contracts/{address}` payload (same payload
    `contract_intel` parses — fetched once and shared, no extra request), and
  - live `eth_call` reads of `owner()`/`getOwner()` and `paused()` via the shared RPC client.

Design guarantees (mirrors honeypot_sim / contract_intel discipline):
  - Unverified / no ABI -> `analyzed=False`, no RPC fired, no false "clean". Absence of an
    ABI is "couldn't see", not "safe".
  - A confirmed renounce (owner == zero address) is the ONLY thing that silences the power
    signals. Retained OR unknown ownership keeps powers flagged, so we never read a false safe.
  - Every RPC read degrades to None on any failure (rpc_client already guarantees this).
"""

import logging

from app.models.token import ContractPrivileges
from app.services import rpc_client

logger = logging.getLogger(__name__)

# Function-name substrings (lowercased) that mark each dangerous power. Matched against
# non-view/pure ABI functions only — a view named `paused` is state, not a power.
_MINT_NEEDLES = ("mint",)
_PAUSE_NEEDLES = ("pause",)  # matches pause / unpause / setPaused
_BLACKLIST_NEEDLES = ("blacklist", "blocklist", "denylist", "setbots", "setblocked", "addbot")
_FEE_NEEDLES = ("fee", "tax")  # gated on a set/update-style mutator below

# Well-known selectors for the live reads (no args -> data is just the selector).
_SEL_OWNER = "0x8da5cb5b"      # owner()
_SEL_GET_OWNER = "0x893d20e8"  # getOwner()
_SEL_PAUSED = "0x5c975abb"     # paused()

_ZERO_WORD = "0" * 64


def _functions(abi: list) -> list[dict]:
    return [e for e in abi if isinstance(e, dict) and e.get("type") == "function"]


def _is_mutator(fn: dict) -> bool:
    """True for a state-changing function (nonpayable/payable), i.e. a real power."""
    return fn.get("stateMutability") not in ("view", "pure")


def _has_read(abi: list, name: str) -> bool:
    return any(f.get("name", "").lower() == name for f in _functions(abi))


def _matches(abi: list, needles: tuple[str, ...]) -> bool:
    for fn in _functions(abi):
        if not _is_mutator(fn):
            continue
        low = (fn.get("name") or "").lower()
        if any(n in low for n in needles):
            return True
    return False


def _has_fee_mutator(abi: list) -> bool:
    """A fee/tax setter: a mutator whose name pairs a set/update verb with fee/tax."""
    for fn in _functions(abi):
        if not _is_mutator(fn):
            continue
        low = (fn.get("name") or "").lower()
        if any(n in low for n in _FEE_NEEDLES) and (low.startswith("set") or low.startswith("update")):
            return True
    return False


def _decode_addr_zero(hex_data: str | None) -> bool | None:
    """From an `owner()` return word: True if the zero address (renounced), False if a real
    owner, None if unreadable. None is 'couldn't confirm', never treated as renounced."""
    if not hex_data:
        return None
    body = hex_data.replace("0x", "")
    if len(body) < 64:
        return None
    return body[:64] == _ZERO_WORD


def _decode_addr(hex_data: str | None) -> str | None:
    if not hex_data:
        return None
    body = hex_data.replace("0x", "")
    if len(body) < 64:
        return None
    return "0x" + body[24:64]


def _decode_bool(hex_data: str | None) -> bool | None:
    if not hex_data:
        return None
    body = hex_data.replace("0x", "")
    if len(body) < 64:
        return None
    try:
        return int(body[:64], 16) != 0
    except ValueError:
        return None


def infer_privileges(
    payload: dict | None,
    owner_hex: str | None = None,
    paused_hex: str | None = None,
) -> ContractPrivileges:
    """Map an ABI + live reads to a ContractPrivileges record. Pure — the test core.

    `owner_hex`/`paused_hex` are the raw `eth_call` return blobs (or None if not read /
    the read failed). Unverified or ABI-less payloads degrade to `analyzed=False`.
    """
    if not payload or not payload.get("is_verified"):
        return ContractPrivileges(
            analyzed=False,
            detail="Contract source/ABI not verified on Blockscout; privilege reads unavailable.",
        )
    abi = payload.get("abi")
    if not isinstance(abi, list) or not abi:
        return ContractPrivileges(
            analyzed=False,
            detail="Verified contract exposes no ABI; privilege reads unavailable.",
        )

    can_mint = _matches(abi, _MINT_NEEDLES)
    can_pause = _matches(abi, _PAUSE_NEEDLES)
    can_blacklist = _matches(abi, _BLACKLIST_NEEDLES)
    can_set_fees = _has_fee_mutator(abi)

    renounced = _decode_addr_zero(owner_hex)  # True/False/None
    owner_address = None if renounced else _decode_addr(owner_hex)
    is_paused = _decode_bool(paused_hex) if can_pause else None

    powers = [p for p, on in (
        ("mint", can_mint), ("pause", can_pause), ("blacklist", can_blacklist), ("fee changes", can_set_fees),
    ) if on]
    if not powers:
        detail = "No mint/pause/blacklist/fee-mutation powers found in the verified ABI."
    elif renounced is True:
        detail = f"Ownership renounced; retained powers ({', '.join(powers)}) are behind a renounced owner."
    elif renounced is False:
        detail = f"Owner retained and can still: {', '.join(powers)}."
    else:
        detail = f"Contract exposes {', '.join(powers)}; ownership state could not be confirmed."

    return ContractPrivileges(
        analyzed=True,
        owner_address=owner_address,
        ownership_renounced=renounced,
        can_mint=can_mint,
        can_pause=can_pause,
        is_paused=is_paused,
        can_blacklist=can_blacklist,
        can_set_fees=can_set_fees,
        detail=detail,
    )


async def fetch_privileges(address: str, payload: dict | None) -> ContractPrivileges:
    """Read live owner/paused state (only when the ABI declares them) and classify.

    Takes the already-fetched smart-contract payload so no extra Blockscout request fires.
    Fires at most two `eth_call`s, and only for reads the ABI actually exposes.
    """
    if not payload or not payload.get("is_verified"):
        return infer_privileges(payload)
    abi = payload.get("abi")
    if not isinstance(abi, list) or not abi:
        return infer_privileges(payload)

    owner_hex = None
    if _has_read(abi, "owner"):
        owner_hex = await rpc_client.eth_call(address, _SEL_OWNER)
    elif _has_read(abi, "getowner"):
        owner_hex = await rpc_client.eth_call(address, _SEL_GET_OWNER)

    paused_hex = None
    if _matches(abi, _PAUSE_NEEDLES) and _has_read(abi, "paused"):
        paused_hex = await rpc_client.eth_call(address, _SEL_PAUSED)

    return infer_privileges(payload, owner_hex=owner_hex, paused_hex=paused_hex)
