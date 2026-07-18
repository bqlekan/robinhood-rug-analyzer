from __future__ import annotations

"""Profile intelligence analyzer + crypto classifier (M23 Deliverable D).

Pure, provider-neutral, offline. Turns a neutral `SocialAccount` into:
  1. a structured `ProfileIntelligence` (display name / username / bio / website /
     links / metadata / verified, plus a lowercased text blob for scanning), and
  2. a fully-explained `CryptoClassification`: account type, confidence band,
     weighted score, the signals that fired, their evidence, and extracted contracts.

Reused by every provider (X today; others later) because it only speaks the neutral
models. It does NOT persist, alert, score KOLs, cluster, or call the rug analyzer —
those are the orchestrator's job (`kol_crypto_pipeline`) or later deliverables.

Classification policy (all thresholds in config, nothing hardcoded):
  - Score = sum of fired signal weights, capped at 100.
  - Confidence band = highest `kol_crypto_confidence_bands` threshold the score meets.
  - "Never classify on a single weak signal": a crypto-project verdict requires at
    least `kol_crypto_min_signals` corroborating signals UNLESS one fired signal is
    in `kol_crypto_strong_signals` (a valid contract address by default), which can
    stand alone. Too few/weak signals -> `individual` (someone crypto-adjacent) or
    `unknown`, never a confident project claim.
  - Account TYPE (official/team/community/infrastructure) is chosen from which
    signals fired + light lexical cues, so the verdict is specific, not just
    "crypto: yes/no".
"""

import re

from app.core.config import settings
from app.models.kol import (
    CryptoClassification,
    Evidence,
    ExtractedContract,
    ProfileIntelligence,
    SocialAccount,
)
from app.services.social import contract_extract, crypto_signals

# Lexical cues for the account-TYPE decision. Deliberately light: the heavy lifting
# is the weighted signals; these only disambiguate *which kind* of crypto account it
# is once crypto-relevance is established. All matched case-insensitively, word-bounded.
_TEAM_CUES = ("founder", "co-founder", "cofounder", "ceo", "cto", "core team", "core dev",
              "developer", "dev @", "building", "builder", "creator of")
_COMMUNITY_CUES = ("community", "fan", "announcements", "news", "updates", "unofficial",
                   "not affiliated", "fam", "army", "holders")
_OFFICIAL_CUES = ("official", "verified", "$", "the official")
_INFRA_CUES = ("exchange", "aggregator", "wallet", "explorer", "analytics", "dashboard",
               "terminal", "bot", "api", "infrastructure", "launchpad", "dex", "protocol")


def _word_hit(text: str, cues: tuple[str, ...]) -> str | None:
    """First cue that appears as a whole word/phrase. Fully word-bounded so a short
    cue like "dex" can't match inside "dexscreener.com" pulled from a link."""
    for cue in cues:
        # "$" is not a word char, so \b won't anchor it; match it literally instead.
        pat = re.escape(cue) if cue == "$" else rf"\b{re.escape(cue)}\b"
        if re.search(pat, text, re.IGNORECASE):
            return cue
    return None


def build_profile_intelligence(account: SocialAccount) -> ProfileIntelligence:
    """Flatten a raw `SocialAccount` into the analyzer's structured input view.

    Never raises on sparse data: missing bio/links/website are simply empty. The
    first http(s) link is treated as the primary `website` when the account doesn't
    otherwise distinguish one; all links are preserved for signal scanning."""
    links = [l for l in (account.links or []) if l]
    website = None
    for l in links:
        if l.lower().startswith(("http://", "https://")):
            website = l
            break

    parts = [
        account.display_name or "",
        account.handle or "",
        account.bio or "",
        website or "",
        " ".join(links),
    ]
    text_blob = " ".join(p for p in parts if p).lower()

    return ProfileIntelligence(
        platform=account.platform,
        handle=account.handle,
        display_name=account.display_name,
        bio=account.bio,
        website=website,
        links=links,
        verified=account.verified,
        followers_count=account.followers_count,
        following_count=account.following_count,
        text_blob=text_blob,
    )


def _confidence_for_score(score: int) -> str:
    """Map a 0..100 score onto a confidence band using the configured thresholds,
    evaluated high->low so the strongest band a score qualifies for wins."""
    bands = settings.kol_crypto_confidence_bands or {}
    for level in ("very_high", "high", "medium", "low", "very_low"):
        if level in bands and score >= int(bands[level]):
            return level
    return "very_low"


def _extract_contracts(intel: ProfileIntelligence) -> list[ExtractedContract]:
    """Mine contracts across the profile's text fields, tagged by source."""
    return contract_extract.extract_from_fields({
        "bio": intel.bio,
        "website": intel.website,
        "links": " ".join(intel.links) if intel.links else None,
        "display_name": intel.display_name,
    })


def _choose_classification(
    intel: ProfileIntelligence,
    fired: list[Evidence],
    contracts: list[ExtractedContract],
) -> str:
    """Pick the account TYPE from the fired signals + light lexical cues.

    Only reached once crypto-relevance passed the corroboration gate, so the choice
    is among the crypto-ish types (+ infrastructure/individual). Order matters:
    infrastructure (tooling) is checked before project types because an aggregator
    mentioning many tokens shouldn't read as an 'official' project."""
    blob = intel.text_blob
    signal_names = {e.signal for e in fired}

    # Infrastructure/tooling: explicit tooling cues, or it's one of the data sites.
    infra_signals = {"dexscreener", "birdeye", "gmgn", "geckoterminal", "coingecko", "coinmarketcap"}
    if _word_hit(blob, _INFRA_CUES) and not contracts:
        return "infrastructure"
    if signal_names & infra_signals and _word_hit(blob, _INFRA_CUES):
        return "infrastructure"

    # Team: a person building/founding a project (dev cues + some crypto signal).
    if _word_hit(blob, _TEAM_CUES):
        return "team"

    # Community: explicitly community/fan/unofficial.
    if _word_hit(blob, _COMMUNITY_CUES):
        return "community"

    # Official: a contract address or official cues alongside project signals — the
    # account presents AS the token/project (name + CA + official links).
    if contracts or _word_hit(blob, _OFFICIAL_CUES):
        return "official"

    # Crypto-relevant but type-ambiguous: default to community (a crypto account that
    # isn't clearly the official project nor a named individual/tool).
    return "community"


def classify_account(account: SocialAccount) -> CryptoClassification:
    """Classify one account with full supporting evidence. Never raises on bad/sparse
    data — a profile with no bio/links yields an `unknown`/`very_low` verdict, not an
    error. This is the single entry point the orchestrator calls per new follow.

    Steps: build intelligence -> extract contracts -> run configured signal detectors
    -> sum weights (cap 100) -> derive confidence band -> apply the corroboration gate
    -> choose an account type. All thresholds/weights come from config."""
    intel = build_profile_intelligence(account)
    contracts = _extract_contracts(intel)
    fired = crypto_signals.detect_signals(intel, contracts)

    score = min(100, sum(e.weight for e in fired))
    confidence = _confidence_for_score(score)
    signal_names = [e.signal for e in fired]

    # Corroboration gate: never classify as a crypto PROJECT on a single weak signal.
    strong = set(settings.kol_crypto_strong_signals or [])
    has_strong = any(s in strong for s in signal_names)
    min_signals = int(settings.kol_crypto_min_signals)
    min_score = int(settings.kol_crypto_min_score)

    enough_corroboration = has_strong or len(fired) >= min_signals
    strong_enough = score >= min_score

    if fired and enough_corroboration and strong_enough:
        classification = _choose_classification(intel, fired, contracts)
    elif fired:
        # Crypto-adjacent but under the bar: a person/account with some crypto hints,
        # not a confident project. Downgrade rather than over-claim.
        classification = "individual"
    else:
        classification = "unknown"

    return CryptoClassification(
        platform=account.platform,
        handle=account.handle,
        account_key=account.key(),
        classification=classification,
        confidence=confidence,
        score=score,
        signals=signal_names,
        evidence=fired,
        contracts=contracts,
    )
