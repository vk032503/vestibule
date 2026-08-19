"""Unit tests for `InMemoryIndexRegistry` (REQ-011).

Concurrency tests use real `threading.Thread`s against `InMemoryIndexRegistry`
directly (per `docs/designs/REQ-011-lld.md` §3.5's "correctly simulate... via real
threading races" mandate — never a hand-scripted call order).
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from vestibule.provisioning.conftest import FIXED_TIME, build_index_registry_entry
from vestibule.provisioning.model import (
    INDEX_PROVISION_CONFLICT,
    IndexRegistryEntry,
    ProvisioningError,
)
from vestibule.provisioning.registry import InMemoryIndexRegistry

# --- get / list_by_vertical --------------------------------------------------------------------


def test_get_returns_none_for_unknown_index_name() -> None:
    registry = InMemoryIndexRegistry()
    assert registry.get("unknown") is None


def test_list_by_vertical_returns_only_matching_entries() -> None:
    registry = InMemoryIndexRegistry()
    registry.register(build_index_registry_entry(index_name="hr-idx", vertical="hr"))
    registry.register(
        build_index_registry_entry(
            index_name="legal-idx", vertical="legal", claim_token="t2"
        )
    )
    assert [e.index_name for e in registry.list_by_vertical("hr")] == ["hr-idx"]


# --- register() (AC3's non-concurrent contract) ------------------------------------------------


def test_register_on_absent_index_name_inserts_and_returns_matching_claim_token() -> (
    None
):
    registry = InMemoryIndexRegistry()
    candidate = build_index_registry_entry(claim_token="fresh-token")
    won = registry.register(candidate)
    assert won.claim_token == "fresh-token"
    assert registry.get(candidate.index_name) == candidate


def test_register_on_existing_index_name_returns_stored_entry_unconditionally_no_validation() -> (
    None
):
    registry = InMemoryIndexRegistry()
    first = build_index_registry_entry(claim_token="t1", dimensions=1024)
    registry.register(first)

    # register() never validates settings-equality — a wildly different candidate for
    # the same index_name still just loses, unconditionally.
    second = build_index_registry_entry(claim_token="t2", dimensions=99999)
    result = registry.register(second)

    assert result == first
    assert result.claim_token == "t1"


# --- touch_verified / mark_ready / mark_failed / mark_retired ---------------------------------


def test_touch_verified_bumps_last_verified_at() -> None:
    registry = InMemoryIndexRegistry()
    entry = build_index_registry_entry(
        status="ready", claim_token=None, claimed_at=None
    )
    registry.register(entry)
    updated = registry.touch_verified(entry.index_name)
    assert updated.last_verified_at is not None


def test_touch_verified_unknown_index_name_raises_conflict() -> None:
    registry = InMemoryIndexRegistry()
    with pytest.raises(ProvisioningError) as exc_info:
        registry.touch_verified("unknown")
    assert exc_info.value.error_code == INDEX_PROVISION_CONFLICT


def test_mark_ready_then_mark_failed_are_mutually_exclusive_terminal_states() -> None:
    registry = InMemoryIndexRegistry()
    entry = build_index_registry_entry(claim_token="t1")
    registry.register(entry)

    ready = registry.mark_ready(entry.index_name, expected_claim_token="t1")
    assert ready.status == "ready"
    assert ready.claim_token is None
    assert ready.claimed_at is None
    assert ready.resolved_template is None

    # Once ready (claim_token cleared), a mark_failed call using the old claim_token
    # is rejected — the two finalization outcomes are mutually exclusive.
    with pytest.raises(ProvisioningError) as exc_info:
        registry.mark_failed(entry.index_name, "boom", expected_claim_token="t1")
    assert exc_info.value.error_code == INDEX_PROVISION_CONFLICT
    assert registry.get(entry.index_name).status == "ready"  # type: ignore[union-attr]


def test_mark_failed_records_reason_and_clears_claim_fields() -> None:
    registry = InMemoryIndexRegistry()
    entry = build_index_registry_entry(claim_token="t1")
    registry.register(entry)

    failed = registry.mark_failed(entry.index_name, "boom", expected_claim_token="t1")

    assert failed.status == "failed"
    assert failed.last_error_message == "boom"
    assert failed.claim_token is None
    assert failed.claimed_at is None
    assert failed.resolved_template is None


def test_mark_ready_expected_claim_token_none_skips_check() -> None:
    registry = InMemoryIndexRegistry()
    entry = build_index_registry_entry(claim_token="t1")
    registry.register(entry)
    ready = registry.mark_ready(entry.index_name, expected_claim_token=None)
    assert ready.status == "ready"


def test_mark_retired_flips_status() -> None:
    registry = InMemoryIndexRegistry()
    entry = build_index_registry_entry(
        status="ready", claim_token=None, claimed_at=None
    )
    registry.register(entry)
    retired = registry.mark_retired(entry.index_name)
    assert retired.status == "retired"


def test_mark_retired_unknown_index_name_raises_conflict() -> None:
    registry = InMemoryIndexRegistry()
    with pytest.raises(ProvisioningError) as exc_info:
        registry.mark_retired("unknown")
    assert exc_info.value.error_code == INDEX_PROVISION_CONFLICT


# --- reclaim() sequential races -----------------------------------------------------------------
#
# Staleness itself (whether a claim is old enough to be reclaimable at all) is an
# `IndexProvisioner`-level decision (`ProvisioningConfig.provisioning_stale_after_seconds`
# has no analog on `IndexRegistry`) — see `test_provisioner.py`'s
# `test_ensure_stale_provisioning_reclaimed_by_waiter_fresh_one_times_out` (AC4) for
# that parametrized behavior. `reclaim()` itself is a pure CAS primitive: it succeeds
# iff the stored entry still equals `observed`, regardless of age.


def test_reclaim_succeeds_only_against_the_exact_observed_stale_entry() -> None:
    """Race A (§3.4 step 3): a second reclaimer using the same originally-observed
    snapshot loses once the first reclaim has already landed."""
    registry = InMemoryIndexRegistry()
    stale = build_index_registry_entry(
        claim_token="t1", claimed_at=FIXED_TIME - timedelta(seconds=301)
    )
    registry.register(stale)

    winner = registry.reclaim(
        stale.index_name,
        observed=stale,
        new_entry=stale.model_copy(update={"claim_token": "t2"}),
    )
    assert winner.claim_token == "t2"

    with pytest.raises(ProvisioningError) as exc_info:
        registry.reclaim(
            stale.index_name,
            observed=stale,  # the same, now-stale snapshot the winner also observed
            new_entry=stale.model_copy(update={"claim_token": "t3"}),
        )
    assert exc_info.value.error_code == INDEX_PROVISION_CONFLICT
    assert registry.get(stale.index_name).claim_token == "t2"  # type: ignore[union-attr]


def test_reclaim_fails_if_original_owner_finished_first() -> None:
    """Race B, sub-ordering 4a (§3.4 step 4a): mark_ready lands before a reclaim
    attempt against the pre-mark_ready snapshot."""
    registry = InMemoryIndexRegistry()
    entry = build_index_registry_entry(claim_token="t1")
    registry.register(entry)

    ready = registry.mark_ready(entry.index_name, expected_claim_token="t1")
    assert ready.status == "ready"

    with pytest.raises(ProvisioningError) as exc_info:
        registry.reclaim(
            entry.index_name,
            observed=entry,  # pre-mark_ready snapshot
            new_entry=entry.model_copy(update={"claim_token": "t2"}),
        )
    assert exc_info.value.error_code == INDEX_PROVISION_CONFLICT
    current = registry.get(entry.index_name)
    assert current is not None
    assert current.status == "ready"


def test_mark_ready_fails_if_reclaimed_first() -> None:
    """Race B, sub-ordering 4b (§3.4 step 4b) — the task's third named race: a
    reclaim's write lands first, and the original (unaware) claim holder's own
    mark_ready is rejected rather than clobbering the reclaimer's fresh claim."""
    registry = InMemoryIndexRegistry()
    entry = build_index_registry_entry(claim_token="t1")
    registry.register(entry)

    reclaimed = registry.reclaim(
        entry.index_name,
        observed=entry,
        new_entry=entry.model_copy(update={"claim_token": "t2"}),
    )
    assert reclaimed.claim_token == "t2"

    with pytest.raises(ProvisioningError) as exc_info:
        registry.mark_ready(entry.index_name, expected_claim_token="t1")
    assert exc_info.value.error_code == INDEX_PROVISION_CONFLICT
    current = registry.get(entry.index_name)
    assert current is not None
    assert current.claim_token == "t2"  # never clobbered


def test_mark_failed_fails_if_reclaimed_first() -> None:
    """Race B, sub-ordering 4b (§3.4 step 4b) — mark_failed's mirror of
    test_mark_ready_fails_if_reclaimed_first: a reclaim's write lands first, and the
    original (unaware/zombie) claim holder's own mark_failed is rejected rather than
    clobbering the reclaimer's fresh claim."""
    registry = InMemoryIndexRegistry()
    entry = build_index_registry_entry(claim_token="t1")
    registry.register(entry)

    reclaimed = registry.reclaim(
        entry.index_name,
        observed=entry,
        new_entry=entry.model_copy(update={"claim_token": "t2"}),
    )
    assert reclaimed.claim_token == "t2"

    with pytest.raises(ProvisioningError) as exc_info:
        registry.mark_failed(
            entry.index_name, "zombie failure", expected_claim_token="t1"
        )
    assert exc_info.value.error_code == INDEX_PROVISION_CONFLICT
    current = registry.get(entry.index_name)
    assert current is not None
    assert current.claim_token == "t2"  # never clobbered
    assert current.status != "failed"  # reclaimer's entry left untouched


# --- concurrency: real threading races (§3.5 mandate) -------------------------------------------


def test_two_threads_racing_register_for_same_new_index_name_exactly_one_wins() -> None:
    """AC3's registry half — N=20 threads, one index_name."""
    registry = InMemoryIndexRegistry()
    n = 20
    barrier = threading.Barrier(n)
    candidates = [
        build_index_registry_entry(claim_token=f"token-{i}") for i in range(n)
    ]
    results: list[IndexRegistryEntry | None] = [None] * n

    def worker(i: int) -> None:
        barrier.wait()
        results[i] = registry.register(candidates[i])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entries = [r for r in results if r is not None]
    assert len(entries) == n  # every call returned, none raised

    winners = [
        i for i in range(n) if entries[i].claim_token == candidates[i].claim_token
    ]
    assert len(winners) == 1
    winning_token = candidates[winners[0]].claim_token
    for entry in entries:
        assert entry.claim_token == winning_token


def test_register_never_raises_on_a_create_race() -> None:
    """The story's "the losing worker does not error"."""
    registry = InMemoryIndexRegistry()
    n = 20
    barrier = threading.Barrier(n)
    candidates = [
        build_index_registry_entry(claim_token=f"token-{i}") for i in range(n)
    ]
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()
        try:
            registry.register(candidates[i])
        except BaseException as exc:  # pragma: no cover - failure path only
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []


def test_ten_threads_racing_reclaim_of_one_stale_entry_exactly_one_wins() -> None:
    registry = InMemoryIndexRegistry()
    stale = build_index_registry_entry(
        claim_token="original", claimed_at=FIXED_TIME - timedelta(seconds=301)
    )
    registry.register(stale)

    n = 10
    barrier = threading.Barrier(n)
    new_entries = [
        stale.model_copy(update={"claim_token": f"reclaim-{i}"}) for i in range(n)
    ]
    results: list[IndexRegistryEntry | ProvisioningError | None] = [None] * n

    def reclaim_worker(i: int) -> None:
        barrier.wait()
        try:
            results[i] = registry.reclaim(
                stale.index_name, observed=stale, new_entry=new_entries[i]
            )
        except ProvisioningError as exc:
            results[i] = exc

    threads = [threading.Thread(target=reclaim_worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if isinstance(r, IndexRegistryEntry)]
    failures = [r for r in results if isinstance(r, ProvisioningError)]
    assert len(successes) == 1
    assert len(failures) == n - 1
    assert all(f.error_code == INDEX_PROVISION_CONFLICT for f in failures)
    current = registry.get(stale.index_name)
    assert current is not None
    assert current.claim_token == successes[0].claim_token
