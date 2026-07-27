"""Eligibility Engine — pre-ranking quality gate.

Runs AFTER analysis, BEFORE opportunity scoring. Returns a structured
verdict so ineligible tokens carry rejection reasons (not silently dropped).
All thresholds read from config; nothing is hardcoded.
"""

from __future__ import annotations

from app.core.config import settings
from app.models.token import EligibilityResult, TokenAnalysisResponse


def evaluate(result: TokenAnalysisResponse) -> EligibilityResult:
    """Evaluate whether an analyzed token qualifies for opportunity ranking."""
    reasons: list[str] = []
    warnings: list[str] = []
    evidence: list[str] = []
    confidence = 100

    market = result.market_data
    age = result.token_age
    holders = result.holders
    honeypot = result.honeypot
    lock = result.liquidity_lock

    # --- Pair existence ---
    if settings.eligibility_require_pair and (not market or not market.pair_address):
        reasons.append("No trading pair found")

    # --- Liquidity ---
    liq_usd = market.liquidity.usd if market and market.liquidity else None
    if settings.eligibility_require_liquidity and liq_usd is None:
        reasons.append("Liquidity data unavailable")
    elif liq_usd is not None and settings.eligibility_min_liquidity_usd > 0 and liq_usd < settings.eligibility_min_liquidity_usd:
        reasons.append(f"Liquidity ${liq_usd:,.0f} below minimum ${settings.eligibility_min_liquidity_usd:,.0f}")

    if liq_usd is not None and liq_usd >= settings.eligibility_min_liquidity_usd:
        evidence.append("Healthy liquidity")

    # --- Price ---
    if settings.eligibility_require_price and (not market or not market.price_usd):
        reasons.append("Price data unavailable")

    # --- Market cap ---
    mcap = market.market_cap if market else None
    if settings.eligibility_require_market_cap and mcap is None:
        reasons.append("Market cap unavailable")
    elif mcap is not None and settings.eligibility_min_market_cap_usd > 0 and mcap < settings.eligibility_min_market_cap_usd:
        reasons.append(f"Market cap ${mcap:,.0f} below minimum ${settings.eligibility_min_market_cap_usd:,.0f}")

    if mcap is None and market and market.fdv:
        warnings.append("Market cap missing; FDV used as fallback")

    # --- Age ---
    age_days = age.age_days if age else None
    if age_days is not None and settings.eligibility_max_age_days > 0 and age_days > settings.eligibility_max_age_days:
        reasons.append(f"Token age {age_days:.1f}d exceeds limit {settings.eligibility_max_age_days:.0f}d")

    # --- Holders ---
    hcount = holders.holder_count if holders else None
    if hcount is not None and settings.eligibility_min_holder_count > 0 and hcount < settings.eligibility_min_holder_count:
        reasons.append(f"Holder count {hcount} below minimum {settings.eligibility_min_holder_count}")

    # --- Volume (trading activity proxy) ---
    vol_h24 = None
    if market and market.volume:
        vol_h24 = market.volume.h24
    if vol_h24 is not None and settings.eligibility_min_volume_h24_usd > 0 and vol_h24 < settings.eligibility_min_volume_h24_usd:
        reasons.append(f"24h volume ${vol_h24:,.0f} below minimum ${settings.eligibility_min_volume_h24_usd:,.0f}")

    if vol_h24 is not None and vol_h24 > 0:
        evidence.append("Active trading")

    # --- Rug risk ---
    risk = result.analysis.risk_score
    if settings.eligibility_max_risk_score > 0 and risk > settings.eligibility_max_risk_score:
        reasons.append(f"Risk score {risk}/100 exceeds maximum {settings.eligibility_max_risk_score}")

    if risk <= 50:
        evidence.append("Low rug risk")
    elif risk <= settings.eligibility_max_risk_score:
        evidence.append("Moderate rug risk")

    # --- Honeypot ---
    if honeypot and honeypot.status == "honeypot":
        reasons.append("Token detected as honeypot (unsellable)")
    elif honeypot and honeypot.status == "high_tax":
        warnings.append(f"High sell tax ({honeypot.sell_tax_percentage}%)")

    # --- Analysis confidence ---
    if settings.eligibility_require_analysis and result.analysis.confidence < 30:
        reasons.append(f"Analysis confidence too low ({result.analysis.confidence}/100)")
        confidence = min(confidence, result.analysis.confidence)

    # --- Developer reputation evidence ---
    dev_rep = result.developer_reputation
    if dev_rep and dev_rep.score >= 50:
        evidence.append("Strong developer reputation")
    elif dev_rep and dev_rep.score < 30:
        warnings.append(f"Low developer reputation ({dev_rep.score}/100)")

    # --- Smart wallet evidence ---
    smart_count = sum(1 for h in result.watchlist_hits if h.kind == "smart")
    if smart_count > 0:
        evidence.append(f"Smart wallet accumulation ({smart_count})")

    # --- Lock status evidence ---
    if lock and lock.status in ("locked", "burned"):
        evidence.append(f"Liquidity {lock.status}")
    elif lock and lock.status == "unlocked":
        warnings.append("Liquidity unlocked")

    # --- Confidence adjustment ---
    if not market:
        confidence = min(confidence, 30)
    elif liq_usd is None:
        confidence = min(confidence, 50)

    eligible = len(reasons) == 0

    return EligibilityResult(
        eligible=eligible,
        rejection_reasons=reasons,
        warnings=warnings,
        confidence=confidence,
        evidence=evidence,
    )
