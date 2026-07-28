"""Shared test fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _clear_deep_cache():
    """Clear the module-level deep analysis cache between tests."""
    from app.services.rug_analyzer import _deep_cache
    _deep_cache._store.clear()
