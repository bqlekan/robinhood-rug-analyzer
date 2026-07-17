from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core import honeypot_artifact


class Settings(BaseSettings):
    app_name: str = "Robinhood Rug Analyzer"
    app_version: str = "0.2.0"
    environment: str = "development"
    log_level: str = "INFO"
    cors_origins: list[str] = ["http://localhost:8000", "http://127.0.0.1:8000"]

    # Robinhood Chain identity. This app is intentionally single-chain.
    chain_id: int = 4663
    chain_name: str = "Robinhood Chain"
    # DexScreener labels Robinhood Chain pairs with this chainId string.
    dexscreener_chain: str = "robinhood"
    # Free, keyless public Blockscout REST API for Robinhood Chain.
    blockscout_base_url: str = "https://robinhoodchain.blockscout.com"
    # Public RPC (rate limited). Override with an Alchemy URL for production.
    rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"

    # Networking.
    http_timeout: float = 12.0
    # Max concurrent outbound connections in the shared HTTP pool. A single bounded
    # pool caps the whole nested scan fan-out (token loop -> funder traces -> creator
    # scans) so a scan cannot exhaust the free Blockscout rate budget.
    http_max_connections: int = 20
    # --- HTTP response cache (near-static reads only) ---
    # Caches immutable/near-static external reads (verified contract source,
    # contract creation facts). Market data, holder metrics, and transfers are
    # never cached so scoring always sees live data.
    http_cache_enabled: bool = True
    http_cache_ttl_seconds: float = 300.0
    http_cache_max_size: int = 512
    # Cap how many tokens the ranked scanner analyzes per request so a single
    # scan cannot exhaust the free Blockscout rate budget.
    scan_max_tokens: int = 15
    # How many top holders to pull for distribution + cluster analysis.
    holder_sample_size: int = 50

    # --- Scan tiering (M2) ---
    # A cheap pre-screen ranks candidates using ONLY list_tokens metadata (no extra
    # requests), promoting anything not confidently low-risk into full deep analysis.
    scan_tiering_enabled: bool = True
    # Light score at/above which a token is promoted to deep analysis. Lower =
    # more tokens promoted (safer, more requests); higher = more skipped.
    scan_light_promote_threshold: int = 25
    # A token needs at least this many holders to be considered confidently
    # low-risk on the cheap signal alone; fewer (or unknown) -> promote.
    scan_established_holder_floor: int = 500
    # Max concurrent deep analyses in flight during a scan (bounds fan-out).
    scan_max_deep_analyses: int = 5

    # --- Honeypot / sell-tax simulation (M10) ---
    # Inert by default: with no router mapped for a token's DEX, no sim calls fire and
    # behavior is unchanged. Activates only once a chain router is sourced and mapped.
    honeypot_sim_enabled: bool = True
    # dexId (as DexScreener labels it) -> Uniswap v3 SwapRouter02 address on this chain.
    # Defaults to the verified Robinhood Chain router so the sim is active there; other
    # chains stay inert until their router is mapped.
    dex_routers: dict[str, str] = {"uniswap": honeypot_artifact.ROBINHOOD_SWAPROUTER02}
    # Native-token amount (wei) the synthetic buyer spends in the simulated buy leg.
    honeypot_sim_buy_wei: int = 10**16  # 0.01 native
    # Sell tax at/above this percent is flagged as a high-severity signal.
    honeypot_high_tax_pct: float = 30.0
    # Wrapped-native token address (path hop for buy/sell). Chain-specific.
    honeypot_weth_address: str | None = honeypot_artifact.ROBINHOOD_WETH
    # Compiled prober-contract runtime bytecode, injected via `code` override so the
    # buy->sell round-trip runs atomically in ONE eth_call (two calls can't share state).
    # ABI: probe(address router,address weth,address token,uint256 buyWei,bytes buyPath,
    # bytes sellPath) -> (uint256 bought,uint256 soldBack); catches the sell revert and
    # returns soldBack=0. Routes are built off-chain (route_discovery), so the pinned
    # bytecode is route-agnostic. Compiled from contracts/HoneypotProber.sol.
    honeypot_prober_code: str | None = honeypot_artifact.PROBER_RUNTIME_CODE
    honeypot_prober_selector: str = honeypot_artifact.PROBER_SELECTOR
    # Uniswap v3 factory, for pool discovery (getPool(tokenA,tokenB,fee)). Chain-specific.
    honeypot_v3_factory: str | None = honeypot_artifact.ROBINHOOD_V3_FACTORY
    # Ordered quote assets tried when routing a buy->sell round-trip. The first entry MUST
    # be the wrapped-native token (the prober always funds itself by wrapping native, so
    # every path starts at WETH). Later entries are reached via a WETH->quote hop, letting
    # the sim reach tokens with no direct WETH pool (e.g. USDG-paired stock tokens). Add a
    # new quote asset by appending its address here -- no code or recompile needed.
    honeypot_quote_assets: list[str] = [
        honeypot_artifact.ROBINHOOD_WETH,
        honeypot_artifact.ROBINHOOD_USDG,
    ]
    # Uniswap v3 fee tiers (hundredths of a bip) probed for pools, cheapest first.
    honeypot_fee_tiers: list[int] = [500, 3000, 10000, 100]
    # Minimum quote-side reserve (pool balanceOf the quote asset, in that asset's base
    # units) for a v3 pool to count as usable. Keyed by lowercased quote-asset address so
    # each asset gets a floor in its OWN decimals (WETH has 18, USDG 6) -- a single scalar
    # can't serve both. Reserves are checked, NOT `liquidity()`: a concentrated-liquidity
    # pool can report zero in-range liquidity while still holding swappable balances. A
    # dust pool (e.g. 6 wei WETH) is rejected so the sim doesn't pick it and misread the
    # near-zero round-trip as a honeypot. Unlisted assets fall back to the "*" default.
    honeypot_min_quote_reserve: dict[str, int] = {
        honeypot_artifact.ROBINHOOD_WETH.lower(): 10**16,  # 0.01 WETH (== one buy leg)
        honeypot_artifact.ROBINHOOD_USDG.lower(): 10**6,   # 1 USDG (6 decimals)
        "*": 1,
    }

    # --- Wallet intelligence ---
    # How many of a token's earliest buyers to treat as candidate insiders.
    insider_early_buyer_count: int = 15
    # A wallet must clear this proxy score (0-100) to be flagged as "smart".
    # NOTE: this is a heuristic estimate from free on-chain signals, NOT verified ROI.
    smart_wallet_min_proxy_score: int = 70
    # Transfer pages to pull when profiling a token's flow (each ~50 rows).
    transfer_scan_pages: int = 2

    # --- Persistent watchlist ---
    watchlist_db_path: str = "data/watchlist.db"
    watchlist_refresh_enabled: bool = True
    # How often the background loop refreshes watchlisted wallets' recent buys.
    watchlist_refresh_seconds: int = 900
    # Cap wallets refreshed per cycle to respect the free Blockscout rate budget.
    watchlist_refresh_batch: int = 25

    # Optional: plug in a free/cheap LLM key later for richer lore summaries.
    # When empty, lore falls back to extractive themes + heuristic sentiment.
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
