from __future__ import annotations

"""Small, reusable in-process TTL cache for near-static external reads.

Only immutable/near-static data should go in here (verified contract source,
contract creation facts). Freshness-sensitive data — market liquidity/price,
holder metrics, transfers — must NOT be cached so scoring always sees live data.

Design notes:
- Bounded: `max_size` with oldest-first eviction, so the cache cannot grow
  unboundedly during a long-running scan process.
- Expiry on read: an entry past its TTL is deleted and reported as a miss; no
  background sweeper thread needed for a single-loop async process.
- Time is injectable (`time_fn`) so tests drive expiry deterministically without
  sleeping. Defaults to `time.monotonic` to be immune to wall-clock jumps.
"""

import time
from typing import Any, Awaitable, Callable

# Sentinel so a cached falsy value is still distinguishable from a miss.
MISS = object()


class TTLCache:
    def __init__(
        self,
        *,
        ttl: float = 300.0,
        max_size: int = 512,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._max_size = max(1, max_size)
        self._now = time_fn
        # key -> (value, expiry_ts). Insertion order = age order (dict preserves it).
        self._store: dict[str, tuple[Any, float]] = {}

    def get(self, key: str) -> Any:
        """Return the cached value, or MISS if absent/expired (expired entries are dropped)."""
        entry = self._store.get(key)
        if entry is None:
            return MISS
        value, expiry = entry
        if self._now() >= expiry:
            del self._store[key]
            return MISS
        return value

    def set(self, key: str, value: Any) -> None:
        # Refresh in place if present; otherwise evict oldest when at capacity.
        if key not in self._store and len(self._store) >= self._max_size:
            oldest = next(iter(self._store))
            del self._store[oldest]
        self._store[key] = (value, self._now() + self._ttl)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


async def cached_call(cache: TTLCache, key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
    """Return cached value on hit; otherwise await `factory` and cache a successful result.

    A `None` result (the clients' signal for a failed/empty fetch) is NOT cached,
    so a transient failure never poisons the cache and is retried next call.
    """
    hit = cache.get(key)
    if hit is not MISS:
        return hit
    value = await factory()
    if value is not None:
        cache.set(key, value)
    return value
