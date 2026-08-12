"""Unit tests for AzureAISearchIndexer (REQ-008).

No live API calls — every test injects a recorded-response fake `_backend` (satisfying
`_SearchBackend`'s duck-typed contract) in place of the real Azure AI Search SDK clients
(same convention as `test_azure_openai.py`/`test_fastembed.py`). The one exception is
the dependency-missing test, which relies on `azure-search-documents` genuinely not
being installed in this project's test environment (it is the optional `azure_search`
extra, never a hard dependency) — a fully hermetic, realistic exercise of the
lazy-import fail-fast path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

import pytest
from azure.core.exceptions import HttpResponseError

from vestibule.indexer.adapters.azure_ai_search import AzureAISearchIndexer
from vestibule.indexer.model import (
    INDEXER_DEPENDENCY_MISSING,
    INDEXER_RATE_LIMITED,
    INDEXER_RECORD_TOO_LARGE,
    INDEXER_SCHEMA_CONFLICT,
    INDEXER_TIMEOUT,
    INDEXER_UPSTREAM_ERROR,
    IndexerError,
    IndexRecord,
)


def _record(chunk_id: str = "c1", *, vector: list[float] | None = None) -> IndexRecord:
    return IndexRecord(
        chunk_id=chunk_id,
        doc_id="d" * 64,
        doc_version="v1",
        vector=vector if vector is not None else [1.0, 0.0],
        text="hello",
        position=0,
        section_path=None,
        element_types=["paragraph"],
        strategy="recursive",
        allowed_groups=["group-a"],
        trust_tier="internal",
        embedding_model="fake-model",
        embedding_dimensions=2,
        config_version="v1",
        indexed_at=datetime.now(timezone.utc),
    )


class _FakeResponse:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.reason = "error"
        self.headers = headers or {}


def _http_error(
    status_code: int, headers: dict[str, str] | None = None
) -> HttpResponseError:
    # _FakeResponse deliberately implements only the subset of azure-core's
    # `_HttpResponseCommonAPI` protocol this module's own code actually reads
    # (status_code/headers) — cast to satisfy HttpResponseError's broader signature.
    fake_response = cast(Any, _FakeResponse(status_code, headers))
    return HttpResponseError(response=fake_response)


class _FakeBackend:
    """Recorded-response fake standing in for `_RealSearchBackend`."""

    def __init__(
        self,
        *,
        schema: tuple[int, str] | None = None,
        upload_results: list[tuple[str, bool, str | None]] | None = None,
        error_sequence: list[Exception | None] | None = None,
        docs_for_doc_id: list[dict[str, Any]] | None = None,
    ) -> None:
        self.schema = schema
        self.upload_results = upload_results or []
        self._error_sequence = list(error_sequence or [])
        self.docs_for_doc_id = docs_for_doc_id or []
        self.create_calls: list[Any] = []
        self.upload_calls: list[list[dict[str, Any]]] = []
        self.delete_calls: list[list[str]] = []
        self.attempt_count = 0

    def _maybe_raise(self) -> None:
        self.attempt_count += 1
        if self._error_sequence:
            error = self._error_sequence.pop(0)
            if error is not None:
                raise error

    def get_index_schema(self, index_name: str) -> tuple[int, str] | None:
        self._maybe_raise()
        return self.schema

    def create_or_update_index(self, definition: Any) -> None:
        self._maybe_raise()
        self.create_calls.append(definition)

    def upload_documents(
        self, index_name: str, documents: list[dict[str, Any]]
    ) -> list[tuple[str, bool, str | None]]:
        self._maybe_raise()
        self.upload_calls.append(documents)
        return self.upload_results

    def delete_documents(self, index_name: str, chunk_ids: list[str]) -> None:
        self._maybe_raise()
        self.delete_calls.append(chunk_ids)

    def documents_for_doc_id(
        self, index_name: str, doc_id: str
    ) -> list[dict[str, Any]]:
        self._maybe_raise()
        return self.docs_for_doc_id


def _make_adapter(
    backend: _FakeBackend, *, max_attempts: int = 3, timeout_seconds: float = 30.0
) -> AzureAISearchIndexer:
    return AzureAISearchIndexer(
        endpoint="https://fake.example.com",
        api_key="fake-key",
        index_name="idx",
        max_attempts=max_attempts,
        backoff_base_seconds=0.01,
        backoff_max_seconds=0.02,
        timeout_seconds=timeout_seconds,
        _sleep=lambda _seconds: None,
        _backend=backend,
    )


# --- dependency missing (PERMANENT, fail fast) -----------------------------------------------


def test_azure_search_missing_dependency_raises_indexer_dependency_missing_permanent() -> (
    None
):
    with pytest.raises(IndexerError) as exc_info:
        AzureAISearchIndexer(
            endpoint="https://fake.example.com", api_key="fake-key", index_name="idx"
        )

    assert exc_info.value.error_code == INDEXER_DEPENDENCY_MISSING


# --- declared capabilities ----------------------------------------------------------------------


def test_default_capabilities() -> None:
    adapter = _make_adapter(_FakeBackend())
    assert adapter.index_name == "idx"
    assert adapter.max_batch_size == 100
    assert adapter.supports_hybrid is True


# --- ensure_schema -----------------------------------------------------------------------------


def test_ensure_schema_creates_index_when_absent() -> None:
    backend = _FakeBackend(schema=None)
    adapter = _make_adapter(backend)

    adapter.ensure_schema(4, "cosine")

    assert len(backend.create_calls) == 1
    assert backend.create_calls[0].dimensions == 4
    assert backend.create_calls[0].metric == "cosine"


def test_ensure_schema_idempotent_when_existing_schema_matches() -> None:
    backend = _FakeBackend(schema=(4, "cosine"))
    adapter = _make_adapter(backend)

    adapter.ensure_schema(4, "cosine")
    adapter.ensure_schema(4, "cosine")

    assert backend.create_calls == []


def test_ensure_schema_conflict_raises_schema_conflict_permanent() -> None:
    backend = _FakeBackend(schema=(8, "cosine"))
    adapter = _make_adapter(backend)

    with pytest.raises(IndexerError) as exc_info:
        adapter.ensure_schema(4, "cosine")

    assert exc_info.value.error_code == INDEXER_SCHEMA_CONFLICT
    assert backend.create_calls == []


# --- upsert --------------------------------------------------------------------------------------


def test_upsert_all_succeed() -> None:
    backend = _FakeBackend(upload_results=[("c1", True, None), ("c2", True, None)])
    adapter = _make_adapter(backend)

    outcome = adapter.upsert([_record("c1"), _record("c2")])

    assert outcome.succeeded == ["c1", "c2"]
    assert outcome.failed == []


def test_upsert_reports_per_item_failure() -> None:
    backend = _FakeBackend(
        upload_results=[("c1", True, None), ("c2", False, "bad request")]
    )
    adapter = _make_adapter(backend)

    outcome = adapter.upsert([_record("c1"), _record("c2")])

    assert outcome.succeeded == ["c1"]
    assert outcome.failed == [("c2", "bad request")]


def test_upsert_record_too_large_raises_before_any_write() -> None:
    backend = _FakeBackend(upload_results=[("c1", True, None)])
    adapter = _make_adapter(backend)
    huge_vector = [0.1] * 5_000_000

    with pytest.raises(IndexerError) as exc_info:
        adapter.upsert([_record("c1", vector=huge_vector)])

    assert exc_info.value.error_code == INDEXER_RECORD_TOO_LARGE
    assert backend.upload_calls == []


# --- delete / count / get_doc_version -------------------------------------------------------------


def test_delete_by_doc_id_deletes_and_returns_count() -> None:
    backend = _FakeBackend(docs_for_doc_id=[{"chunk_id": "c1"}, {"chunk_id": "c2"}])
    adapter = _make_adapter(backend)

    deleted = adapter.delete_by_doc_id("d1")

    assert deleted == 2
    assert backend.delete_calls == [["c1", "c2"]]


def test_delete_by_doc_id_unknown_doc_returns_zero_without_deleting() -> None:
    backend = _FakeBackend(docs_for_doc_id=[])
    adapter = _make_adapter(backend)

    assert adapter.delete_by_doc_id("unknown") == 0
    assert backend.delete_calls == []


def test_count_by_doc_id() -> None:
    backend = _FakeBackend(docs_for_doc_id=[{"chunk_id": "c1"}])
    adapter = _make_adapter(backend)
    assert adapter.count_by_doc_id("d1") == 1


def test_get_doc_version_returns_stored_version() -> None:
    backend = _FakeBackend(docs_for_doc_id=[{"chunk_id": "c1", "doc_version": "v9"}])
    adapter = _make_adapter(backend)
    assert adapter.get_doc_version("d1") == "v9"


def test_get_doc_version_unknown_doc_returns_none() -> None:
    backend = _FakeBackend(docs_for_doc_id=[])
    adapter = _make_adapter(backend)
    assert adapter.get_doc_version("unknown") is None


# --- retry: rate limited / upstream / unmapped ------------------------------------------------


def test_upsert_rate_limited_exhausted_raises_indexer_rate_limited() -> None:
    backend = _FakeBackend(
        upload_results=[("c1", True, None)],
        error_sequence=[_http_error(429), _http_error(429), _http_error(429)],
    )
    adapter = _make_adapter(backend, max_attempts=3)

    with pytest.raises(IndexerError) as exc_info:
        adapter.upsert([_record("c1")])

    assert exc_info.value.error_code == INDEXER_RATE_LIMITED


def test_upsert_upstream_5xx_exhausted_raises_indexer_upstream_error() -> None:
    backend = _FakeBackend(
        upload_results=[("c1", True, None)],
        error_sequence=[_http_error(503), _http_error(503)],
    )
    adapter = _make_adapter(backend, max_attempts=2)

    with pytest.raises(IndexerError) as exc_info:
        adapter.upsert([_record("c1")])

    assert exc_info.value.error_code == INDEXER_UPSTREAM_ERROR


def test_upsert_retries_transient_failure_then_succeeds() -> None:
    backend = _FakeBackend(
        upload_results=[("c1", True, None)], error_sequence=[_http_error(429)]
    )
    adapter = _make_adapter(backend, max_attempts=3)

    outcome = adapter.upsert([_record("c1")])

    assert outcome.succeeded == ["c1"]


def test_upsert_unmapped_http_error_propagates_unchanged() -> None:
    backend = _FakeBackend(
        upload_results=[("c1", True, None)], error_sequence=[_http_error(400)]
    )
    adapter = _make_adapter(backend, max_attempts=3)

    with pytest.raises(HttpResponseError):
        adapter.upsert([_record("c1")])


def test_upsert_generic_exception_is_not_retried_and_propagates_unchanged() -> None:
    backend = _FakeBackend(
        upload_results=[("c1", True, None)], error_sequence=[RuntimeError("boom")]
    )
    adapter = _make_adapter(backend, max_attempts=3)

    with pytest.raises(RuntimeError, match="boom"):
        adapter.upsert([_record("c1")])

    assert backend.attempt_count == 1  # never retried


def test_upsert_honors_retry_after_header_over_exponential_backoff() -> None:
    backend = _FakeBackend(
        upload_results=[("c1", True, None)],
        error_sequence=[_http_error(429, {"retry-after": "7.5"})],
    )
    observed_delays: list[float] = []
    adapter = AzureAISearchIndexer(
        endpoint="https://fake.example.com",
        api_key="fake-key",
        index_name="idx",
        max_attempts=3,
        backoff_base_seconds=0.01,
        backoff_max_seconds=0.02,
        _sleep=observed_delays.append,
        _backend=backend,
    )

    adapter.upsert([_record("c1")])

    assert observed_delays == [7.5]


# --- timeout --------------------------------------------------------------------------------------


def test_upsert_timeout_exhausted_raises_indexer_timeout() -> None:
    import time as time_module

    class _SlowBackend(_FakeBackend):
        def upload_documents(
            self, index_name: str, documents: list[dict[str, Any]]
        ) -> list[tuple[str, bool, str | None]]:
            time_module.sleep(0.2)
            return super().upload_documents(index_name, documents)

    backend = _SlowBackend(upload_results=[("c1", True, None)])
    adapter = _make_adapter(backend, max_attempts=2, timeout_seconds=0.01)

    with pytest.raises(IndexerError) as exc_info:
        adapter.upsert([_record("c1")])

    assert exc_info.value.error_code == INDEXER_TIMEOUT
