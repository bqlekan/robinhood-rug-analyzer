"""Alpha Timeline Engine — converts raw analysis into a chronological story.

Sits AFTER the analysis pipeline. Consumes TokenAnalysisResponse outputs,
generates chronological events explaining how a token evolved. Each provider
is a standalone function; future timeline sources register a new provider —
no changes to the engine.
"""

from __future__ import annotations

from typing import Callable, Protocol

from app.models.token import (
    AlphaTimeline,
    TimelineEvent,
    TimelineSummary,
    TokenAnalysisResponse,
)

# ---------------------------------------------------------------------------
# Provider protocol — any callable returning a list of events from analysis
# ---------------------------------------------------------------------------


class TimelineProvider(Protocol):
    def __call__(self, result: TokenAnalysisResponse) -> list[TimelineEvent]: ...


# ---------------------------------------------------------------------------
# Individual providers — each mines events from one analysis dimension
# ---------------------------------------------------------------------------


def _launch_events(r: TokenAnalysisResponse) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    ts = None
    if r.token_age and r.token_age.created_at_iso:
        ts = r.token_age.created_at_iso
    if r.dev and r.dev.creation_tx:
        events.append(TimelineEvent(
            timestamp=ts,
            title="Contract deployed",
            category="Launch",
            severity="info",
            confidence="high" if ts else "low",
            source="blockscout",
            evidence=f"Creation tx: {r.dev.creation_tx[:18]}…" if r.dev.creation_tx else None,
            impact="Token contract now exists on chain.",
            explanation=f"Contract deployed by {r.dev.creator_address[:12]}…" if r.dev.creator_address else "Contract deployed.",
        ))
    if r.contract_intel and r.contract_intel.verified:
        events.append(TimelineEvent(
            timestamp=ts,
            title="Contract verified",
            category="Contract",
            severity="info",
            confidence="high",
            source="blockscout",
            evidence=f"Compiler: {r.contract_intel.compiler}, template: {r.contract_intel.template}",
            impact="Source code is publicly readable — transparency increased.",
            explanation=f"Contract source verified on Blockscout ({r.contract_intel.language or 'Solidity'}).",
        ))
    if r.launchpad and r.launchpad.name != "Unknown":
        events.append(TimelineEvent(
            timestamp=ts,
            title="Launchpad detected",
            category="Launch",
            severity="info",
            confidence=r.launchpad.confidence,
            source="launchpad_registry",
            evidence=r.launchpad.detail,
            impact="Token launched through a known platform.",
            explanation=f"Launched via {r.launchpad.name} (confidence: {r.launchpad.confidence}).",
        ))
    return events


def _liquidity_events(r: TokenAnalysisResponse) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    m = r.market_data
    ts = None
    if r.token_age and r.token_age.created_at_iso:
        ts = r.token_age.created_at_iso
    if m and m.liquidity and m.liquidity.usd is not None and m.liquidity.usd > 0:
        liq = m.liquidity.usd
        sev = "info" if liq >= 5000 else "medium" if liq >= 1000 else "high"
        events.append(TimelineEvent(
            timestamp=ts,
            title="Liquidity added",
            category="Liquidity",
            severity=sev,
            confidence="high",
            source="dexscreener",
            evidence=f"Pool liquidity: ${liq:,.0f}",
            impact="Token is now tradeable on DEX." if liq >= 1000 else "Low liquidity — high slippage risk.",
            explanation=f"Liquidity pool funded with ${liq:,.0f}.",
        ))
    if m and m.pair_created_at:
        events.append(TimelineEvent(
            timestamp=ts,
            title="Trading enabled",
            category="Liquidity",
            severity="info",
            confidence="high",
            source="dexscreener",
            evidence=f"DEX: {m.dex_id or 'unknown'}, pair: {m.pair_address[:12]}…" if m.pair_address else None,
            impact="Token is now available for public trading.",
            explanation="DEX pair created and trading is live.",
        ))
    ll = r.liquidity_lock
    if ll:
        if ll.status == "burned":
            events.append(TimelineEvent(
                timestamp=ts,
                title="LP tokens burned",
                category="Liquidity",
                severity="info",
                confidence="high",
                source="blockscout",
                evidence=f"LP burned, {ll.locked_percentage:.0f}% of LP supply" if ll.locked_percentage else "LP sent to burn address",
                impact="Liquidity cannot be removed — permanent commitment.",
                explanation="LP tokens were sent to a burn address, permanently locking liquidity.",
            ))
        elif ll.status == "locked":
            unlock_info = ""
            if ll.unlock_in_days is not None:
                if ll.unlock_in_days > 0:
                    unlock_info = f" Unlock in ~{ll.unlock_in_days:.0f} days."
                else:
                    unlock_info = " Lock has expired."
            events.append(TimelineEvent(
                timestamp=ts,
                title="Liquidity locked",
                category="Liquidity",
                severity="info" if (ll.unlock_in_days or 0) > 30 else "medium",
                confidence="high",
                source="blockscout",
                evidence=f"Locker: {ll.locker_label or 'unknown'}.{unlock_info}",
                impact="Liquidity secured in a locker contract.",
                explanation=f"LP tokens locked.{unlock_info}",
            ))
    # Liquidity trend
    if r.trend and r.trend.has_prior and r.trend.liquidity_change_pct is not None:
        pct = r.trend.liquidity_change_pct
        if abs(pct) >= 10:
            direction = "increased" if pct > 0 else "decreased"
            sev = "info" if pct > 0 else ("high" if pct <= -40 else "medium")
            events.append(TimelineEvent(
                timestamp=None,
                title=f"Liquidity {direction}",
                category="Liquidity",
                severity=sev,
                confidence="high",
                source="snapshot",
                evidence=f"Liquidity changed {pct:+.1f}% since prior scan.",
                impact="Improving liquidity depth." if pct > 0 else "Potential liquidity drain — rug risk elevated.",
                explanation=f"Pool liquidity {direction} by {abs(pct):.1f}% compared to previous analysis.",
            ))
    # Market cap milestones
    if m and m.market_cap and m.market_cap > 0:
        thresholds = [
            (1_000_000, "$1M"),
            (500_000, "$500K"),
            (100_000, "$100K"),
            (10_000, "$10K"),
        ]
        for thresh, label in thresholds:
            if m.market_cap >= thresh:
                events.append(TimelineEvent(
                    timestamp=None,
                    title=f"Market cap reached {label}",
                    category="Liquidity",
                    severity="info",
                    confidence="high",
                    source="dexscreener",
                    evidence=f"Current market cap: ${m.market_cap:,.0f}",
                    impact=f"Token crossed the {label} market cap threshold.",
                    explanation=f"Market capitalisation at ${m.market_cap:,.0f}.",
                ))
                break
    return events


def _ownership_events(r: TokenAnalysisResponse) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    p = r.contract_privileges
    if not p or not p.analyzed:
        return events
    if p.ownership_renounced is True:
        events.append(TimelineEvent(
            timestamp=None,
            title="Ownership renounced",
            category="Ownership",
            severity="info",
            confidence="high",
            source="rpc",
            evidence="owner() returns zero address.",
            impact="Developer can no longer exercise admin powers.",
            explanation="Contract ownership has been renounced — no admin controls remain.",
        ))
    elif p.ownership_renounced is False:
        powers = []
        if p.can_mint:
            powers.append("mint")
        if p.can_pause:
            powers.append("pause")
        if p.can_blacklist:
            powers.append("blacklist")
        if p.can_set_fees:
            powers.append("set fees")
        sev = "high" if len(powers) >= 2 else "medium" if powers else "low"
        events.append(TimelineEvent(
            timestamp=None,
            title="Owner retained with powers",
            category="Ownership",
            severity=sev,
            confidence="high",
            source="rpc",
            evidence=f"Owner: {p.owner_address[:12]}…, powers: {', '.join(powers) or 'none detected'}" if p.owner_address else f"Powers: {', '.join(powers) or 'none detected'}",
            impact="Developer retains administrative control over the contract.",
            explanation=f"Ownership not renounced. Retained powers: {', '.join(powers) or 'none dangerous detected'}.",
        ))
    if p.is_paused is True:
        events.append(TimelineEvent(
            timestamp=None,
            title="Contract is paused",
            category="Security",
            severity="critical",
            confidence="high",
            source="rpc",
            evidence="paused() returns true.",
            impact="Transfers may be blocked — token could be frozen.",
            explanation="The contract is currently in a paused state.",
        ))
    return events


def _developer_events(r: TokenAnalysisResponse) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    d = r.dev
    if not d:
        return events
    ts = None
    if r.token_age and r.token_age.created_at_iso:
        ts = r.token_age.created_at_iso
    if d.transferred_out and d.transfers_out_count:
        sev = "high" if (d.transferred_out_percentage or 0) >= 10 else "medium"
        events.append(TimelineEvent(
            timestamp=None,
            title="Developer transfer detected",
            category="Developer",
            severity=sev,
            confidence="high",
            source="blockscout",
            evidence=f"Transferred to {d.transfers_out_count} wallet(s), ~{d.transferred_out_percentage or 0:.1f}% of supply.",
            impact="Potential selling pressure increased." if sev == "high" else "Minor outflow from deployer.",
            explanation=f"Developer transferred ~{d.transferred_out_percentage or 0:.1f}% of supply to {d.transfers_out_count} address(es).",
        ))
    if d.dev_holding_percentage is not None and d.dev_holding_percentage > 5:
        events.append(TimelineEvent(
            timestamp=ts,
            title="Developer holds significant supply",
            category="Developer",
            severity="medium" if d.dev_holding_percentage >= 10 else "low",
            confidence="high",
            source="blockscout",
            evidence=f"Deployer holds {d.dev_holding_percentage:.1f}% of total supply.",
            impact="Concentrated dev holdings create sell pressure risk.",
            explanation=f"The deployer wallet currently holds {d.dev_holding_percentage:.1f}% of total supply.",
        ))
    if d.tokens_rugged and d.tokens_rugged > 0:
        events.append(TimelineEvent(
            timestamp=ts,
            title="Developer linked to prior rugs",
            category="Developer",
            severity="critical",
            confidence="high",
            source="blockscout",
            evidence=f"This deployer has {d.tokens_rugged} previously rugged token(s) out of {d.tokens_launched} launched.",
            impact="Serial rugger — extremely elevated risk.",
            explanation=f"Deployer previously launched {d.tokens_launched} tokens, {d.tokens_rugged} were rugged.",
        ))
    if d.reputation and d.reputation not in ("unknown",):
        rep_map = {"serial_rugger": "critical", "suspicious": "high", "mixed": "medium", "new": "info", "established": "info", "reliable": "info"}
        sev = rep_map.get(d.reputation, "info")
        events.append(TimelineEvent(
            timestamp=ts,
            title=f"Developer reputation: {d.reputation.replace('_', ' ')}",
            category="Developer",
            severity=sev,
            confidence="high",
            source="developer_reputation",
            evidence=f"Launched {d.tokens_launched or 0} tokens, {d.tokens_alive or 0} alive, {d.tokens_rugged or 0} rugged.",
            impact=f"Developer track record rated: {d.reputation.replace('_', ' ')}.",
            explanation=f"Based on deployer history across {d.tokens_launched or 0} token launches.",
        ))
    return events


def _holder_events(r: TokenAnalysisResponse) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    h = r.holders
    if not h:
        return events
    if h.holder_count is not None:
        thresholds = [
            (10000, "10,000"),
            (5000, "5,000"),
            (1000, "1,000"),
            (500, "500"),
            (100, "100"),
        ]
        for thresh, label in thresholds:
            if h.holder_count >= thresh:
                events.append(TimelineEvent(
                    timestamp=None,
                    title=f"Holder count reached {label}",
                    category="Holder Growth",
                    severity="info",
                    confidence="high",
                    source="blockscout",
                    evidence=f"Current holders: {h.holder_count:,}",
                    impact="Growing community indicates genuine interest.",
                    explanation=f"Token now has {h.holder_count:,} holders.",
                ))
                break
    if h.top10_percentage is not None and h.top10_percentage > 50:
        events.append(TimelineEvent(
            timestamp=None,
            title="High holder concentration",
            category="Holder Growth",
            severity="high",
            confidence="high",
            source="blockscout",
            evidence=f"Top 10 wallets hold {h.top10_percentage:.1f}% of supply.",
            impact="Whale-dominated distribution — vulnerable to coordinated selling.",
            explanation=f"Top 10 holders control {h.top10_percentage:.1f}% of total supply (excluding LP).",
        ))
    # Trend: holder count change
    if r.trend and r.trend.has_prior and r.trend.holder_count_change is not None:
        change = r.trend.holder_count_change
        if abs(change) >= 10:
            direction = "grew" if change > 0 else "shrank"
            events.append(TimelineEvent(
                timestamp=None,
                title=f"Holder count {direction}",
                category="Holder Growth",
                severity="info" if change > 0 else "medium",
                confidence="high",
                source="snapshot",
                evidence=f"Holder count changed by {change:+,} since prior scan.",
                impact="Community is growing." if change > 0 else "Holders are leaving.",
                explanation=f"Holder count {direction} by {abs(change):,} compared to previous analysis.",
            ))
    # Trend: concentration change
    if r.trend and r.trend.has_prior and r.trend.concentration_change_pct is not None:
        cpct = r.trend.concentration_change_pct
        if abs(cpct) >= 5:
            direction = "increased" if cpct > 0 else "decreased"
            events.append(TimelineEvent(
                timestamp=None,
                title=f"Top-10 concentration {direction}",
                category="Holder Growth",
                severity="medium" if cpct > 0 else "info",
                confidence="high",
                source="snapshot",
                evidence=f"Top-10 concentration changed {cpct:+.1f} percentage points.",
                impact="Whales accumulating." if cpct > 0 else "Distribution improving.",
                explanation=f"Top-10 holder concentration {direction} by {abs(cpct):.1f} percentage points.",
            ))
    return events


def _smart_wallet_events(r: TokenAnalysisResponse) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    smart_hits = [h for h in r.watchlist_hits if h.kind == "smart"]
    if not smart_hits:
        return events
    for i, hit in enumerate(smart_hits[:5], 1):
        ordinal = {1: "First", 2: "Second", 3: "Third"}.get(i, f"#{i}")
        prior = f" Seen on {hit.prior_tokens} prior tokens." if hit.prior_tokens else ""
        events.append(TimelineEvent(
            timestamp=None,
            title=f"{ordinal} Smart Wallet buy",
            category="Smart Wallet",
            severity="info",
            confidence="medium",
            source="wallet_intel",
            evidence=f"Wallet {hit.address[:12]}… (proxy score {hit.proxy_score or 'N/A'}) holds {hit.holding_percentage or 0:.1f}%.{prior}",
            impact="Smart money entering — positive alpha signal.",
            explanation=f"A heuristically-identified smart wallet entered this token.",
        ))
    if len(smart_hits) >= 3:
        events.append(TimelineEvent(
            timestamp=None,
            title="Multiple Smart Wallets accumulating",
            category="Smart Wallet",
            severity="info",
            confidence="medium",
            source="wallet_intel",
            evidence=f"{len(smart_hits)} smart wallets detected in holders.",
            impact="Strong smart money convergence — elevated alpha signal.",
            explanation=f"{len(smart_hits)} distinct smart wallets are holding this token.",
        ))
    return events


def _insider_events(r: TokenAnalysisResponse) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    insider_hits = [h for h in r.watchlist_hits if h.kind == "insider"]
    if insider_hits:
        events.append(TimelineEvent(
            timestamp=None,
            title="Known insider wallets present",
            category="Insider",
            severity="high",
            confidence="high",
            source="watchlist",
            evidence=f"{len(insider_hits)} previously-flagged insider wallet(s) hold this token.",
            impact="Wallets with a history of insider behaviour are active.",
            explanation=f"{len(insider_hits)} insider wallet(s) from previous analyses detected among holders.",
        ))
    for ins in r.insiders[:3]:
        reason = ins.reason.replace("_", " ")
        events.append(TimelineEvent(
            timestamp=None,
            title=f"Insider detected: {reason}",
            category="Insider",
            severity="medium",
            confidence="medium",
            source="wallet_intel",
            evidence=f"Wallet {ins.address[:12]}… — {reason}, holds {ins.holding_percentage or 0:.1f}%.",
            impact="Insider wallet activity detected.",
            explanation=f"{ins.note}" if ins.note else f"Wallet flagged as {reason}.",
        ))
    return events


def _cluster_events(r: TokenAnalysisResponse) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    c = r.clusters
    if c and c.clusters:
        for cl in c.clusters[:3]:
            events.append(TimelineEvent(
                timestamp=None,
                title=f"Wallet cluster detected ({cl.link_type or 'unknown'} link)",
                category="Insider",
                severity="medium" if cl.combined_percentage and cl.combined_percentage >= 5 else "low",
                confidence="high",
                source="analyzers",
                evidence=f"{len(cl.member_addresses)} wallets, {cl.combined_percentage or 0:.1f}% combined.",
                impact="Coordinated wallets holding a meaningful share of supply.",
                explanation=f"Cluster of {len(cl.member_addresses)} wallets linked by {cl.link_type or 'shared funder'}, holding {cl.combined_percentage or 0:.1f}%.",
            ))
    b = r.bundle
    if b and b.classification and b.classification != "Normal":
        events.append(TimelineEvent(
            timestamp=None,
            title=f"Bundle activity: {b.classification}",
            category="Insider",
            severity="high" if b.classification in ("Heavy", "Extreme") else "medium",
            confidence="high",
            source="analyzers",
            evidence=f"Bundle score {b.score}/100, {b.bundled_wallets or 0} wallets, {b.bundled_percentage or 0:.1f}% supply.",
            impact="Coordinated launch activity — potential sybil attack.",
            explanation=b.detail or f"Bundled wallets detected with {b.classification.lower()} severity.",
        ))
    bt = r.buy_timing
    if bt and bt.coordinated:
        events.append(TimelineEvent(
            timestamp=None,
            title="Coordinated buy timing detected",
            category="Insider",
            severity="medium",
            confidence="high",
            source="analyzers",
            evidence=f"{bt.same_block_wallets or 0} wallets bought in the same block.",
            impact="Multiple wallets bought simultaneously — possible coordination.",
            explanation=bt.detail or "Same-block or same-window purchases detected.",
        ))
    return events


def _honeypot_events(r: TokenAnalysisResponse) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    hp = r.honeypot
    if not hp:
        return events
    if hp.status == "honeypot":
        events.append(TimelineEvent(
            timestamp=None,
            title="Token is a honeypot",
            category="Security",
            severity="critical",
            confidence="high",
            source="honeypot_sim",
            evidence=hp.detail or "Sell simulation failed — token cannot be sold.",
            impact="Cannot sell — total loss if bought.",
            explanation="Simulated buy→sell round-trip failed. This token traps buyer funds.",
        ))
    elif hp.status == "high_tax":
        events.append(TimelineEvent(
            timestamp=None,
            title="High sell tax detected",
            category="Security",
            severity="high",
            confidence="high",
            source="honeypot_sim",
            evidence=f"Round-trip loss: ~{hp.sell_tax_percentage or 0:.1f}%.",
            impact="Extreme tax erodes most of the trade value.",
            explanation=f"Sell simulation shows ~{hp.sell_tax_percentage or 0:.1f}% round-trip loss.",
        ))
    elif hp.status == "sellable":
        tax_note = f" Round-trip loss ~{hp.sell_tax_percentage:.1f}%." if hp.sell_tax_percentage else ""
        events.append(TimelineEvent(
            timestamp=None,
            title="Token is sellable",
            category="Security",
            severity="info",
            confidence="high",
            source="honeypot_sim",
            evidence=f"Sell simulation succeeded.{tax_note}",
            impact="Token can be sold — not a honeypot.",
            explanation=f"Buy→sell round-trip simulation passed.{tax_note}",
        ))
    return events


def _network_events(r: TokenAnalysisResponse) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    net = r.developer_network
    if net and net.cluster_size > 1:
        events.append(TimelineEvent(
            timestamp=None,
            title=f"Developer network: {net.cluster_size} tokens in ecosystem",
            category="Network",
            severity="info" if net.score >= 50 else "medium",
            confidence=net.cluster_confidence,
            source="developer_network",
            evidence=f"Network score {net.score}/100. Success rate: {net.historical_success_rate * 100:.0f}%." if net.historical_success_rate is not None else f"Network score {net.score}/100.",
            impact="Token is part of a multi-project ecosystem." if net.score >= 50 else "Developer ecosystem shows concerning patterns.",
            explanation=f"Deployer has launched {net.cluster_size} tokens. Network trust: {net.network_trust:.0f}/100." if net.network_trust is not None else f"Deployer has launched {net.cluster_size} tokens.",
        ))
    rep = r.developer_reputation
    if rep and rep.score is not None:
        events.append(TimelineEvent(
            timestamp=None,
            title=f"Developer reputation score: {rep.score}/100",
            category="Developer",
            severity="info" if rep.score >= 50 else "high" if rep.score < 25 else "medium",
            confidence="high",
            source="developer_reputation",
            evidence="; ".join(rep.evidence[:3]) if rep.evidence else f"Score based on {rep.total_contracts_deployed} contracts deployed.",
            impact="Established developer with positive history." if rep.score >= 50 else "Developer track record is concerning.",
            explanation=f"Developer reputation assessed at {rep.score}/100.",
        ))
    return events


def _wallet_reputation_events(r: TokenAnalysisResponse) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    if not r.wallet_reputations:
        return events
    for wr in r.wallet_reputations[:3]:
        events.append(TimelineEvent(
            timestamp=None,
            title=f"Smart Wallet reputation: {wr.score}/100",
            category="Smart Wallet",
            severity="info",
            confidence=wr.confidence,
            source="smart_wallet_reputation",
            evidence=f"Wallet {wr.address[:12]}…: {wr.surviving_projects} surviving projects, {wr.rugs_entered} rugs entered.",
            impact="High-reputation smart wallet is active." if wr.score >= 50 else "Smart wallet has mixed history.",
            explanation=f"Wallet reputation {wr.score}/100 based on cross-token analysis.",
        ))
    return events


def _opportunity_events(r: TokenAnalysisResponse) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    a = r.analysis
    if a.risk_score <= 30:
        events.append(TimelineEvent(
            timestamp=None,
            title="Low risk score",
            category="Opportunity",
            severity="info",
            confidence=a.confidence_level,
            source="scoring",
            evidence=f"Risk score: {a.risk_score}/100, confidence: {a.confidence}%.",
            impact="Analysis indicates low rug-pull risk.",
            explanation=f"Combined risk assessment: {a.risk_score}/100 ({a.risk_level}).",
        ))
    elif a.risk_score >= 70:
        events.append(TimelineEvent(
            timestamp=None,
            title="High risk score",
            category="Risk",
            severity="critical" if a.risk_score >= 85 else "high",
            confidence=a.confidence_level,
            source="scoring",
            evidence=f"Risk score: {a.risk_score}/100, confidence: {a.confidence}%.",
            impact="Analysis indicates elevated rug-pull risk.",
            explanation=f"Combined risk assessment: {a.risk_score}/100 ({a.risk_level}). {len(a.signals)} warning signals detected.",
        ))
    return events


def _kol_events(r: TokenAnalysisResponse) -> list[TimelineEvent]:
    # ponytail: KOL data is not on TokenAnalysisResponse yet; stub for future provider
    return []


# ---------------------------------------------------------------------------
# Provider registry — append to extend, no engine change
# ---------------------------------------------------------------------------

PROVIDERS: list[TimelineProvider] = [
    _launch_events,
    _liquidity_events,
    _ownership_events,
    _developer_events,
    _holder_events,
    _smart_wallet_events,
    _insider_events,
    _cluster_events,
    _honeypot_events,
    _network_events,
    _wallet_reputation_events,
    _opportunity_events,
    _kol_events,
]


# ---------------------------------------------------------------------------
# Deduplication + ordering
# ---------------------------------------------------------------------------

def _dedup_events(events: list[TimelineEvent]) -> list[TimelineEvent]:
    seen: set[str] = set()
    out: list[TimelineEvent] = []
    for e in events:
        key = (e.title, e.category, e.evidence or "")
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def _sort_events(events: list[TimelineEvent]) -> list[TimelineEvent]:
    def sort_key(e: TimelineEvent) -> tuple:
        ts = e.timestamp or ""
        sev = _SEVERITY_ORDER.get(e.severity, 5)
        return (ts == "", ts, sev)
    return sorted(events, key=sort_key)


# ---------------------------------------------------------------------------
# Summary generation
# ---------------------------------------------------------------------------

def _generate_summary(r: TokenAnalysisResponse, events: list[TimelineEvent]) -> TimelineSummary:
    # Launch quality
    launch = "unknown"
    hp_status = r.honeypot.status if r.honeypot else None
    if hp_status == "honeypot":
        launch = "dangerous"
    elif hp_status == "high_tax":
        launch = "risky"
    elif hp_status == "sellable":
        launch = "healthy" if r.analysis.risk_score < 50 else "concerning"
    elif r.analysis.risk_score < 30:
        launch = "healthy"
    elif r.analysis.risk_score < 60:
        launch = "moderate"
    else:
        launch = "concerning"

    # Developer behaviour
    dev_beh = "unknown"
    d = r.dev
    if d:
        if d.tokens_rugged and d.tokens_rugged > 0:
            dev_beh = "dangerous"
        elif d.reputation == "serial_rugger":
            dev_beh = "dangerous"
        elif d.reputation == "suspicious":
            dev_beh = "negative"
        elif d.reputation in ("reliable", "established"):
            dev_beh = "positive"
        elif d.reputation == "new":
            dev_beh = "neutral"
        elif d.transferred_out and (d.transferred_out_percentage or 0) >= 10:
            dev_beh = "concerning"
        else:
            dev_beh = "neutral"

    # Liquidity evolution
    liq_evo = "unknown"
    m = r.market_data
    if m and m.liquidity and m.liquidity.usd is not None:
        liq = m.liquidity.usd
        if r.trend and r.trend.has_prior and r.trend.liquidity_change_pct is not None:
            if r.trend.liquidity_change_pct <= -40:
                liq_evo = "draining"
            elif r.trend.liquidity_change_pct <= -10:
                liq_evo = "declining"
            elif r.trend.liquidity_change_pct >= 10:
                liq_evo = "growing"
            else:
                liq_evo = "stable"
        elif liq >= 5000:
            liq_evo = "healthy"
        elif liq >= 1000:
            liq_evo = "thin"
        else:
            liq_evo = "minimal"

    # Community growth
    community = "unknown"
    h = r.holders
    if h and h.holder_count is not None:
        if h.holder_count >= 1000:
            community = "organic" if (h.top10_percentage or 100) < 50 else "whale-dominated"
        elif h.holder_count >= 100:
            community = "growing" if (h.top10_percentage or 100) < 60 else "concentrated"
        elif h.holder_count >= 10:
            community = "early"
        else:
            community = "minimal"

    # Smart money
    smart_count = sum(1 for hit in r.watchlist_hits if hit.kind == "smart")
    smart_money = "none"
    if smart_count >= 3:
        smart_money = "strong"
    elif smart_count >= 1:
        smart_money = "increasing"

    # Narrative
    pieces: list[str] = []
    if launch in ("healthy",):
        pieces.append("Healthy launch")
    elif launch == "dangerous":
        pieces.append("DANGEROUS — honeypot detected")
    elif launch == "risky":
        pieces.append("Risky launch with high tax")
    elif launch == "concerning":
        pieces.append("Launch shows concerning signals")
    else:
        pieces.append("Early-stage token")

    if dev_beh == "dangerous":
        pieces.append("by a serial rugger")
    elif dev_beh == "positive":
        pieces.append("from a reputable developer")
    elif dev_beh == "negative":
        pieces.append("from a suspicious developer")

    if smart_money == "strong":
        pieces.append("attracting strong smart money interest")
    elif smart_money == "increasing":
        pieces.append("with some smart wallet activity")

    if liq_evo == "draining":
        pieces.append("— liquidity is draining")
    elif liq_evo == "growing":
        pieces.append("with growing liquidity")
    elif liq_evo == "stable":
        pieces.append("with stable liquidity")

    narrative = " ".join(pieces) + "." if pieces else "Insufficient data for narrative."

    return TimelineSummary(
        launch_quality=launch,
        developer_behaviour=dev_beh,
        liquidity_evolution=liq_evo,
        community_growth=community,
        smart_money=smart_money,
        narrative=narrative,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_timeline(result: TokenAnalysisResponse) -> AlphaTimeline:
    """Build the Alpha Timeline from an already-completed analysis. Pure, lightweight."""
    all_events: list[TimelineEvent] = []
    for provider in PROVIDERS:
        all_events.extend(provider(result))
    events = _sort_events(_dedup_events(all_events))
    summary = _generate_summary(result, events)
    return AlphaTimeline(events=events, summary=summary)
