from __future__ import annotations

"""Weighted, explainable rug-risk scoring.

Every risk dimension contributes zero or more `RiskSignal`s with point values.
The final score is the capped sum, so each contribution stays auditable in the UI.
"""

from app.core.config import settings
from app.models.token import (
    BundleAnalysis,
    BuyTimingAnalysis,
    ClusterAnalysis,
    ContractPrivileges,
    DevProfile,
    HolderDistribution,
    HoneypotResult,
    LaunchpadInfo,
    LiquidityLock,
    RiskSignal,
    RugAnalysis,
    TokenAge,
    TokenLore,
    TokenMarketData,
    WatchlistHit,
)

LIMITATIONS = [
    "This is a heuristic risk screen for Robinhood Chain tokens, not financial advice.",
    "Public APIs (Blockscout, DexScreener) can be delayed, incomplete, or rate-limited.",
    "Holder distribution and clusters are computed from a sampled top-holders page, not the full holder set.",
    "Dev launch history and LP-lock detection depend on public on-chain markers and known registries; absence of evidence is not proof of safety.",
]


def _sig(signals: list[RiskSignal], name: str, category: str, severity: str, points: int, description: str) -> None:
    signals.append(RiskSignal(name=name, category=category, severity=severity, points=points, description=description))


def _score_level(score: int) -> str:
    if score >= 75:
        return "critical"
    if score >= 50:
        return "high"
    if score >= 25:
        return "medium"
    return "low"


# Weighted contribution of each core input to overall confidence. A low score with
# missing inputs means "couldn't see," not "safe" — this lets the UI say which.
_CONFIDENCE_WEIGHTS = {
    "market": 30,
    "holders": 30,
    "age": 15,
    "dev": 15,
    "liquidity_lock": 10,
}


def _confidence_level(pct: int) -> str:
    if pct >= 75:
        return "high"
    if pct >= 40:
        return "medium"
    return "low"


def _confidence(present: dict[str, bool]) -> tuple[int, str]:
    """0-100 data-completeness score from which core inputs were available.

    Independent of risk: it reflects how much the analysis could actually see,
    so a low risk_score backed by thin data is distinguishable from a clean read.
    """
    total = sum(_CONFIDENCE_WEIGHTS.values())
    got = sum(w for key, w in _CONFIDENCE_WEIGHTS.items() if present.get(key))
    pct = round(got / total * 100) if total else 0
    return pct, _confidence_level(pct)


def score_token(
    *,
    age: TokenAge | None,
    market: TokenMarketData | None,
    holders: HolderDistribution | None,
    clusters: ClusterAnalysis | None,
    dev: DevProfile | None,
    liquidity_lock: LiquidityLock | None,
    launchpad: LaunchpadInfo | None,
    lore: TokenLore | None,
    data_sources: list[str],
    honeypot: HoneypotResult | None = None,
    privileges: ContractPrivileges | None = None,
    bundle: BundleAnalysis | None = None,
    buy_timing: BuyTimingAnalysis | None = None,
    watchlist_hits: list[WatchlistHit] | None = None,
) -> RugAnalysis:
    signals: list[RiskSignal] = []

    # --- Age ---
    if age and age.age_hours is not None:
        if age.age_hours < 24:
            _sig(signals, "Very new token", "age", "high", 20, "Token is less than 24 hours old; new launches carry elevated rug risk.")
        elif age.age_hours < 72:
            _sig(signals, "New token", "age", "medium", 10, "Token is less than 3 days old.")
    else:
        _sig(signals, "Unknown age", "age", "low", 5, "Could not determine token age from public data.")

    # --- Market / liquidity ---
    if not market:
        _sig(signals, "No market pair found", "market", "high", 30, "DexScreener returned no active Robinhood Chain liquidity pair.")
    else:
        liq = market.liquidity.usd if market.liquidity else None
        vol = market.volume.h24 if market.volume else None
        chg = market.price_change.h24 if market.price_change else None
        if liq is None:
            _sig(signals, "Missing liquidity data", "market", "medium", 15, "Liquidity data is unavailable.")
        elif liq < 5_000:
            _sig(signals, "Very low liquidity", "market", "high", 25, "USD liquidity is below $5,000, making exits risky.")
        elif liq < 25_000:
            _sig(signals, "Low liquidity", "market", "medium", 12, "USD liquidity is below $25,000.")
        if vol is None or vol < 1_000:
            _sig(signals, "Low trading activity", "market", "medium", 10, "24h volume is missing or below $1,000.")
        if chg is not None and chg <= -50:
            _sig(signals, "Severe 24h drawdown", "market", "high", 15, "Price is down more than 50% over 24h.")
        elif chg is not None and chg >= 300:
            _sig(signals, "Extreme 24h pump", "market", "medium", 10, "Price is up more than 300% over 24h; unstable hype.")

    # --- Holders / distribution ---
    if holders:
        if holders.holder_count is not None and holders.holder_count < 50:
            _sig(signals, "Few holders", "holders", "high", 18, f"Only {holders.holder_count} holders; easy for a few wallets to control price.")
        elif holders.holder_count is not None and holders.holder_count < 200:
            _sig(signals, "Low holder count", "holders", "medium", 8, f"{holders.holder_count} holders is still concentrated.")
        if holders.top1_percentage is not None and holders.top1_percentage >= 30:
            _sig(signals, "Whale holder", "holders", "high", 18, f"Top holder controls {holders.top1_percentage}% of supply.")
        if holders.top10_percentage is not None and holders.top10_percentage >= 70:
            _sig(signals, "Highly concentrated supply", "holders", "high", 20, f"Top 10 holders control {holders.top10_percentage}% of supply.")
        elif holders.top10_percentage is not None and holders.top10_percentage >= 50:
            _sig(signals, "Concentrated supply", "holders", "medium", 10, f"Top 10 holders control {holders.top10_percentage}% of supply.")

    # --- Clusters ---
    if clusters and clusters.clusters:
        pct = clusters.clustered_percentage or 0
        if pct >= 25:
            _sig(signals, "Coordinated holder clusters", "clusters", "high", 18, f"Wallets sharing a common funder hold ~{pct}% of supply; possible coordinated control.")
        elif pct >= 10:
            _sig(signals, "Holder clusters detected", "clusters", "medium", 10, f"Shared-funder wallets hold ~{pct}% of supply.")

    # --- Bundler / sybil launch (M14) ---
    # The bundle score is additive metadata; scoring reacts only to a positively
    # classified bundle (Heavy/Extreme), so a Normal/Moderate pattern adds nothing.
    # The cluster signals above already cover generic shared-funder concentration.
    if bundle and bundle.classification in ("Heavy", "Extreme"):
        severity = "high" if bundle.classification == "Extreme" else "medium"
        points = 18 if bundle.classification == "Extreme" else 10
        _sig(signals, "Bundled / sybil launch", "clusters", severity, points,
             bundle.detail or f"{bundle.classification} bundling detected: {bundle.bundled_wallets} wallets from one funder.")

    # --- Coordinated buy timing (M15) ---
    # Same-block / launch-window buy cohorts signal coordinated control independent of
    # funding source. Only a positively coordinated cohort scores; a single buyer or an
    # organically-spaced launch adds nothing.
    if buy_timing and buy_timing.coordinated:
        _sig(signals, "Coordinated buy timing", "clusters", "medium", 12,
             buy_timing.detail or "Multiple wallets bought in the same block / launch window; likely coordinated.")

    # --- Persistent wallet reputation (M17) ---
    # A watchlisted smart/insider wallet holding this token carries a cross-token history.
    # Only wallets with a non-trivial prior-token count score (min_prior_tokens floor) so a
    # first sighting is not penalised. Insiders with history are the stronger rug signal;
    # recurring smart wallets are informational, so they get a lighter weight.
    if watchlist_hits:
        min_prior = settings.wallet_reputation_min_prior_tokens
        recurring = [h for h in watchlist_hits if (h.prior_tokens or 0) >= min_prior]
        insider_rep = [h for h in recurring if h.kind == "insider"]
        smart_rep = [h for h in recurring if h.kind == "smart"]
        if insider_rep:
            top = max(h.prior_tokens for h in insider_rep)
            severity = "high" if (len(insider_rep) >= 2 or top >= 4) else "medium"
            points = 20 if severity == "high" else 12
            _sig(signals, "Repeat insider wallets present", "clusters", severity, points,
                 f"{len(insider_rep)} wallet(s) flagged as insiders on prior tokens hold this token "
                 f"(up to {top} prior tokens); recurring insider presence across launches.")
        if smart_rep:
            top = max(h.prior_tokens for h in smart_rep)
            _sig(signals, "Recurring smart wallets present", "clusters", "low", 4,
                 f"{len(smart_rep)} recurring smart wallet(s) hold this token (up to {top} prior tokens). "
                 "Informational: estimated from free on-chain behavior, not verified ROI.")

    # --- Dev ---
    if dev:
        if dev.dev_holding_percentage is not None:
            if dev.dev_holding_percentage >= 20:
                _sig(signals, "Large dev holdings", "dev", "high", 18, f"Deployer wallet holds {dev.dev_holding_percentage}% of supply.")
            elif dev.dev_holding_percentage >= 10:
                _sig(signals, "Notable dev holdings", "dev", "medium", 9, f"Deployer wallet holds {dev.dev_holding_percentage}% of supply.")
        if dev.reputation == "serial_rugger":
            _sig(signals, "Serial rugger deployer", "dev", "critical", 35, f"Deployer has {dev.tokens_rugged} prior likely-rugged launches.")
        elif dev.reputation == "mixed":
            _sig(signals, "Mixed deployer history", "dev", "medium", 12, f"Deployer has {dev.tokens_rugged} likely-rugged and {dev.tokens_alive} alive launches.")
        if dev.transferred_out:
            moved = dev.transferred_out_percentage
            if moved is not None and moved >= 10:
                _sig(signals, "Dev distributed large supply", "dev", "high", 16, f"Deployer moved ~{moved}% of supply out to {dev.transfers_out_count} wallet(s); possible pre-dump distribution.")
            else:
                _sig(signals, "Dev moved tokens out", "dev", "medium", 8, f"Deployer transferred tokens to {dev.transfers_out_count} other wallet(s).")

    # --- Liquidity lock ---
    if liquidity_lock:
        if liquidity_lock.status == "unlocked":
            _sig(signals, "Liquidity not locked", "liquidity", "high", 20, "No known locker or burn address holds the LP; liquidity can be pulled.")
        elif liquidity_lock.status == "unknown":
            _sig(signals, "LP lock status unknown", "liquidity", "medium", 8, "Could not confirm whether liquidity is locked or burned.")
        # M13: a near-term unlock is nearly as dangerous as no lock — presence alone
        # gave false confidence. Only fires when a real unlock schedule was read; a lock
        # with no schedule or a long horizon keeps the reassurance a lock normally gives.
        elif liquidity_lock.status == "locked" and liquidity_lock.unlock_in_days is not None:
            if liquidity_lock.unlock_in_days <= settings.lp_lock_near_term_days:
                _sig(signals, "LP lock expiring soon", "liquidity", "high", 18,
                     f"LP lock unlocks in ~{liquidity_lock.unlock_in_days} days; liquidity can be pulled once it expires.")

    # --- Launchpad ---
    if launchpad and launchpad.name == "Unknown":
        _sig(signals, "Unknown launchpad", "launchpad", "low", 5, "Token was not launched from a recognized launchpad; origin unclear.")

    # --- Honeypot / sell-tax (M10) ---
    # Only a positive detection scores. "unknown" (could not simulate) and "sellable"
    # add no points and are NOT folded into confidence, so a failed sim never inflates
    # risk nor drags the data-completeness score — it is strictly a bonus detector.
    if honeypot:
        if honeypot.status == "honeypot":
            _sig(signals, "Unsellable in simulation", "honeypot", "critical", 40,
                 honeypot.detail or "A simulated buy succeeded but the sell reverted; token appears unsellable (honeypot).")
        elif honeypot.status == "high_tax":
            tax = honeypot.sell_tax_percentage
            _sig(signals, "Extreme sell tax", "honeypot", "high", 20,
                 honeypot.detail or f"Simulated sell incurs a ~{tax}% tax, well above normal.")

    # --- Contract privileges / authority (M11) ---
    # Retained-power signals fire only when ownership is NOT confirmed-renounced: a
    # confirmed renounce (owner == zero) neutralizes onlyOwner powers and silences them.
    # Retained (False) OR unknown (None) ownership keeps them flagged — never a false clean.
    # Like honeypot, this is an additive bonus detector: not folded into confidence, and
    # analyzed=False (unverified/no ABI) scores nothing rather than a false "no powers".
    if privileges and privileges.analyzed:
        if privileges.is_paused:
            _sig(signals, "Trading currently paused", "privileges", "critical", 30,
                 "Contract's paused() reads true right now; transfers/trading are frozen.")
        if privileges.ownership_renounced is not True:
            retained = privileges.ownership_renounced is False  # False=owner known, None=unconfirmed
            note = "Owner retained" if retained else "Ownership unconfirmed"
            if privileges.can_mint:
                _sig(signals, "Mintable supply", "privileges", "high", 18,
                     f"{note}; contract exposes a mint function — supply can be inflated / diluted.")
            if privileges.can_blacklist:
                _sig(signals, "Blacklist/denylist power", "privileges", "high", 18,
                     f"{note}; contract can blacklist wallets — a common way to block sellers.")
            if privileges.can_pause:
                _sig(signals, "Pausable transfers", "privileges", "high", 15,
                     f"{note}; contract can pause transfers — trading can be frozen at will.")
            if privileges.can_set_fees:
                _sig(signals, "Mutable fees/tax", "privileges", "medium", 10,
                     f"{note}; contract can change fees/tax — sell tax can be raised after buys.")

    # --- Lore sentiment ---
    if lore and lore.sentiment == "negative":
        _sig(signals, "Negative social sentiment", "lore", "medium", 10, "Public discussion around this token skews negative (scam/rug mentions).")

    score = min(sum(s.points for s in signals), 100)
    confidence, confidence_level = _confidence(
        {
            "market": market is not None,
            # holder_count present means the holders dimension had real data.
            "holders": bool(holders and holders.holder_count is not None),
            "age": bool(age and age.age_hours is not None),
            "dev": bool(dev and dev.creator_address),
            "liquidity_lock": bool(liquidity_lock and liquidity_lock.status != "unknown"),
        }
    )
    return RugAnalysis(
        risk_score=score,
        risk_level=_score_level(score),
        confidence=confidence,
        confidence_level=confidence_level,
        signals=signals,
        data_sources=data_sources,
        limitations=LIMITATIONS,
    )


def score_token_light(holder_count: int | None) -> RugAnalysis:
    """Cheap first-pass score from `list_tokens` metadata only (no extra requests).

    Uses ONLY holder count — the single risk-relevant field the token list returns.
    Reuses the exact holder thresholds/points from `score_token` so a light score is
    directly comparable to the deep score's holder contribution. The promotion policy
    (promote on high light score OR unknown holder count) lives in the scanner, so this
    stays a pure scorer.
    """
    signals: list[RiskSignal] = []
    if holder_count is not None and holder_count < 50:
        _sig(signals, "Few holders", "holders", "high", 18, f"Only {holder_count} holders; easy for a few wallets to control price.")
    elif holder_count is not None and holder_count < 200:
        _sig(signals, "Low holder count", "holders", "medium", 8, f"{holder_count} holders is still concentrated.")

    score = min(sum(s.points for s in signals), 100)
    # A light pre-screen saw only holder count from the token list, so confidence
    # is intentionally low — a low light score is "not yet examined", not "safe".
    confidence, confidence_level = _confidence({"holders": holder_count is not None})
    return RugAnalysis(
        risk_score=score,
        risk_level=_score_level(score),
        confidence=confidence,
        confidence_level=confidence_level,
        signals=signals,
        data_sources=["Blockscout token list (light pre-screen)"],
        limitations=LIMITATIONS,
    )
