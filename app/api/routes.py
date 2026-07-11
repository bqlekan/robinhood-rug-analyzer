from fastapi import APIRouter

from app.models.token import TokenAnalysisRequest, TokenAnalysisResponse
from app.services.blockchain_detector import detect_blockchain

router = APIRouter(prefix="/api/v1", tags=["tokens"])


@router.post("/analyze", response_model=TokenAnalysisResponse)
async def analyze_token(payload: TokenAnalysisRequest) -> TokenAnalysisResponse:
    """Accept a token address and return architecture-ready stub data."""
    blockchain = detect_blockchain(payload.contract_address)
    return TokenAnalysisResponse(
        contract_address=payload.contract_address,
        detected_blockchain=blockchain,
        status="analysis_not_implemented",
        message="Token analysis is not implemented yet. Project architecture is ready.",
    )
