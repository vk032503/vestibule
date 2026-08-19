"""Unit tests for `IndexProvisioner` (REQ-011).

Concurrency tests use real `threading.Thread`s against `InMemoryIndexRegistry` (per
`docs/designs/REQ-011-lld.md` §3.5's "correctly simulate... via real threading races"
mandate), never a hand-scripted call order.
"""

from __future__ import annotations

import inspect
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from vestibule.indexer.adapters.in_memory import InMemoryIndexer
from vestibule.provisioning.adapters.base import IndexProvisionerAdapter
from vestibule.provisioning.adapters.in_memory import InMemoryProvisioner
from vestibule.provisioning.conftest import (
    FIXED_TIME,
    build_index_registry_entry,
    build_index_template,
    build_scenario,
    build_template_store,
)
from vestibule.provisioning.model import (
    INDEX_AUTO_CREATE_DISABLED,
    INDEX_PROVISION_CONFLICT,
    INDEX_PROVISION_FAILED,
    INDEX_PROVISION_TIMEOUT,
    INDEX_PROVISIONER_UNAVAILABLE,
    INDEX_REGISTRY_UNAVAILABLE,
    INDEX_RETIRED,
    INDEX_SCHEMA_DRIFT,
    INDEX_TEMPLATE_INVALID,
    IndexDescription,
    IndexRegistryEntry,
    IndexTemplate,
    ProvisioningError,
)
from vestibule.provisioning.provisioner import IndexProvisioner, ProvisioningConfig
from vestibule.provisioning.registry import IndexRegistry, InMemoryIndexRegistry
from vestibule.provisioning.stores.cached_registry import CachedIndexRegistry
from vestibule.provisioning.templates import IndexTemplateStore


class _ManualClock:
    """Deterministic, manually-advanced stand-in for `datetime.now(timezone.utc)`."""

    def __init__(self, start: datetime = FIXED_TIME) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class _RecordingAdapter(IndexProvisionerAdapter):
    """Programmable, thread-safe `IndexProvisionerAdapter` test double.

    Reports `index_exists`/`describe_index` off a single internal `(dimensions,
    metric)` schema slot — `None` means "absent" — mutated by `create_index` (unless
    `create_index_error` is set, in which case `create_index` always raises instead)
    and by the test-only `set_schema` helper (for simulating a live index changing
    out from under a `ready` entry).
    """

    def __init__(
        self,
        *,
        initial_schema: tuple[int, str] | None = None,
        create_index_error: BaseException | None = None,
    ) -> None:
        self._schema = initial_schema
        self._create_index_error = create_index_error
        self._lock = threading.Lock()
        self.index_exists_calls: list[str] = []
        self.create_index_calls: list[tuple[str, IndexTemplate, int, str]] = []
        self.describe_index_calls: list[str] = []
        self.delete_index_calls: list[str] = []

    def index_exists(self, index_name: str) -> bool:
        with self._lock:
            self.index_exists_calls.append(index_name)
            return self._schema is not None

    def create_index(
        self, index_name: str, template: IndexTemplate, dimensions: int, metric: str
    ) -> None:
        with self._lock:
            self.create_index_calls.append((index_name, template, dimensions, metric))
            if self._create_index_error is not None:
                raise self._create_index_error
            self._schema = (dimensions, metric)

    def describe_index(self, index_name: str) -> IndexDescription | None:
        with self._lock:
            self.describe_index_calls.append(index_name)
            if self._schema is None:
                return None
            dimensions, metric = self._schema
        return IndexDescription(dimensions=dimensions, metric=metric)

    def delete_index(self, index_name: str) -> None:
        with self._lock:
            self.delete_index_calls.append(index_name)
            self._schema = None

    def set_schema(self, schema: tuple[int, str] | None) -> None:
        """Test-only: simulates a live index's schema changing out of band."""
        with self._lock:
            self._schema = schema


class _CountingAdapter(IndexProvisionerAdapter):
    """Wraps another `IndexProvisionerAdapter`, thread-safely counting `create_index`
    calls."""

    def __init__(self, wrapped: IndexProvisionerAdapter) -> None:
        self._wrapped = wrapped
        self._lock = threading.Lock()
        self.create_index_calls = 0

    def index_exists(self, index_name: str) -> bool:
        return self._wrapped.index_exists(index_name)

    def create_index(
        self, index_name: str, template: IndexTemplate, dimensions: int, metric: str
    ) -> None:
        with self._lock:
            self.create_index_calls += 1
        self._wrapped.create_index(index_name, template, dimensions, metric)

    def describe_index(self, index_name: str) -> IndexDescription | None:
        return self._wrapped.describe_index(index_name)

    def delete_index(self, index_name: str) -> None:
        self._wrapped.delete_index(index_name)


class _SpyTemplateStore(IndexTemplateStore):
    """Wraps another `IndexTemplateStore`, counting `get_or_raise` calls only."""

    def __init__(self, wrapped: IndexTemplateStore) -> None:
        self._wrapped = wrapped
        self.get_or_raise_calls = 0

    def get(self, template_id: str) -> IndexTemplate | None:
        return self._wrapped.get(template_id)

    def get_or_raise(self, template_id: str) -> IndexTemplate:
        self.get_or_raise_calls += 1
        return self._wrapped.get_or_raise(template_id)


class _MarkRetiredSpyRegistry(InMemoryIndexRegistry):
    """Spies on `mark_retired` call count only; every other method is unchanged."""

    def __init__(self) -> None:
        super().__init__()
        self.mark_retired_calls = 0

    def mark_retired(self, index_name: str) -> IndexRegistryEntry:
        self.mark_retired_calls += 1
        return super().mark_retired(index_name)


class _RaisingRegistry(InMemoryIndexRegistry):
    """Test double: raises a fixed, injected `ProvisioningError` from one named
    method (once), then delegates to the real implementation for every other call —
    deterministically exercises a non-CONFLICT error passthrough without needing a
    genuine backend failure."""

    def __init__(self, *, method: str, error: ProvisioningError) -> None:
        super().__init__()
        self._method = method
        self._error = error
        self._raised = False

    def mark_ready(
        self, index_name: str, *, expected_claim_token: str | None
    ) -> IndexRegistryEntry:
        if self._method == "mark_ready" and not self._raised:
            self._raised = True
            raise self._error
        return super().mark_ready(index_name, expected_claim_token=expected_claim_token)

    def mark_failed(
        self, index_name: str, reason: str, *, expected_claim_token: str | None
    ) -> IndexRegistryEntry:
        if self._method == "mark_failed" and not self._raised:
            self._raised = True
            raise self._error
        return super().mark_failed(
            index_name, reason, expected_claim_token=expected_claim_token
        )

    def reclaim(
        self,
        index_name: str,
        observed: IndexRegistryEntry,
        new_entry: IndexRegistryEntry,
    ) -> IndexRegistryEntry:
        if self._method == "reclaim" and not self._raised:
            self._raised = True
            raise self._error
        return super().reclaim(index_name, observed, new_entry)


class _EntryVanishesAfterMarkReadyConflictRegistry(InMemoryIndexRegistry):
    """`mark_ready` raises `INDEX_PROVISION_CONFLICT` once, deleting the entry from
    the backing store before doing so — a pathological case that never happens in
    practice (nothing in this module ever deletes a registry row), exercised here
    purely to cover `_finalize_ready`'s own defensive `current is None` recovery
    branch."""

    def __init__(self) -> None:
        super().__init__()
        self._raised = False

    def mark_ready(
        self, index_name: str, *, expected_claim_token: str | None
    ) -> IndexRegistryEntry:
        if not self._raised:
            self._raised = True
            with self._lock:
                self._entries.pop(index_name, None)
            raise ProvisioningError(
                index_name, "vanished", error_code=INDEX_PROVISION_CONFLICT
            )
        return super().mark_ready(index_name, expected_claim_token=expected_claim_token)


class _BlindGetRegistry(InMemoryIndexRegistry):
    """`get()` always reports no entry, while every other method (`register`/
    `reclaim`/`mark_*`) behaves normally against the real backing store — simulates a
    TOCTOU gap between `ensure()`'s initial `get()` and its own `register()` call, so
    a pre-seeded terminal-status entry is observed via `register()`'s own
    "already exists" return rather than `get()`'s. Deterministically exercises
    `_after_losing_claim`'s non-`"ready"` branches without relying on real thread
    scheduling. Only safe to use with a pre-seeded *terminal*-status entry (`failed`/
    `retired`) — a `provisioning` entry would send `_wait_for_ready` into an infinite
    loop, since it too calls `get()`."""

    def get(self, index_name: str) -> IndexRegistryEntry | None:
        return None


class _FirstGetMissingRegistry(InMemoryIndexRegistry):
    """The very first `get()` call reports no entry (simulating a TOCTOU gap where a
    concurrent winner's write lands between this caller's initial `get()` and its own
    `register()` call); every subsequent `get()` call behaves normally against the
    real backing store. Deterministically exercises `_after_losing_claim`'s
    `"provisioning"` dispatch branch without relying on real thread scheduling."""

    def __init__(self) -> None:
        super().__init__()
        self._get_calls = 0

    def get(self, index_name: str) -> IndexRegistryEntry | None:
        self._get_calls += 1
        if self._get_calls == 1:
            return None
        return super().get(index_name)


class _ProvisioningThenReadyRegistry(InMemoryIndexRegistry):
    """The first `get()` (`ensure()`'s own initial read) returns a pre-seeded
    `provisioning` entry with `claimed_at=None` (defensive: `_is_stale` must treat a
    missing `claimed_at` as "not stale" rather than raising) — the wait loop's own
    first re-`get()` then observes a different, already-`ready` entry (simulating
    another worker finishing normally while this caller was sleeping). Covers both
    `_is_stale`'s `claimed_at is None` branch and `_wait_for_ready`'s
    `refreshed.status != "provisioning"` dispatch."""

    def __init__(self, ready_entry: IndexRegistryEntry) -> None:
        super().__init__()
        self._ready_entry = ready_entry
        self._get_calls = 0

    def get(self, index_name: str) -> IndexRegistryEntry | None:
        self._get_calls += 1
        if self._get_calls == 1:
            return build_index_registry_entry(claimed_at=None)
        return self._ready_entry


class _EntryVanishesDuringWaitRegistry(InMemoryIndexRegistry):
    """The entry `_wait_for_ready` observes at claim time is deleted from the
    backing store before the wait loop's first re-`get()` — a pathological case
    (nothing in this module ever deletes a registry row), exercised here purely to
    cover `_wait_for_ready`'s own defensive `refreshed is None` recovery branch."""

    def __init__(self) -> None:
        super().__init__()
        self._get_calls = 0

    def get(self, index_name: str) -> IndexRegistryEntry | None:
        self._get_calls += 1
        if self._get_calls == 2:
            with self._lock:
                self._entries.pop(index_name, None)
            return None
        return super().get(index_name)


def _provisioner(
    *,
    registry: IndexRegistry | None = None,
    adapter: IndexProvisionerAdapter | None = None,
    config: ProvisioningConfig | None = None,
    **provisioner_kwargs: object,
) -> tuple[IndexProvisioner, IndexRegistry, IndexProvisionerAdapter]:
    real_registry = registry if registry is not None else InMemoryIndexRegistry()
    real_adapter = (
        adapter if adapter is not None else InMemoryProvisioner(InMemoryIndexer())
    )
    provisioner = IndexProvisioner(
        real_registry,
        real_adapter,
        build_template_store(),
        config or ProvisioningConfig(),
        **provisioner_kwargs,  # type: ignore[arg-type]
    )
    return provisioner, real_registry, real_adapter


# --- AC1 / AC2: unknown index / ready index -----------------------------------------------------


def test_ensure_unknown_index_creates_registers_marks_ready_returns_entry() -> None:
    provisioner, registry, adapter = _provisioner()
    scenario = build_scenario()

    entry = provisioner.ensure(scenario)

    assert entry.status == "ready"
    assert entry.dimensions == scenario.embedder.target_dimensions
    assert entry.template_id == "standard-v1"
    assert registry.get(scenario.index_name) == entry
    assert isinstance(adapter, InMemoryProvisioner)


def test_ensure_ready_index_returns_immediately_without_calling_create_index() -> None:
    adapter = _RecordingAdapter(initial_schema=None)
    provisioner, _registry, _adapter = _provisioner(adapter=adapter)
    scenario = build_scenario()

    first = provisioner.ensure(scenario)
    second = provisioner.ensure(scenario)

    assert second == first
    assert len(adapter.create_index_calls) == 1


# --- AC3: the core concurrency test ---------------------------------------------------------


def test_ensure_two_concurrent_calls_for_same_new_index_exactly_one_create_index_call() -> (
    None
):
    """Scope note: this exercises the *original, non-stale* claim race only (§3.3) —
    "exactly one create_index call" is the correct, unqualified assertion here."""
    counting_adapter = _CountingAdapter(InMemoryProvisioner(InMemoryIndexer()))
    provisioner, _registry, _adapter = _provisioner(adapter=counting_adapter)
    scenario = build_scenario()

    n = 2
    barrier = threading.Barrier(n)
    results: list[IndexRegistryEntry] = [None, None]  # type: ignore[list-item]

    def worker(i: int) -> None:
        barrier.wait()
        results[i] = provisioner.ensure(scenario)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert counting_adapter.create_index_calls == 1
    assert results[0].status == results[1].status == "ready"
    assert results[0].dimensions == results[1].dimensions
    assert results[0].metric == results[1].metric
    assert results[0].template_id == results[1].template_id
    assert results[0].template_version == results[1].template_version


def test_ensure_slow_but_alive_worker_survives_stale_reclaim_both_create_index_calls_converge() -> (
    None
):
    """AC3 completeness — Race C (§3.4 step 7, the design-review BLOCKER on §3.3 step
    7). Uses a real `threading.Event` to hold W1 inside its own `create_index` call
    while a shared, injected `_clock` seam is advanced past
    `provisioning_stale_after_seconds`, letting W3 legitimately reclaim and also call
    `create_index` while W1's own call is still blocked."""
    registry = InMemoryIndexRegistry()
    clock = _ManualClock()
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    create_calls: list[tuple[int, str]] = []
    schema: dict[str, tuple[int, str]] = {}

    class _BlockingAdapter(IndexProvisionerAdapter):
        def index_exists(self, index_name: str) -> bool:
            with lock:
                return index_name in schema

        def create_index(
            self, index_name: str, template: IndexTemplate, dimensions: int, metric: str
        ) -> None:
            with lock:
                is_first_call = not create_calls
                create_calls.append((dimensions, metric))
            if is_first_call:
                started.set()
                assert release.wait(timeout=5), "release event never set"
            with lock:
                schema[index_name] = (dimensions, metric)

        def describe_index(self, index_name: str) -> IndexDescription | None:
            with lock:
                found = schema.get(index_name)
            return (
                None
                if found is None
                else IndexDescription(dimensions=found[0], metric=found[1])
            )

        def delete_index(self, index_name: str) -> None:
            with lock:
                schema.pop(index_name, None)

    adapter = _BlockingAdapter()
    config = ProvisioningConfig(provisioning_stale_after_seconds=300.0)
    templates = build_template_store()
    provisioner1 = IndexProvisioner(
        registry, adapter, templates, config, _clock=clock, _sleep=lambda s: None
    )
    provisioner2 = IndexProvisioner(
        registry, adapter, templates, config, _clock=clock, _sleep=lambda s: None
    )
    scenario = build_scenario()

    results: dict[str, IndexRegistryEntry] = {}

    def w1() -> None:
        results["w1"] = provisioner1.ensure(scenario)

    t1 = threading.Thread(target=w1)
    t1.start()
    assert started.wait(timeout=5), "W1 never entered create_index"

    # W1 has made no registry write since its original claim (claim time only) —
    # advancing the clock alone is enough to make W3 observe staleness.
    clock.advance(301.0)

    results["w3"] = provisioner2.ensure(scenario)

    release.set()
    t1.join(timeout=5)
    assert not t1.is_alive()

    # (a) two create_index invocations.
    assert len(create_calls) == 2
    # (c) both calls' recorded definitions match the template.
    for dimensions, metric in create_calls:
        assert dimensions == scenario.indexer.dimensions
        assert metric == "cosine"
    # (e)/(f): both ensure() calls returned successfully, to an equivalent ready entry.
    w1_entry, w3_entry = results["w1"], results["w3"]
    assert w1_entry.status == w3_entry.status == "ready"
    assert w1_entry.dimensions == w3_entry.dimensions == scenario.indexer.dimensions
    assert w1_entry.metric == w3_entry.metric
    assert w1_entry.template_id == w3_entry.template_id
    assert w1_entry.template_version == w3_entry.template_version
    assert w1_entry.claim_token is None
    assert w3_entry.claim_token is None


def test_ensure_reclaim_uses_original_claims_resolved_template_not_a_freshly_loaded_one_even_when_they_differ() -> (
    None
):
    """Assumption A14 — the design-review MAJOR resolution, round 2."""
    registry = InMemoryIndexRegistry()
    original_template = build_index_template(
        hnsw=build_index_template().hnsw.model_copy(update={"ef_construction": 400})
    )
    stale_entry = build_index_registry_entry(
        claim_token="original-token",
        claimed_at=FIXED_TIME - timedelta(seconds=301),
        resolved_template=original_template,
    )
    registry.register(stale_entry)

    different_template = build_index_template(
        hnsw=build_index_template().hnsw.model_copy(update={"ef_construction": 999})
    )
    spy_templates = _SpyTemplateStore(build_template_store(different_template))
    recording_adapter = _RecordingAdapter(initial_schema=None)
    provisioner = IndexProvisioner(
        registry,
        recording_adapter,
        spy_templates,
        ProvisioningConfig(),
        _clock=lambda: FIXED_TIME,
    )

    ready = provisioner.ensure(build_scenario())

    assert ready.status == "ready"
    used_template = recording_adapter.create_index_calls[0][1]
    assert used_template.hnsw.ef_construction == 400  # the original claim's template
    assert spy_templates.get_or_raise_calls == 0  # never re-resolved by the reclaimer


# --- AC4: stale reclamation / timeout ---------------------------------------------------------


def test_ensure_stale_provisioning_reclaimed_by_waiter_fresh_one_times_out() -> None:
    clock = _ManualClock()
    adapter = _RecordingAdapter(initial_schema=None)
    config = ProvisioningConfig(
        provisioning_stale_after_seconds=300.0,
        wait_for_provisioning_timeout_seconds=10.0,
        wait_poll_interval_seconds=2.0,
    )
    registry = InMemoryIndexRegistry()
    templates = build_template_store()
    provisioner = IndexProvisioner(
        registry,
        adapter,
        templates,
        config,
        _clock=clock,
        _sleep=lambda seconds: clock.advance(seconds),
    )

    # Stale claim (claimed 301s ago) is reclaimable and reaches ready.
    stale_scenario = build_scenario(index_name="stale-idx", scenario_id="stale")
    registry.register(
        build_index_registry_entry(
            index_name="stale-idx",
            scenario_id="stale",
            claim_token="dead-worker-token",
            claimed_at=clock.now - timedelta(seconds=301),
            created_at=clock.now - timedelta(seconds=301),
        )
    )
    reclaimed = provisioner.ensure(stale_scenario)
    assert reclaimed.status == "ready"

    # A fresh claim (claimed just now) is not reclaimable within the wait window —
    # the waiter times out with INDEX_PROVISION_TIMEOUT (TRANSIENT).
    clock.now = FIXED_TIME
    fresh_scenario = build_scenario(index_name="fresh-idx", scenario_id="fresh")
    registry.register(
        build_index_registry_entry(
            index_name="fresh-idx",
            scenario_id="fresh",
            claim_token="live-worker-token",
            claimed_at=clock.now,
            created_at=clock.now,
        )
    )
    with pytest.raises(ProvisioningError) as exc_info:
        provisioner.ensure(fresh_scenario)
    assert exc_info.value.error_code == INDEX_PROVISION_TIMEOUT


# --- AC5: create_index failure after claim -----------------------------------------------------


def test_ensure_create_index_failure_after_claim_marks_failed_subsequent_ensure_raises_provision_failed() -> (
    None
):
    adapter = _RecordingAdapter(
        initial_schema=None,
        create_index_error=ProvisioningError(
            "boom-index", "upstream 503", error_code=INDEX_PROVISIONER_UNAVAILABLE
        ),
    )
    provisioner, registry, _adapter = _provisioner(adapter=adapter)
    scenario = build_scenario()

    with pytest.raises(ProvisioningError) as first_exc:
        provisioner.ensure(scenario)
    assert first_exc.value.error_code == INDEX_PROVISIONER_UNAVAILABLE
    assert registry.get(scenario.index_name).status == "failed"  # type: ignore[union-attr]

    with pytest.raises(ProvisioningError) as second_exc:
        provisioner.ensure(scenario)
    assert second_exc.value.error_code == INDEX_PROVISION_FAILED
    assert len(adapter.create_index_calls) == 1  # never retried automatically


# --- already-exists after a won claim ------------------------------------------------------


def test_ensure_index_already_exists_with_compatible_schema_after_won_claim_is_success() -> (
    None
):
    scenario = build_scenario()
    adapter = _RecordingAdapter(initial_schema=(scenario.indexer.dimensions, "cosine"))
    provisioner, registry, _adapter = _provisioner(adapter=adapter)

    entry = provisioner.ensure(scenario)

    assert entry.status == "ready"
    assert adapter.create_index_calls == []


def test_ensure_index_already_exists_with_incompatible_schema_after_won_claim_raises_drift() -> (
    None
):
    scenario = build_scenario()
    adapter = _RecordingAdapter(initial_schema=(1, "cosine"))  # wrong dimensions
    provisioner, registry, _adapter = _provisioner(adapter=adapter)

    with pytest.raises(ProvisioningError) as exc_info:
        provisioner.ensure(scenario)
    assert exc_info.value.error_code == INDEX_SCHEMA_DRIFT
    assert registry.get(scenario.index_name).status == "failed"  # type: ignore[union-attr]


def test_ensure_out_of_band_third_party_created_index_before_either_worker_claims_still_one_create_index_call() -> (
    None
):
    scenario = build_scenario()
    adapter = _RecordingAdapter(initial_schema=(scenario.indexer.dimensions, "cosine"))
    provisioner, _registry, _adapter = _provisioner(adapter=adapter)

    n = 2
    barrier = threading.Barrier(n)
    results: list[IndexRegistryEntry] = [None, None]  # type: ignore[list-item]

    def worker(i: int) -> None:
        barrier.wait()
        results[i] = provisioner.ensure(scenario)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert adapter.create_index_calls == []
    assert results[0].status == results[1].status == "ready"


# --- AC6 / drift verification caching ---------------------------------------------------------


def test_ensure_live_index_dimension_mismatch_raises_schema_drift() -> None:
    scenario = build_scenario()
    adapter = _RecordingAdapter(initial_schema=(scenario.indexer.dimensions, "cosine"))
    config = ProvisioningConfig(verification_interval_seconds=300.0)
    # Starts at real "now" (not FIXED_TIME): InMemoryIndexRegistry.mark_ready/
    # touch_verified always stamp last_verified_at via the real wall clock (the LLD's
    # own interface gives InMemoryIndexRegistry no clock-injection seam at all) — this
    # clock must start close to that real stamp for verification_interval_seconds
    # elapsed-time arithmetic to mean anything.
    clock = _ManualClock(datetime.now(timezone.utc))
    provisioner, registry, _adapter = _provisioner(
        adapter=adapter, config=config, _clock=clock
    )

    ready = provisioner.ensure(scenario)
    assert ready.status == "ready"

    # Verification interval elapses, and the live index has since drifted.
    clock.advance(301.0)
    adapter.set_schema((999, "cosine"))

    with pytest.raises(ProvisioningError) as exc_info:
        provisioner.ensure(scenario)
    assert exc_info.value.error_code == INDEX_SCHEMA_DRIFT
    # Drift does not terminalize the entry — it fails loudly every time until a
    # human resolves it, not once and then silently.
    assert registry.get(scenario.index_name).status == "ready"  # type: ignore[union-attr]


def test_ensure_drift_verification_skipped_within_interval_performed_after() -> None:
    scenario = build_scenario()
    adapter = _RecordingAdapter(initial_schema=(scenario.indexer.dimensions, "cosine"))
    config = ProvisioningConfig(verification_interval_seconds=300.0)
    clock = _ManualClock(datetime.now(timezone.utc))  # see the sibling test's comment
    provisioner, _registry, _adapter = _provisioner(
        adapter=adapter, config=config, _clock=clock
    )

    provisioner.ensure(scenario)
    describe_calls_after_creation = len(adapter.describe_index_calls)

    clock.advance(100.0)
    provisioner.ensure(scenario)
    assert len(adapter.describe_index_calls) == describe_calls_after_creation  # skipped

    clock.advance(201.0)  # total 301s elapsed
    provisioner.ensure(scenario)
    assert len(adapter.describe_index_calls) == describe_calls_after_creation + 1


# --- AC7 / AC8: auto_create / template validation -----------------------------------------------


def test_ensure_auto_create_false_missing_index_raises_auto_create_disabled() -> None:
    adapter = _RecordingAdapter(initial_schema=None)
    config = ProvisioningConfig(auto_create=False)
    provisioner, registry, _adapter = _provisioner(adapter=adapter, config=config)
    scenario = build_scenario()

    with pytest.raises(ProvisioningError) as exc_info:
        provisioner.ensure(scenario)
    assert exc_info.value.error_code == INDEX_AUTO_CREATE_DISABLED
    assert registry.get(scenario.index_name) is None
    assert adapter.create_index_calls == []
    assert adapter.index_exists_calls == []


def test_ensure_template_dimensions_inconsistent_with_embedder_settings_raises_template_invalid_before_creation() -> (
    None
):
    conflicting_template = build_index_template(dimensions=2048)
    provisioner, registry, adapter = _provisioner(
        adapter=_RecordingAdapter(initial_schema=None)
    )
    provisioner = IndexProvisioner(
        registry,
        adapter,
        build_template_store(conflicting_template),
        ProvisioningConfig(),
    )
    scenario = build_scenario()  # scenario.indexer.dimensions == 1024, not 2048

    with pytest.raises(ProvisioningError) as exc_info:
        provisioner.ensure(scenario)
    assert exc_info.value.error_code == INDEX_TEMPLATE_INVALID
    assert registry.get(scenario.index_name) is None
    assert isinstance(adapter, _RecordingAdapter)
    assert adapter.create_index_calls == []


def test_ensure_dimensions_derived_from_scenario_indexer_dimensions_when_template_dimensions_null() -> (
    None
):
    """Assumption A2."""
    null_dims_template = build_index_template(dimensions=None)
    provisioner, registry, _adapter = _provisioner()
    provisioner = IndexProvisioner(
        registry,
        InMemoryProvisioner(InMemoryIndexer()),
        build_template_store(null_dims_template),
        ProvisioningConfig(),
    )
    scenario = build_scenario()

    entry = provisioner.ensure(scenario)

    assert entry.dimensions == scenario.indexer.dimensions == 1024


def test_ensure_template_explicit_dimensions_conflicting_with_scenario_raises_template_invalid() -> (
    None
):
    """Assumption A2's "second boundary" check."""
    conflicting_template = build_index_template(dimensions=1)
    registry = InMemoryIndexRegistry()
    provisioner = IndexProvisioner(
        registry,
        InMemoryProvisioner(InMemoryIndexer()),
        build_template_store(conflicting_template),
        ProvisioningConfig(),
    )
    scenario = build_scenario(index_template_id="standard-v1")

    with pytest.raises(ProvisioningError) as exc_info:
        provisioner.ensure(scenario)
    assert exc_info.value.error_code == INDEX_TEMPLATE_INVALID


# --- register idempotency / retired ------------------------------------------------------------


def test_ensure_register_idempotent_identical_settings_twice_returns_same_entry() -> (
    None
):
    provisioner, _registry, adapter = _provisioner(adapter=_RecordingAdapter())
    scenario = build_scenario()

    first = provisioner.ensure(scenario)
    second = provisioner.ensure(scenario)

    assert first == second
    assert isinstance(adapter, _RecordingAdapter)
    assert len(adapter.create_index_calls) == 1


def test_ensure_retired_index_raises_index_retired() -> None:
    registry = InMemoryIndexRegistry()
    entry = build_index_registry_entry(
        status="ready", claim_token=None, claimed_at=None
    )
    registry.register(entry)
    registry.mark_retired(entry.index_name)
    provisioner, _r, _a = _provisioner(registry=registry)

    with pytest.raises(ProvisioningError) as exc_info:
        provisioner.ensure(build_scenario())
    assert exc_info.value.error_code == INDEX_RETIRED


# --- mark_retired is never called by IndexProvisioner (story: Out of scope) --------------------


def test_mark_retired_is_never_called_by_anything_in_this_module() -> None:
    registry = _MarkRetiredSpyRegistry()
    adapter = _RecordingAdapter(initial_schema=None)
    config = ProvisioningConfig(
        provisioning_stale_after_seconds=300.0,
        wait_for_provisioning_timeout_seconds=4.0,
        wait_poll_interval_seconds=1.0,
    )
    clock = _ManualClock()
    provisioner = IndexProvisioner(
        registry,
        adapter,
        build_template_store(),
        config,
        _clock=clock,
        _sleep=lambda seconds: clock.advance(seconds),
    )

    scenario = build_scenario()
    provisioner.ensure(scenario)  # create path
    provisioner.ensure(scenario)  # ready fast path

    stale_scenario = build_scenario(index_name="stale-idx", scenario_id="stale-2")
    registry.register(
        build_index_registry_entry(
            index_name="stale-idx",
            scenario_id="stale-2",
            claim_token="dead",
            claimed_at=clock.now - timedelta(seconds=301),
            created_at=clock.now - timedelta(seconds=301),
        )
    )
    provisioner.ensure(stale_scenario)  # reclaim path

    assert registry.mark_retired_calls == 0


# --- composition with CachedIndexRegistry (Assumption A16) ------------------------------------


def test_ensure_works_correctly_when_registry_is_cached_index_registry_wrapped() -> (
    None
):
    wrapped = InMemoryIndexRegistry()
    cached = CachedIndexRegistry(wrapped, ttl_seconds=60)
    provisioner, _r, _a = _provisioner(registry=cached)
    scenario = build_scenario()

    first = provisioner.ensure(scenario)
    second = provisioner.ensure(scenario)

    assert first.status == second.status == "ready"


# --- defensive/recovery branches (Assumption A7, and internal conflict re-raises) --------------


def test_call_adapter_wraps_unmapped_exception_as_index_provisioner_unavailable() -> (
    None
):
    """Assumption A7 — any adapter exception not already a `ProvisioningError` is
    wrapped as `INDEX_PROVISIONER_UNAVAILABLE`, the closest declared code."""
    adapter = _RecordingAdapter(
        initial_schema=None, create_index_error=RuntimeError("boom")
    )
    provisioner, _registry, _adapter = _provisioner(adapter=adapter)

    with pytest.raises(ProvisioningError) as exc_info:
        provisioner.ensure(build_scenario())
    assert exc_info.value.error_code == INDEX_PROVISIONER_UNAVAILABLE


def test_finalize_ready_reraises_non_conflict_mark_ready_error() -> None:
    registry = _RaisingRegistry(
        method="mark_ready",
        error=ProvisioningError("idx", "boom", error_code=INDEX_REGISTRY_UNAVAILABLE),
    )
    provisioner, _registry, _adapter = _provisioner(
        registry=registry, adapter=_RecordingAdapter(initial_schema=None)
    )

    with pytest.raises(ProvisioningError) as exc_info:
        provisioner.ensure(build_scenario())
    assert exc_info.value.error_code == INDEX_REGISTRY_UNAVAILABLE


def test_absorb_conflict_reraises_non_conflict_mark_failed_error() -> None:
    registry = _RaisingRegistry(
        method="mark_failed",
        error=ProvisioningError("idx", "boom", error_code=INDEX_REGISTRY_UNAVAILABLE),
    )
    adapter = _RecordingAdapter(
        initial_schema=None,
        create_index_error=ProvisioningError(
            "idx", "create boom", error_code=INDEX_PROVISIONER_UNAVAILABLE
        ),
    )
    provisioner, _registry, _adapter = _provisioner(registry=registry, adapter=adapter)

    with pytest.raises(ProvisioningError) as exc_info:
        provisioner.ensure(build_scenario())
    # _absorb_conflict's own re-raise of the mark_failed failure takes precedence over
    # the bare "raise" that would otherwise re-surface the original create_index error.
    assert exc_info.value.error_code == INDEX_REGISTRY_UNAVAILABLE


def test_try_reclaim_reraises_non_conflict_error() -> None:
    registry = _RaisingRegistry(
        method="reclaim",
        error=ProvisioningError("idx", "boom", error_code=INDEX_REGISTRY_UNAVAILABLE),
    )
    stale = build_index_registry_entry(
        claim_token="dead",
        claimed_at=FIXED_TIME - timedelta(seconds=301),
        created_at=FIXED_TIME - timedelta(seconds=301),
    )
    registry.register(stale)
    provisioner, _registry, _adapter = _provisioner(
        registry=registry,
        adapter=_RecordingAdapter(initial_schema=None),
        _clock=lambda: FIXED_TIME,
    )

    with pytest.raises(ProvisioningError) as exc_info:
        provisioner.ensure(build_scenario())
    assert exc_info.value.error_code == INDEX_REGISTRY_UNAVAILABLE


def test_try_reclaim_returns_none_and_wait_loop_continues_after_a_lost_reclaim() -> (
    None
):
    """Race A/B (absorbed): a lost `reclaim()` attempt does not propagate — the wait
    loop simply continues polling, and a subsequent attempt against the same
    (unchanged) stale snapshot succeeds."""
    registry = _RaisingRegistry(
        method="reclaim",
        error=ProvisioningError(
            "idx", "lost the reclaim race", error_code=INDEX_PROVISION_CONFLICT
        ),
    )
    stale = build_index_registry_entry(
        claim_token="dead",
        claimed_at=FIXED_TIME - timedelta(seconds=301),
        created_at=FIXED_TIME - timedelta(seconds=301),
    )
    registry.register(stale)
    config = ProvisioningConfig(
        provisioning_stale_after_seconds=300.0,
        wait_for_provisioning_timeout_seconds=10.0,
        wait_poll_interval_seconds=1.0,
    )
    provisioner, _registry, _adapter = _provisioner(
        registry=registry,
        adapter=_RecordingAdapter(initial_schema=None),
        config=config,
        _clock=lambda: FIXED_TIME,
        _sleep=lambda seconds: None,
    )

    entry = provisioner.ensure(build_scenario())

    assert entry.status == "ready"


def test_finalize_ready_claims_anew_if_entry_vanished_after_conflict() -> None:
    registry = _EntryVanishesAfterMarkReadyConflictRegistry()
    provisioner, _registry, _adapter = _provisioner(
        registry=registry, adapter=_RecordingAdapter(initial_schema=None)
    )

    entry = provisioner.ensure(build_scenario())

    assert entry.status == "ready"


def test_wait_for_ready_claims_anew_if_entry_vanishes_mid_wait() -> None:
    registry = _EntryVanishesDuringWaitRegistry()
    registry.register(
        build_index_registry_entry(
            claim_token="dead", claimed_at=FIXED_TIME, created_at=FIXED_TIME
        )
    )
    config = ProvisioningConfig(
        provisioning_stale_after_seconds=300.0,
        wait_for_provisioning_timeout_seconds=10.0,
        wait_poll_interval_seconds=1.0,
    )
    provisioner, _registry, _adapter = _provisioner(
        registry=registry,
        adapter=_RecordingAdapter(initial_schema=None),
        config=config,
        _clock=lambda: FIXED_TIME,
        _sleep=lambda seconds: None,
    )

    entry = provisioner.ensure(build_scenario())

    assert entry.status == "ready"


def test_after_losing_claim_dispatches_a_retired_winner_to_index_retired() -> None:
    """Exercises `_after_losing_claim`'s `"retired"` branch directly: `_claim_new`'s
    own `register()` call observes an already-`retired` entry rather than winning."""
    registry = _BlindGetRegistry()
    registry.register(
        build_index_registry_entry(status="retired", claim_token=None, claimed_at=None)
    )
    provisioner, _registry, _adapter = _provisioner(
        registry=registry, adapter=_RecordingAdapter(initial_schema=None)
    )

    with pytest.raises(ProvisioningError) as exc_info:
        provisioner.ensure(build_scenario())
    assert exc_info.value.error_code == INDEX_RETIRED


def test_after_losing_claim_dispatches_a_provisioning_winner_to_wait_for_ready() -> (
    None
):
    """Exercises `_after_losing_claim`'s `"provisioning"` branch directly:
    `_claim_new`'s own `register()` call observes an already-`provisioning` (and
    never-resolved) entry rather than winning; the resulting `_wait_for_ready` call
    eventually times out."""
    registry = _FirstGetMissingRegistry()
    winner = build_index_registry_entry(
        claim_token="winner-token", claimed_at=FIXED_TIME, created_at=FIXED_TIME
    )
    registry.register(winner)
    clock = _ManualClock(FIXED_TIME)
    config = ProvisioningConfig(
        provisioning_stale_after_seconds=300.0,
        wait_for_provisioning_timeout_seconds=2.0,
        wait_poll_interval_seconds=1.0,
    )
    provisioner, _registry, _adapter = _provisioner(
        registry=registry,
        adapter=_RecordingAdapter(initial_schema=None),
        config=config,
        _clock=clock,
        _sleep=lambda seconds: clock.advance(seconds),
    )

    with pytest.raises(ProvisioningError) as exc_info:
        provisioner.ensure(build_scenario())
    assert exc_info.value.error_code == INDEX_PROVISION_TIMEOUT


def test_wait_for_ready_treats_missing_claimed_at_as_not_stale_and_dispatches_on_status_change() -> (
    None
):
    ready_entry = build_index_registry_entry(
        status="ready",
        claim_token=None,
        claimed_at=None,
        last_verified_at=FIXED_TIME,
    )
    registry = _ProvisioningThenReadyRegistry(ready_entry)
    config = ProvisioningConfig(
        wait_for_provisioning_timeout_seconds=10.0, wait_poll_interval_seconds=1.0
    )
    provisioner, _registry, _adapter = _provisioner(
        registry=registry,
        adapter=_RecordingAdapter(initial_schema=None),
        config=config,
        _clock=lambda: FIXED_TIME,
        _sleep=lambda seconds: None,
    )

    entry = provisioner.ensure(build_scenario())

    assert entry == ready_entry


def test_after_losing_claim_dispatches_a_failed_winner_to_index_provision_failed() -> (
    None
):
    """Exercises `_after_losing_claim`'s `"failed"` branch directly: `_claim_new`'s
    own `register()` call observes an already-`failed` entry rather than winning."""
    registry = _BlindGetRegistry()
    registry.register(
        build_index_registry_entry(
            status="failed",
            claim_token=None,
            claimed_at=None,
            last_error_message="boom",
        )
    )
    provisioner, _registry, _adapter = _provisioner(
        registry=registry, adapter=_RecordingAdapter(initial_schema=None)
    )

    with pytest.raises(ProvisioningError) as exc_info:
        provisioner.ensure(build_scenario())
    assert exc_info.value.error_code == INDEX_PROVISION_FAILED


# --- config/code sync ----------------------------------------------------------------------


def test_config_index_provisioning_yaml_tunables_match_provisioning_config_defaults() -> (
    None
):
    config_path = (
        Path(__file__).resolve().parents[3] / "config" / "index_provisioning.yaml"
    )
    text = config_path.read_text(encoding="utf-8")
    defaults = {
        name: param.default
        for name, param in inspect.signature(ProvisioningConfig).parameters.items()
    }

    def _yaml_value(key: str) -> str:
        match = re.search(rf"\n  {re.escape(key)}:\s*(\S+)", text)
        assert match is not None, key
        return match.group(1)

    assert _yaml_value("default_template_id") == defaults["default_template_id"]
    assert (
        float(_yaml_value("provisioning_stale_after_seconds"))
        == defaults["provisioning_stale_after_seconds"]
    )
    assert (
        float(_yaml_value("verification_interval_seconds"))
        == defaults["verification_interval_seconds"]
    )
    assert (
        float(_yaml_value("wait_for_provisioning_timeout_seconds"))
        == defaults["wait_for_provisioning_timeout_seconds"]
    )
    assert (
        float(_yaml_value("wait_poll_interval_seconds"))
        == defaults["wait_poll_interval_seconds"]
    )
    assert _yaml_value("auto_create") == str(defaults["auto_create"]).lower()
