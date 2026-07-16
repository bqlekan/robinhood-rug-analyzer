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
