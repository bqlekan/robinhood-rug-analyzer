"""Unit tests for the contract-source inference (no network)."""

from app.services import contract_intel


def _payload(source: str, additional=None, verified=True, name="MyToken", compiler="v0.8.20"):
    return {
        "is_verified": verified,
        "source_code": source,
        "additional_sources": [{"file_path": p} for p in (additional or [])],
        "name": name,
        "compiler_version": compiler,
        "language": "solidity",
    }


def test_openzeppelin_erc20_template_and_no_protocol():
    src = 'pragma solidity ^0.8.0;\nimport "@openzeppelin/contracts/token/ERC20/ERC20.sol";\ncontract T is ERC20 {}'
    intel = contract_intel.infer_from_contract(_payload(src))
    assert intel.verified is True
    assert intel.template == "OpenZeppelin ERC20"
    assert intel.protocol is None
    assert intel.contract_name == "MyToken"


def test_chainlink_ccip_bridge_token_flags_protocol():
    src = 'import {IBurnMintERC20} from "@chainlink/contracts/src/v0.8/shared/token/IBurnMintERC20.sol";'
    intel = contract_intel.infer_from_contract(
        _payload(src, additional=["node_modules/@chainlink/contracts/src/v0.8/CCIPReceiver.sol"])
    )
    assert intel.protocol == "Chainlink CCIP"
    assert intel.protocol_confidence == "high"


def test_uniswap_v2_core_recognized():
    src = 'import "@uniswap/v2-core/contracts/UniswapV2Pair.sol";'
    intel = contract_intel.infer_from_contract(_payload(src))
    assert intel.template == "Uniswap V2 core"
    assert intel.protocol == "Uniswap V2"


def test_unverified_contract_returns_unknown():
    intel = contract_intel.infer_from_contract({"is_verified": False, "source_code": ""})
    assert intel.verified is False
    assert intel.template == "unknown"
    assert intel.protocol is None


def test_custom_verified_contract_reports_bespoke():
    src = 'pragma solidity ^0.8.0;\ncontract T { function foo() public {} }'
    intel = contract_intel.infer_from_contract(_payload(src))
    assert intel.template == "custom"
    assert intel.protocol is None
    assert "bespoke" in (intel.detail or "").lower()


def test_missing_payload_returns_unverified():
    intel = contract_intel.infer_from_contract(None)
    assert intel.verified is False
    assert "not available" in (intel.detail or "").lower()
