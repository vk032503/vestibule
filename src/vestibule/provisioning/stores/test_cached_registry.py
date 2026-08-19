"""Unit tests for CachedIndexRegistry (REQ-011, Assumption A16).

Run against a `_CountingRegistry`-shaped test double, mirroring
`vestibule.scenario.stores.test_cached_store`'s own `_CountingStore`/`_FakeClock`
convention.
"""

from __future__ import annotations

import inspect
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from vestibule.provisioning.conftest import build_index_registry_entry
from vestibule.provisioning.model import (
    INDEX_PROVISION_CONFLICT,
    IndexRegistryEntry,
    ProvisioningError,
)
from vestibule.provisioning.registry import IndexRegistry
from vestibule.provisioning.stores.cached_registry import CachedIndexRegistry


class _FakeClock:
    """Deterministic, manually-advanced stand-in for `time.monotonic`."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _CountingRegistry(IndexRegistry):
    """Wraps a fixed set of entries, counting calls to each method."""

    def __init__(self, entries: dict[str, IndexRegistryEntry]) -> None:
        self._entries = entries
        self.get_calls = 0
        self.list_by_vertical_calls = 0
        self.register_calls = 0
        self.reclaim_calls = 0
        self.touch_verified_calls = 0
        self.mark_ready_calls = 0
        self.mark_failed_calls = 0
        self.mark_retired_calls = 0

    def get(self, index_name: str) -> IndexRegistryEntry | None:
        self.get_calls += 1
        return self._entries.get(index_name)

    def list_by_vertical(self, vertical: str) -> list[IndexRegistryEntry]:
        self.list_by_vertical_calls += 1
        return [e for e in self._entries.values() if e.vertical == vertical]

    def register(self, entry: IndexRegistryEntry) -> IndexRegistryEntry:
        self.register_calls += 1
        existing = self._entries.get(entry.index_name)
        if existing is not None:
            return existing
        self._entries[entry.index_name] = entry
        return entry

    def reclaim(
        self,
        index_name: str,
        observed: IndexRegistryEntry,
        new_entry: IndexRegistryEntry,
    ) -> IndexRegistryEntry:
        self.reclaim_calls += 1
        if self._entries.get(index_name) != observed:
            raise ProvisioningError(
                index_name, "conflict", error_code=INDEX_PROVISION_CONFLICT
            )
        self._entries[index_name] = new_entry
        return new_entry

    def touch_verified(self, index_name: str) -> IndexRegistryEntry:
        self.touch_verified_calls += 1
        current = self._entries[index_name]
        updated = current.model_copy(
            update={"last_verified_at": datetime.now(timezone.utc)}
        )
        self._entries[index_name] = updated
        return updated

    def mark_ready(
        self, index_name: str, *, expected_claim_token: str | None
    ) -> IndexRegistryEntry:
        self.mark_ready_calls += 1
        current = self._entries[index_name]
        if (
            expected_claim_token is not None
            and current.claim_token != expected_claim_token
        ):
            raise ProvisioningError(
                index_name, "conflict", error_code=INDEX_PROVISION_CONFLICT
            )
        updated = current.model_copy(update={"status": "ready", "claim_token": None})
        self._entries[index_name] = updated
        return updated

    def mark_failed(
        self, index_name: str, reason: str, *, expected_claim_token: str | None
    ) -> IndexRegistryEntry:
        self.mark_failed_calls += 1
        current = self._entries[index_name]
        if (
            expected_claim_token is not None
            and current.claim_token != expected_claim_token
        ):
            raise ProvisioningError(
                index_name, "conflict", error_code=INDEX_PROVISION_CONFLICT
            )
        updated = current.model_copy(
            update={
                "status": "failed",
                "last_error_message": reason,
                "claim_token": None,
            }
        )
        self._entries[index_name] = updated
        return updated

    def mark_retired(self, index_name: str) -> IndexRegistryEntry:
        self.mark_retired_calls += 1
        updated = self._entries[index_name].model_copy(update={"status": "retired"})
        self._entries[index_name] = updated
        return updated


# --- get()/list_by_vertical() cached within TTL -------------------------------------------------


def test_get_serves_from_cache_within_ttl_wrapped_get_called_once() -> None:
    entry = build_index_registry_entry()
    wrapped = _CountingRegistry({entry.index_name: entry})
    cached = CachedIndexRegistry(wrapped, ttl_seconds=60, _now=_FakeClock())

    first = cached.get(entry.index_name)
    second = cached.get(entry.index_name)

    assert first == second == entry
    assert wrapped.get_calls == 1


def test_list_by_vertical_serves_from_cache_within_ttl() -> None:
    entry = build_index_registry_entry()
    wrapped = _CountingRegistry({entry.index_name: entry})
    cached = CachedIndexRegistry(wrapped, ttl_seconds=60, _now=_FakeClock())

    cached.list_by_vertical("hr")
    cached.list_by_vertical("hr")

    assert wrapped.list_by_vertical_calls == 1


def test_get_re_reads_wrapped_registry_after_ttl_elapses() -> None:
    entry = build_index_registry_entry()
    wrapped = _CountingRegistry({entry.index_name: entry})
    clock = _FakeClock()
    cached = CachedIndexRegistry(wrapped, ttl_seconds=1, _now=clock)

    cached.get(entry.index_name)
    cached.get(entry.index_name)
    assert wrapped.get_calls == 1

    clock.advance(1.0)
    cached.get(entry.index_name)
    assert wrapped.get_calls == 2


def test_get_still_cached_just_under_ttl() -> None:
    entry = build_index_registry_entry()
    wrapped = _CountingRegistry({entry.index_name: entry})
    clock = _FakeClock()
    cached = CachedIndexRegistry(wrapped, ttl_seconds=1, _now=clock)

    cached.get(entry.index_name)
    clock.advance(0.99)
    cached.get(entry.index_name)

    assert wrapped.get_calls == 1


# --- claim-path cache-bypass rule (Assumption A16, load-bearing) --------------------------------


def test_register_never_served_from_or_populates_cache_even_when_cache_seeded_with_stale_wrong_answer() -> (
    None
):
    entry = build_index_registry_entry()
    wrapped = _CountingRegistry({})
    cached = CachedIndexRegistry(wrapped, ttl_seconds=60, _now=_FakeClock())

    # Prime the cache with a stale "absent" answer.
    assert cached.get(entry.index_name) is None
    assert wrapped.get_calls == 1

    # Seed the wrapped registry out from under the cache, simulating another
    # process's already-landed write.
    wrapped._entries[entry.index_name] = entry

    candidate = build_index_registry_entry(claim_token="a-different-fresh-token")
    result = cached.register(candidate)

    assert wrapped.register_calls == 1
    assert result == entry  # the wrapped registry's real answer, not the stale cache
    assert result.claim_token != candidate.claim_token  # candidate did not win


def test_reclaim_never_served_from_or_populates_cache_even_when_cache_seeded_with_stale_wrong_answer() -> (
    None
):
    entry = build_index_registry_entry(claim_token="t1")
    wrapped = _CountingRegistry({entry.index_name: entry})
    cached = CachedIndexRegistry(wrapped, ttl_seconds=60, _now=_FakeClock())

    # Prime the cache with the pre-change snapshot.
    stale_snapshot = cached.get(entry.index_name)
    assert stale_snapshot == entry

    # A mark_ready runs directly against wrapped, bypassing the cache entirely.
    wrapped.mark_ready(entry.index_name, expected_claim_token="t1")

    with pytest.raises(ProvisioningError) as exc_info:
        cached.reclaim(
            entry.index_name,
            observed=stale_snapshot,  # the cached, now-stale snapshot
            new_entry=entry.model_copy(update={"claim_token": "t2"}),
        )
    assert exc_info.value.error_code == INDEX_PROVISION_CONFLICT
    assert wrapped.reclaim_calls == 1


# --- successful claim-path writes invalidate -----------------------------------------------------


def test_register_invalidates_cache_so_subsequent_get_reflects_the_new_claim() -> None:
    entry = build_index_registry_entry()
    wrapped = _CountingRegistry({})
    cached = CachedIndexRegistry(wrapped, ttl_seconds=60, _now=_FakeClock())

    assert cached.get(entry.index_name) is None  # cached miss

    cached.register(entry)

    assert cached.get(entry.index_name) == entry
    assert wrapped.get_calls == 2  # not served from the stale cached miss


def test_mark_ready_invalidates_cache_so_subsequent_get_reflects_ready_status() -> None:
    entry = build_index_registry_entry(claim_token="t1")
    wrapped = _CountingRegistry({entry.index_name: entry})
    cached = CachedIndexRegistry(wrapped, ttl_seconds=60, _now=_FakeClock())

    cached.get(entry.index_name)  # cache the provisioning snapshot
    cached.mark_ready(entry.index_name, expected_claim_token="t1")

    refreshed = cached.get(entry.index_name)
    assert refreshed is not None
    assert refreshed.status == "ready"


def test_mark_failed_invalidates_cache() -> None:
    entry = build_index_registry_entry(claim_token="t1")
    wrapped = _CountingRegistry({entry.index_name: entry})
    cached = CachedIndexRegistry(wrapped, ttl_seconds=60, _now=_FakeClock())

    cached.get(entry.index_name)
    cached.mark_failed(entry.index_name, "boom", expected_claim_token="t1")

    refreshed = cached.get(entry.index_name)
    assert refreshed is not None
    assert refreshed.status == "failed"


def test_mark_retired_invalidates_cache() -> None:
    entry = build_index_registry_entry(
        status="ready", claim_token=None, claimed_at=None
    )
    wrapped = _CountingRegistry({entry.index_name: entry})
    cached = CachedIndexRegistry(wrapped, ttl_seconds=60, _now=_FakeClock())

    cached.get(entry.index_name)
    cached.mark_retired(entry.index_name)

    refreshed = cached.get(entry.index_name)
    assert refreshed is not None
    assert refreshed.status == "retired"


def test_touch_verified_invalidates_cache_so_subsequent_get_reflects_bumped_last_verified_at() -> (
    None
):
    entry = build_index_registry_entry(
        status="ready", claim_token=None, claimed_at=None, last_verified_at=None
    )
    wrapped = _CountingRegistry({entry.index_name: entry})
    cached = CachedIndexRegistry(wrapped, ttl_seconds=60, _now=_FakeClock())

    cached.get(entry.index_name)  # caches last_verified_at=None
    cached.touch_verified(entry.index_name)

    refreshed = cached.get(entry.index_name)
    assert refreshed is not None
    assert refreshed.last_verified_at is not None


# --- conflict-path writes do NOT invalidate (symmetry, design-review MINOR) ---------------------


def test_reclaim_conflict_does_not_invalidate_cache() -> None:
    entry = build_index_registry_entry(claim_token="t1")
    wrapped = _CountingRegistry({entry.index_name: entry})
    cached = CachedIndexRegistry(wrapped, ttl_seconds=60, _now=_FakeClock())

    cached.get(entry.index_name)
    assert wrapped.get_calls == 1

    with pytest.raises(ProvisioningError):
        cached.reclaim(
            entry.index_name,
            observed=entry.model_copy(update={"claim_token": "wrong"}),
            new_entry=entry.model_copy(update={"claim_token": "t2"}),
        )

    # A conflicting reclaim changed nothing on the wrapped store — the cache must
    # still be serving the pre-conflict snapshot, not re-querying wrapped.
    cached.get(entry.index_name)
    assert wrapped.get_calls == 1


def test_mark_ready_conflict_does_not_invalidate_cache() -> None:
    entry = build_index_registry_entry(claim_token="t1")
    wrapped = _CountingRegistry({entry.index_name: entry})
    cached = CachedIndexRegistry(wrapped, ttl_seconds=60, _now=_FakeClock())

    cached.get(entry.index_name)
    assert wrapped.get_calls == 1

    with pytest.raises(ProvisioningError):
        cached.mark_ready(entry.index_name, expected_claim_token="wrong-token")

    cached.get(entry.index_name)
    assert wrapped.get_calls == 1


def test_mark_failed_conflict_does_not_invalidate_cache() -> None:
    entry = build_index_registry_entry(claim_token="t1")
    wrapped = _CountingRegistry({entry.index_name: entry})
    cached = CachedIndexRegistry(wrapped, ttl_seconds=60, _now=_FakeClock())

    cached.get(entry.index_name)
    assert wrapped.get_calls == 1

    with pytest.raises(ProvisioningError):
        cached.mark_failed(entry.index_name, "boom", expected_claim_token="wrong-token")

    cached.get(entry.index_name)
    assert wrapped.get_calls == 1


# --- delegation --------------------------------------------------------------------------------


def test_invalidate_clears_whole_cache_regardless_of_key() -> None:
    a = build_index_registry_entry(index_name="a")
    b = build_index_registry_entry(index_name="b", claim_token="t2")
    wrapped = _CountingRegistry({"a": a, "b": b})
    cached = CachedIndexRegistry(wrapped, ttl_seconds=60, _now=_FakeClock())

    cached.get("a")
    cached.get("b")
    assert wrapped.get_calls == 2

    cached.invalidate("a")  # any key clears everything (whole-cache-clear design)

    cached.get("a")
    cached.get("b")
    assert wrapped.get_calls == 4


# --- config/code sync ----------------------------------------------------------------------


def test_config_index_provisioning_yaml_registry_cache_tunables_match_cached_index_registry_defaults() -> (
    None
):
    config_path = (
        Path(__file__).resolve().parents[4] / "config" / "index_provisioning.yaml"
    )
    text = config_path.read_text(encoding="utf-8")
    default_ttl = (
        inspect.signature(CachedIndexRegistry.__init__)
        .parameters["ttl_seconds"]
        .default
    )

    match = re.search(r"ttl_seconds:\s*(\d+)", text)
    assert match is not None
    assert int(match.group(1)) == default_ttl
    assert "enabled: true" in text
