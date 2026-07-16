from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # Cap how many tokens the ranked scanner analyzes per request so a single
    # scan cannot exhaust the free Blockscout rate budget.
    scan_max_tokens: int = 15
    # How many top holders to pull for distribution + cluster analysis.
    holder_sample_size: int = 50

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
