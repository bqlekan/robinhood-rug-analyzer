# Robinhood Chain Rug Analyzer

FastAPI web app that screens tokens on **Robinhood Chain** for rug-pull risk. It ranks active tokens by risk and produces a transparent, explainable score for any single token, built entirely on free public data sources.

## What It Does

- **Ranked scanner** — pulls active Robinhood Chain tokens and ranks them by rug-risk score.
- **Single-token drill-down** — full analysis for any contract address across these dimensions:
  - **Age** — how new the token/pair is.
  - **Market data** — price, liquidity, volume, and price change from DexScreener.
  - **Holder distribution** — holder count and top-1 / top-10 concentration from a sampled holders page.
  - **Clusters** — groups of holders that share a common funding wallet (possible coordinated control).
  - **Dev profile** — deployer address, dev holdings, and best-effort launch reputation.
  - **Liquidity lock** — whether LP tokens are burned or held by a known locker.
  - **Launchpad** — origin detection against a known-launchpad registry.
  - **Lore** — social narrative and sentiment via public web search.
- Produces a **0–100 weighted risk score** where every point is attributable to a named, categorized signal.
- Surfaces **limitations** clearly: this is a heuristic screen, not financial advice.

## Project Structure

```text
robinhood-rug-analyzer/
+-- app/
|   +-- api/                 # FastAPI route handlers (/analyze, /scan, /chain)
|   +-- core/                # Settings (chain config) and logging setup
|   +-- models/              # Pydantic request/response schemas
|   +-- services/
|   |   +-- blockscout_client.py     # Robinhood Chain explorer (tokens, holders, txs)
|   |   +-- dexscreener_client.py    # Market pairs, price, liquidity, volume
|   |   +-- lore_client.py           # Public web search + sentiment
|   |   +-- launchpad_registry.py    # Known launchpads / lockers / burn addresses
|   |   +-- analyzers.py             # Pure per-dimension analysis
|   |   +-- scoring.py               # Weighted, explainable risk scoring
|   |   +-- rug_analyzer.py          # Orchestrator: analyze one token / scan+rank
|   +-- main.py              # FastAPI app entrypoint (serves API + frontend)
+-- frontend/                # Static HTML/CSS/JavaScript UI (scanner + drill-down)
+-- tests/                   # Unit tests for analyzers, scoring, and lore parsing
+-- render.yaml              # Render deployment config
+-- requirements.txt
```

## Data Sources

- **Blockscout (Robinhood Chain)** — token metadata, holders, and address/transaction data.
- **DexScreener** — token pair, price, liquidity, volume, and price-change data.
- **DuckDuckGo HTML search** — public social/news lore and sentiment signals.

All sources are free and public. The app does not scrape private pages, use API keys, or bypass access controls.

## Local Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Then open `http://127.0.0.1:8000`.

## API Usage

Analyze a single token:

```text
POST /api/v1/analyze
{ "contract_address": "0x...", "include_lore": true }
```

Scan and rank active tokens:

```text
POST /api/v1/scan
{ "limit": 10, "include_lore": false }
```

Chain info:

```text
GET /api/v1/chain
```

Smart-wallet watchlist (insiders + smart-wallet proxies discovered during analysis/scans):

```text
GET /api/v1/watchlist          # grouped smart + insider wallets with recent buys
GET /api/v1/wallet/{address}   # one watchlisted wallet's detail
```

Key response fields for `/analyze`:

- `token_age`, `market_data`, `holders`, `clusters`, `dev`, `liquidity_lock`, `launchpad`, `lore`
- `insiders` — early buyers and deployer-funded wallets detected from sampled transfers.
- `watchlist_hits` — watchlisted smart/insider wallets that currently hold this token.
- `analysis.risk_score` — 0 to 100 heuristic score.
- `analysis.risk_level` — `low`, `medium`, `high`, or `critical`.
- `analysis.signals` — explainable risk signals (name, category, severity, points) that produced the score.
- `analysis.limitations` — caveats on data completeness and interpretation.

Smart-wallet scores are heuristic estimates from free on-chain behavior (early entry, position
distribution, count of surviving tokens held), not verified trade-level ROI.

## Configuration

Chain targeting and analysis knobs live in `app/core/config.py` (overridable via environment/`.env`):

- Blockscout base URL, DexScreener chain id, and chain name/id for Robinhood Chain.
- `holder_sample_size` — how many top holders to sample.
- `scan_max_tokens` — upper bound on tokens per scan.
- `insider_early_buyer_count` — how many earliest recipients to flag as insiders.
- `smart_wallet_min_proxy_score` — threshold to promote a wallet to "smart" on the watchlist.
- `transfer_scan_pages` — transfer pages fetched per token for insider/cluster/dev depth.
- `watchlist_db_path`, `watchlist_refresh_enabled`, `watchlist_refresh_seconds`, `watchlist_refresh_batch` — SQLite-backed watchlist and its optional background refresh.

No third-party API keys are required. The watchlist is stored in a local SQLite file (`watchlist_db_path`).

## Testing

```bash
python -m pytest tests/ -q
```

The suite covers the pure analysis dimensions, the scoring engine (clean vs. rug, score capping), and lore parsing — all without network access.

## Deployment

This project includes `render.yaml` for Render's free web service tier; the same
commands work on any host that can run a Python web process.

- **Runtime:** Python 3.12 (pinned via `PYTHON_VERSION=3.12.8` in `render.yaml`; the
  code targets 3.10+ syntax). No Node toolchain — the frontend is static.
- **Build command:** `pip install -r requirements.txt`
- **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
  (binds all interfaces and honors the platform-provided `$PORT`).
- **Health check:** `GET /health` returns `{"status": "ok", ...}` — dependency-free
  (no DB, no outbound calls). Wired as Render's `healthCheckPath`; point any uptime
  monitor or load balancer at it.
- **Static UI + API, one process:** the API is served under `/api/v1/*` and the static
  frontend is mounted at `/` by the same app, so the UI is **same-origin** with the API
  and needs no CORS for normal use.

### Environment variables

Every setting in `app/core/config.py` has a safe default, so the app boots with **no
required secrets** and **no API keys**. Override via environment (or a local `.env`,
which is git-ignored). Common production overrides:

| Variable | Default | Purpose |
|----------|---------|---------|
| `ENVIRONMENT` | `development` | Set to `production` on deploy (done in `render.yaml`). |
| `LOG_LEVEL` | `INFO` | Root log level. |
| `CORS_ORIGINS` | `["http://localhost:8000", "http://127.0.0.1:8000"]` | Only needed if a **separate-origin** client calls the API. |
| `RPC_URL` | public Robinhood Chain RPC | Override with a private RPC (e.g. Alchemy) for production rate limits. |

Optional background engines: the watchlist refresh loop is on by default
(`WATCHLIST_REFRESH_ENABLED=true`); the monitoring/alert engines are **off by
default** and opt-in via `TOKEN_MONITOR_ENABLED`, `KOL_SCHEDULER_ENABLED`, and
`ALERTS_ENABLED` (see `app/core/config.py` for the full set). The KOL scheduler also
requires browser binaries (`python -m playwright install chromium`); Playwright is
lazy-imported, so the app and test suite run without them.

### Logging & data

Logs stream to stdout (captured by the platform) and to a rotating `logs/app.log`.
Runtime SQLite stores under `data/` are regenerated on startup; both `logs/` and
`data/` are git-ignored.

## Limitations

This is a heuristic screening tool, not financial advice. Holder distribution and clusters are computed from a sampled top-holders page, not the full holder set. Dev history and LP-lock detection rely on public on-chain markers and known registries — absence of evidence is not proof of safety. Insider and smart-wallet labels are behavioral heuristics derived from sampled transfers (early entry, deployer funding, position distribution); free public APIs do not expose trade-level profit, so "smart" is an estimate, not verified ROI. Public APIs can be delayed, incomplete, or rate-limited. Always do your own research before making financial decisions.
