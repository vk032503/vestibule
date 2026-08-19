"""Unit tests for TableStorageScenarioStore (REQ-010).

No live API calls — every test injects a recorded-response fake `_backend` (satisfying
`_TableBackend`'s duck-typed contract) in place of the real Azure Table Storage SDK
client (same convention as `test_azure_ai_search.py`). The one exception is the
dependency-missing test, which relies on `azure-data-tables` genuinely not being
installed in this project's test environment (it is the optional `table_storage`
extra, never a hard dependency) — a fully hermetic, realistic exercise of the
lazy-import fail-fast path.
"""

from __future__ import annotations

import time as time_module
from datetime import datetime, timezone
from typing import Any, cast

import pytest
from azure.core.exceptions import HttpResponseError

from vestibule.scenario.model import (
    SCENARIO_CONFLICT,
    SCENARIO_INVALID,
    SCENARIO_STORE_DEPENDENCY_MISSING,
    SCENARIO_STORE_UNAVAILABLE,
    ChunkerSettings,
    EmbedderSettings,
    IndexerSettings,
    Scenario,
    ScenarioError,
)
from vestibule.scenario.stores.table_storage_store import (
    TableStorageScenarioStore,
    _TableEntityRecord,
)

_FIXED_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _scenario(
    scenario_id: str = "hr-1", vertical: str = "hr", *, enabled: bool = True
) -> Scenario:
    return Scenario(
        scenario_id=scenario_id,
        vertical=vertical,
        config_version="1",
        index_name=f"{scenario_id}-idx",
        chunker=ChunkerSettings(
            target_chunk_tokens=512,
            overlap_tokens=64,
            min_chunk_tokens=40,
            max_chunk_tokens=800,
        ),
        embedder=EmbedderSettings(
            adapter="azure_openai",
            model="text-embedding-3-large",
            target_dimensions=1024,
        ),
        indexer=IndexerSettings(
            metric="cosine",
            dimensions=1024,
            hnsw_m=4,
            hnsw_ef_construction=400,
            hnsw_ef_search=500,
        ),
        acl_source="envelope",
        default_trust_tier="internal",
        enabled=enabled,
        created_at=_FIXED_TIME,
        updated_at=_FIXED_TIME,
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


def _make_store(
    backend: _FakeTableBackend, *, max_attempts: int = 3, timeout_seconds: float = 30.0
) -> TableStorageScenarioStore:
    return TableStorageScenarioStore(
        connection_string="UseDevelopmentStorage=true",
        table_name="VestibuleScenarios",
        max_attempts=max_attempts,
        backoff_base_seconds=0.01,
        backoff_max_seconds=0.02,
        timeout_seconds=timeout_seconds,
        _sleep=lambda _seconds: None,
        _backend=backend,
    )


# --- dependency missing (PERMANENT, fail fast) -----------------------------------------------


def test_missing_dependency_raises_scenario_store_dependency_missing_permanent() -> (
    None
):
    with pytest.raises(ScenarioError) as exc_info:
        TableStorageScenarioStore(
            connection_string="UseDevelopmentStorage=true",
            table_name="VestibuleScenarios",
        )
    assert exc_info.value.error_code == SCENARIO_STORE_DEPENDENCY_MISSING


# --- is_writable -------------------------------------------------------------------------------


def test_table_storage_store_is_writable_is_true() -> None:
    store = _make_store(_FakeTableBackend())
    assert store.is_writable is True


# --- get / get_by_vertical / list_all ------------------------------------------------------


def test_get_returns_none_when_absent() -> None:
    store = _make_store(_FakeTableBackend())
    assert store.get("missing") is None


def test_get_returns_stored_scenario() -> None:
    backend = _FakeTableBackend()
    store = _make_store(backend)
    written = store.upsert(_scenario("hr-1", "hr"))

    fetched = store.get("hr-1")

    assert fetched == written


def test_get_by_vertical_excludes_disabled_scenarios() -> None:
    backend = _FakeTableBackend()
    store = _make_store(backend)
    store.upsert(_scenario("hr-1", "hr", enabled=False))

    assert store.get_by_vertical("hr") is None


def test_list_all_default_excludes_disabled_includes_with_flag() -> None:
    backend = _FakeTableBackend()
    store = _make_store(backend)
    store.upsert(_scenario("hr-1", "hr", enabled=True))
    store.upsert(_scenario("legal-1", "legal", enabled=False))

    assert {s.scenario_id for s in store.list_all()} == {"hr-1"}
    assert {s.scenario_id for s in store.list_all(include_disabled=True)} == {
        "hr-1",
        "legal-1",
    }


# --- upsert bumps config_version/updated_at (AC4) -------------------------------------------


def test_upsert_new_scenario_sets_config_version_to_one() -> None:
    store = _make_store(_FakeTableBackend())
    written = store.upsert(_scenario("hr-1", "hr"))
    assert written.config_version == "1"


def test_upsert_existing_scenario_increments_config_version_and_updated_at() -> None:
    store = _make_store(_FakeTableBackend())
    first = store.upsert(_scenario("hr-1", "hr"))

    second = store.upsert(_scenario("hr-1", "hr"))

    assert second.config_version == "2"
    assert second.updated_at >= first.updated_at
    assert second.created_at == first.created_at


def test_upsert_rejects_invalid_scenario_before_any_write() -> None:
    backend = _FakeTableBackend()
    store = _make_store(backend)
    bad = _scenario("hr-1", "hr").model_copy(
        update={
            "indexer": IndexerSettings(
                metric="cosine",
                dimensions=99999,
                hnsw_m=4,
                hnsw_ef_construction=400,
                hnsw_ef_search=500,
            )
        }
    )
    # model_copy bypasses Scenario's own validators — the store re-validates on upsert.
    with pytest.raises(ScenarioError) as exc_info:
        store.upsert(bad)
    assert exc_info.value.error_code == SCENARIO_INVALID
    assert backend.query() == []


# --- delete --------------------------------------------------------------------------------


def test_delete_removes_and_returns_true() -> None:
    store = _make_store(_FakeTableBackend())
    store.upsert(_scenario("hr-1", "hr"))

    assert store.delete("hr-1") is True
    assert store.get("hr-1") is None


def test_delete_unknown_scenario_returns_false() -> None:
    store = _make_store(_FakeTableBackend())
    assert store.delete("missing") is False


# --- conflict (TRANSIENT, never retried) ----------------------------------------------------


def test_upsert_etag_conflict_raises_scenario_conflict_transient() -> None:
    # Each upsert() makes exactly two backend calls (get_entity, upsert_entity): the
    # first upsert's pair (None, None) succeeds; the second upsert's get_entity (None)
    # succeeds, then its upsert_entity raises the 412 ETag conflict.
    backend = _FakeTableBackend(error_sequence=[None, None, None, _http_error(412)])
    store = _make_store(backend)
    store.upsert(_scenario("hr-1", "hr"))

    with pytest.raises(ScenarioError) as exc_info:
        store.upsert(_scenario("hr-1", "hr"))
    assert exc_info.value.error_code == SCENARIO_CONFLICT


# --- unavailable: 429/5xx exhausted (TRANSIENT) ---------------------------------------------


def test_get_rate_limited_exhausted_raises_scenario_store_unavailable() -> None:
    backend = _FakeTableBackend(
        error_sequence=[_http_error(429), _http_error(429), _http_error(429)]
    )
    store = _make_store(backend, max_attempts=3)

    with pytest.raises(ScenarioError) as exc_info:
        store.get("hr-1")
    assert exc_info.value.error_code == SCENARIO_STORE_UNAVAILABLE


def test_get_upstream_5xx_exhausted_raises_scenario_store_unavailable() -> None:
    backend = _FakeTableBackend(
        error_sequence=[_http_error(503), _http_error(503), _http_error(503)]
    )
    store = _make_store(backend, max_attempts=3)

    with pytest.raises(ScenarioError) as exc_info:
        store.get("hr-1")
    assert exc_info.value.error_code == SCENARIO_STORE_UNAVAILABLE


# --- timeout (TRANSIENT) --------------------------------------------------------------------


def test_get_timeout_exhausted_raises_scenario_store_unavailable() -> None:
    class _SlowBackend(_FakeTableBackend):
        def query(
            self, *, partition_key: str | None = None, row_key: str | None = None
        ) -> list[_TableEntityRecord]:
            time_module.sleep(0.2)
            return super().query(partition_key=partition_key, row_key=row_key)

    store = _make_store(_SlowBackend(), max_attempts=2, timeout_seconds=0.01)

    with pytest.raises(ScenarioError) as exc_info:
        store.get("hr-1")
    assert exc_info.value.error_code == SCENARIO_STORE_UNAVAILABLE


# --- index_template_id round trip (REQ-011 Assumption A1) ------------------------------------


def test_table_storage_round_trip_preserves_index_template_id() -> None:
    store = _make_store(_FakeTableBackend())
    written = store.upsert(
        _scenario("hr-1", "hr").model_copy(update={"index_template_id": "standard-v1"})
    )
    assert written.index_template_id == "standard-v1"

    fetched = store.get("hr-1")
    assert fetched is not None
    assert fetched.index_template_id == "standard-v1"


def test_table_storage_round_trip_defaults_index_template_id_to_none_when_absent() -> (
    None
):
    backend = _FakeTableBackend()
    # Simulates a pre-REQ-011 entity written before index_template_id existed: no
    # such property at all in the stored data.
    backend.seed(
        _TableEntityRecord(
            partition_key="hr",
            row_key="hr-1",
            data={
                "scenario_id": "hr-1",
                "vertical": "hr",
                "config_version": "1",
                "index_name": "hr-1-idx",
                "acl_source": "envelope",
                "default_trust_tier": "internal",
                "enabled": True,
                "created_at": _FIXED_TIME.isoformat(),
                "updated_at": _FIXED_TIME.isoformat(),
                "chunker_json": ChunkerSettings(
                    target_chunk_tokens=512,
                    overlap_tokens=64,
                    min_chunk_tokens=40,
                    max_chunk_tokens=800,
                ).model_dump_json(),
                "embedder_json": EmbedderSettings(
                    adapter="azure_openai",
                    model="text-embedding-3-large",
                    target_dimensions=1024,
                ).model_dump_json(),
                "indexer_json": IndexerSettings(
                    metric="cosine",
                    dimensions=1024,
                    hnsw_m=4,
                    hnsw_ef_construction=400,
                    hnsw_ef_search=500,
                ).model_dump_json(),
            },
            etag="1",
        )
    )
    store = _make_store(backend)

    fetched = store.get("hr-1")

    assert fetched is not None
    assert fetched.index_template_id is None


# --- corrupt stored entity -------------------------------------------------------------------


def test_get_raises_scenario_invalid_for_corrupt_stored_entity() -> None:
    backend = _FakeTableBackend()
    backend.seed(
        _TableEntityRecord(
            partition_key="hr", row_key="hr-1", data={"scenario_id": "hr-1"}, etag="1"
        )
    )
    store = _make_store(backend)

    with pytest.raises(ScenarioError) as exc_info:
        store.get("hr-1")
    assert exc_info.value.error_code == SCENARIO_INVALID
