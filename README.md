# Robinhood Rug Analyzer

FastAPI web app for screening token contract addresses against public web data sources and returning a transparent heuristic rug-risk score.

## What It Does

- Accepts a token contract address from the frontend or API.
- Detects the likely blockchain from address format and DexScreener pair metadata.
- Fetches public market data from DexScreener.
- Fetches EVM honeypot simulation data from Honeypot.is when available.
- Calculates a risk score from visible signals such as missing liquidity, low liquidity, low volume, large drawdowns, extreme pumps, honeypot status, and high sell tax.
- Shows limitations clearly so users know this is a screening tool, not financial advice.

## Project Structure

```text
robinhood-rug-analyzer/
+-- app/
|   +-- api/                 # FastAPI route handlers
|   +-- core/                # Settings and logging setup
|   +-- models/              # Pydantic request/response schemas
|   +-- services/            # Blockchain detection, data clients, rug analyzer
|   +-- main.py              # FastAPI app entrypoint
+-- frontend/                # Static HTML/CSS/JavaScript UI
+-- logs/                    # Runtime logs
+-- .env.example             # Example API key/config values
+-- .gitignore
+-- README.md
+-- render.yaml              # Render deployment config
+-- requirements.txt
```

## Data Sources

- DexScreener public API for token pair, price, liquidity, volume, and price-change data.
- Honeypot.is public API for EVM honeypot and tax simulation where supported.

The app does not scrape private pages or bypass access controls. It uses public web APIs designed for programmatic access.

## Local Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Then open `http://127.0.0.1:8000`.

## API Usage

```text
POST /api/v1/analyze
```

Example body:

```json
{
  "contract_address": "0x0000000000000000000000000000000000000000"
}
```

Example response fields:

- `detected_blockchain` - likely chain from DexScreener or address format.
- `market_data` - best liquidity pair data from DexScreener.
- `honeypot_data` - honeypot and tax simulation fields when available.
- `analysis.risk_score` - 0 to 100 heuristic score.
- `analysis.risk_level` - `low`, `medium`, `high`, or `critical`.
- `analysis.signals` - explainable risk signals that contributed to the score.

## Environment Variables

Copy or edit `.env.example` for local `.env` values:

```env
APP_NAME="Robinhood Rug Analyzer"
ENVIRONMENT="development"
LOG_LEVEL="INFO"
ETHERSCAN_API_KEY=""
BSCSCAN_API_KEY=""
POLYGONSCAN_API_KEY=""
```

Explorer API keys are reserved for future deeper checks such as verified source code, ownership, holder concentration, and LP lock analysis.

## Render Deployment

This project includes `render.yaml` for Render's free web service tier.

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

## Limitations

This is an early heuristic screening tool. It does not guarantee that a token is safe or unsafe. Public APIs can be delayed, unavailable, incomplete, or rate-limited. Always perform manual due diligence before making financial decisions.
