"""CachedScenarioStore — TTL-cache decorator over any ScenarioStore backend (REQ-010).

Avoids a store read per document while keeping runtime edits reasonably fresh (story:
"Avoids a store read per document while keeping runtime edits reasonably fresh"). No
direct precedent elsewhere in this codebase — thread-safe via one `threading.RLock`
guarding every cache dict, held for the full duration of each public method, the same
coarse-grained, correct-under-concurrency pattern as
`vestibule.ledger.store.InMemoryLedgerStore`/`vestibule.indexer.adapters.in_memory
.InMemoryIndexer` (relevant here since multiple ingestion workers may read through one
shared `CachedScenarioStore` instance).

Design decision (`invalidate` scope): `invalidate(scenario_id)` clears this store's
*entire* cache, not just the one `scenario_id` entry. A more surgical implementation
would need a `scenario_id -> vertical` reverse index to also evict the matching
`get_by_vertical`/`list_all` entries, and that bookkeeping itself has an edge case (a
scenario's `vertical` can change between the cached read and the write being
invalidated). Clearing everything is simple, always correct, and cheap — this store
holds at most a few dozen scenarios in practice (story budget: "small by nature").
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from vestibule.scenario.model import Scenario
from vestibule.scenario.stores.base import ScenarioStore

_DEFAULT_TTL_SECONDS = 60.0


class CachedScenarioStore(ScenarioStore):
    """TTL-cache decorator wrapping any other `ScenarioStore` backend."""

    def __init__(
        self,
        wrapped: ScenarioStore,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        _now: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initializes an empty cache over `wrapped`.

        Args:
            wrapped: The backend every cache miss delegates to.
            ttl_seconds: How long a cached entry remains fresh before the next read
                re-queries `wrapped`. Defaults to 60 seconds.
            _now: Internal test-injection seam for the monotonic clock; production
                callers should leave this at its default (`time.monotonic`).
        """
        self._wrapped = wrapped
        self._ttl_seconds = ttl_seconds
        self._now = _now
        self._lock = threading.RLock()
        self._by_id: dict[str, tuple[Scenario | None, float]] = {}
        self._by_vertical: dict[str, tuple[Scenario | None, float]] = {}
        self._list_cache: dict[bool, tuple[list[Scenario], float]] = {}

    @property
    def is_writable(self) -> bool:
        """See `ScenarioStore.is_writable`. Delegates to the wrapped backend."""
        return self._wrapped.is_writable

    def get(self, scenario_id: str) -> Scenario | None:
        """See `ScenarioStore.get`. Served from cache within `ttl_seconds`."""
        with self._lock:
            cached = self._by_id.get(scenario_id)
            if cached is not None and not self._is_expired(cached[1]):
                return cached[0]
        value = self._wrapped.get(scenario_id)
        with self._lock:
            self._by_id[scenario_id] = (value, self._now())
        return value

    def get_by_vertical(self, vertical: str) -> Scenario | None:
        """See `ScenarioStore.get_by_vertical`. Served from cache within `ttl_seconds`."""
        with self._lock:
            cached = self._by_vertical.get(vertical)
            if cached is not None and not self._is_expired(cached[1]):
                return cached[0]
        value = self._wrapped.get_by_vertical(vertical)
        with self._lock:
            self._by_vertical[vertical] = (value, self._now())
        return value

    def list_all(self, include_disabled: bool = False) -> list[Scenario]:
        """See `ScenarioStore.list_all`. Served from cache within `ttl_seconds`."""
        with self._lock:
            cached = self._list_cache.get(include_disabled)
            if cached is not None and not self._is_expired(cached[1]):
                return list(cached[0])
        value = self._wrapped.list_all(include_disabled)
        with self._lock:
            self._list_cache[include_disabled] = (value, self._now())
        return list(value)

    def upsert(self, scenario: Scenario) -> Scenario:
        """See `ScenarioStore.upsert`. Invalidates the whole cache after a successful write."""
        result = self._wrapped.upsert(scenario)
        self.invalidate(result.scenario_id)
        return result

    def delete(self, scenario_id: str) -> bool:
        """See `ScenarioStore.delete`. Invalidates the whole cache after the call."""
        result = self._wrapped.delete(scenario_id)
        self.invalidate(scenario_id)
        return result

    def invalidate(self, scenario_id: str) -> None:
        """Forces the next read of anything to re-query the wrapped backend.

        Args:
            scenario_id: Accepted for the story's documented call shape
                (`invalidate(scenario_id)`) and for a future more-surgical
                implementation; this implementation clears the entire cache regardless
                of which `scenario_id` is passed — see this module's docstring.
        """
        del scenario_id  # See this module's docstring: whole-cache clear by design.
        with self._lock:
            self._by_id.clear()
            self._by_vertical.clear()
            self._list_cache.clear()

    def _is_expired(self, cached_at: float) -> bool:
        """Whether a cache entry stamped at `cached_at` has outlived `ttl_seconds`."""
        return (self._now() - cached_at) >= self._ttl_seconds
