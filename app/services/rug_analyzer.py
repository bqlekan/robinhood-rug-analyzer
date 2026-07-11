from __future__ import annotations

from app.models.token import (
    HoneypotData,
    LiquiditySnapshot,
    PriceChangeSnapshot,
    RiskSignal,
    RugAnalysis,
    TokenAnalysisResponse,
    TokenMarketData,
    VolumeSnapshot,
)
from app.services.blockchain_detector import detect_blockchain
from app.services.dexscreener_client import choose_best_pair, fetch_token_pairs
from app.services.honeypot_client import fetch_honeypot_data


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_market_data(pair: dict | None) -> TokenMarketData | None:
    if not pair:
        return None

    base_token = pair.get("baseToken") or {}
    quote_token = pair.get("quoteToken") or {}
    liquidity = pair.get("liquidity") or {}
    volume = pair.get("volume") or {}
    price_change = pair.get("priceChange") or {}

    return TokenMarketData(
        chain_id=pair.get("chainId"),
        dex_id=pair.get("dexId"),
        pair_address=pair.get("pairAddress"),
        base_token_name=base_token.get("name"),
        base_token_symbol=base_token.get("symbol"),
        quote_token_symbol=quote_token.get("symbol"),
        price_usd=pair.get("priceUsd"),
        market_cap=_to_float(pair.get("marketCap")),
        fdv=_to_float(pair.get("fdv")),
        liquidity=LiquiditySnapshot(
            usd=_to_float(liquidity.get("usd")),
            base=_to_float(liquidity.get("base")),
            quote=_to_float(liquidity.get("quote")),
        ),
        volume=VolumeSnapshot(
            h24=_to_float(volume.get("h24")),
            h6=_to_float(volume.get("h6")),
            h1=_to_float(volume.get("h1")),
            m5=_to_float(volume.get("m5")),
        ),
        price_change=PriceChangeSnapshot(
            h24=_to_float(price_change.get("h24")),
            h6=_to_float(price_change.get("h6")),
            h1=_to_float(price_change.get("h1")),
            m5=_to_float(price_change.get("m5")),
        ),
        pair_created_at=pair.get("pairCreatedAt"),
        url=pair.get("url"),
    )


def _build_honeypot_data(payload: dict | None) -> HoneypotData | None:
    if not payload:
        return None

    simulation = payload.get("simulationResult") or {}
    summary = {
        "token": payload.get("token"),
        "pair": payload.get("pair"),
        "flags": payload.get("flags"),
    }

    return HoneypotData(
        is_honeypot=payload.get("honeypotResult", {}).get("isHoneypot"),
        buy_tax=_to_float(simulation.get("buyTax")),
        sell_tax=_to_float(simulation.get("sellTax")),
        simulation_success=bool(simulation) if simulation is not None else None,
        raw_summary={key: value for key, value in summary.items() if value is not None},
    )


def _add_signal(signals: list[RiskSignal], name: str, severity: str, points: int, description: str) -> None:
    signals.append(RiskSignal(name=name, severity=severity, points=points, description=description))


def _score_level(score: int) -> str:
    if score >= 75:
        return "critical"
    if score >= 50:
        return "high"
    if score >= 25:
        return "medium"
    return "low"


def _analyze(market_data: TokenMarketData | None, honeypot_data: HoneypotData | None, source_count: int) -> RugAnalysis:
    signals: list[RiskSignal] = []

    if not market_data:
        _add_signal(
            signals,
            "No market pair found",
            "high",
            35,
            "DexScreener did not return active liquidity pairs for this address.",
        )
    else:
        liquidity_usd = market_data.liquidity.usd if market_data.liquidity else None
        volume_h24 = market_data.volume.h24 if market_data.volume else None
        price_change_h24 = market_data.price_change.h24 if market_data.price_change else None

        if liquidity_usd is None:
            _add_signal(signals, "Missing liquidity", "medium", 20, "Liquidity data is unavailable.")
        elif liquidity_usd < 5_000:
            _add_signal(signals, "Very low liquidity", "high", 30, "USD liquidity is below $5,000, making exits risky.")
        elif liquidity_usd < 25_000:
            _add_signal(signals, "Low liquidity", "medium", 15, "USD liquidity is below $25,000.")

        if volume_h24 is None or volume_h24 < 1_000:
            _add_signal(signals, "Low trading activity", "medium", 15, "24h trading volume is missing or below $1,000.")

        if price_change_h24 is not None and price_change_h24 <= -50:
            _add_signal(signals, "Severe 24h drawdown", "high", 20, "Price is down more than 50% over 24h.")
        elif price_change_h24 is not None and price_change_h24 >= 200:
            _add_signal(signals, "Extreme 24h pump", "medium", 15, "Price is up more than 200% over 24h, which can indicate unstable hype.")

    if honeypot_data is None:
        _add_signal(signals, "Honeypot check unavailable", "medium", 15, "No honeypot simulation result was available for this token/chain.")
    else:
        if honeypot_data.is_honeypot is True:
            _add_signal(signals, "Honeypot detected", "critical", 70, "The token appears unsellable according to Honeypot.is simulation.")
        if honeypot_data.sell_tax is not None and honeypot_data.sell_tax >= 20:
            _add_signal(signals, "High sell tax", "high", 30, "Sell tax is 20% or higher.")
        elif honeypot_data.sell_tax is not None and honeypot_data.sell_tax >= 10:
            _add_signal(signals, "Elevated sell tax", "medium", 15, "Sell tax is 10% or higher.")

    if source_count < 2:
        _add_signal(signals, "Limited source coverage", "low", 5, "Risk score is based on limited public data sources.")

    score = min(sum(signal.points for signal in signals), 100)
    return RugAnalysis(
        risk_score=score,
        risk_level=_score_level(score),
        signals=signals,
        data_sources=["DexScreener", "Honeypot.is"],
        limitations=[
            "This is a heuristic risk screen, not financial advice.",
            "Ownership, holder distribution, LP lock status, and verified source code checks require chain-specific explorers/API keys.",
            "Public APIs can be delayed, incomplete, rate-limited, or unavailable.",
        ],
    )


async def analyze_token_contract(contract_address: str) -> TokenAnalysisResponse:
    normalized = contract_address.strip()
    detected_blockchain = detect_blockchain(normalized)

    pairs = await fetch_token_pairs(normalized)
    best_pair = choose_best_pair(pairs)
    market_data = _build_market_data(best_pair)

    chain_id = market_data.chain_id if market_data else detected_blockchain
    honeypot_payload = await fetch_honeypot_data(normalized, chain_id)
    honeypot_data = _build_honeypot_data(honeypot_payload)

    source_count = int(market_data is not None) + int(honeypot_data is not None)
    analysis = _analyze(market_data, honeypot_data, source_count)

    if market_data and market_data.chain_id:
        detected_blockchain = market_data.chain_id

    return TokenAnalysisResponse(
        contract_address=normalized,
        detected_blockchain=detected_blockchain,
        status="analysis_completed",
        message="Rug-risk heuristic analysis completed using public web data sources.",
        market_data=market_data,
        honeypot_data=honeypot_data,
        analysis=analysis,
    )
