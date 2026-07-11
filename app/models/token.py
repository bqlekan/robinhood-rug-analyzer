from typing import Any

from pydantic import BaseModel, Field


class TokenAnalysisRequest(BaseModel):
    contract_address: str = Field(..., min_length=3, description="Token contract address")


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


class HoneypotData(BaseModel):
    is_honeypot: bool | None = None
    buy_tax: float | None = None
    sell_tax: float | None = None
    simulation_success: bool | None = None
    raw_summary: dict[str, Any] = Field(default_factory=dict)


class RiskSignal(BaseModel):
    name: str
    severity: str
    points: int
    description: str


class RugAnalysis(BaseModel):
    risk_score: int
    risk_level: str
    signals: list[RiskSignal]
    data_sources: list[str]
    limitations: list[str]


class TokenAnalysisResponse(BaseModel):
    contract_address: str
    detected_blockchain: str
    status: str
    message: str
    market_data: TokenMarketData | None = None
    honeypot_data: HoneypotData | None = None
    analysis: RugAnalysis
