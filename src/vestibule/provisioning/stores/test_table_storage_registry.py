"""Unit tests for TableStorageIndexRegistry (REQ-011).

No live API calls — every test injects a recorded-response fake `_backend` (satisfying
`_TableBackend`'s duck-typed contract), mirroring
`vestibule.scenario.stores.test_table_storage_store`'s convention. The one exception is
the dependency-missing test, which relies on `azure-data-tables` genuinely not being
installed in this project's test environment.
"""

from __future__ import annotations

import time as time_module
from typing import Any, cast

import pytest
from azure.core.exceptions import HttpResponseError

from vestibule.provisioning.conftest import build_index_registry_entry
from vestibule.provisioning.model import (
    INDEX_PROVISION_CONFLICT,
    INDEX_PROVISIONER_DEPENDENCY_MISSING,
    INDEX_REGISTRY_UNAVAILABLE,
    ProvisioningError,
)
from vestibule.provisioning.stores._entity_codec import entry_to_entity_data
from vestibule.provisioning.stores.table_storage_registry import (
    TableStorageIndexRegistry,
    _TableEntityRecord,
)


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.reason = "error"
        self.headers: dict[str, str] = {}


def _http_error(status_code: int) -> HttpResponseError:
    fake_response = cast(Any, _FakeResponse(status_code))
    return HttpResponseError(response=fake_response)


class _FakeTableBackend:
    """Recorded-response fake standing in for `_RealTableBackend`."""

    def __init__(self, *, error_sequence: list[Exception | None] | None = None) -> None:
        self._entities: dict[tuple[str, str], _TableEntityRecord] = {}
        self._error_sequence = list(error_sequence or [])
        self._etag_counter = 0
        self.attempt_count = 0

    def seed(self, record: _TableEntityRecord) -> None:
        self._entities[(record.partition_key, record.row_key)] = record

    def _maybe_raise(self) -> None:
        self.attempt_count += 1
        if self._error_sequence:
            error = self._error_sequence.pop(0)
            if error is not None:
                raise error

    def get_entity(self, partition_key: str, row_key: str) -> _TableEntityRecord | None:
        self._maybe_raise()
        return self._entities.get((partition_key, row_key))

    def query(
        self, *, partition_key: str | None = None, row_key: str | None = None
    ) -> list[_TableEntityRecord]:
        self._maybe_raise()
        results = list(self._entities.values())
        if partition_key is not None:
            results = [r for r in results if r.partition_key == partition_key]
        if row_key is not None:
            results = [r for r in results if r.row_key == row_key]
        return results

    def upsert_entity(
        self,
        partition_key: str,
        row_key: str,
        data: dict[str, Any],
        *,
        etag: str | None,
    ) -> str:
        self._maybe_raise()
        key = (partition_key, row_key)
        existing = self._entities.get(key)
        if etag is None and existing is not None:
            raise _http_error(409)
        if etag is not None and (existing is None or existing.etag != etag):
            raise _http_error(412)
        self._etag_counter += 1
        new_etag = str(self._etag_counter)
        self._entities[key] = _TableEntityRecord(
            partition_key, row_key, dict(data), new_etag
        )
        return new_etag

    def delete_entity(self, partition_key: str, row_key: str) -> None:
        self._maybe_raise()
        self._entities.pop((partition_key, row_key), None)


def _make_registry(
    backend: _FakeTableBackend, *, max_attempts: int = 3, timeout_seconds: float = 30.0
) -> TableStorageIndexRegistry:
    return TableStorageIndexRegistry(
        connection_string="UseDevelopmentStorage=true",
        table_name="VestibuleIndexRegistry",
        max_attempts=max_attempts,
        backoff_base_seconds=0.01,
        backoff_max_seconds=0.02,
        timeout_seconds=timeout_seconds,
        _sleep=lambda _seconds: None,
        _backend=backend,
    )


# --- dependency missing (PERMANENT, fail fast) -----------------------------------------------


def test_missing_dependency_raises_index_provisioner_dependency_missing_permanent() -> (
    None
):
    with pytest.raises(ProvisioningError) as exc_info:
        TableStorageIndexRegistry(
            connection_string="UseDevelopmentStorage=true",
            table_name="VestibuleIndexRegistry",
        )
    assert exc_info.value.error_code == INDEX_PROVISIONER_DEPENDENCY_MISSING


# --- get / list_by_vertical --------------------------------------------------------------------


def test_get_returns_none_when_absent() -> None:
    registry = _make_registry(_FakeTableBackend())
    assert registry.get("missing") is None


def test_get_returns_stored_entry_after_register() -> None:
    backend = _FakeTableBackend()
    registry = _make_registry(backend)
    entry = build_index_registry_entry()
    registry.register(entry)

    fetched = registry.get(entry.index_name)

    assert fetched == entry


def test_list_by_vertical_filters_by_partition_key() -> None:
    backend = _FakeTableBackend()
    registry = _make_registry(backend)
    registry.register(build_index_registry_entry(index_name="hr-idx", vertical="hr"))
    registry.register(
        build_index_registry_entry(
            index_name="legal-idx", vertical="legal", claim_token="t2"
        )
    )

    assert [e.index_name for e in registry.list_by_vertical("hr")] == ["hr-idx"]


# --- register() (409 caught, never propagated) -------------------------------------------------


def test_register_on_absent_index_name_inserts() -> None:
    registry = _make_registry(_FakeTableBackend())
    entry = build_index_registry_entry(claim_token="winner")
    result = registry.register(entry)
    assert result.claim_token == "winner"


def test_register_on_existing_index_name_returns_stored_entry_without_raising() -> None:
    backend = _FakeTableBackend()
    registry = _make_registry(backend)
    first = build_index_registry_entry(claim_token="t1")
    registry.register(first)

    second = build_index_registry_entry(claim_token="t2")
    result = registry.register(second)

    assert result == first
    assert result.claim_token == "t1"


# --- reclaim() -----------------------------------------------------------------------------


def test_reclaim_succeeds_against_the_exact_observed_entry() -> None:
    backend = _FakeTableBackend()
    registry = _make_registry(backend)
    entry = build_index_registry_entry(claim_token="t1")
    registry.register(entry)

    reclaimed = registry.reclaim(
        entry.index_name,
        observed=entry,
        new_entry=entry.model_copy(update={"claim_token": "t2"}),
    )

    assert reclaimed.claim_token == "t2"
    assert registry.get(entry.index_name).claim_token == "t2"  # type: ignore[union-attr]


def test_reclaim_against_a_stale_observed_snapshot_raises_conflict() -> None:
    backend = _FakeTableBackend()
    registry = _make_registry(backend)
    entry = build_index_registry_entry(claim_token="t1")
    registry.register(entry)
    registry.reclaim(
        entry.index_name,
        observed=entry,
        new_entry=entry.model_copy(update={"claim_token": "t2"}),
    )

    with pytest.raises(ProvisioningError) as exc_info:
        registry.reclaim(
            entry.index_name,
            observed=entry,  # stale — the live entry now has claim_token="t2"
            new_entry=entry.model_copy(update={"claim_token": "t3"}),
        )
    assert exc_info.value.error_code == INDEX_PROVISION_CONFLICT


def test_reclaim_unknown_index_name_raises_conflict() -> None:
    backend = _FakeTableBackend()
    registry = _make_registry(backend)
    entry = build_index_registry_entry()
    with pytest.raises(ProvisioningError) as exc_info:
        registry.reclaim(
            entry.index_name,
            observed=entry,
            new_entry=entry.model_copy(update={"claim_token": "t2"}),
        )
    assert exc_info.value.error_code == INDEX_PROVISION_CONFLICT


# --- touch_verified / mark_ready / mark_failed / mark_retired ---------------------------------


def test_touch_verified_bumps_last_verified_at() -> None:
    backend = _FakeTableBackend()
    registry = _make_registry(backend)
    entry = build_index_registry_entry(
        status="ready", claim_token=None, claimed_at=None
    )
    registry.register(entry)

    updated = registry.touch_verified(entry.index_name)

    assert updated.last_verified_at is not None


def test_mark_ready_clears_claim_fields_and_stamps_last_verified_at() -> None:
    backend = _FakeTableBackend()
    registry = _make_registry(backend)
    entry = build_index_registry_entry(claim_token="t1")
    registry.register(entry)

    ready = registry.mark_ready(entry.index_name, expected_claim_token="t1")

    assert ready.status == "ready"
    assert ready.claim_token is None
    assert ready.claimed_at is None
    assert ready.resolved_template is None
    assert ready.last_verified_at is not None


def test_mark_ready_etag_conflict_raises_index_provision_conflict_transient() -> None:
    """The story's explicit `test_ensure_registry_etag_conflict_raises_provision_conflict`
    — targets the registry method directly, since `IndexProvisioner.ensure()` itself
    absorbs this conflict internally."""
    backend = _FakeTableBackend()
    registry = _make_registry(backend)
    entry = build_index_registry_entry(claim_token="t1")
    registry.register(entry)
    registry.reclaim(
        entry.index_name,
        observed=entry,
        new_entry=entry.model_copy(update={"claim_token": "t2"}),
    )

    with pytest.raises(ProvisioningError) as exc_info:
        registry.mark_ready(entry.index_name, expected_claim_token="t1")
    assert exc_info.value.error_code == INDEX_PROVISION_CONFLICT


def test_mark_failed_records_reason() -> None:
    backend = _FakeTableBackend()
    registry = _make_registry(backend)
    entry = build_index_registry_entry(claim_token="t1")
    registry.register(entry)

    failed = registry.mark_failed(entry.index_name, "boom", expected_claim_token="t1")

    assert failed.status == "failed"
    assert failed.last_error_message == "boom"


def test_mark_retired_flips_status() -> None:
    backend = _FakeTableBackend()
    registry = _make_registry(backend)
    entry = build_index_registry_entry(
        status="ready", claim_token=None, claimed_at=None
    )
    registry.register(entry)

    retired = registry.mark_retired(entry.index_name)

    assert retired.status == "retired"


# --- unavailable: 429/5xx exhausted (TRANSIENT) ---------------------------------------------


def test_get_rate_limited_exhausted_raises_index_registry_unavailable() -> None:
    backend = _FakeTableBackend(
        error_sequence=[_http_error(429), _http_error(429), _http_error(429)]
    )
    registry = _make_registry(backend, max_attempts=3)

    with pytest.raises(ProvisioningError) as exc_info:
        registry.get("idx")
    assert exc_info.value.error_code == INDEX_REGISTRY_UNAVAILABLE


def test_get_upstream_5xx_exhausted_raises_index_registry_unavailable() -> None:
    backend = _FakeTableBackend(
        error_sequence=[_http_error(503), _http_error(503), _http_error(503)]
    )
    registry = _make_registry(backend, max_attempts=3)

    with pytest.raises(ProvisioningError) as exc_info:
        registry.get("idx")
    assert exc_info.value.error_code == INDEX_REGISTRY_UNAVAILABLE


def test_get_timeout_exhausted_raises_index_registry_unavailable() -> None:
    class _SlowBackend(_FakeTableBackend):
        def query(
            self, *, partition_key: str | None = None, row_key: str | None = None
        ) -> list[_TableEntityRecord]:
            time_module.sleep(0.2)
            return super().query(partition_key=partition_key, row_key=row_key)

    registry = _make_registry(_SlowBackend(), max_attempts=2, timeout_seconds=0.01)

    with pytest.raises(ProvisioningError) as exc_info:
        registry.get("idx")
    assert exc_info.value.error_code == INDEX_REGISTRY_UNAVAILABLE


# --- corrupt stored entity -------------------------------------------------------------------


def test_get_raises_index_registry_unavailable_for_corrupt_stored_entity() -> None:
    backend = _FakeTableBackend()
    backend.seed(
        _TableEntityRecord(
            partition_key="hr", row_key="idx-1", data={"index_name": "idx-1"}, etag="1"
        )
    )
    registry = _make_registry(backend)

    with pytest.raises(ProvisioningError) as exc_info:
        registry.get("idx-1")
    assert exc_info.value.error_code == INDEX_REGISTRY_UNAVAILABLE


# --- resolved_template JSON round trip ----------------------------------------------------------


def test_resolved_template_round_trips_through_json_while_provisioning() -> None:
    backend = _FakeTableBackend()
    registry = _make_registry(backend)
    entry = build_index_registry_entry(claim_token="t1")
    registry.register(entry)

    fetched = registry.get(entry.index_name)

    assert fetched is not None
    assert fetched.resolved_template == entry.resolved_template


def test_entry_to_entity_data_omits_none_optional_fields() -> None:
    entry = build_index_registry_entry(
        status="ready",
        claim_token=None,
        claimed_at=None,
        resolved_template=None,
        last_error_message=None,
        document_count=None,
    )
    data = entry_to_entity_data(entry)
    for key in (
        "claim_token",
        "claimed_at",
        "resolved_template_json",
        "last_error_message",
        "document_count",
    ):
        assert key not in data
