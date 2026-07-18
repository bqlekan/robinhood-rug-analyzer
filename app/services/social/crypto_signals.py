from __future__ import annotations

"""Config-driven crypto signal detectors (M23 Deliverable D).

A *signal* is one observable hint that an account is crypto-related — a DexScreener
link, a Telegram handle, a "CA:" marker, a chain keyword, a valid contract address.
Each detector is a small pure function `(ProfileIntelligence, contracts) -> Evidence
| None`. Detectors are registered in `_DETECTORS` keyed by the same signal name used
in `settings.kol_crypto_signal_weights`, so:

  - adding a signal = add a weight in config + register a detector here, no change to
    the analyzer or classifier;
  - tuning a signal = edit its weight in config, zero code change;
  - a signal with no configured weight contributes 0 (disabled) automatically.

This is the "new signals through configuration" extension point the spec requires.
Everything here is pure and offline — detectors read only the already-built
`ProfileIntelligence` and the already-extracted contracts, never the network.

Detectors return structured `Evidence` (signal, human detail, applied weight,
source) rather than bare booleans, so every fired signal is self-explaining and the
final classification can show exactly what it saw.
"""

import re
from typing import Callable

from app.core.config import settings
from app.models.kol import Evidence, ExtractedContract, ProfileIntelligence

# A detector inspects the profile (+ already-extracted contracts) and either returns
# structured Evidence (signal fired) or None (absent). Weight is filled in by the
# registry from config so detectors don't hardcode magnitudes.
Detector = Callable[[ProfileIntelligence, list[ExtractedContract]], "Evidence | None"]


# --- helpers -----------------------------------------------------------------


def _domain_signal(name: str, patterns: tuple[str, ...], label: str) -> Detector:
    """Build a detector that fires when any of `patterns` appears in the text blob
    or in any link. Used for the aggregator/social-platform link signals."""
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]

    def detect(intel: ProfileIntelligence, _contracts: list[ExtractedContract]) -> Evidence | None:
        haystacks = [(intel.text_blob, "bio")] + [(l, "links") for l in intel.links]
        if intel.website:
            haystacks.append((intel.website, "website"))
        for text, source in haystacks:
            if not text:
                continue
            for rx in compiled:
                m = rx.search(text)
                if m:
                    return Evidence(signal=name, detail=f"{label}: {m.group(0)}", source=source)
        return None

    return detect


def _keyword_signal(name: str, keywords: tuple[str, ...], label: str) -> Detector:
    """Detector firing on any whole-word keyword match in the text blob."""
    rx = re.compile(r"\b(" + "|".join(re.escape(k) for k in keywords) + r")\b", re.IGNORECASE)

    def detect(intel: ProfileIntelligence, _contracts: list[ExtractedContract]) -> Evidence | None:
        m = rx.search(intel.text_blob)
        if m:
            return Evidence(signal=name, detail=f"{label}: '{m.group(1)}'", source="bio")
        return None

    return detect


# --- individual detectors ----------------------------------------------------


def _detect_contract_address(intel: ProfileIntelligence, contracts: list[ExtractedContract]) -> Evidence | None:
    """Strongest signal: a structurally-valid, extractable contract address."""
    if not contracts:
        return None
    supported = [c for c in contracts if c.supported]
    chosen = supported[0] if supported else contracts[0]
    n = len(contracts)
    detail = f"{n} contract{'s' if n != 1 else ''} extracted; e.g. {chosen.address} ({chosen.chain or 'unknown chain'})"
    return Evidence(signal="contract_address", detail=detail, source=chosen.source or "profile")


def _detect_ca_prefix(intel: ProfileIntelligence, _contracts: list[ExtractedContract]) -> Evidence | None:
    rx = re.compile(r"\b(ca|contract|mint)\b\s*[:=]", re.IGNORECASE)
    m = rx.search(intel.text_blob)
    if m:
        return Evidence(signal="ca_prefix", detail=f"explicit '{m.group(0).strip()}' marker", source="bio")
    return None


def _detect_ticker_cashtag(intel: ProfileIntelligence, _contracts: list[ExtractedContract]) -> Evidence | None:
    # $TICKER cashtag, 2-10 uppercase alnum. Requires a letter so "$100" doesn't match.
    m = re.search(r"\$[A-Z][A-Z0-9]{1,9}\b", intel.text_blob.upper())
    if m and any(c.isalpha() for c in m.group(0)[1:]):
        return Evidence(signal="ticker_cashtag", detail=f"cashtag {m.group(0)}", source="bio")
    return None


# Aggregator / data-site link signals (each a key in kol_crypto_signal_weights).
_dexscreener = _domain_signal("dexscreener", (r"dexscreener\.com", r"\bdexscreener\b"), "DexScreener")
_birdeye = _domain_signal("birdeye", (r"birdeye\.so", r"\bbirdeye\b"), "Birdeye")
_gmgn = _domain_signal("gmgn", (r"gmgn\.ai", r"\bgmgn\b"), "GMGN")
_geckoterminal = _domain_signal("geckoterminal", (r"geckoterminal\.com", r"\bgeckoterminal\b"), "GeckoTerminal")
_coingecko = _domain_signal("coingecko", (r"coingecko\.com", r"\bcoingecko\b"), "CoinGecko")
_coinmarketcap = _domain_signal("coinmarketcap", (r"coinmarketcap\.com", r"\bcoinmarketcap\b", r"\bcmc\b"), "CoinMarketCap")
_pumpfun = _domain_signal("pumpfun", (r"pump\.fun", r"\bpumpfun\b", r"\bpump\s*fun\b"), "Pump.fun")
_telegram = _domain_signal("telegram", (r"t\.me/", r"telegram\.me/", r"\btelegram\b", r"@[A-Za-z0-9_]{5,}"), "Telegram")
_discord = _domain_signal("discord", (r"discord\.gg/", r"discord\.com/invite", r"\bdiscord\b"), "Discord")
_github = _domain_signal("github", (r"github\.com/", r"\bgithub\b"), "GitHub")


def _detect_official_website(intel: ProfileIntelligence, _contracts: list[ExtractedContract]) -> Evidence | None:
    """A website link that isn't just one of the aggregators/socials above."""
    if not intel.website:
        return None
    low = intel.website.lower()
    boring = ("t.me", "telegram", "discord", "twitter.com", "x.com", "linktr.ee")
    if any(b in low for b in boring):
        return None
    return Evidence(signal="official_website", detail=f"website {intel.website}", source="website")


_chain_keyword = _keyword_signal(
    "chain_keyword", ("solana", "ethereum", "base chain", "robinhood chain", "onchain", "on-chain"), "chain reference"
)
_crypto_keyword = _keyword_signal(
    "crypto_keyword",
    ("token", "airdrop", "presale", "whitelist", "liquidity", "mint", "defi", "web3", "memecoin", "hodl", "degen"),
    "crypto lexicon",
)


# --- registry ----------------------------------------------------------------
# Signal name -> detector. The name MUST match a key in kol_crypto_signal_weights
# for the signal to carry weight; unregistered weights simply never fire, and
# registered detectors with no configured weight contribute 0. This is the single
# place to extend detection: add a (name, detector) pair + a config weight.
_DETECTORS: dict[str, Detector] = {
    "contract_address": _detect_contract_address,
    "ca_prefix": _detect_ca_prefix,
    "ticker_cashtag": _detect_ticker_cashtag,
    "dexscreener": _dexscreener,
    "birdeye": _birdeye,
    "gmgn": _gmgn,
    "geckoterminal": _geckoterminal,
    "coingecko": _coingecko,
    "coinmarketcap": _coinmarketcap,
    "pumpfun": _pumpfun,
    "official_website": _detect_official_website,
    "telegram": _telegram,
    "discord": _discord,
    "github": _github,
    "chain_keyword": _chain_keyword,
    "crypto_keyword": _crypto_keyword,
}


def detect_signals(
    intel: ProfileIntelligence,
    contracts: list[ExtractedContract],
) -> list[Evidence]:
    """Run every registered detector whose signal has a positive configured weight,
    returning the fired evidence with each weight filled in from config.

    A detector that raises is skipped (never aborts the whole classification) — a
    single bad pattern must not sink the pipeline. Weights come from
    `settings.kol_crypto_signal_weights`; a signal absent there or weighted <= 0 is
    treated as disabled and its detector isn't even run."""
    weights = settings.kol_crypto_signal_weights or {}
    out: list[Evidence] = []
    for name, detector in _DETECTORS.items():
        weight = int(weights.get(name, 0))
        if weight <= 0:
            continue
        try:
            ev = detector(intel, contracts)
        except Exception:  # noqa: BLE001 — one bad detector must not sink the batch
            continue
        if ev is not None:
            ev.weight = weight
            out.append(ev)
    return out


def registered_signals() -> list[str]:
    """Signal names with a registered detector — for docs/introspection/tests."""
    return list(_DETECTORS)
