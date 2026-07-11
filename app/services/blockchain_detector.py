def detect_blockchain(contract_address: str) -> str:
    """Detect blockchain from address format only; no external analysis yet."""
    normalized = contract_address.strip()

    if normalized.startswith("0x") and len(normalized) == 42:
        return "evm_compatible_unknown"

    if 32 <= len(normalized) <= 44 and not normalized.startswith("0x"):
        return "solana_candidate"

    return "unknown"
