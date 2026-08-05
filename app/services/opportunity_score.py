"""Opportunity Score (Alpha) — signal-based scoring engine.

Sits AFTER the analysis pipeline. Consumes TokenAnalysisResponse outputs,
produces a weighted composite score with per-signal explanations. Each scorer
is a standalone function; future milestones (KOL, Wallet Reputation, Momentum,
etc.) just register a new scorer — no changes to the engine.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from app.core.config import settings
from app.models.token import OpportunityResult, OpportunitySignal, TokenAnalysisResponse


@dataclass(slots=True)
class SignalResult:
    name: str
    value: int  # 0-100
    positive: bool
    detail: str


Scorer = Callable[[TokenAnalysisResponse], SignalResult | None]


# --- Individual signal scorers ---


def _score_risk(result: TokenAnalysisResponse) -> SignalResult:
    v = max(0, 100 - result.analysis.risk_score)
    positive = v >= 60
    detail = f"Rug risk {result.analysis.risk_score}/100"
    if positive:
        detail = f"Low rug risk ({result.analysis.risk_score}/100)"
    else:
        detail = f"Elevated rug risk ({result.analysis.risk_score}/100)"
    return SignalResult(name="risk", value=v, positive=positive, detail=detail)


def _score_freshness(result: TokenAnalysisResponse) -> SignalResult | None:
    if not result.token_age or result.token_age.age_hours is None:
        return None
    max_hours = settings.scan_max_launch_age_days * 24
    if max_hours <= 0:
        return None
    age_h = result.token_age.age_hours
    v = int(max(0, min(100, 100 * (1 - age_h / max_hours))))
    positive = v >= 50
    if age_h < 1:
        detail = "Fresh launch (<1h old)"
    elif age_h < 24:
        detail = f"Fresh launch ({age_h:.0f}h old)"
    else:
        detail = f"Launched {age_h / 24:.1f}d ago"
    return SignalResult(name="freshness", value=v, positive=positive, detail=detail)


def _score_liquidity(result: TokenAnalysisResponse) -> SignalResult | None:
    if not result.market_data or not result.market_data.liquidity:
        return None
    liq = result.market_data.liquidity.usd
    if liq is None:
        return None
    v = min(100, int(math.log10(max(liq, 1)) / math.log10(100_000) * 100))
    v = max(0, v)
    positive = v >= 50
    if liq >= 1000:
        detail = f"Liquidity ${liq / 1000:.0f}k"
    else:
        detail = f"Liquidity ${liq:.0f}"
    if not positive:
        detail = f"Low liquidity (${liq:.0f})"
    return SignalResult(name="liquidity", value=v, positive=positive, detail=detail)


def _score_smart_wallets(result: TokenAnalysisResponse) -> SignalResult | None:
    count = sum(1 for h in result.watchlist_hits if h.kind == "smart")
    v = min(100, count * 33)
    positive = count > 0
    if count == 0:
        detail = "No smart wallet activity"
    elif count == 1:
        detail = "1 smart wallet buying"
    else:
        detail = f"{count} smart wallets buying"
    return SignalResult(name="smart_wallets", value=v, positive=positive, detail=detail)


def _score_holder_quality(result: TokenAnalysisResponse) -> SignalResult | None:
    if not result.holders or result.holders.top10_percentage is None:
        return None
    pct = result.holders.top10_percentage
    v = max(0, 100 - int(pct))
    positive = v >= 50
    if positive:
        detail = f"Healthy distribution (top10: {pct:.0f}%)"
    else:
        detail = f"Concentrated holders (top10: {pct:.0f}%)"
    return SignalResult(name="holder_quality", value=v, positive=positive, detail=detail)


def _score_honeypot(result: TokenAnalysisResponse) -> SignalResult | None:
    if not result.honeypot:
        return None
    sellable = result.honeypot.status == "sellable"
    v = 100 if sellable else 0
    if sellable:
        detail = "Sellable (not a honeypot)"
    else:
        detail = f"Honeypot risk ({result.honeypot.status})"
    return SignalResult(name="honeypot", value=v, positive=sellable, detail=detail)


def _score_verified(result: TokenAnalysisResponse) -> SignalResult | None:
    if not result.contract_intel:
        return None
    verified = result.contract_intel.verified
    v = 100 if verified else 0
    detail = "Contract verified" if verified else "Contract not verified"
    return SignalResult(name="verified", value=v, positive=verified, detail=detail)


def _score_dev_reputation(result: TokenAnalysisResponse) -> SignalResult | None:
    rep = result.developer_reputation
    if rep is None:
        return None
    v = max(0, min(100, rep.score))
    positive = v >= 50
    if positive:
        detail = f"Developer reputation {v}/100"
    else:
        detail = f"Low developer reputation ({v}/100)"
    return SignalResult(name="dev_reputation", value=v, positive=positive, detail=detail)


def _score_wallet_reputation(result: TokenAnalysisResponse) -> SignalResult | None:
    reps = result.wallet_reputations
    if not reps:
        return None
    avg = sum(r.score for r in reps) / len(reps)
    v = max(0, min(100, int(avg)))
    positive = v >= 50
    if positive:
        detail = f"Avg wallet reputation {v}/100 ({len(reps)} wallets)"
    else:
        detail = f"Low wallet reputation ({v}/100, {len(reps)} wallets)"
    return SignalResult(name="wallet_reputation", value=v, positive=positive, detail=detail)


def _score_developer_network(result: TokenAnalysisResponse) -> SignalResult | None:
    net = result.developer_network
    if net is None:
        return None
    v = max(0, min(100, net.score))
    positive = v >= 50
    size = net.cluster_size
    if positive:
        detail = f"Network score {v}/100 ({size} token ecosystem)"
    else:
        detail = f"Weak network ({v}/100, {size} token{'s' if size != 1 else ''})"
    return SignalResult(name="developer_network", value=v, positive=positive, detail=detail)


# Module-level scorer registry. Append here to add future signals.
SCORERS: list[Scorer] = [
    _score_risk,
    _score_freshness,
    _score_liquidity,
    _score_smart_wallets,
    _score_holder_quality,
    _score_honeypot,
    _score_verified,
    _score_dev_reputation,
    _score_wallet_reputation,
    _score_developer_network,
]


def _alpha_level(score: int) -> str:
    if score >= 75:
        return "excellent"
    if score >= 50:
        return "high"
    if score >= 25:
        return "medium"
    return "low"


def score_opportunity(result: TokenAnalysisResponse) -> OpportunityResult:
    """Compute the Opportunity (Alpha) Score from analysis outputs."""
    weights = settings.opportunity_score_weights
    results: list[SignalResult] = []
    for scorer in SCORERS:
        sr = scorer(result)
        if sr is not None:
            results.append(sr)

    if not results:
        return OpportunityResult(alpha_score=None, alpha_level=None, signals=[])

    total_weight = sum(weights.get(r.name, 0) for r in results)
    if total_weight <= 0:
        return OpportunityResult(alpha_score=None, alpha_level=None, signals=[])

    weighted_sum = sum(r.value * weights.get(r.name, 0) for r in results)
    alpha = int(weighted_sum / total_weight)
    alpha = max(0, min(100, alpha))

    signals = [
        OpportunitySignal(name=r.name, positive=r.positive, detail=r.detail)
        for r in results
    ]

    return OpportunityResult(
        alpha_score=alpha,
        alpha_level=_alpha_level(alpha),
        signals=signals,
    )


# ---------------------------------------------------------------------------
# Lite scoring — zero-RPC prioritization from discovery metadata only (D3)
# ---------------------------------------------------------------------------

# ponytail: inline weights, not a config setting — YAGNI until someone needs to tune lite
# scoring independently. Upgrade path: move to settings.lite_score_weights.
_LITE_WEIGHTS = {
    "has_pair": 15,
    "liquidity": 25,
    "holder_count": 15,
    "freshness": 15,
    "market_cap": 15,
    "source_diversity": 15,
}


def score_opportunity_lite(candidate) -> int:
    """Cheap 0-100 prioritization from discovery-time data only. No RPC calls.

    Uses fields already on DiscoveredCandidate (holder_count, pair dict,
    source_count). Reuses the same log-scale formulas as the full scorers.
    """
    pair = candidate.pair or {}
    score = 0
    total_w = 0

    def _add(value, weight):
        nonlocal score, total_w
        if value is not None:
            score += value * weight
            total_w += weight

    # Has DexScreener pair
    _add(100 if candidate.pair else 0, _LITE_WEIGHTS["has_pair"])

    # Liquidity from pair (same formula as _score_liquidity)
    liq = (pair.get("liquidity") or {}).get("usd")
    if liq is not None and liq > 0:
        _add(min(100, int(math.log10(max(liq, 1)) / math.log10(100_000) * 100)), _LITE_WEIGHTS["liquidity"])
    else:
        _add(0, _LITE_WEIGHTS["liquidity"])

    # Holder count
    hc = candidate.holder_count
    if hc is not None and hc > 0:
        _add(min(100, int(math.log10(max(hc, 1)) / math.log10(10_000) * 100)), _LITE_WEIGHTS["holder_count"])

    # Freshness from pair creation (same formula as _score_freshness)
    created = pair.get("pairCreatedAt")
    if created:
        import time as _time
        age_ms = max(0, int(_time.time() * 1000) - int(created))
        max_age_ms = settings.scan_max_launch_age_days * 86_400_000
        if max_age_ms > 0:
            _add(max(0, int(100 * (1 - age_ms / max_age_ms))), _LITE_WEIGHTS["freshness"])
        else:
            _add(50, _LITE_WEIGHTS["freshness"])

    # Market cap from pair
    mc = pair.get("marketCap") or pair.get("fdv")
    if mc is not None and mc > 0:
        _add(min(100, int(math.log10(max(mc, 1)) / math.log10(1_000_000) * 100)), _LITE_WEIGHTS["market_cap"])

    # Source diversity (multiple providers found this address)
    sc = getattr(candidate, "source_count", 1)
    _add(min(100, sc * 33), _LITE_WEIGHTS["source_diversity"])

    return int(score / total_w) if total_w > 0 else 0
