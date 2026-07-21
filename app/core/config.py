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
    # M22: the chain abstraction (`app/core/chains.py`) reads the fields below to
    # build the active `ChainConfig`. `default_chain` selects which registered
    # chain is active; only "robinhood" is registered today.
    default_chain: str = "robinhood"
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
    # How many top holders to pull for distribution + cluster analysis (single page cap).
    holder_sample_size: int = 50

    # M12: how many holder pages (~50 rows each) to walk for the primary token, so
    # concentration/top1/top10/clusters see beyond the first ~50 rows. Bounded to keep
    # request volume in check (paging multiplies requests). 1 == prior single-page behaviour.
    holder_scan_pages: int = 4

    # M13: an LP lock unlocking within this many days is treated as near-term — nearly
    # as dangerous as no lock — and scored as a high signal rather than the reassurance
    # a long lock gives. A lock unlocking beyond this horizon scores as before.
    lp_lock_near_term_days: int = 30

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

    # --- Smart-wallet cross-token survival (M16) ---
    # Computing surviving_tokens costs one /addresses/{addr}/tokens lookup per wallet, so
    # only the strongest same-token candidates are checked. Ranked by their single-token
    # proxy score, the top N have their survival counted; 0 disables the cross-token pass
    # (reverts to the prior inert behaviour). Bounds request amplification per analyze.
    smart_wallet_survival_candidates: int = 10
    # A held token counts toward "surviving" only if its balance is positive AND it still
    # has at least this many holders — a dead/rugged token often collapses to a handful of
    # wallets, so requiring a floor keeps "survived" meaning "still a live token".
    smart_wallet_survival_min_holders: int = 50

    # --- Persistent deployer reputation (M18) ---
    # A deployer's launch history is recomputed live on every analyze by _scan_creator_launches
    # (bounded, but pays the request cost each time). Once persisted, a known deployer's history
    # is reused within this TTL so the live scan is skipped on a cache hit. Outcomes change over
    # time (an alive token can rug later), so the cache expires and refreshes after the TTL —
    # a deployer's status can only worsen, never get frozen good. 0 disables the cache (always
    # scans live), reverting to pre-M18 behaviour.
    deployer_reputation_ttl_hours: float = 24.0

    # --- Historical scan snapshots & trend detection (M19) ---
    # Every analyze persists a compact snapshot (score + key metrics + timestamp). On the
    # next analyze, the prior snapshot is diffed to surface slow-rug trends a single point-in-
    # time score misses. Master switch; disabling reverts to no persistence / no trend signal.
    snapshot_enabled: bool = True
    # Separate sqlite DB from the wallet watchlist so snapshot growth stays decoupled.
    snapshot_db_path: str = "data/snapshots.db"
    # Rows kept per token (newest-first); bounds DB growth. Older rows are pruned each write.
    snapshot_history_retain: int = 50
    # A liquidity DROP of at least this percent vs. the prior snapshot raises a trend signal
    # ("liquidity bleeding out" — a slow rug). Only drops score; a rise is reassuring, not risk.
    snapshot_liquidity_drop_pct: float = 40.0
    # A rise in top-10 holder concentration of at least this many percentage POINTS vs. the
    # prior snapshot raises a trend signal (dev/insider quietly accumulating).
    snapshot_concentration_rise_pct: float = 15.0

    # --- Persistent wallet reputation (M17) ---
    # A watchlisted wallet appearing on this token is scored as reputation risk only once
    # it has been seen on at least this many OTHER tokens (prior-token history). A floor
    # avoids a false signal on a wallet's first-ever sighting (history == just this token).
    wallet_reputation_min_prior_tokens: int = 2

    # --- Funder graph / bundler detection (M14) ---
    # How many hops back to trace each holder's funding chain. 1 == prior single-hop
    # behaviour; 2-3 catches funder->intermediary->fresh-wallet bundling. Bounded because
    # each hop multiplies address-transaction lookups (hop count x traced wallets).
    funder_max_hops: int = 2
    # A single funder linking at least this many sampled holders (directly or via the
    # traced chain) is treated as a bundler: one funder -> many fresh wallets.
    bundler_min_cluster_wallets: int = 3

    # --- Same-block / coordinated-buy timing (M15) ---
    # Distinct buyers landing in one block (or within `coordinated_buy_window_seconds`
    # of each other) is a coordination signal independent of funding source. A cohort
    # counts only at/above this many wallets, so an ordinary 1-2 buyer block never flags.
    coordinated_buy_min_cohort: int = 3
    # Buyers within this many seconds of each other (when block numbers are unavailable)
    # count as the same timing cohort. 0 == same-block only.
    coordinated_buy_window_seconds: int = 2

    # --- Persistent watchlist ---
    watchlist_db_path: str = "data/watchlist.db"
    watchlist_refresh_enabled: bool = True
    # How often the background loop refreshes watchlisted wallets' recent buys.
    watchlist_refresh_seconds: int = 900
    # Cap wallets refreshed per cycle to respect the free Blockscout rate budget.
    watchlist_refresh_batch: int = 25

    # --- KOL Intelligence Engine (M23, Deliverable A: watchlist foundation) ---
    # Master switch. Deliverable A only builds the watchlist + provider abstraction;
    # no scraping/diffing/alerts run yet, so this gates future background work.
    kol_intel_enabled: bool = False
    # Separate sqlite DB from the wallet watchlist so the two stores stay decoupled
    # and independently scalable (KOL data grows with snapshots later).
    kol_db_path: str = "data/kol.db"
    # Default platform assumed when a seed/entry omits one. Must be a platform the
    # domain model knows (app/models/kol.SOCIAL_PLATFORMS); "x" is the first with a
    # provider implementation.
    kol_default_platform: str = "x"
    # Config-driven watchlist. Editing this list (add/remove an entry, flip
    # `enabled`, change `tier`) is the no-code way to manage who is watched;
    # `kol_watchlist.sync_from_config()` reconciles these into the store on startup.
    # Each item: {handle, platform?, display_name?, tier?, enabled?, notes?}.
    kol_watchlist_seed: list[dict] = []
    # When True, a config seed whose `enabled`/`tier`/`display_name`/`notes` differ
    # from the stored row overwrites the stored values on sync (config is source of
    # truth). When False, config only ever ADDS missing KOLs and never clobbers
    # operator edits made via the API. Removal from config never auto-deletes.
    kol_config_overwrites: bool = True
    # Snapshot retention (M23 Deliverable C). How many of the most recent snapshots
    # to keep per KOL; older ones are pruned after each save so the table can't grow
    # without bound. Diffing only ever needs the latest complete snapshot, but a few
    # are retained for history/debugging. Set <= 0 to disable pruning (keep all).
    kol_snapshot_retain: int = 10

    # --- X (Twitter) scraping (M23 Deliverable B) ---
    # Persistent browser profile directory. Cookies/session live here so we reuse
    # an authenticated session across runs instead of logging in every time. Keep
    # it out of version control; it holds session credentials.
    x_user_data_dir: str = "data/x_profile"
    # Headless by default for servers. Set False for the one-time manual login /
    # reauthentication flow so a human can complete the X challenge in a real window.
    x_headless: bool = True
    # Per-navigation timeout (ms) for goto/waits — X can be slow under load.
    x_nav_timeout_ms: int = 30000
    # Infinite-scroll tuning for the Following page.
    # Pause between scroll steps to let virtualized rows render + network settle.
    x_scroll_pause_ms: int = 900
    # Hard cap on scroll iterations so a huge/rate-limited account can't loop forever.
    x_scroll_max_rounds: int = 300
    # Stop once this many consecutive scrolls yield no new handles (list end reached).
    x_scroll_stable_rounds: int = 3
    # Safety cap on collected handles per snapshot (a runaway account or DOM bug
    # can't blow up memory / the DB row).
    x_following_max: int = 5000
    # Optional explicit path to a chromium executable (e.g. system Chrome). Empty
    # uses Playwright's bundled browser.
    x_browser_executable: str = ""

    # --- Crypto intelligence pipeline (M23 Deliverable D) ---
    # Master switch for classifying newly-followed accounts and (for confident
    # crypto projects) invoking the existing rug analyzer. Off by default: like the
    # rest of M23 it's opt-in and does no background work until enabled.
    kol_crypto_intel_enabled: bool = False
    # Minimum weighted signal score for a classification to be trusted enough to act
    # on (i.e. to hand extracted contracts to the rug analyzer). Scores below this
    # still classify + persist (as evidence) but are treated as too weak to analyze.
    # Expressed on the same 0..100 scale the confidence bands below use.
    kol_crypto_min_score: int = 45
    # Never classify on a single weak signal: require at least this many independent
    # corroborating signals before a non-"unknown"/"individual" classification is
    # allowed. One strong signal (a valid contract address) can satisfy this on its
    # own (see kol_crypto_strong_signals); weak signals must corroborate each other.
    kol_crypto_min_signals: int = 2
    # Confidence band lower bounds on the 0..100 weighted-score scale. A score >= a
    # band's threshold earns that band. Tunable without code changes; ordered
    # high->low at read time so overlapping edits still resolve deterministically.
    kol_crypto_confidence_bands: dict[str, int] = {
        "very_high": 85,
        "high": 65,
        "medium": 45,
        "low": 25,
        "very_low": 0,
    }
    # Signal weights (0..100 contribution each, summed then capped at 100). Additive,
    # data-driven, and fully config-editable: add a new signal name here and register
    # its detector in crypto_signals to extend detection with no logic change. A
    # "strong" signal (see below) alone can carry a classification; others corroborate.
    kol_crypto_signal_weights: dict[str, int] = {
        "contract_address": 55,   # a valid, extractable on-chain address (strongest)
        "dexscreener": 30,
        "birdeye": 25,
        "gmgn": 25,
        "geckoterminal": 25,
        "coingecko": 20,
        "coinmarketcap": 20,
        "pumpfun": 30,
        "official_website": 15,
        "telegram": 15,
        "discord": 12,
        "github": 12,
        "chain_keyword": 10,      # "solana"/"ethereum"/"base"/"robinhood" mention
        "ca_prefix": 15,          # explicit "CA:" marker in bio/links
        "ticker_cashtag": 8,      # $TICKER style cashtag
        "crypto_keyword": 6,      # generic crypto lexicon ("token", "airdrop", ...)
    }
    # Signals strong enough that ONE of them satisfies kol_crypto_min_signals. Only a
    # verifiable, extractable contract address qualifies by default — everything else
    # must corroborate. Editable so the policy can be tuned without code changes.
    kol_crypto_strong_signals: list[str] = ["contract_address"]
    # Cap on how many distinct contracts to hand to the rug analyzer per account, so a
    # profile that lists many addresses can't fan out into an unbounded analysis burst.
    kol_crypto_max_contracts_per_account: int = 5

    # --- KOL Intelligence scoring & correlation (M23 Deliverable F) ----------
    # Master switch for the scoring + cluster + correlation engine. Off by default:
    # like the rest of M23 it's opt-in and does no work until enabled. When on, a new
    # follow of a crypto project triggers (re)scoring of that project across all KOLs.
    kol_score_enabled: bool = False
    # Per-tier weighting. The KEY is the KOL tier (as a string, since env/JSON config
    # keys are strings) and the VALUE is that tier's influence weight. Adding a Tier 4
    # or re-weighting Tier 2 is a config edit — NEVER a code change, and NEVER a
    # hardcoded KOL name. A tier missing here contributes the configured default.
    kol_tier_weights: dict[str, int] = {"1": 40, "2": 25, "3": 12}
    # Weight applied to a KOL whose tier isn't listed above (unknown/misconfigured
    # tier). Deliberately low so an unrecognized tier never inflates a score.
    kol_tier_default_weight: int = 10
    # KOL Intelligence Score = weighted sum of components below, each contribution
    # capped, the total capped at 100. Every component that fires produces a piece of
    # structured Evidence, so no score is opaque. All weights are config-editable.
    #   kol_convergence   — reward for MULTIPLE distinct KOLs on the same project
    #                       (per additional KOL beyond the first), the core alpha signal
    #   tier_quality      — contribution from the summed tier weights of contributors
    #   crypto_confidence — how confident the crypto classification is (very_high..low)
    #   analysis_safety   — the project passed analysis / low rug risk (reuse, not recompute)
    #   cluster_bonus     — bonus when a cluster (see below) is detected
    #   recency           — bonus for follows landing close together / recently
    #   alpha             — OPTIONAL: an external alpha score, when one exists (future
    #                       extensible — contributes nothing while no alpha scorer exists)
    kol_score_weights: dict[str, int] = {
        "kol_convergence": 20,     # per additional distinct KOL beyond the first
        "tier_quality": 1,         # multiplied into summed tier weights (scaled below)
        "crypto_confidence": 20,   # scaled by the classification confidence band
        "analysis_safety": 20,     # scaled by (100 - risk_score)/100 when analyzed
        "cluster_bonus": 15,       # flat, when any cluster type is detected
        "recency": 10,             # scaled by how tight/recent the follow window is
        "alpha": 20,               # scaled by external alpha in [0,100] when present
    }
    # Divisor applied to the summed tier weights before the `tier_quality` weight, so
    # tier quality lands on the same 0..~100 component scale as the others. Config so
    # the scale can be retuned when tier weights change.
    kol_tier_quality_divisor: int = 1
    # Maps a crypto-classification confidence band to a 0..1 multiplier for the
    # `crypto_confidence` component. Config-driven so the influence of "medium" vs
    # "high" is tunable without code. Bands not listed default to 0.
    kol_confidence_multipliers: dict[str, float] = {
        "very_high": 1.0, "high": 0.8, "medium": 0.55, "low": 0.3, "very_low": 0.1,
    }
    # Confidence bands for the KOL Intelligence Score itself (0..100), high->low, same
    # pattern as the crypto confidence bands. A score >= a band's threshold earns it.
    kol_score_confidence_bands: dict[str, int] = {
        "very_high": 80, "high": 60, "medium": 40, "low": 20, "very_low": 0,
    }
    # --- Cluster detection (config-driven; no hardcoded timing) --------------
    # Minimum distinct KOLs converging on one project (within the window) to call it a
    # cluster at all. A single KOL is a follow, not a cluster.
    kol_cluster_min_kols: int = 2
    # Rolling time window (hours) over which converging follows count toward one
    # cluster. Follows spread wider than this don't cluster together. Fully config.
    kol_cluster_window_hours: float = 72.0
    # A "rapid" cluster: min KOLs converging inside the tighter rapid window (hours).
    # Tighter convergence = stronger conviction, hence its own typed cluster.
    kol_cluster_rapid_window_hours: float = 6.0
    kol_cluster_rapid_min_kols: int = 2
    # A "tier-1" cluster: at least this many Tier-1 KOLs among the contributors.
    kol_cluster_tier1_min: int = 2
    # A "high-conviction" cluster: the computed KOL Intelligence Score reaches this.
    kol_cluster_high_conviction_score: int = 75
    # --- Correlation / momentum thresholds -----------------------------------
    # A project is "momentum" when its distinct-KOL count grows by at least this many
    # since the last persisted score (drives the ProjectMomentumDetected event).
    kol_momentum_min_new_kols: int = 1
    # Minimum KOL Intelligence Score for a project's correlation object to be treated
    # as "actionable" intelligence (below this it's persisted as history but flagged
    # low-conviction). Config so the bar moves without code.
    kol_intel_min_actionable_score: int = 40
    # Retention: how many score-history and cluster-history rows to keep per project.
    # <= 0 disables pruning (keep all). History powers future analytics/AI timelines.
    kol_intel_history_retain: int = 200

    # --- Deliverable H: notification & delivery layer ------------------------
    # Master switch. Off by default — the whole notification layer is opt-in and
    # inert until enabled, exactly like the KOL crypto/scoring switches. When off,
    # events are still produced + persisted; they're simply not delivered.
    notify_enabled: bool = False
    # Destinations to deliver through, by name. Only the roadmap sinks exist today:
    # "log" (emit via the app logger) and "memory" (in-process buffer — a UI feed /
    # test sink). Telegram/Discord/webhook are future adapters: register one in
    # `notifications._PROVIDER_FACTORIES` and add its name here, no producer change.
    notify_providers: list[str] = ["log"]
    # Forwarding rules (all config-driven). An intelligence event is delivered only
    # when its project's KOL Intelligence Score, score-confidence band, and distinct
    # KOL (cluster) size ALL clear these bars AND its type is in the forward list.
    notify_min_score: int = 0
    notify_min_confidence: str = "very_low"   # one of CONFIDENCE_LEVELS (very_low..very_high)
    notify_min_cluster_size: int = 0
    # Event types to forward. Defaults to the alert-worthy convergence signals (the
    # flagship cluster + momentum events), not the every-update bookkeeping ones.
    # An empty list forwards NO events (delivery off without flipping the switch).
    notify_event_types: list[str] = [
        "kol_cluster_detected", "high_conviction_cluster", "project_momentum_detected",
    ]

    # --- Token Watchlist & Monitoring Engine (M24) --------------------------
    # Master switch for the background monitoring scheduler. Off by default —
    # like every other engine here it is opt-in and does NO background work
    # until enabled. When off, the watchlist store + management API still work
    # (you can add/list tokens); only the periodic scheduler stays dormant.
    token_monitor_enabled: bool = False
    # Separate sqlite DB from the wallet + KOL stores so the monitoring domain
    # (watchlist, history, events) stays decoupled and independently scalable.
    token_monitor_db_path: str = "data/token_monitor.db"
    # Scheduler cadence. The loop wakes every `interval_seconds` and processes
    # the enabled watchlist. Configurable so operators can trade freshness for
    # the free-tier API budget the analyzer consumes.
    token_monitor_interval_seconds: int = 900
    # Concurrency limit: how many tokens are analyzed in parallel within one
    # cycle. Bounds the load a single cycle puts on the reused analyzer + its
    # upstream data sources. Must be >= 1.
    token_monitor_concurrency: int = 3
    # Per-token analysis timeout (seconds). A single token whose analysis hangs
    # can never stall the whole cycle — it is abandoned after this budget and
    # treated as a (retryable) failure.
    token_monitor_timeout_seconds: int = 120
    # Retry policy for a token whose monitoring cycle fails (analyzer error or
    # timeout). `attempts` is the total number of tries per token per cycle
    # (1 = no retry); `backoff_seconds` is the base delay between tries.
    token_monitor_retry_attempts: int = 2
    token_monitor_retry_backoff_seconds: float = 1.0
    # History retention: how many monitoring-history rows to keep per token.
    # Older rows are pruned after each save so the table can't grow without
    # bound. <= 0 disables pruning (keep all).
    token_monitor_history_retain: int = 200
    # Config-driven seed watchlist. Editing this list is the no-code way to
    # manage which tokens are monitored; `token_monitor.sync_from_config()`
    # reconciles it into the store on startup. Each item is either a bare
    # address string or {address, label?, enabled?, options?}.
    token_monitor_seed: list = []

    # --- KOL Intelligence Automation (M25) ----------------------------------
    # Master switch for the background KOL capture scheduler. Off by default —
    # like every other engine here it is opt-in and does NO background work
    # until enabled. When off, the KOL watchlist + `capture_following` still
    # work on demand; only the periodic scheduler stays dormant. Independent of
    # `kol_intel_enabled` (which gates the pipeline internals) so the loop can be
    # toggled without touching pipeline behaviour.
    kol_scheduler_enabled: bool = False
    # Scheduler cadence. The loop wakes every `interval_seconds` and captures the
    # enabled KOL roster once. Configurable so operators trade freshness for the
    # scraping/rate budget the X provider consumes.
    kol_scheduler_interval_seconds: int = 3600
    # Concurrency limit: how many KOLs are captured in parallel within one cycle.
    # Kept low by default — X scraping is heavier and more rate-sensitive than the
    # REST analyzer, so a small fan-out avoids tripping rate limits. Must be >= 1.
    kol_scheduler_concurrency: int = 2
    # Per-KOL capture timeout (seconds). A single KOL whose capture hangs can never
    # stall the whole cycle — it is abandoned after this budget and treated as a
    # (retryable) failure. X scraping can be slow, so this is generous.
    kol_scheduler_timeout_seconds: int = 180
    # Retry policy for a KOL whose capture fails (provider error or timeout).
    # `attempts` is the total tries per KOL per cycle (1 = no retry); `backoff_seconds`
    # is the base delay between tries, multiplied by the attempt number.
    kol_scheduler_retry_attempts: int = 2
    kol_scheduler_retry_backoff_seconds: float = 2.0

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
