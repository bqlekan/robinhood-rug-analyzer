from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

# EVM contract address: 0x followed by 40 hex chars. Validated at the trust
# boundary so malformed input never reaches outbound API calls.
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def is_valid_address(address: str | None) -> bool:
    return bool(address) and bool(_ADDRESS_RE.match(address.strip()))


class TokenAnalysisRequest(BaseModel):
    contract_address: str = Field(..., description="Token contract address on Robinhood Chain")
    include_lore: bool = Field(True, description="Fetch and interpret social lore for the token")

    @field_validator("contract_address")
    @classmethod
    def _validate_address(cls, v: str) -> str:
        v = v.strip()
        if not is_valid_address(v):
            raise ValueError("contract_address must be a 0x-prefixed 40-hex-character address")
        return v


class ScanRequest(BaseModel):
    # D3: limit is a backward-compat alias for page_size. Old callers sending
    # {"limit": 5} get identical visible behaviour (5 results on page 1), but
    # the backend now discovers broadly and paginates after ranking.
    limit: int | None = Field(None, ge=1, le=200, description="Deprecated: use page_size")
    page: int = Field(1, ge=1, description="1-indexed page number")
    page_size: int = Field(15, ge=1, le=200, description="Results per page")
    include_lore: bool = Field(False, description="Fetch lore for each token (slower)")

    def effective_page_size(self) -> int:
        """limit is a backward-compat alias for page_size."""
        if self.limit is not None and self.page_size == 15:
            return min(self.limit, 200)
        return self.page_size


# --- Market data (DexScreener) ---


class LiquiditySnapshot(BaseModel):
    usd: float | None = None
    base: float | None = None
    quote: float | None = None


class VolumeSnapshot(BaseModel):
    h24: float | None = None
    h6: float | None = None
    h1: float | None = None
    m5: float | None = None
    buys: int | None = None
    sells: int | None = None


class PriceChangeSnapshot(BaseModel):
    h24: float | None = None
    h6: float | None = None
    h1: float | None = None
    m5: float | None = None


class TokenMarketData(BaseModel):
    chain_id: str | None = None
    dex_id: str | None = None
    pair_address: str | None = None
    base_token_name: str | None = None
    base_token_symbol: str | None = None
    quote_token_symbol: str | None = None
    price_usd: str | None = None
    market_cap: float | None = None
    fdv: float | None = None
    liquidity: LiquiditySnapshot | None = None
    volume: VolumeSnapshot | None = None
    price_change: PriceChangeSnapshot | None = None
    pair_created_at: int | None = None
    url: str | None = None
    websites: list[str] = Field(default_factory=list)
    socials: list[dict[str, str]] = Field(default_factory=list)


# --- Age ---


class TokenAge(BaseModel):
    created_at_iso: str | None = None
    age_hours: float | None = None
    age_days: float | None = None
    source: str | None = None  # "pair_created_at" | "contract_creation"


# --- Holders & distribution ---


class HolderEntry(BaseModel):
    address: str
    percentage: float | None = None
    value: str | None = None
    is_contract: bool = False
    label: str | None = None  # e.g. "UniswapV2Pair", locker name
    is_scam: bool = False


class HolderDistribution(BaseModel):
    holder_count: int | None = None
    top10_percentage: float | None = None
    top1_percentage: float | None = None
    # Concentration index in [0,1]; higher = more concentrated among the sample.
    concentration_index: float | None = None
    sampled_holders: int = 0
    top_holders: list[HolderEntry] = Field(default_factory=list)
    # LP pair address (excluded from top_holders and the percentages above).
    lp_address: str | None = None
    lp_percentage: float | None = None


# --- Clusters ---


class HolderCluster(BaseModel):
    funder_address: str | None = None
    member_addresses: list[str]
    combined_percentage: float | None = None
    # How the members are linked: "shared_funder" | "mutual_transfer" | "mixed"
    link_type: str = "shared_funder"


class ClusterAnalysis(BaseModel):
    clusters: list[HolderCluster] = Field(default_factory=list)
    clustered_percentage: float | None = None
    note: str | None = None


class BundleAnalysis(BaseModel):
    """Bundler / sybil-launch summary (M14). Additive metadata — never replaces scoring.

    A bundler funds many fresh wallets from one source so they all buy the same token
    at launch, faking organic distribution. `score` (0-100) grades how strong that
    pattern is; `classification` buckets it for the UI.
    """
    score: int = 0
    classification: str = "Normal"  # "Normal" | "Moderate" | "Heavy" | "Extreme"
    # Wallets tied to the largest single funder (the bundle), and their combined supply %.
    bundled_wallets: int = 0
    bundled_percentage: float | None = None
    top_funder: str | None = None
    creator_funded_bundle: bool = False
    signals: list[str] = Field(default_factory=list)
    detail: str | None = None


class BuyTimingAnalysis(BaseModel):
    """Same-block / within-seconds-of-launch buy coordination (M15). Additive metadata.

    Wallets buying in the same block, or within a few seconds of the first buy, are
    coordinated regardless of who funded them — a control signal complementary to the
    funder-based clusters. `same_block_wallets` is the size of the largest same-block
    cohort; `first_window_wallets` is how many distinct buyers landed inside the launch
    window. Both exclude the mint/creator/LP so a normal launch is not mistaken for a cohort.
    """
    same_block_wallets: int = 0
    same_block_number: int | None = None
    first_window_wallets: int = 0
    coordinated: bool = False
    detail: str | None = None


# --- Dev / creator ---


class LaunchedToken(BaseModel):
    address: str
    name: str | None = None
    symbol: str | None = None
    liquidity_usd: float | None = None
    outcome: str  # "alive" | "likely_rugged" | "unknown"


class DevTransfer(BaseModel):
    to_address: str
    amount_percentage: float | None = None  # % of supply moved, if computable
    timestamp: str | None = None


class DevProfile(BaseModel):
    creator_address: str | None = None
    creation_tx: str | None = None
    dev_holding_percentage: float | None = None
    tokens_launched: int | None = None
    tokens_rugged: int | None = None
    tokens_alive: int | None = None
    launched_tokens: list[LaunchedToken] = Field(default_factory=list)
    reputation: str | None = None  # "clean" | "mixed" | "serial_rugger" | "unknown"
    # Did the deployer move tokens out to other wallets (distribution/dump risk)?
    transferred_out: bool = False
    transfers_out_count: int = 0
    transferred_out_percentage: float | None = None
    dev_transfers: list[DevTransfer] = Field(default_factory=list)
    note: str | None = None


# --- Liquidity lock ---


class LiquidityLock(BaseModel):
    status: str  # "locked" | "burned" | "unlocked" | "unknown"
    locked_percentage: float | None = None
    locker_label: str | None = None
    # M13: the locker/burn address holding the LP, and its unlock schedule when the
    # locker exposes one. unlock_timestamp is unix seconds; unlock_in_days is the
    # horizon from "now" (negative = already unlocked). Both None when unread/unknown.
    locker_address: str | None = None
    unlock_timestamp: int | None = None
    unlock_in_days: float | None = None
    detail: str | None = None


# --- Historical trend (M19) ---


class TokenTrend(BaseModel):
    """Time-series deltas vs. the prior stored snapshot (M19). Additive metadata.

    A single snapshot can't see a *slow rug* — liquidity bleeding out over days or the
    dev quietly accumulating. `has_prior` is False on a token's first-ever analyze (no
    baseline yet), in which case no deltas are computed and nothing scores. Percentages
    are signed: liquidity_change_pct < 0 is a drop; concentration_change_pct > 0 is a rise.
    """
    has_prior: bool = False
    prior_captured_at: str | None = None
    liquidity_change_pct: float | None = None
    concentration_change_pct: float | None = None  # top-10 %-point change
    holder_count_change: int | None = None
    risk_score_change: int | None = None
    signals: list[str] = Field(default_factory=list)
    detail: str | None = None


# --- Launchpad ---


class LaunchpadInfo(BaseModel):
    name: str  # e.g. "NOXA Fun", "Bags", "Pump.fun", "Unknown"
    confidence: str  # "high" | "medium" | "low"
    detail: str | None = None


# --- Honeypot / sell-tax simulation (M10) ---


class HoneypotResult(BaseModel):
    # "honeypot" (unsellable) | "high_tax" | "sellable" | "unknown" (could not simulate).
    status: str
    sell_tax_percentage: float | None = None
    buy_tax_percentage: float | None = None
    detail: str | None = None


# --- Contract intel (source-derived) ---


class ContractIntel(BaseModel):
    verified: bool = False
    contract_name: str | None = None
    compiler: str | None = None
    language: str | None = None
    # Best-guess template/protocol the source was based on (OpenZeppelin, Uniswap, custom, etc.).
    template: str = "unknown"
    # Higher-level protocol family this token was deployed under, if inferable.
    protocol: str | None = None
    protocol_confidence: str = "low"  # "high" | "medium" | "low"
    imports: list[str] = Field(default_factory=list)
    detail: str | None = None


# --- Contract privileges / authority (live reads, M11) ---


class ContractPrivileges(BaseModel):
    # False when unverified or no ABI: "couldn't see", NOT "no powers".
    analyzed: bool = False
    owner_address: str | None = None
    # True = renounced (owner is zero), False = owner retained, None = couldn't confirm.
    ownership_renounced: bool | None = None
    can_mint: bool = False
    can_pause: bool = False
    is_paused: bool | None = None  # live paused() read; None if not exposed/unreadable
    can_blacklist: bool = False
    can_set_fees: bool = False
    detail: str | None = None


# --- Lore ---


class LoreSource(BaseModel):
    title: str
    url: str
    snippet: str | None = None
    source: str  # "duckduckgo" | "reddit" | "dexscreener"


class TokenLore(BaseModel):
    summary: str | None = None
    themes: list[str] = Field(default_factory=list)
    sentiment: str | None = None  # "positive" | "neutral" | "negative" | "unknown"
    sources: list[LoreSource] = Field(default_factory=list)
    generated_by: str  # "llm" | "extractive" | "none"


# --- Wallet intelligence (insiders + smart-wallet proxy) ---


class InsiderWallet(BaseModel):
    address: str
    # Why it's flagged: "early_buyer" | "dev_funded" | "dev_recipient"
    reason: str
    holding_percentage: float | None = None
    buy_rank: int | None = None  # 1 = first buyer after launch
    note: str | None = None


class SmartWallet(BaseModel):
    address: str
    # Heuristic proxy score in [0,100]. NOT a verified ROI figure.
    proxy_score: int
    signals: list[str] = Field(default_factory=list)
    surviving_tokens: int | None = None
    estimate_note: str = (
        "Estimated from free on-chain behavior (early entries, surviving holdings). "
        "Not a verified ROI; free public APIs lack trade-level profit data."
    )


class WalletActivity(BaseModel):
    token_address: str
    symbol: str | None = None
    direction: str = "buy"  # currently only buys are tracked
    amount: str | None = None
    timestamp: str | None = None


class WatchlistEntry(BaseModel):
    address: str
    kind: str  # "smart" | "insider"
    proxy_score: int | None = None
    label: str | None = None
    first_seen: str | None = None
    last_refreshed: str | None = None
    # M21: distinct tokens this wallet has been recorded active on (cross-token history,
    # from the M17 persisted memory). 0 until it appears on more than one token.
    prior_tokens: int = 0
    recent_buys: list[WalletActivity] = Field(default_factory=list)


class WatchlistHit(BaseModel):
    """A watchlisted wallet that holds or bought the token under analysis."""
    address: str
    kind: str  # "smart" | "insider"
    proxy_score: int | None = None
    holding_percentage: float | None = None
    # M17: how many OTHER tokens this wallet has been recorded active on (persisted
    # cross-token memory). 0 = first sighting; higher = a recurring wallet with history.
    prior_tokens: int = 0


# --- Scoring ---


class RiskSignal(BaseModel):
    name: str
    category: str  # age | holders | clusters | dev | liquidity | launchpad | market | honeypot | privileges | lore
    severity: str  # low | medium | high | critical
    points: int
    description: str


class RugAnalysis(BaseModel):
    risk_score: int
    risk_level: str  # low | medium | high | critical
    signals: list[RiskSignal]
    data_sources: list[str]
    limitations: list[str]
    # Data-completeness confidence in [0,100]: how much of the analysis was backed
    # by real data. A low risk_score with low confidence means "couldn't see much",
    # not "confirmed safe". Additive metadata only — does not affect risk_score.
    confidence: int = 100
    confidence_level: str = "high"  # low | medium | high


class DeveloperReputationResult(BaseModel):
    score: int
    evidence: list[str] = Field(default_factory=list)
    deployer: str | None = None
    wallet_age_days: float | None = None
    total_contracts_deployed: int = 0
    token_contracts_deployed: int = 0
    verified_contracts: int = 0
    launchpad_deployments: int = 0
    abandoned_launches: int = 0
    healthy_liquidity_launches: int = 0
    meaningful_holder_launches: int = 0
    surviving_contracts: int = 0
    wallet_transaction_count: int | None = None
    funding_source: str | None = None


class NetworkSibling(BaseModel):
    address: str
    name: str | None = None
    symbol: str | None = None
    outcome: str  # "alive" | "likely_rugged" | "unknown"
    liquidity_usd: float | None = None
    holder_count: int | None = None
    market_cap: float | None = None
    verified: bool = False
    shared_wallets: int = 0
    shared_infrastructure: list[str] = Field(default_factory=list)


class DeveloperNetworkResult(BaseModel):
    score: int
    cluster_confidence: str = "low"  # "high" | "medium" | "low"
    evidence: list[str] = Field(default_factory=list)
    deployer: str | None = None
    funding_wallet: str | None = None
    cluster_size: int = 0
    siblings: list[NetworkSibling] = Field(default_factory=list)
    historical_success_rate: float | None = None
    historical_failure_rate: float | None = None
    avg_liquidity_usd: float | None = None
    avg_holder_count: float | None = None
    avg_survival_days: float | None = None
    wallet_reuse_score: float | None = None
    infrastructure_reuse_score: float | None = None
    funding_reputation: str | None = None  # "clean" | "mixed" | "rug_linked" | "unknown"
    launch_consistency: float | None = None
    project_quality: float | None = None
    network_risk: float | None = None
    network_trust: float | None = None


class SmartWalletReputationResult(BaseModel):
    score: int
    confidence: str = "medium"
    evidence: list[str] = Field(default_factory=list)
    address: str
    wallet_age_days: float | None = None
    total_transactions: int | None = None
    token_interactions: int = 0
    launches_entered: int = 0
    avg_entry_timing_hours: float | None = None
    avg_holding_period_days: float | None = None
    surviving_projects: int = 0
    rugs_entered: int = 0
    successful_launches: int = 0
    early_entry_frequency: float | None = None
    consistency_score: float | None = None
    active: bool = True
    dormant_days: float | None = None


# --- Alpha Timeline ---


class TimelineEvent(BaseModel):
    timestamp: str | None = None
    title: str
    category: str
    severity: str = "info"  # info | low | medium | high | critical
    confidence: str = "medium"  # low | medium | high
    source: str
    evidence: str | None = None
    impact: str | None = None
    explanation: str | None = None


class TimelineSummary(BaseModel):
    launch_quality: str = "unknown"
    developer_behaviour: str = "unknown"
    liquidity_evolution: str = "unknown"
    community_growth: str = "unknown"
    smart_money: str = "unknown"
    narrative: str = "Insufficient data."


class AlphaTimeline(BaseModel):
    events: list[TimelineEvent] = Field(default_factory=list)
    summary: TimelineSummary = Field(default_factory=TimelineSummary)


class EnrichmentField(BaseModel):
    status: str = "not_analysed"  # "known" | "unknown" | "unavailable" | "not_analysed"
    source: str | None = None
    confidence: str = "medium"  # "high" | "medium" | "low"
    reason: str | None = None


class EnrichmentReport(BaseModel):
    pair: EnrichmentField = Field(default_factory=EnrichmentField)
    price: EnrichmentField = Field(default_factory=EnrichmentField)
    liquidity: EnrichmentField = Field(default_factory=EnrichmentField)
    fdv: EnrichmentField = Field(default_factory=EnrichmentField)
    market_cap: EnrichmentField = Field(default_factory=EnrichmentField)
    volume_h24: EnrichmentField = Field(default_factory=EnrichmentField)
    holders: EnrichmentField = Field(default_factory=EnrichmentField)
    developer: EnrichmentField = Field(default_factory=EnrichmentField)
    verification: EnrichmentField = Field(default_factory=EnrichmentField)
    launchpad: EnrichmentField = Field(default_factory=EnrichmentField)
    smart_wallets: EnrichmentField = Field(default_factory=EnrichmentField)
    data_confidence: int = 0

    def compute_data_confidence(self) -> int:
        fields = [
            self.pair, self.price, self.liquidity, self.fdv, self.market_cap,
            self.volume_h24, self.holders, self.developer, self.verification,
            self.launchpad, self.smart_wallets,
        ]
        known = sum(1 for f in fields if f.status == "known")
        self.data_confidence = int(known / len(fields) * 100)
        return self.data_confidence


class TokenAnalysisResponse(BaseModel):
    contract_address: str
    chain: str
    status: str
    message: str
    token_age: TokenAge | None = None
    market_data: TokenMarketData | None = None
    holders: HolderDistribution | None = None
    clusters: ClusterAnalysis | None = None
    bundle: BundleAnalysis | None = None
    buy_timing: BuyTimingAnalysis | None = None
    dev: DevProfile | None = None
    liquidity_lock: LiquidityLock | None = None
    launchpad: LaunchpadInfo | None = None
    honeypot: HoneypotResult | None = None
    contract_intel: ContractIntel | None = None
    contract_privileges: ContractPrivileges | None = None
    lore: TokenLore | None = None
    insiders: list[InsiderWallet] = Field(default_factory=list)
    watchlist_hits: list[WatchlistHit] = Field(default_factory=list)
    trend: TokenTrend | None = None
    developer_reputation: DeveloperReputationResult | None = None
    developer_network: DeveloperNetworkResult | None = None
    wallet_reputations: list[SmartWalletReputationResult] = Field(default_factory=list)
    timeline: AlphaTimeline | None = None
    alpha_score: int | None = None
    alpha_level: str | None = None
    alpha_signals: list["OpportunitySignal"] = Field(default_factory=list)
    analysis: RugAnalysis
    enrichment: EnrichmentReport | None = None


class QualificationResult(BaseModel):
    qualification_level: str  # "excellent" | "good" | "speculative" | "high_risk" | "excluded"
    confidence_score: int = 50
    confidence_factors: list[str] = Field(default_factory=list)
    rejection_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


EligibilityResult = QualificationResult


class OpportunitySignal(BaseModel):
    name: str
    positive: bool
    detail: str


class OpportunityResult(BaseModel):
    alpha_score: int
    alpha_level: str
    signals: list[OpportunitySignal] = Field(default_factory=list)


class RankedToken(BaseModel):
    contract_address: str
    name: str | None = None
    symbol: str | None = None
    risk_score: int
    risk_level: str
    holder_count: int | None = None
    liquidity_usd: float | None = None
    market_cap: float | None = None
    fdv: float | None = None
    volume_h24: float | None = None
    price_usd: str | None = None
    price_change_h24: float | None = None
    age_hours: float | None = None
    age_days: float | None = None
    top_signal: str | None = None
    # Watchlisted wallets (smart/insider) that bought or hold this token.
    flagged_by: list[WatchlistHit] = Field(default_factory=list)
    alpha_score: int | None = None
    alpha_level: str | None = None
    alpha_signals: list[OpportunitySignal] = Field(default_factory=list)
    # Qualification engine (pre-ranking classifier).
    qualification_level: str = "speculative"  # "excellent" | "good" | "speculative" | "high_risk" | "excluded"
    confidence_score: int = 50
    eligible: bool = True
    excluded_from_ranking: bool = False
    rejection_reasons: list[str] = Field(default_factory=list)
    eligibility_evidence: list[str] = Field(default_factory=list)
    eligibility_warnings: list[str] = Field(default_factory=list)
    # Independent dimension scores (0-100, None = not computed).
    security_score: int | None = None
    liquidity_score: int | None = None
    dev_reputation_score: int | None = None
    dev_network_score: int | None = None
    smart_wallet_score: int | None = None
    holder_quality_score: int | None = None
    momentum_score: int | None = None
    composite_score: int | None = None
    # Liquidity lock summary (from full analysis).
    lock_status: str | None = None
    lock_percentage: float | None = None
    lock_provider: str | None = None
    # Enrichment metadata.
    data_confidence: int | None = None
    enrichment_status: str | None = None  # "complete" | "partial" | "minimal"


# --- Launchpad plugin definitions ---


class LaunchpadDefinition(BaseModel):
    """Configuration for a single launchpad discovery source.

    Drives the plugin-based launchpad discovery engine — each enabled entry
    is dispatched to the strategy matching ``discovery_mode``. New launchpads
    are added by configuration only; the engine has zero launchpad-specific
    branches.
    """

    name: str
    enabled: bool = False
    discovery_mode: str  # registered strategy key: "event" | "factory_scan" | "contract_creation_scan"
    factory_address: str | None = None
    deployer_address: str | None = None
    topic0: str | None = None  # 32-byte hex hash of the event signature
    event_signature: str | None = None  # human-readable, e.g. "TokenLaunched(address,...)"
    token_index: int = 0  # token address in topics[1 + token_index]
    start_block: int = 0
    confidence: str = "low"  # "high" | "medium" | "low"


class SourceDiagnostic(BaseModel):
    """Per-provider diagnostics for the discovery pipeline."""
    source: str
    raw: int = 0
    accepted: int = 0
    rejected_established: int = 0
    rejected_invalid_address: int = 0
    rejected_duplicate: int = 0
    rejected_zero_holders: int = 0
    rejected_age: int = 0
    rejected_liquidity: int = 0
    rejected_other: int = 0
    error: str | None = None


class DiscoveryDiagnostics(BaseModel):
    """Full pipeline diagnostics attached to ScanResponse."""
    sources: list[SourceDiagnostic] = Field(default_factory=list)
    total_raw: int = 0
    total_after_dedup: int = 0
    total_after_filters: int = 0
    enriched: int = 0
    # D3: per-stage observability
    light_scored: int = 0
    deep_analyzed: int = 0
    deep_cache_hits: int = 0
    deep_cache_misses: int = 0
    deep_analysis_duration_ms: int = 0
    reached_qualification: int = 0
    reached_ranking: int = 0
    excluded: int = 0


class ScanResponse(BaseModel):
    chain: str
    status: str
    message: str
    analyzed: int
    ranked_tokens: list[RankedToken]
    excluded_tokens: list[RankedToken] = Field(default_factory=list)
    limitations: list[str]
    discovery: DiscoveryDiagnostics | None = None
    # D3: pagination metadata — display only, never affects discovery/analysis.
    page: int = 1
    page_size: int = 15
    total_ranked: int = 0
    total_pages: int = 1


class WatchlistResponse(BaseModel):
    smart_wallets: list[WatchlistEntry] = Field(default_factory=list)
    insider_wallets: list[WatchlistEntry] = Field(default_factory=list)
    note: str
