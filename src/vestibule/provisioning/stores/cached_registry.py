"""CachedIndexRegistry — TTL-cache decorator over any IndexRegistry backend (REQ-011,
Assumption A16).

Mirrors `CachedScenarioStore` (`vestibule.scenario.stores.cached_store`, read directly)
shape and conventions exactly — same `threading.RLock`-guarded
`dict[str, tuple[T, float]]` cache, same `time.monotonic`-based TTL via an injectable
`_now` seam, same whole-cache-clear `invalidate(key)` scope decision — applied to a
different port.

LOAD-BEARING CONSTRAINT: `get()`/`list_by_vertical()` are the only cached reads.
`register()`/`reclaim()` ALWAYS delegate straight to the wrapped registry's real
backing store, every call, and never read from or write into the cache themselves —
only the invalidation they trigger on their own mutating write touches the cache.
Caching any part of the claim path would silently break the exclusivity/CAS guarantees
Assumptions A13/A14 and `docs/designs/REQ-011-lld.md` §3.3/§3.4 depend on: a cached "no
entry exists" (or cached stale) read could let two callers each believe they are first.
Do not "simplify" this by routing `register()`/`reclaim()` through the cache.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from vestibule.provisioning.model import IndexRegistryEntry
from vestibule.provisioning.registry import IndexRegistry

_DEFAULT_TTL_SECONDS = 10.0
# Deliberately well below both the wait loop's poll cadence
# (`wait_poll_interval_seconds`, default 2.0) and its separate overall timeout bound
# (`wait_for_provisioning_timeout_seconds`, default 60.0) — a TTL comparable to or
# larger than the overall timeout bound would risk a waiting worker never observing a
# genuine `provisioning -> ready` transition the live store already reflects,
# producing a spurious INDEX_PROVISION_TIMEOUT on a provision that actually succeeded
# quickly (Assumption A16). Also deliberately smaller than CachedScenarioStore's own
# 60.0-second default for the identical reason.


class CachedIndexRegistry(IndexRegistry):
    """TTL-cache decorator wrapping any other `IndexRegistry` backend.

    `get()`/`list_by_vertical()` are served from cache within `ttl_seconds`; every
    mutating method (`register`/`reclaim`/`mark_ready`/`mark_failed`/`mark_retired`/
    `touch_verified`) always delegates straight to `wrapped`, uncached, then
    invalidates the affected `index_name` on this decorator's own cache
    (Assumption A16 — load-bearing, see this module's own docstring).
    """

    def __init__(
        self,
        wrapped: IndexRegistry,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        _now: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initializes an empty cache over `wrapped`.

        Args:
            wrapped: The backend every cache miss (and every mutating call)
                delegates to.
            ttl_seconds: How long a cached `get`/`list_by_vertical` entry remains
                fresh before the next read re-queries `wrapped`. Defaults to 10
                seconds.
            _now: Internal test-injection seam for the monotonic clock; production
                callers should leave this at its default (`time.monotonic`).
        """
        self._wrapped = wrapped
        self._ttl_seconds = ttl_seconds
        self._now = _now
        self._lock = threading.RLock()
        self._by_name: dict[str, tuple[IndexRegistryEntry | None, float]] = {}
        self._by_vertical: dict[str, tuple[list[IndexRegistryEntry], float]] = {}

    def get(self, index_name: str) -> IndexRegistryEntry | None:
        """See `IndexRegistry.get`. Served from cache within `ttl_seconds`."""
        with self._lock:
            cached = self._by_name.get(index_name)
            if cached is not None and not self._is_expired(cached[1]):
                return cached[0]
        value = self._wrapped.get(index_name)
        with self._lock:
            self._by_name[index_name] = (value, self._now())
        return value

    def list_by_vertical(self, vertical: str) -> list[IndexRegistryEntry]:
        """See `IndexRegistry.list_by_vertical`. Served from cache within `ttl_seconds`."""
        with self._lock:
            cached = self._by_vertical.get(vertical)
            if cached is not None and not self._is_expired(cached[1]):
                return list(cached[0])
        value = self._wrapped.list_by_vertical(vertical)
        with self._lock:
            self._by_vertical[vertical] = (value, self._now())
        return list(value)

    def register(self, entry: IndexRegistryEntry) -> IndexRegistryEntry:
        """See `IndexRegistry.register`.

        NEVER served from or written into the cache (Assumption A16) — always
        delegates straight to `wrapped.register(entry)`. Invalidates
        `entry.index_name` after a successful delegate call, regardless of whether the
        return value indicates this call won or lost — either outcome means the live
        store's answer for this `index_name` may now differ from whatever was
        previously cached.
        """
        result = self._wrapped.register(entry)
        self.invalidate(entry.index_name)
        return result

    def reclaim(
        self,
        index_name: str,
        observed: IndexRegistryEntry,
        new_entry: IndexRegistryEntry,
    ) -> IndexRegistryEntry:
        """See `IndexRegistry.reclaim`.

        NEVER served from or written into the cache (Assumption A16) — always
        delegates straight to `wrapped.reclaim(...)`. Invalidates `index_name` after a
        successful delegate call; a raised `INDEX_PROVISION_CONFLICT` means nothing
        changed on the wrapped store, so the exception propagates before reaching the
        invalidate call — no invalidation runs on that path.
        """
        result = self._wrapped.reclaim(index_name, observed, new_entry)
        self.invalidate(index_name)
        return result

    def touch_verified(self, index_name: str) -> IndexRegistryEntry:
        """See `IndexRegistry.touch_verified`.

        Delegates straight to `wrapped.touch_verified`, then invalidates
        `index_name`. Required so a `get()` immediately afterward reflects the
        just-bumped `last_verified_at` rather than a stale cached snapshot — without
        this, a `CachedIndexRegistry`-served `get()` within the TTL window would keep
        returning the pre-touch entry, causing a spurious extra `describe_index` call
        on every subsequent `ensure()` within the stale cache window (Assumption A16).
        """
        result = self._wrapped.touch_verified(index_name)
        self.invalidate(index_name)
        return result

    def mark_ready(
        self, index_name: str, *, expected_claim_token: str | None
    ) -> IndexRegistryEntry:
        """See `IndexRegistry.mark_ready`.

        Delegates straight to `wrapped.mark_ready`, then invalidates `index_name` on
        success. A raised `INDEX_PROVISION_CONFLICT` propagates before the invalidate
        call — nothing changed on the wrapped store to invalidate for.
        """
        result = self._wrapped.mark_ready(
            index_name, expected_claim_token=expected_claim_token
        )
        self.invalidate(index_name)
        return result

    def mark_failed(
        self, index_name: str, reason: str, *, expected_claim_token: str | None
    ) -> IndexRegistryEntry:
        """See `IndexRegistry.mark_failed`. Same conflict-propagates-before-invalidate
        behavior as `mark_ready`."""
        result = self._wrapped.mark_failed(
            index_name, reason, expected_claim_token=expected_claim_token
        )
        self.invalidate(index_name)
        return result

    def mark_retired(self, index_name: str) -> IndexRegistryEntry:
        """See `IndexRegistry.mark_retired`. Delegates then invalidates `index_name`."""
        result = self._wrapped.mark_retired(index_name)
        self.invalidate(index_name)
        return result

    def invalidate(self, index_name: str) -> None:
        """Forces the next `get`/`list_by_vertical` read of anything to re-query
        `wrapped`.

        Mirrors `CachedScenarioStore.invalidate`'s whole-cache-clear-regardless-of-key
        design, not a per-key evict: a per-`index_name` reverse index into
        `list_by_vertical`'s own cached rows would need `index_name -> vertical`
        bookkeeping with its own edge cases, for a store expected to hold, in
        practice, one row per provisioned index. Evicting other indexes' cheap cached
        entries this way is still far cheaper than it looks — each costs only one
        ~15-40ms Table Storage point-read to repopulate, well under the ~100-300ms
        `describe_index` calls this invalidation exists to help avoid for the touched
        index.

        Args:
            index_name: Accepted for the documented call shape
                (`invalidate(index_name)`) and for a future more-surgical
                implementation; this implementation clears the entire cache regardless
                of which `index_name` is passed.
        """
        del index_name  # Whole-cache clear by design — see this method's docstring.
        with self._lock:
            self._by_name.clear()
            self._by_vertical.clear()

    def _is_expired(self, cached_at: float) -> bool:
        """Whether a cache entry stamped at `cached_at` has outlived `ttl_seconds`."""
        return (self._now() - cached_at) >= self._ttl_seconds
