"""Unit tests for the M22 chain abstraction (`app/core/chains.py`).

The abstraction is purely architectural: exactly one chain (Robinhood) is
registered, and its `ChainConfig` is built live from `settings` so behaviour is
identical to the pre-M22 direct-`settings` reads. These tests pin that contract:
- the active chain mirrors every `settings.*` chain field,
- `active()` re-reads `settings` on every call (env override / monkeypatch flows through),
- lookup by slug works and unknown slugs raise.
"""

import pytest

from app.core import chains
from app.core.config import settings


def test_active_is_robinhood_default():
    c = chains.active()
    assert c.slug == chains.ROBINHOOD
    assert settings.default_chain == chains.ROBINHOOD


def test_active_mirrors_settings_identity_and_endpoints():
    c = chains.active()
    assert c.chain_id == settings.chain_id
    assert c.chain_name == settings.chain_name
    assert c.dexscreener_chain == settings.dexscreener_chain
    assert c.blockscout_base_url == settings.blockscout_base_url
    assert c.rpc_url == settings.rpc_url


def test_active_mirrors_settings_dex_topology():
    c = chains.active()
    assert c.weth_address == settings.honeypot_weth_address
    assert c.v3_factory == settings.honeypot_v3_factory
    assert c.dex_routers == settings.dex_routers
    assert c.quote_assets == settings.honeypot_quote_assets
    assert c.fee_tiers == settings.honeypot_fee_tiers
    assert c.min_quote_reserve == settings.honeypot_min_quote_reserve


def test_active_reflects_settings_live(monkeypatch):
    # The whole point: a monkeypatch/env override on the underlying settings field
    # must flow through, so existing service tests that patch settings still work.
    monkeypatch.setattr(settings, "chain_name", "Test Chain")
    monkeypatch.setattr(settings, "honeypot_weth_address", "0x" + "ab" * 20)
    c = chains.active()
    assert c.chain_name == "Test Chain"
    assert c.weth_address == "0x" + "ab" * 20


def test_get_by_slug():
    assert chains.get(chains.ROBINHOOD).slug == chains.ROBINHOOD


def test_get_unknown_slug_raises():
    with pytest.raises(KeyError):
        chains.get("nope")


def test_active_unknown_default_raises(monkeypatch):
    monkeypatch.setattr(settings, "default_chain", "nope")
    with pytest.raises(KeyError):
        chains.active()
