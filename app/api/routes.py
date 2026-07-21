from fastapi import APIRouter, HTTPException

from app.core.config import settings
from app.models.token import (
    ScanRequest,
    ScanResponse,
    TokenAnalysisRequest,
    TokenAnalysisResponse,
    WatchlistEntry,
    WatchlistResponse,
)
from app.services import snapshot_store, watchlist_store
from app.services.rug_analyzer import analyze_token_contract, scan_and_rank
from app.services.wallet_intel import refresh_watchlisted

router = APIRouter(prefix="/api/v1", tags=["tokens"])

_WATCHLIST_NOTE = (
    "Smart-wallet scores are heuristic estimates from free on-chain behavior, "
    "not verified ROI. Free public APIs do not expose trade-level profit data."
)


@router.get("/chain")
async def chain_info() -> dict:
    """Expose the single chain this analyzer targets."""
    return {
        "chain_name": settings.chain_name,
        "chain_id": settings.chain_id,
        "explorer": settings.blockscout_base_url,
        "dexscreener_chain": settings.dexscreener_chain,
    }


@router.post("/analyze", response_model=TokenAnalysisResponse)
async def analyze_token(payload: TokenAnalysisRequest) -> TokenAnalysisResponse:
    """Full rug-risk analysis of a single Robinhood Chain token."""
    return await analyze_token_contract(payload.contract_address, include_lore=payload.include_lore)


@router.post("/scan", response_model=ScanResponse)
async def scan_tokens(payload: ScanRequest) -> ScanResponse:
    """Scan active Robinhood Chain tokens and return a risk-ranked list."""
    return await scan_and_rank(payload.limit, include_lore=payload.include_lore)


@router.get("/watchlist", response_model=WatchlistResponse)
async def get_watchlist(kind: str | None = None, sort: str = "score") -> WatchlistResponse:
    """Return flagged smart and insider wallets with what they've been buying.

    M21: `kind` filters to "smart" or "insider" (omit for both); `sort` is "score"
    (default) or "recency". Each wallet carries its cross-token `prior_tokens` count.
    """
    kind = kind if kind in ("smart", "insider") else None
    smart = watchlist_store.get_watchlist(kind="smart", sort=sort) if kind in (None, "smart") else []
    insider = watchlist_store.get_watchlist(kind="insider", sort=sort) if kind in (None, "insider") else []
    return WatchlistResponse(
        smart_wallets=smart,
        insider_wallets=insider,
        note=_WATCHLIST_NOTE,
    )


@router.post("/watchlist/refresh")
async def refresh_watchlist() -> dict:
    """On-request watchlist refresh (M21).

    A fallback for idle-prone hosts (e.g. Render free tier) where the background
    `_watchlist_refresh_loop` may be suspended: re-pulls recent buys for a bounded
    batch of the oldest-refreshed wallets. Bounded by `watchlist_refresh_batch`.
    """
    refreshed = await refresh_watchlisted(settings.watchlist_refresh_batch)
    return {"refreshed": refreshed}


@router.get("/wallet/{address}", response_model=WatchlistEntry)
async def get_wallet(address: str) -> WatchlistEntry:
    """Detail for a single tracked wallet: its flag, score, and recent buys."""
    entry = watchlist_store.get_wallet(address)
    if not entry:
        raise HTTPException(status_code=404, detail="Wallet not found in watchlist.")
    return entry


@router.get("/history/{address}")
async def get_token_history(address: str, limit: int = 50) -> dict:
    """Stored analysis snapshots for a token, newest first (M19 trend history)."""
    return {
        "contract_address": address,
        "snapshots": snapshot_store.list_snapshots(address, limit=limit),
    }
