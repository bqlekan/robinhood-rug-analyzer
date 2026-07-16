from __future__ import annotations

"""Read a verified smart contract and infer the template/protocol behind it.

Blockscout's `/smart-contracts/{address}` returns the flattened Solidity source,
compiler metadata, ABI, and additional source paths. That's enough to tell whether
a token was built from a common template (OpenZeppelin, Uniswap V2 clone, ERC20 fork)
versus a bespoke deploy — a useful signal for both risk scoring and the launchpad card.

The parser is a set of substring heuristics on the source text. It is intentionally
lightweight and defensive: no AST, no external tools, and if the contract is not
verified it degrades gracefully to `unknown`.
"""

import logging

from app.models.token import ContractIntel
from app.services import blockscout_client

logger = logging.getLogger(__name__)

# Ordered import-path signatures. First match wins for the "template" field.
TEMPLATE_SIGNATURES: list[tuple[str, str]] = [
    ("@openzeppelin/contracts-upgradeable", "OpenZeppelin Upgradeable"),
    ("@openzeppelin/contracts", "OpenZeppelin ERC20"),
    ("@solmate", "Solmate"),
    ("@solady", "Solady"),
    ("@rari-capital", "Solmate (Rari)"),
    ("@uniswap/v2-core", "Uniswap V2 core"),
    ("@uniswap/v2-periphery", "Uniswap V2 periphery"),
    ("@uniswap/v3-core", "Uniswap V3 core"),
    ("@uniswap/v3-periphery", "Uniswap V3 periphery"),
    ("@chainlink/contracts", "Chainlink CCIP / OZ hybrid"),
    ("@layerzerolabs", "LayerZero OFT"),
    ("erc20a.sol", "ERC721A"),  # rare in ERC20 but flagged if present
]

# Higher-level protocol families (broader than the template).
PROTOCOL_SIGNATURES: list[tuple[str, str, str]] = [
    # (needle_lowercase, protocol_label, confidence)
    ("@chainlink/contracts", "Chainlink CCIP", "high"),
    ("@layerzerolabs", "LayerZero", "high"),
    ("@uniswap/v2-core", "Uniswap V2", "high"),
    ("@uniswap/v3-core", "Uniswap V3", "high"),
    ("iburnminterc20", "Burn/Mint bridge token (CCIP-style)", "medium"),
    ("basedex", "Base DEX fork", "medium"),
    ("pancake", "PancakeSwap fork", "medium"),
]


def _first_match(text: str, table: list[tuple[str, str]]) -> str | None:
    for needle, label in table:
        if needle in text:
            return label
    return None


def _collect_imports(source: str, additional_paths: list[str]) -> list[str]:
    """Grab import specifiers from the flattened source and additional file paths."""
    imports: list[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("import"):
            # Keep it short — one line per import, deduped later.
            imports.append(stripped.rstrip(";"))
    imports.extend(additional_paths)
    # Dedup preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for imp in imports:
        if imp not in seen:
            seen.add(imp)
            out.append(imp)
    return out[:20]


def infer_from_contract(payload: dict | None) -> ContractIntel:
    """Turn Blockscout's smart-contract payload into a ContractIntel record."""
    if not payload:
        return ContractIntel(verified=False, detail="Contract source not available on Blockscout.")

    verified = bool(payload.get("is_verified"))
    source = (payload.get("source_code") or "").lower()
    additional = [x.get("file_path", "") for x in (payload.get("additional_sources") or []) if isinstance(x, dict)]
    additional_blob = " ".join(additional).lower()
    combined = source + " " + additional_blob

    template = _first_match(combined, TEMPLATE_SIGNATURES) or "custom"

    protocol: str | None = None
    protocol_confidence = "low"
    for needle, label, conf in PROTOCOL_SIGNATURES:
        if needle in combined:
            protocol = label
            protocol_confidence = conf
            break

    if not verified:
        detail = "Contract bytecode is not verified on Blockscout; protocol inference disabled."
        template = "unknown"
    elif template == "custom" and not protocol:
        detail = "Verified source contains no recognized template imports; likely a bespoke deploy."
    else:
        detail = f"Source matches {template}{' / ' + protocol if protocol else ''}."

    imports = _collect_imports(payload.get("source_code") or "", additional) if verified else []

    return ContractIntel(
        verified=verified,
        contract_name=payload.get("name"),
        compiler=payload.get("compiler_version"),
        language=payload.get("language"),
        template=template,
        protocol=protocol,
        protocol_confidence=protocol_confidence,
        imports=imports,
        detail=detail,
    )


async def fetch_contract_intel(address: str) -> ContractIntel:
    payload = await blockscout_client.get_smart_contract(address)
    return infer_from_contract(payload)
