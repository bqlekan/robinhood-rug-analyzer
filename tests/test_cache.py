"""Unit tests for the TTL cache (no network)."""

import asyncio

from app.services.cache import MISS, TTLCache, cached_call


class _Clock:
    """Manually-advanced clock so expiry is deterministic without sleeping."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_hit_within_ttl():
    clock = _Clock()
    cache = TTLCache(ttl=300.0, time_fn=clock)
    cache.set("k", {"v": 1})
    clock.advance(299.0)
    assert cache.get("k") == {"v": 1}


def test_miss_after_ttl():
    clock = _Clock()
    cache = TTLCache(ttl=300.0, time_fn=clock)
    cache.set("k", {"v": 1})
    clock.advance(300.0)  # expiry is inclusive (now >= expiry)
    assert cache.get("k") is MISS
    # Expired entry is dropped, not just hidden.
    assert len(cache) == 0


def test_miss_when_absent():
    cache = TTLCache()
    assert cache.get("nope") is MISS


def test_falsy_value_is_distinguishable_from_miss():
    cache = TTLCache()
    cache.set("empty", [])
    assert cache.get("empty") == []  # not MISS


def test_bounded_eviction_drops_oldest():
    cache = TTLCache(max_size=2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)  # evicts "a" (oldest)
    assert cache.get("a") is MISS
    assert cache.get("b") == 2
    assert cache.get("c") == 3
    assert len(cache) == 2


def test_set_existing_key_does_not_evict():
    cache = TTLCache(max_size=2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("a", 99)  # refresh in place, must not evict "b"
    assert cache.get("a") == 99
    assert cache.get("b") == 2


def test_clear():
    cache = TTLCache()
    cache.set("a", 1)
    cache.clear()
    assert cache.get("a") is MISS
    assert len(cache) == 0


def test_cached_call_hit_avoids_second_fetch():
    cache = TTLCache()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return {"data": "x"}

    async def run():
        first = await cached_call(cache, "k", factory)
        second = await cached_call(cache, "k", factory)
        return first, second

    first, second = asyncio.run(run())
    assert first == {"data": "x"}
    assert second == {"data": "x"}
    assert calls["n"] == 1  # second call served from cache


def test_cached_call_does_not_cache_none():
    cache = TTLCache()
    calls = {"n": 0}

    async def failing_factory():
        calls["n"] += 1
        return None  # clients return None on failed/empty fetch

    async def run():
        await cached_call(cache, "k", failing_factory)
        await cached_call(cache, "k", failing_factory)

    asyncio.run(run())
    assert calls["n"] == 2  # None never cached, so it retries
    assert len(cache) == 0


def test_cached_call_refetches_after_expiry():
    clock = _Clock()
    cache = TTLCache(ttl=300.0, time_fn=clock)
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return calls["n"]

    async def run():
        a = await cached_call(cache, "k", factory)
        clock.advance(300.0)
        b = await cached_call(cache, "k", factory)
        return a, b

    a, b = asyncio.run(run())
    assert a == 1
    assert b == 2  # expired -> refetched
    assert calls["n"] == 2
