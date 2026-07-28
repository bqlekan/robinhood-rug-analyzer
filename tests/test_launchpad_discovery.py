"""Unit tests for the plugin-based launchpad discovery engine.

Covers: strategy dispatch, all three built-in strategies, disabled definitions,
unknown mode handling, checkpoint resume, and engine extensibility (add a
config-only launchpad with a custom strategy and no code changes).
"""

import asyncio

import pytest

from app.models.token import LaunchpadDefinition
from app.services import launchpad_discovery, rpc_client, blockscout_client, rpc_checkpoint


def _run(coro):
    return asyncio.run(coro)


def _make_defn(**overrides):
    base = {
        "name": "Test",
        "enabled": True,
        "discovery_mode": "event",
        "factory_address": "0x" + "ab" * 20,
        "topic0": "0x" + "cd" * 32,
        "token_index": 0,
        "start_block": 100,
        "confidence": "high",
    }
    base.update(overrides)
    return LaunchpadDefinition(**base)


# ---------------------------------------------------------------------------
# EventLogDiscovery
# ---------------------------------------------------------------------------

class TestEventLogDiscovery:
    def test_extracts_token_from_topics(self, monkeypatch):
        token_a = "0x" + "a1" * 20
        token_b = "0x" + "b2" * 20

        async def fake_chunked(**kwargs):
            return [
                {"topics": ["0xtopic0", "0x" + "00" * 12 + token_a[2:]]},
                {"topics": ["0xtopic0", "0x" + "00" * 12 + token_b[2:]]},
            ]

        monkeypatch.setattr(rpc_client, "get_logs_chunked", fake_chunked)
        monkeypatch.setattr(rpc_checkpoint, "load_checkpoint", lambda k: None)
        monkeypatch.setattr(rpc_checkpoint, "save_checkpoint", lambda k, b: None)

        defn = _make_defn(name="TestPad")
        strategy = launchpad_discovery.EventLogDiscovery()
        result = _run(strategy.discover(defn))

        assert len(result) == 2
        assert result[0]["address"] == token_a
        assert result[1]["address"] == token_b
        assert result[0]["source"] == "launchpad:TestPad"

    def test_resumes_from_checkpoint(self, monkeypatch):
        captured_from = None

        async def fake_chunked(**kwargs):
            nonlocal captured_from
            captured_from = kwargs.get("from_block")
            return []

        monkeypatch.setattr(rpc_client, "get_logs_chunked", fake_chunked)
        monkeypatch.setattr(rpc_checkpoint, "load_checkpoint", lambda k: 5000)
        monkeypatch.setattr(rpc_checkpoint, "save_checkpoint", lambda k, b: None)

        defn = _make_defn(start_block=100)
        strategy = launchpad_discovery.EventLogDiscovery()
        _run(strategy.discover(defn))

        assert captured_from == 5001  # saved block + 1

    def test_missing_topic0_returns_empty(self, monkeypatch):
        defn = _make_defn(topic0=None)
        strategy = launchpad_discovery.EventLogDiscovery()
        result = _run(strategy.discover(defn))
        assert result == []

    def test_token_index_offset(self, monkeypatch):
        """token_index=1 means the token address is in topics[2]."""
        token = "0x" + "cc" * 20

        async def fake_chunked(**kwargs):
            return [
                {"topics": ["0xtopic0", "0xother", "0x" + "00" * 12 + token[2:]]},
            ]

        monkeypatch.setattr(rpc_client, "get_logs_chunked", fake_chunked)
        monkeypatch.setattr(rpc_checkpoint, "load_checkpoint", lambda k: None)
        monkeypatch.setattr(rpc_checkpoint, "save_checkpoint", lambda k, b: None)

        defn = _make_defn(token_index=1)
        strategy = launchpad_discovery.EventLogDiscovery()
        result = _run(strategy.discover(defn))
        assert result[0]["address"] == token


# ---------------------------------------------------------------------------
# FactoryTransactionDiscovery
# ---------------------------------------------------------------------------

class TestFactoryTransactionDiscovery:
    def test_extracts_created_contract(self, monkeypatch):
        addr = "0x" + "ff" * 20

        async def fake_txs(address, pages=1):
            return [
                {"created_contract": {"hash": addr}},
                {"created_contract": None},
                {"to": "0xother"},
            ]

        monkeypatch.setattr(blockscout_client, "get_address_transactions_paged", fake_txs)

        defn = _make_defn(discovery_mode="factory_scan", name="FacPad")
        strategy = launchpad_discovery.FactoryTransactionDiscovery()
        result = _run(strategy.discover(defn))

        assert len(result) == 1
        assert result[0]["address"] == addr
        assert result[0]["source"] == "launchpad:FacPad"

    def test_missing_factory_returns_empty(self, monkeypatch):
        defn = _make_defn(discovery_mode="factory_scan", factory_address=None)
        strategy = launchpad_discovery.FactoryTransactionDiscovery()
        result = _run(strategy.discover(defn))
        assert result == []


# ---------------------------------------------------------------------------
# ContractCreationDiscovery
# ---------------------------------------------------------------------------

class TestContractCreationDiscovery:
    def test_extracts_created_contract(self, monkeypatch):
        addr = "0x" + "dd" * 20

        async def fake_txs(address, pages=1):
            return [
                {"created_contract": {"hash": addr}},
            ]

        monkeypatch.setattr(blockscout_client, "get_address_transactions_paged", fake_txs)

        defn = _make_defn(
            discovery_mode="contract_creation_scan",
            deployer_address="0x" + "ee" * 20,
            name="CrePad",
        )
        strategy = launchpad_discovery.ContractCreationDiscovery()
        result = _run(strategy.discover(defn))

        assert len(result) == 1
        assert result[0]["address"] == addr
        assert result[0]["source"] == "launchpad:CrePad"

    def test_falls_back_to_factory_address(self, monkeypatch):
        """When deployer_address is None, uses factory_address."""
        captured_addr = None

        async def fake_txs(address, pages=1):
            nonlocal captured_addr
            captured_addr = address
            return []

        monkeypatch.setattr(blockscout_client, "get_address_transactions_paged", fake_txs)

        factory = "0x" + "ab" * 20
        defn = _make_defn(
            discovery_mode="contract_creation_scan",
            deployer_address=None,
            factory_address=factory,
        )
        strategy = launchpad_discovery.ContractCreationDiscovery()
        _run(strategy.discover(defn))
        assert captured_addr == factory


# ---------------------------------------------------------------------------
# Engine — discover_all
# ---------------------------------------------------------------------------

class TestDiscoverAll:
    def test_disabled_definition_skipped(self, monkeypatch):
        call_count = 0
        orig_discover = launchpad_discovery.EventLogDiscovery.discover

        async def counting_discover(self, defn):
            nonlocal call_count
            call_count += 1
            return [{"address": "0x" + "11" * 20, "source": f"launchpad:{defn.name}"}]

        monkeypatch.setattr(launchpad_discovery.EventLogDiscovery, "discover", counting_discover)
        monkeypatch.setattr(rpc_checkpoint, "load_checkpoint", lambda k: None)
        monkeypatch.setattr(rpc_checkpoint, "save_checkpoint", lambda k, b: None)

        defs = [
            _make_defn(name="Enabled", enabled=True),
            _make_defn(name="Disabled", enabled=False),
        ]
        result = _run(launchpad_discovery.discover_all(defs))
        assert call_count == 1
        assert result[0]["source"] == "launchpad:Enabled"

    def test_unknown_mode_logs_warning_and_skips(self, monkeypatch):
        defs = [_make_defn(discovery_mode="bogus_mode")]
        result = _run(launchpad_discovery.discover_all(defs))
        assert result == []

    def test_engine_dispatches_custom_strategy(self, monkeypatch):
        """Register a custom strategy under a novel mode, add a config-only
        definition using it, and verify the engine dispatches to it —
        no code changes to the engine needed."""

        class CustomStrategy:
            mode = "custom_test"

            async def discover(self, defn):
                return [{"address": "0x" + "99" * 20, "source": f"launchpad:{defn.name}"}]

        # Register and clean up after
        launchpad_discovery.register_strategy(CustomStrategy())

        defn = _make_defn(discovery_mode="custom_test", name="CustomPad")
        result = _run(launchpad_discovery.discover_all([defn]))

        assert len(result) == 1
        assert result[0]["source"] == "launchpad:CustomPad"

        # Cleanup
        del launchpad_discovery._STRATEGIES["custom_test"]

    def test_strategy_exception_doesnt_crash_engine(self, monkeypatch):
        """If one strategy raises, others still complete."""

        async def failing_discover(self, defn):
            raise RuntimeError("oops")

        monkeypatch.setattr(launchpad_discovery.EventLogDiscovery, "discover", failing_discover)

        async def ok_discover(self, defn):
            return [{"address": "0x" + "22" * 20, "source": f"launchpad:{defn.name}"}]

        monkeypatch.setattr(launchpad_discovery.ContractCreationDiscovery, "discover", ok_discover)

        defs = [
            _make_defn(name="Broken", discovery_mode="event"),
            _make_defn(name="Working", discovery_mode="contract_creation_scan",
                       deployer_address="0x" + "33" * 20),
        ]
        result = _run(launchpad_discovery.discover_all(defs))
        assert len(result) == 1
        assert result[0]["source"] == "launchpad:Working"

    def test_multiple_launchpads_concurrent(self, monkeypatch):
        """All enabled definitions run concurrently and results are merged."""

        async def fake_discover(self, defn):
            return [{"address": "0x" + defn.name[-2:].encode().hex()[:40].ljust(40, "0"),
                      "source": f"launchpad:{defn.name}"}]

        monkeypatch.setattr(launchpad_discovery.ContractCreationDiscovery, "discover", fake_discover)

        defs = [
            _make_defn(name="Pad_A", discovery_mode="contract_creation_scan",
                       deployer_address="0x" + "a1" * 20),
            _make_defn(name="Pad_B", discovery_mode="contract_creation_scan",
                       deployer_address="0x" + "b2" * 20),
        ]
        result = _run(launchpad_discovery.discover_all(defs))
        assert len(result) == 2
        sources = {r["source"] for r in result}
        assert "launchpad:Pad_A" in sources
        assert "launchpad:Pad_B" in sources

    def test_empty_definitions_returns_empty(self):
        result = _run(launchpad_discovery.discover_all([]))
        assert result == []
