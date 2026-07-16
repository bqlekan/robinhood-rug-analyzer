"""Contract-address validation at the trust boundary (no network)."""

import pytest
from pydantic import ValidationError

from app.models.token import TokenAnalysisRequest, is_valid_address

VALID = "0x" + "a" * 40


def test_is_valid_address_accepts_canonical():
    assert is_valid_address(VALID)
    assert is_valid_address("0x" + "0123456789abcdefABCDEF" + "0" * 18)


def test_is_valid_address_rejects_junk():
    assert not is_valid_address(None)
    assert not is_valid_address("")
    assert not is_valid_address("0x123")               # too short
    assert not is_valid_address("a" * 42)              # no 0x prefix
    assert not is_valid_address("0x" + "z" * 40)       # non-hex
    assert not is_valid_address("0x" + "a" * 41)       # too long


def test_is_valid_address_strips_whitespace():
    assert is_valid_address(f"  {VALID}  ")


def test_request_model_accepts_valid_and_strips():
    req = TokenAnalysisRequest(contract_address=f"  {VALID}  ")
    assert req.contract_address == VALID


def test_request_model_rejects_invalid():
    with pytest.raises(ValidationError):
        TokenAnalysisRequest(contract_address="not-an-address")
