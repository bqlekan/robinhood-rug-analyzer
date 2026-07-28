"""Qualification Engine — pre-ranking classifier.

Runs AFTER analysis, BEFORE opportunity scoring. Returns a structured
classification so every tradable token is ranked — only genuinely
non-investable tokens are excluded.
"""

from __future__ import annotations

import math

from app.core.config import settings
from app.models.token import QualificationResult, TokenAnalysisResponse


def evaluate(result: TokenAnalysisResponse) -> QualificationResult:
    """Classify an analyzed token for ranking."""
    reasons: list[str] = []
    warnings: list[str] = []
    evidence: list[str] = []

    market = result.market_data
    age = result.token_age
    holders = result.holders
    honeypot = result.honeypot
    lock = result.liquidity_lock

    # ── Hard exclusions (genuinely non-investable) ────────────────────

    if honeypot and honeypot.status == "honeypot":
        reasons.append("Token detected as honeypot (unsellable)")

    liq_usd = market.liquidity.usd if market and market.liquidity else None

    # ponytail: zero-liq moved to warning — scoring handles it, not a gate
    if liq_usd is not None and liq_usd == 0:
        warnings.append("Zero on-chain liquidity")

    # No market data only excludes if also no holders (truly dead contract)
    if not market and (not holders or not holders.holder_count):
        reasons.append("Dead contract (no market data, no holders)")

    risk = result.analysis.risk_score
    if risk >= settings.qualification_hard_exclude_risk_score:
        reasons.append(f"Risk score {risk}/100 indicates proven rug")

    if honeypot and honeypot.status == "high_tax" and (honeypot.sell_tax_percentage or 0) >= 90:
        reasons.append("Trading effectively disabled (sell tax >= 90%)")

    if reasons:
        return QualificationResult(
            qualification_level="excluded",
            confidence_score=_compute_confidence(result),
            confidence_factors=_confidence_breakdown(result),
            rejection_reasons=reasons,
            warnings=warnings,
            evidence=evidence,
        )

    # ── Positive evidence (pros) ─────────────────────────────────────

    intel = result.contract_intel
    if intel and intel.verified:
        evidence.append("Contract source verified")

    if liq_usd is not None and liq_usd > 0:
        evidence.append(f"Liquidity ${liq_usd:,.0f}")
    if market and market.volume and market.volume.h24 and market.volume.h24 > 0:
        evidence.append("Active trading")
    if market and market.pair_address:
        evidence.append("Listed on DexScreener")
    if risk <= 50:
        evidence.append("Low rug risk")
    elif risk <= 80:
        evidence.append("Moderate rug risk")

    dev_rep = result.developer_reputation
    if dev_rep and dev_rep.score >= 50:
        evidence.append("Strong developer reputation")

    smart_count = sum(1 for h in result.watchlist_hits if h.kind == "smart")
    if smart_count > 0:
        evidence.append(f"Smart wallet accumulation ({smart_count})")

    if lock and lock.status in ("locked", "burned"):
        evidence.append(f"Liquidity {lock.status}")

    holder_count = holders.holder_count if holders else None
    if holder_count and holder_count > 0:
        evidence.append(f"Active holder base ({holder_count})")

    # ── Warnings (cons) ──────────────────────────────────────────────

    if dev_rep and dev_rep.score < 30:
        warnings.append(f"Low developer reputation ({dev_rep.score}/100)")
    elif not dev_rep:
        warnings.append("Developer reputation unknown")

    if lock and lock.status == "unlocked":
        warnings.append("Liquidity unlocked")

    if honeypot and honeypot.status == "high_tax":
        warnings.append(f"High sell tax ({honeypot.sell_tax_percentage}%)")

    if market and not market.market_cap and market.fdv:
        warnings.append("Market cap missing; FDV used as fallback")

    if not market:
        warnings.append("No market data available")
    else:
        if liq_usd is None:
            warnings.append("Liquidity unknown")
        if not (market.volume and market.volume.h24 and market.volume.h24 > 0):
            warnings.append("No trading volume detected")
        if not market.market_cap:
            warnings.append("Market cap unknown")
        if not market.pair_address:
            warnings.append("Pair not indexed on DexScreener")

    if not holder_count:
        warnings.append("Holder statistics unavailable")

    if smart_count == 0:
        warnings.append("No smart-wallet accumulation")

    # ── Classification ────────────────────────────────────────────────

    dev_score = dev_rep.score if dev_rep else None
    has_lock = lock and lock.status in ("locked", "burned")

    level = _classify(risk, liq_usd, holder_count, dev_score, has_lock)
    confidence = _compute_confidence(result)

    return QualificationResult(
        qualification_level=level,
        confidence_score=confidence,
        confidence_factors=_confidence_breakdown(result),
        rejection_reasons=[],
        warnings=warnings,
        evidence=evidence,
    )


def _classify(
    risk: int,
    liq_usd: float | None,
    holder_count: int | None,
    dev_score: int | None,
    has_lock: bool,
) -> str:
    if (
        risk <= settings.qualification_excellent_max_risk
        and liq_usd is not None and liq_usd >= settings.qualification_excellent_min_liquidity_usd
        and holder_count is not None and holder_count >= settings.qualification_excellent_min_holders
        and ((dev_score is not None and dev_score >= 50) or has_lock)
    ):
        return "excellent"

    if (
        risk <= settings.qualification_good_max_risk
        and liq_usd is not None and liq_usd >= settings.qualification_good_min_liquidity_usd
    ):
        return "good"

    # ponytail: "avoid" tier removed — unknown-liq tokens rank as speculative/high_risk
    if risk <= settings.qualification_speculative_max_risk:
        return "speculative"

    return "high_risk"


def _compute_confidence(result: TokenAnalysisResponse) -> int:
    """Weighted composite of 8 dimensions — None dimensions skipped from average."""
    weights = {
        "data_completeness": 20,
        "liquidity_quality": 15,
        "verification": 10,
        "developer_reputation": 15,
        "developer_network": 10,
        "smart_wallet_activity": 10,
        "holder_quality": 10,
        "age": 10,
    }
    scores = _confidence_scores(result)
    known = {k: v for k, v in scores.items() if v is not None}
    if not known:
        return 30  # Neutral when nothing is known
    total = sum(known[k] * weights[k] for k in known)
    divisor = sum(weights[k] for k in known)
    return max(0, min(100, round(total / divisor)))


def _confidence_scores(result: TokenAnalysisResponse) -> dict[str, int]:
    market = result.market_data
    holders = result.holders
    dev_rep = result.developer_reputation
    dev_net = result.developer_network
    age = result.token_age
    intel = result.contract_intel

    # data_completeness: fraction of key market fields that are non-None
    expected_fields = 0
    present_fields = 0
    if market:
        for val in [
            market.market_cap, market.fdv, market.price_usd,
            market.volume.h24 if market.volume else None,
            market.liquidity.usd if market.liquidity else None,
            market.price_change.h24 if market.price_change else None,
        ]:
            expected_fields += 1
            if val is not None:
                present_fields += 1
    else:
        expected_fields = 6
    data_completeness = round(100 * present_fields / expected_fields) if expected_fields else 0

    # liquidity_quality: log-scaled from $0 to $100k
    liq_raw = market.liquidity.usd if market and market.liquidity else None
    if liq_raw is not None and liq_raw > 0:
        liquidity_quality = min(100, round(math.log10(max(liq_raw, 1)) * 20))
    elif liq_raw is not None:
        liquidity_quality = 0  # Known zero
    else:
        liquidity_quality = None  # Unknown

    # verification
    verification = 100 if intel and intel.verified else 0

    # developer_reputation
    developer_reputation = dev_rep.score if dev_rep else None

    # developer_network
    developer_network = dev_net.score if dev_net else None

    # smart_wallet_activity
    smart_count = sum(1 for h in result.watchlist_hits if h.kind == "smart")
    smart_wallet_activity = min(100, smart_count * 25)

    # holder_quality: inverse of concentration
    if holders and holders.top10_percentage is not None:
        holder_quality = max(0, min(100, round(100 - holders.top10_percentage)))
    else:
        holder_quality = None  # Unknown

    # age: newer = less certainty; >7d = fairly established
    age_days = age.age_days if age else None
    if age_days is None:
        age_score = 30
    elif age_days >= 7:
        age_score = 90
    elif age_days >= 3:
        age_score = 70
    elif age_days >= 1:
        age_score = 50
    else:
        age_score = 30

    return {
        "data_completeness": data_completeness,
        "liquidity_quality": liquidity_quality,
        "verification": verification,
        "developer_reputation": developer_reputation,
        "developer_network": developer_network,
        "smart_wallet_activity": smart_wallet_activity,
        "holder_quality": holder_quality,
        "age": age_score,
    }


def _confidence_breakdown(result: TokenAnalysisResponse) -> list[str]:
    scores = _confidence_scores(result)
    labels = {
        "data_completeness": "Data completeness",
        "liquidity_quality": "Liquidity quality",
        "verification": "Contract verification",
        "developer_reputation": "Developer reputation",
        "developer_network": "Developer network",
        "smart_wallet_activity": "Smart wallet activity",
        "holder_quality": "Holder quality",
        "age": "Token age",
    }
    return [f"{labels[k]}: {scores[k] if scores[k] is not None else '?'}/100" for k in labels]
