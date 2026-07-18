from __future__ import annotations

"""Contract-address extraction from social profile text (M23 Deliverable D).

Pure, provider-neutral, no I/O. Given free-form profile text (bio, links, website,
display name) it finds candidate on-chain addresses, labels a best-effort chain,
normalizes and validates them, and returns `ExtractedContract`s carrying where each
was found and why. It NEVER reaches the network — validation is purely structural
(shape/checksum-agnostic), matching how `models/token.is_valid_address` guards the
outbound boundary elsewhere in this codebase.

Chain support:
  - EVM (`0x` + 40 hex): validated with the project's existing `is_valid_address`.
    This is the only family the single-chain rug analyzer can actually analyze, so
    only these are marked `supported=True`. The labeled chain is "robinhood" (the
    configured chain) unless an explicit chain keyword sits next to the address.
  - Solana (base58, 32-44 chars): recognized and recorded for intelligence/history,
    but `supported=False` — the analyzer is EVM/Robinhood-only. Recording rather
    than dropping keeps a complete evidence trail for later cross-chain work.

Design: extraction is deliberately conservative. A bare 0x string is accepted (it's
structurally unambiguous), but base58 blobs are only treated as Solana addresses
when they match the length window AND aren't obviously something else, because
base58 has no delimiter and over-eager matching would invent contracts. Every
returned contract carries `evidence` so a human/AI can audit the extraction.
"""

import re

from app.core.config import settings
from app.models.kol import ExtractedContract
from app.models.token import is_valid_address

# EVM address: 0x + 40 hex. Word-bounded so it isn't clipped out of a longer hex run.
_EVM_RE = re.compile(r"0x[0-9a-fA-F]{40}")

# Explicit "contract address" markers people put in bios. Case-insensitive; the
# address itself follows (possibly after ":", whitespace, or a newline).
_CA_MARKER_RE = re.compile(r"\b(?:ca|contract|token\s*address|mint)\b\s*[:=\-]?\s*", re.IGNORECASE)

# Solana / base58 candidate: 32-44 base58 chars (no 0, O, I, l). Bounded by non-
# base58 chars so it doesn't slice mid-URL. This is a *candidate* — see _looks_solana.
_BASE58_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")

# Chain keywords -> normalized chain label, for when an address sits next to one.
_CHAIN_KEYWORDS: dict[str, str] = {
    "solana": "solana",
    "sol": "solana",
    "ethereum": "ethereum",
    "eth": "ethereum",
    "base": "base",
    "robinhood": "robinhood",
    "rhc": "robinhood",
}


def _nearby_chain(text: str, start: int, end: int, window: int = 24) -> str | None:
    """Best-effort chain label from keywords immediately around a match."""
    lo = max(0, start - window)
    context = text[lo:start] + " " + text[end:end + window]
    context = context.lower()
    for kw, chain in _CHAIN_KEYWORDS.items():
        if re.search(rf"\b{re.escape(kw)}\b", context):
            return chain
    return None


def _has_ca_marker(text: str, start: int) -> bool:
    """True if a 'CA:'-style marker immediately precedes the address at `start`."""
    lo = max(0, start - 32)
    return bool(_CA_MARKER_RE.search(text[lo:start]))


def _looks_solana(candidate: str) -> bool:
    """Filter base58 false positives: require mixed case OR digits so ordinary
    lowercase words (which are valid base58) don't masquerade as addresses. Solana
    addresses are high-entropy; plain English words are not."""
    has_upper = any(c.isupper() for c in candidate)
    has_digit = any(c.isdigit() for c in candidate)
    return has_upper or has_digit


def extract_contracts(
    text: str | None,
    *,
    source: str | None = None,
    max_contracts: int | None = None,
) -> list[ExtractedContract]:
    """Extract, normalize, validate, and de-duplicate contracts from one text field.

    `source` labels where the text came from (bio/website/links/...) for evidence.
    Returns at most `max_contracts` (defaults to the configured per-account cap),
    de-duplicated by lowercased address, EVM (supported) ordered before others.
    Malformed input, empty text, and parse hiccups yield an empty list — never raise.
    """
    if not text:
        return []
    cap = max_contracts if max_contracts is not None else settings.kol_crypto_max_contracts_per_account

    found: dict[str, ExtractedContract] = {}
    evm_spans: list[tuple[int, int]] = []

    # 1. EVM addresses — structurally validated by the project's own guard.
    for m in _EVM_RE.finditer(text):
        evm_spans.append((m.start(), m.end()))
        raw = m.group(0)
        if not is_valid_address(raw):
            continue
        addr = raw.lower()
        if addr in found:
            continue
        chain = _nearby_chain(text, m.start(), m.end()) or settings.dexscreener_chain
        marked = _has_ca_marker(text, m.start())
        # Only the configured (EVM) chain is analyzable by the single-chain analyzer.
        supported = chain in {settings.dexscreener_chain, "ethereum", "base", "robinhood"}
        note = "CA: marker" if marked else "0x address"
        if chain:
            note += f" ({chain})"
        found[addr] = ExtractedContract(
            address=addr, chain=chain, supported=supported, source=source, evidence=note,
        )

    # 2. Solana / base58 — recorded for history, not analyzable here (supported=False).
    for m in _BASE58_RE.finditer(text):
        cand = m.group(0)
        # Skip base58 runs that overlap an EVM match: the 40 hex chars after "0x" are
        # themselves valid base58, so an EVM address would otherwise spawn a phantom
        # "solana" contract from its own tail.
        if any(m.start() < e and m.end() > s for s, e in evm_spans):
            continue
        if cand.startswith("0x") or not _looks_solana(cand):
            continue
        # Require a corroborating signal: an explicit CA marker or a nearby "solana"
        # keyword. Base58 has no delimiter, so a naked blob alone is too weak.
        chain = _nearby_chain(text, m.start(), m.end())
        marked = _has_ca_marker(text, m.start())
        if chain != "solana" and not marked:
            continue
        if cand in found:
            continue
        found[cand] = ExtractedContract(
            address=cand, chain="solana", supported=False, source=source,
            evidence="base58 mint" + (" (CA: marker)" if marked else " (solana keyword)"),
        )

    # EVM/supported first, then by discovery order; apply the cap.
    ordered = sorted(found.values(), key=lambda c: (not c.supported,))
    return ordered[:cap] if cap and cap > 0 else ordered


def extract_from_fields(fields: dict[str, str | None]) -> list[ExtractedContract]:
    """Extract across several named fields (e.g. {"bio":..., "website":..., "links":...}),
    tagging each contract with the field it came from and de-duplicating across all of
    them. The per-account cap is applied once, after merging."""
    merged: dict[str, ExtractedContract] = {}
    for source, text in fields.items():
        for c in extract_contracts(text, source=source, max_contracts=None):
            merged.setdefault(c.address.lower() if c.supported else c.address, c)
    ordered = sorted(merged.values(), key=lambda c: (not c.supported,))
    cap = settings.kol_crypto_max_contracts_per_account
    return ordered[:cap] if cap and cap > 0 else ordered
