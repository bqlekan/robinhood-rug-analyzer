from pydantic import BaseModel, Field


class TokenAnalysisRequest(BaseModel):
    contract_address: str = Field(..., min_length=3, description="Token contract address")


class TokenAnalysisResponse(BaseModel):
    contract_address: str
    detected_blockchain: str
    status: str
    message: str
