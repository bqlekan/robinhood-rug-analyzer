from fastapi import APIRouter

from app.models.token import TokenAnalysisRequest, TokenAnalysisResponse
from app.services.rug_analyzer import analyze_token_contract

router = APIRouter(prefix="/api/v1", tags=["tokens"])


@router.post("/analyze", response_model=TokenAnalysisResponse)
async def analyze_token(payload: TokenAnalysisRequest) -> TokenAnalysisResponse:
    """Analyze a token address using public web data sources."""
    return await analyze_token_contract(payload.contract_address)
