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
    limit: int = Field(15, ge=1, le=50, description="How many tokens to analyze and rank")
    include_lore: bool = Field(False, description="Fetch lore for each token (slower)")


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
    analysis: RugAnalysis


class RankedToken(BaseModel):
    contract_address: str
    name: str | None = None
    symbol: str | None = None
    risk_score: int
    risk_level: str
    holder_count: int | None = None
    liquidity_usd: float | None = None
    market_cap: float | None = None
    age_hours: float | None = None
    age_days: float | None = None
    top_signal: str | None = None
    # Watchlisted wallets (smart/insider) that bought or hold this token.
    flagged_by: list[WatchlistHit] = Field(default_factory=list)


class ScanResponse(BaseModel):
    chain: str
    status: str
    message: str
    analyzed: int
    ranked_tokens: list[RankedToken]
    limitations: list[str]


class WatchlistResponse(BaseModel):
    smart_wallets: list[WatchlistEntry] = Field(default_factory=list)
    insider_wallets: list[WatchlistEntry] = Field(default_factory=list)
    note: str
