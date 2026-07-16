"""M10-C: creation-evidence retrieval prefers RPC, falls back to Blockscout.

The two sources shape fields differently; these tests pin that the helper
normalizes each into the same (factory_to, topics) tuple the registry matcher
consumes, and that RPC is preferred when it returns anything usable.
"""

import asyncio

from app.services import rug_analyzer


def _run(coro):
    return asyncio.run(coro)


def test_prefers_rpc_and_skips_blockscout(monkeypatch):
    # RPC tx `to` is a plain hex string; receipt logs live under `logs`.
    async def rpc_tx(_):
        return {"to": "0xFACTORY", "hash": "0xtx"}

    async def rpc_receipt(_):
        return {"logs": [{"topics": ["0xTOPIC", ""]}, {"topics": None}]}

    bs_called = {"n": 0}

    async def bs_tx(_):
        bs_called["n"] += 1
        return {"to": {"hash": "0xWRONG"}}

    monkeypatch.setattr(rug_analyzer.rpc_client, "get_transaction_by_hash", rpc_tx)
    monkeypatch.setattr(rug_analyzer.rpc_client, "get_transaction_receipt", rpc_receipt)
    monkeypatch.setattr(rug_analyzer.blockscout_client, "get_transaction", bs_tx)

    factory, topics = _run(rug_analyzer._fetch_creation_evidence("0xtx"))
    assert factory == "0xFACTORY"
    assert topics == ["0xTOPIC"]  # empties dropped
    assert bs_called["n"] == 0  # Blockscout never touched when RPC yields data


def test_falls_back_to_blockscout_when_rpc_empty(monkeypatch):
    async def none(_):
        return None

    # Blockscout tx `to` is a nested {"hash": ...}; logs come from a separate call.
    async def bs_tx(_):
        return {"to": {"hash": "0xBSFACTORY"}}

    async def bs_logs(_):
        return [{"topics": ["0xBSTOPIC"]}]

    monkeypatch.setattr(rug_analyzer.rpc_client, "get_transaction_by_hash", none)
    monkeypatch.setattr(rug_analyzer.rpc_client, "get_transaction_receipt", none)
    monkeypatch.setattr(rug_analyzer.blockscout_client, "get_transaction", bs_tx)
    monkeypatch.setattr(rug_analyzer.blockscout_client, "get_transaction_logs", bs_logs)

    factory, topics = _run(rug_analyzer._fetch_creation_evidence("0xtx"))
    assert factory == "0xBSFACTORY"
    assert topics == ["0xBSTOPIC"]
