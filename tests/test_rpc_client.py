"""Unit tests for the JSON-RPC client (M10 deliverable A). No real network.

A fake httpx client returns canned JSON-RPC bodies so the real `_rpc` parsing
runs: result extraction, error-object -> None, transport failure -> None.
"""

import asyncio

import httpx
import pytest

from app.services import rpc_client


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _FakeClient:
    """Records the last POST and returns a canned body (or raises)."""

    def __init__(self, *, body=None, raises=None):
        self._body = body
        self._raises = raises
        self.calls = []

    async def post(self, url, json=None):
        self.calls.append((url, json))
        if self._raises is not None:
            raise self._raises
        return _FakeResponse(self._body)


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    # Exercise the RPC path directly, without the TTL cache in the way.
    monkeypatch.setattr(rpc_client.settings, "http_cache_enabled", False)


def _use_client(monkeypatch, client):
    monkeypatch.setattr(rpc_client, "get_client", lambda: client)
    return client


def test_result_field_returned(monkeypatch):
    client = _use_client(monkeypatch, _FakeClient(body={"jsonrpc": "2.0", "id": 1, "result": "0xabc"}))
    out = asyncio.run(rpc_client.eth_call("0xto", "0xdata"))
    assert out == "0xabc"
    # Payload shape is correct JSON-RPC.
    _, sent = client.calls[0]
    assert sent["method"] == "eth_call"
    assert sent["params"] == [{"to": "0xto", "data": "0xdata"}, "latest"]


def test_eth_call_omits_override_param_by_default(monkeypatch):
    client = _use_client(monkeypatch, _FakeClient(body={"result": "0x"}))
    asyncio.run(rpc_client.eth_call("0xto", "0xdata"))
    _, sent = client.calls[0]
    assert sent["params"] == [{"to": "0xto", "data": "0xdata"}, "latest"]  # no 3rd param


def test_eth_call_appends_state_override(monkeypatch):
    client = _use_client(monkeypatch, _FakeClient(body={"result": "0x2a"}))
    override = {"0xdead": {"code": "0x602a"}}
    asyncio.run(rpc_client.eth_call("0xto", "0xdata", state_override=override))
    _, sent = client.calls[0]
    assert sent["params"] == [{"to": "0xto", "data": "0xdata"}, "latest", override]


def test_rpc_error_object_degrades_to_none(monkeypatch):
    _use_client(monkeypatch, _FakeClient(body={"jsonrpc": "2.0", "id": 1,
                                               "error": {"code": -32000, "message": "execution reverted"}}))
    assert asyncio.run(rpc_client.eth_call("0xto", "0xdata")) is None


def test_transport_failure_degrades_to_none(monkeypatch):
    _use_client(monkeypatch, _FakeClient(raises=httpx.ConnectError("boom")))
    assert asyncio.run(rpc_client.get_transaction_by_hash("0xdead")) is None


def test_missing_result_is_none(monkeypatch):
    # A well-formed body with neither result nor error (e.g. unknown tx) -> None.
    _use_client(monkeypatch, _FakeClient(body={"jsonrpc": "2.0", "id": 1, "result": None}))
    assert asyncio.run(rpc_client.get_transaction_receipt("0xmissing")) is None


# ---------------------------------------------------------------------------
# eth_getLogs
# ---------------------------------------------------------------------------

class TestGetLogs:
    def test_returns_list_on_success(self, monkeypatch):
        logs = [{"topics": ["0xabc"], "data": "0x00"}]
        _use_client(monkeypatch, _FakeClient(body={"jsonrpc": "2.0", "id": 1, "result": logs}))
        result = asyncio.run(rpc_client.get_logs(
            address="0xfactory", topics=["0xabc"], from_block=100, to_block=200,
        ))
        assert result == logs

    def test_returns_empty_on_rpc_error(self, monkeypatch):
        _use_client(monkeypatch, _FakeClient(body={"jsonrpc": "2.0", "id": 1,
                                                    "error": {"code": -32000, "message": "timeout"}}))
        result = asyncio.run(rpc_client.get_logs(address="0xfactory", topics=["0xabc"]))
        assert result == []

    def test_filter_params_correct(self, monkeypatch):
        client = _use_client(monkeypatch, _FakeClient(body={"result": []}))
        asyncio.run(rpc_client.get_logs(
            address="0xfactory", topics=["0xtopic0", None], from_block=10, to_block=20,
        ))
        _, sent = client.calls[0]
        filt = sent["params"][0]
        assert filt["address"] == "0xfactory"
        assert filt["topics"] == ["0xtopic0", None]
        assert filt["fromBlock"] == hex(10)
        assert filt["toBlock"] == hex(20)

    def test_omits_address_and_topics_when_none(self, monkeypatch):
        client = _use_client(monkeypatch, _FakeClient(body={"result": []}))
        asyncio.run(rpc_client.get_logs(from_block=0, to_block="latest"))
        _, sent = client.calls[0]
        filt = sent["params"][0]
        assert "address" not in filt
        assert "topics" not in filt


# ---------------------------------------------------------------------------
# eth_getLogs chunked
# ---------------------------------------------------------------------------

class TestGetLogsChunked:
    def test_splits_range_into_chunks(self, monkeypatch):
        call_count = 0

        async def fake_get_logs(address=None, topics=None, from_block=0, to_block="latest"):
            nonlocal call_count
            call_count += 1
            return [{"topics": ["0xt"], "blockNumber": hex(from_block)}]

        monkeypatch.setattr(rpc_client, "get_logs", fake_get_logs)
        # Block number resolution: fake eth_blockNumber
        _use_client(monkeypatch, _FakeClient(body={"result": hex(5000)}))

        result = asyncio.run(rpc_client.get_logs_chunked(
            address="0xfactory", topics=["0xt"],
            from_block=0, to_block=4999,
            chunk_size=1000, max_chunks=10,
        ))
        assert call_count == 5  # 0-999, 1000-1999, 2000-2999, 3000-3999, 4000-4999
        assert len(result) == 5

    def test_respects_max_chunks(self, monkeypatch):
        call_count = 0

        async def fake_get_logs(address=None, topics=None, from_block=0, to_block="latest"):
            nonlocal call_count
            call_count += 1
            return []

        monkeypatch.setattr(rpc_client, "get_logs", fake_get_logs)

        asyncio.run(rpc_client.get_logs_chunked(
            address="0xfactory", topics=["0xt"],
            from_block=0, to_block=100_000,
            chunk_size=2000, max_chunks=3,
        ))
        assert call_count == 3

    def test_invokes_checkpoint_cb(self, monkeypatch):
        checkpoints = []

        async def fake_get_logs(address=None, topics=None, from_block=0, to_block="latest"):
            return []

        monkeypatch.setattr(rpc_client, "get_logs", fake_get_logs)

        asyncio.run(rpc_client.get_logs_chunked(
            address="0xfactory", topics=["0xt"],
            from_block=0, to_block=3999,
            chunk_size=2000, max_chunks=10,
            checkpoint_cb=lambda blk: checkpoints.append(blk),
        ))
        assert checkpoints == [1999, 3999]

    def test_retries_transient_failure(self, monkeypatch):
        attempt = 0

        async def fake_get_logs(address=None, topics=None, from_block=0, to_block="latest"):
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                raise ConnectionError("transient")
            return [{"topics": ["0xt"]}]

        monkeypatch.setattr(rpc_client, "get_logs", fake_get_logs)
        # Shorten retry sleep
        async def _noop_sleep(_):
            pass

        monkeypatch.setattr(rpc_client.asyncio, "sleep", _noop_sleep)

        result = asyncio.run(rpc_client.get_logs_chunked(
            address="0xfactory", topics=["0xt"],
            from_block=0, to_block=100,
            chunk_size=200, max_chunks=1, retries=2,
        ))
        assert len(result) == 1
        assert attempt == 2


# ---------------------------------------------------------------------------
# Startup diagnostics
# ---------------------------------------------------------------------------

class TestCheckRpc:
    def test_reports_chain_id_block_and_provider(self, monkeypatch):
        monkeypatch.setattr(
            rpc_client.settings, "rpc_url", "https://rpc.mainnet.chain.robinhood.com"
        )

        async def fake_rpc(method, params):
            return {"eth_chainId": hex(4663), "eth_blockNumber": hex(9_000_000)}[method]

        monkeypatch.setattr(rpc_client, "_rpc", fake_rpc)
        out = asyncio.run(rpc_client.check_rpc())
        assert out["chain_id"] == 4663
        assert out["block"] == 9_000_000
        # Provider is the hostname only — an API key in the path must never leak.
        assert out["provider"] == "rpc.mainnet.chain.robinhood.com"

    def test_provider_is_hostname_only_for_alchemy_url(self, monkeypatch):
        monkeypatch.setattr(
            rpc_client.settings, "rpc_url",
            "https://robinhood-mainnet.g.alchemy.com/v2/SECRET_KEY",
        )

        async def fake_rpc(method, params):
            return hex(1)

        monkeypatch.setattr(rpc_client, "_rpc", fake_rpc)
        out = asyncio.run(rpc_client.check_rpc())
        assert out["provider"] == "robinhood-mainnet.g.alchemy.com"
        assert "SECRET_KEY" not in str(out)

    def test_degrades_to_error_keys_on_failure(self, monkeypatch):
        async def fake_rpc(method, params):
            return None

        monkeypatch.setattr(rpc_client, "_rpc", fake_rpc)
        out = asyncio.run(rpc_client.check_rpc())
        assert "chain_id" not in out and "block" not in out
        assert out["chain_id_error"] and out["block_error"]
