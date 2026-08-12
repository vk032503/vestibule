"""Unit tests for the Indexer orchestrator (REQ-008)."""

from __future__ import annotations

import re
import string
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vestibule.analyzer.model import ElementType
from vestibule.chunker.model import Chunk
from vestibule.embedder.model import EmbeddedChunk
from vestibule.envelope.model import ArrivalEnvelope, TrustTier, build_envelope
from vestibule.identity.derive import derive_chunk_id
from vestibule.indexer.adapters.base import IndexerAdapter, UpsertOutcome
from vestibule.indexer.adapters.in_memory import InMemoryIndexer
from vestibule.indexer.conftest import embedded_chunk, make_envelope
from vestibule.indexer.indexer import Indexer, IndexerConfig, RetryConfig
from vestibule.indexer.model import (
    INDEXER_DIMENSION_MISMATCH,
    INDEXER_EMPTY_RECORDS,
    INDEXER_INTERNAL,
    INDEXER_MIXED_MODEL_BATCH,
    INDEXER_PARTIAL_BATCH_FAILURE,
    INDEXER_RATE_LIMITED,
    INDEXER_RECORD_TOO_LARGE,
    INDEXER_SCHEMA_CONFLICT,
    INDEXER_TIMEOUT,
    INDEXER_UPSTREAM_ERROR,
    IndexerError,
    IndexRecord,
)
from vestibule.ledger.store import (
    EnvelopeSummary,
    InMemoryLedgerStore,
    LedgerTransitionInvalid,
    Status,
)


def _embedded_chunk_at(
    doc_id: str, position: int, *, embedding_model: str = "fake-model", dims: int = 4
) -> EmbeddedChunk:
    """Builds an `EmbeddedChunk` at an exact `position` (bypasses the shared conftest
    counter) — needed when a test must control which `chunk_id`s collide/don't."""
    chunk = Chunk(
        chunk_id=derive_chunk_id(doc_id, position),
        doc_id=doc_id,
        position=position,
        element_types=[ElementType.PARAGRAPH],
        text=f"chunk {position}",
        token_count=2,
        metadata={"strategy": "recursive", "section_path": None},
    )
    return EmbeddedChunk(
        chunk=chunk,
        vector=[1.0] * dims,
        embedding_model=embedding_model,
        embedding_dimensions=dims,
        truncated_from=None,
    )


class _FakeAdapter(IndexerAdapter):
    """A test-double `IndexerAdapter` backed by a real `InMemoryIndexer` for realistic
    upsert/delete/count/get_doc_version semantics, with configurable failure injection
    layered on top."""

    def __init__(
        self,
        *,
        max_batch_size: int = 32,
        index_name: str = "fake-index",
        supports_hybrid: bool = False,
        raise_exc: Exception | None = None,
        fail_chunk_ids_once: set[str] | None = None,
    ) -> None:
        self._backing = InMemoryIndexer(index_name=index_name)
        self._max_batch_size = max_batch_size
        self._supports_hybrid = supports_hybrid
        self._raise_exc = raise_exc
        self._fail_chunk_ids_once = set(fail_chunk_ids_once or set())
        self._already_failed_once: set[str] = set()
        self.batch_calls: list[list[str]] = []
        self.received_records: list[IndexRecord] = []
        self.ensure_schema_calls: list[tuple[int, str]] = []

    @property
    def index_name(self) -> str:
        return self._backing.index_name

    @property
    def max_batch_size(self) -> int:
        return self._max_batch_size

    @property
    def supports_hybrid(self) -> bool:
        return self._supports_hybrid

    def ensure_schema(self, dimensions: int, metric: str) -> None:
        self.ensure_schema_calls.append((dimensions, metric))
        self._backing.ensure_schema(dimensions, metric)

    def upsert(self, records: list[IndexRecord]) -> UpsertOutcome:
        self.batch_calls.append([r.chunk_id for r in records])
        self.received_records.extend(records)
        if self._raise_exc is not None:
            raise self._raise_exc
        to_fail = [
            r
            for r in records
            if r.chunk_id in self._fail_chunk_ids_once
            and r.chunk_id not in self._already_failed_once
        ]
        fail_ids = {r.chunk_id for r in to_fail}
        to_succeed = [r for r in records if r.chunk_id not in fail_ids]
        for r in to_fail:
            self._already_failed_once.add(r.chunk_id)
        succeeded = self._backing.upsert(to_succeed).succeeded
        failed = [(r.chunk_id, "simulated failure") for r in to_fail]
        return UpsertOutcome(succeeded=succeeded, failed=failed)

    def delete_by_doc_id(self, doc_id: str) -> int:
        return self._backing.delete_by_doc_id(doc_id)

    def count_by_doc_id(self, doc_id: str) -> int:
        return self._backing.count_by_doc_id(doc_id)

    def get_doc_version(self, doc_id: str) -> str | None:
        return self._backing.get_doc_version(doc_id)


def _build_indexer(
    *, config: IndexerConfig | None = None, adapter: IndexerAdapter | None = None
) -> tuple[Indexer, InMemoryLedgerStore, _FakeAdapter]:
    ledger = InMemoryLedgerStore()
    fake_adapter = adapter if isinstance(adapter, _FakeAdapter) else _FakeAdapter()
    indexer = Indexer(
        ledger, config or IndexerConfig(dimensions=4), adapter or fake_adapter
    )
    return indexer, ledger, fake_adapter


def _seed_indexing(ledger: InMemoryLedgerStore, doc_id: str) -> None:
    """Advances a freshly-created row to Status.INDEXING — Indexer's entry state."""
    ledger.create(doc_id, EnvelopeSummary(config_version="v1"))
    for status in (
        Status.ANALYZING,
        Status.CHUNKING,
        Status.EMBEDDING,
        Status.INDEXING,
    ):
        ledger.transition(doc_id, status, status)


def _advance_to(ledger: InMemoryLedgerStore, doc_id: str, target: Status) -> None:
    """Advances a freshly-created row through legal forward edges up to `target`."""
    ledger.create(doc_id, EnvelopeSummary(config_version="v1"))
    for status in (
        Status.ANALYZING,
        Status.CHUNKING,
        Status.EMBEDDING,
        Status.INDEXING,
        Status.INDEXED,
    ):
        ledger.transition(doc_id, status, status)
        if status == target:
            return


# --- happy path -----------------------------------------------------------------------------


def test_index_success_transitions_indexing_to_indexed() -> None:
    indexer, ledger, _adapter = _build_indexer()
    envelope = make_envelope("happy-path")
    _seed_indexing(ledger, envelope.doc_id)

    result = indexer.index(envelope, [embedded_chunk("hello world")])

    assert result.chunks_upserted == 1
    assert result.chunks_deleted == 0
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.INDEXED


def test_index_result_carries_index_name_model_and_dimensions() -> None:
    adapter = _FakeAdapter(index_name="my-index")
    indexer, ledger, _ = _build_indexer(adapter=adapter)
    envelope = make_envelope("result-fields")
    _seed_indexing(ledger, envelope.doc_id)

    result = indexer.index(
        envelope, [embedded_chunk("a", embedding_model="m1", embedding_dimensions=4)]
    )

    assert result.index_name == "my-index"
    assert result.embedding_model == "m1"
    assert result.embedding_dimensions == 4


def test_index_record_carries_allowed_groups_trust_tier_and_doc_version() -> None:
    adapter = _FakeAdapter()
    indexer, ledger, _ = _build_indexer(adapter=adapter)
    envelope = build_envelope(
        source="src",
        blob_path="allowed-groups-test",
        content_hash="a" * 64,
        trust_tier=TrustTier.CONFIDENTIAL,
        allowed_groups=["group-a", "group-b"],
    )
    _seed_indexing(ledger, envelope.doc_id)

    indexer.index(envelope, [embedded_chunk("a")])

    record = adapter.received_records[0]
    assert record.allowed_groups == ["group-a", "group-b"]
    assert record.trust_tier == "confidential"
    assert record.doc_version == "a" * 64


def test_index_record_config_version_from_ledger_row() -> None:
    adapter = _FakeAdapter()
    ledger = InMemoryLedgerStore()
    indexer = Indexer(ledger, IndexerConfig(dimensions=4), adapter)
    envelope = make_envelope("config-version")
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="config-v42"))
    for status in (
        Status.ANALYZING,
        Status.CHUNKING,
        Status.EMBEDDING,
        Status.INDEXING,
    ):
        ledger.transition(envelope.doc_id, status, status)

    indexer.index(envelope, [embedded_chunk("a")])

    assert adapter.received_records[0].config_version == "config-v42"


# --- batching (story test plan) --------------------------------------------------------------


def test_batching_250_records_with_max_batch_size_100_makes_3_upsert_calls() -> None:
    adapter = _FakeAdapter(max_batch_size=100)
    indexer, ledger, _ = _build_indexer(adapter=adapter)
    envelope = make_envelope("batching-250")
    _seed_indexing(ledger, envelope.doc_id)
    chunks = [embedded_chunk(f"chunk {i}") for i in range(250)]

    result = indexer.index(envelope, chunks)

    assert result.chunks_upserted == 250
    assert len(adapter.batch_calls) == 3
    assert [len(batch) for batch in adapter.batch_calls] == [100, 100, 50]


def test_batch_size_config_overrides_adapter_default_when_smaller() -> None:
    adapter = _FakeAdapter(max_batch_size=32)
    indexer, ledger, _ = _build_indexer(
        adapter=adapter, config=IndexerConfig(dimensions=4, batch_size=10)
    )
    envelope = make_envelope("batch-size-override")
    _seed_indexing(ledger, envelope.doc_id)
    chunks = [embedded_chunk(f"chunk {i}") for i in range(25)]

    indexer.index(envelope, chunks)

    assert [len(batch) for batch in adapter.batch_calls] == [10, 10, 5]


def test_batch_size_config_capped_at_adapter_max_batch_size() -> None:
    adapter = _FakeAdapter(max_batch_size=8)
    indexer, ledger, _ = _build_indexer(
        adapter=adapter, config=IndexerConfig(dimensions=4, batch_size=100)
    )
    envelope = make_envelope("batch-size-capped")
    _seed_indexing(ledger, envelope.doc_id)
    chunks = [embedded_chunk(f"chunk {i}") for i in range(20)]

    indexer.index(envelope, chunks)

    assert [len(batch) for batch in adapter.batch_calls] == [8, 8, 4]


# --- idempotency (story test plan) -----------------------------------------------------------


def test_indexing_same_chunk_list_twice_leaves_same_chunk_count() -> None:
    adapter = _FakeAdapter()
    indexer, ledger, _ = _build_indexer(adapter=adapter)
    envelope = make_envelope("idempotency")
    _seed_indexing(ledger, envelope.doc_id)
    chunks = [embedded_chunk(f"chunk {i}") for i in range(5)]

    indexer.index(envelope, chunks)
    first_count = adapter.count_by_doc_id(envelope.doc_id)
    indexer.index(envelope, chunks)
    second_count = adapter.count_by_doc_id(envelope.doc_id)

    assert first_count == 5
    assert second_count == 5


# --- version change / delete-before-upsert (story test plan) -----------------------------------


def test_version_change_shrinking_doc_purges_orphan_chunks() -> None:
    # A terminal Status.INDEXED ledger row can never transition again (Contract #3), so
    # a second ingestion "run" for the same doc_id is modeled here as its own ledger row
    # (own InMemoryLedgerStore/Indexer instance) sharing the same adapter/vector-store —
    # exactly what persists across real re-ingestions, unlike the per-run ledger.
    adapter = _FakeAdapter()
    envelope_v1 = _envelope_with_version("shrink-doc", content_hash="1" * 64)
    envelope_v2 = _envelope_with_version("shrink-doc", content_hash="2" * 64)
    assert envelope_v1.doc_id == envelope_v2.doc_id
    doc_id = envelope_v1.doc_id

    ledger_v1 = InMemoryLedgerStore()
    indexer_v1 = Indexer(ledger_v1, IndexerConfig(dimensions=4), adapter)
    _seed_indexing(ledger_v1, doc_id)
    indexer_v1.index(envelope_v1, [_embedded_chunk_at(doc_id, i) for i in range(10)])
    assert adapter.count_by_doc_id(doc_id) == 10

    ledger_v2 = InMemoryLedgerStore()
    indexer_v2 = Indexer(ledger_v2, IndexerConfig(dimensions=4), adapter)
    _seed_indexing(ledger_v2, doc_id)
    result = indexer_v2.index(
        envelope_v2, [_embedded_chunk_at(doc_id, i) for i in range(6)]
    )

    assert adapter.count_by_doc_id(doc_id) == 6
    assert result.chunks_deleted == 10
    assert result.chunks_upserted == 6


def test_delete_before_upsert_disabled_by_config_leaves_orphans() -> None:
    adapter = _FakeAdapter()
    config = IndexerConfig(dimensions=4, delete_before_upsert_on_version_change=False)
    envelope_v1 = _envelope_with_version("disabled-delete", content_hash="1" * 64)
    envelope_v2 = _envelope_with_version("disabled-delete", content_hash="2" * 64)
    doc_id = envelope_v1.doc_id

    ledger_v1 = InMemoryLedgerStore()
    indexer_v1 = Indexer(ledger_v1, config, adapter)
    _seed_indexing(ledger_v1, doc_id)
    indexer_v1.index(envelope_v1, [_embedded_chunk_at(doc_id, i) for i in range(10)])

    ledger_v2 = InMemoryLedgerStore()
    indexer_v2 = Indexer(ledger_v2, config, adapter)
    _seed_indexing(ledger_v2, doc_id)
    result = indexer_v2.index(
        envelope_v2, [_embedded_chunk_at(doc_id, i) for i in range(6)]
    )

    assert result.chunks_deleted == 0
    assert adapter.count_by_doc_id(doc_id) == 10  # orphaned positions 6-9 remain


def _envelope_with_version(label: str, *, content_hash: str) -> ArrivalEnvelope:
    """Builds an `ArrivalEnvelope` for a fixed `(source, blob_path)` pair keyed by
    `label` — same `label` always yields the same `doc_id` (Contract #2), so two calls
    with the same `label` and different `content_hash` simulate two versions of the
    same document."""
    return build_envelope(
        source="version-change-src",
        blob_path=f"version-change-blob-{label}",
        content_hash=content_hash,
        trust_tier=TrustTier.INTERNAL,
    )


# --- dimension mismatch (F, PERMANENT) ----------------------------------------------------------


def test_dimension_mismatch_raises_before_any_write() -> None:
    adapter = _FakeAdapter()
    indexer, ledger, _ = _build_indexer(adapter=adapter)
    envelope = make_envelope("dimension-mismatch")
    _seed_indexing(ledger, envelope.doc_id)
    bad_chunk = embedded_chunk("bad", embedding_dimensions=8)

    with pytest.raises(IndexerError) as exc_info:
        indexer.index(envelope, [bad_chunk])

    assert exc_info.value.error_code == INDEXER_DIMENSION_MISMATCH
    assert adapter.batch_calls == []
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.FAILED
    assert row.last_error_code == INDEXER_DIMENSION_MISMATCH


# --- mixed model batch (F, PERMANENT) -----------------------------------------------------------


def test_mixed_model_batch_raises_before_any_write() -> None:
    adapter = _FakeAdapter()
    indexer, ledger, _ = _build_indexer(adapter=adapter)
    envelope = make_envelope("mixed-model")
    _seed_indexing(ledger, envelope.doc_id)
    chunks = [
        embedded_chunk("a", embedding_model="model-a"),
        embedded_chunk("b", embedding_model="model-b"),
    ]

    with pytest.raises(IndexerError) as exc_info:
        indexer.index(envelope, chunks)

    assert exc_info.value.error_code == INDEXER_MIXED_MODEL_BATCH
    assert adapter.batch_calls == []
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.FAILED


# --- empty records (F, PERMANENT) ---------------------------------------------------------------


def test_index_empty_records_raises_indexer_empty_records_permanent_ledger_ends_failed() -> (
    None
):
    indexer, ledger, _ = _build_indexer()
    envelope = make_envelope("empty-records")
    _seed_indexing(ledger, envelope.doc_id)

    with pytest.raises(IndexerError) as exc_info:
        indexer.index(envelope, [])

    assert exc_info.value.error_code == INDEXER_EMPTY_RECORDS
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.FAILED
    assert row.last_error_code == INDEXER_EMPTY_RECORDS


def test_index_empty_records_benign_concurrent_loss_skips_ledger_write_but_still_raises() -> (
    None
):
    indexer, ledger, _ = _build_indexer()
    envelope = make_envelope("empty-records-benign")
    _advance_to(ledger, envelope.doc_id, Status.INDEXED)
    before = ledger.get(envelope.doc_id)

    with pytest.raises(IndexerError) as exc_info:
        indexer.index(envelope, [])

    assert exc_info.value.error_code == INDEXER_EMPTY_RECORDS
    after = ledger.get(envelope.doc_id)
    assert after == before
    assert after is not None
    assert after.status == Status.INDEXED


def test_index_empty_records_unknown_doc_id_reraises_ledger_transition_invalid() -> (
    None
):
    indexer, _ledger, _ = _build_indexer()
    envelope = make_envelope("empty-records-unknown-doc-id")

    with pytest.raises(LedgerTransitionInvalid) as exc_info:
        indexer.index(envelope, [])

    assert exc_info.value.observed_row is None


def test_index_empty_records_already_failed_reraises_ledger_transition_invalid() -> (
    None
):
    indexer, ledger, _ = _build_indexer()
    envelope = make_envelope("empty-records-already-failed")
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="v1"))
    ledger.transition(
        envelope.doc_id, Status.FAILED, Status.FAILED, error_code="SOME_PRIOR_FAILURE"
    )

    with pytest.raises(LedgerTransitionInvalid) as exc_info:
        indexer.index(envelope, [])

    assert exc_info.value.observed_row is not None
    assert exc_info.value.observed_row.status == Status.FAILED


# --- construction-time schema conflict (PERMANENT, fail fast) ----------------------------------


def test_construction_schema_conflict_raises_before_any_document_in_scope() -> None:
    adapter = InMemoryIndexer()
    adapter.ensure_schema(8, "cosine")
    ledger = InMemoryLedgerStore()

    with pytest.raises(IndexerError) as exc_info:
        Indexer(ledger, IndexerConfig(dimensions=4, metric="cosine"), adapter)

    assert exc_info.value.error_code == INDEXER_SCHEMA_CONFLICT


# --- ensure_schema idempotent (AC8) -------------------------------------------------------------


def test_ensure_schema_called_twice_across_two_indexer_instances_does_not_error() -> (
    None
):
    adapter = InMemoryIndexer()
    ledger = InMemoryLedgerStore()

    Indexer(ledger, IndexerConfig(dimensions=4, metric="cosine"), adapter)
    Indexer(ledger, IndexerConfig(dimensions=4, metric="cosine"), adapter)


# --- partial batch failure (story test plan) ----------------------------------------------------


def test_partial_batch_failure_retries_only_failed_chunk_ids() -> None:
    envelope = make_envelope("partial-batch-retry")
    doc_id = envelope.doc_id
    failing_chunk_id = derive_chunk_id(doc_id, 1)
    adapter = _FakeAdapter(fail_chunk_ids_once={failing_chunk_id})
    indexer, ledger, _ = _build_indexer(adapter=adapter)
    _seed_indexing(ledger, doc_id)
    chunks = [_embedded_chunk_at(doc_id, i) for i in range(3)]

    result = indexer.index(envelope, chunks)

    assert result.chunks_upserted == 3
    assert len(adapter.batch_calls) == 2
    assert adapter.batch_calls[1] == [failing_chunk_id]


def test_partial_batch_failure_persisting_after_retry_raises_partial_batch_failure() -> (
    None
):
    envelope = make_envelope("partial-batch-persists")
    doc_id = envelope.doc_id

    class _AlwaysFailAdapter(_FakeAdapter):
        def upsert(self, records: list[IndexRecord]) -> UpsertOutcome:
            self.batch_calls.append([r.chunk_id for r in records])
            return UpsertOutcome(
                succeeded=[], failed=[(r.chunk_id, "still failing") for r in records]
            )

    adapter = _AlwaysFailAdapter()
    indexer, ledger, _ = _build_indexer(adapter=adapter)
    _seed_indexing(ledger, doc_id)

    with pytest.raises(IndexerError) as exc_info:
        indexer.index(envelope, [_embedded_chunk_at(doc_id, 0)])

    assert exc_info.value.error_code == INDEXER_PARTIAL_BATCH_FAILURE
    row = ledger.get(doc_id)
    assert row is not None
    assert row.status == Status.INDEXING
    assert row.last_error_code == INDEXER_PARTIAL_BATCH_FAILURE


# --- PERMANENT adapter-raised failures (mid-flow) -----------------------------------------------


@pytest.mark.parametrize(
    "error_code", [INDEXER_SCHEMA_CONFLICT, INDEXER_RECORD_TOO_LARGE]
)
def test_permanent_adapter_error_terminalizes_ledger(error_code: str) -> None:
    adapter = _FakeAdapter(raise_exc=IndexerError("", "boom", error_code=error_code))
    indexer, ledger, _ = _build_indexer(adapter=adapter)
    envelope = make_envelope(f"permanent-{error_code}")
    _seed_indexing(ledger, envelope.doc_id)

    with pytest.raises(IndexerError) as exc_info:
        indexer.index(envelope, [embedded_chunk("hello")])

    assert exc_info.value.error_code == error_code
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.FAILED
    assert row.last_error_code == error_code


# --- TRANSIENT adapter failures -------------------------------------------------------------------


@pytest.mark.parametrize(
    "error_code", [INDEXER_RATE_LIMITED, INDEXER_UPSTREAM_ERROR, INDEXER_TIMEOUT]
)
def test_transient_adapter_error_self_transitions_indexing(error_code: str) -> None:
    adapter = _FakeAdapter(raise_exc=IndexerError("", "boom", error_code=error_code))
    indexer, ledger, _ = _build_indexer(adapter=adapter)
    envelope = make_envelope(f"transient-{error_code}")
    _seed_indexing(ledger, envelope.doc_id)

    with pytest.raises(IndexerError) as exc_info:
        indexer.index(envelope, [embedded_chunk("hello")])

    assert exc_info.value.error_code == error_code
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.INDEXING
    assert row.last_error_code == error_code


def test_unmapped_indexer_error_code_from_adapter_wrapped_as_indexer_internal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _FakeAdapter(
        raise_exc=IndexerError("", "custom failure", error_code=INDEXER_EMPTY_RECORDS)
    )
    indexer, ledger, _ = _build_indexer(adapter=adapter)
    envelope = make_envelope("unexpected-indexer-error")
    _seed_indexing(ledger, envelope.doc_id)

    with caplog.at_level("ERROR", logger="vestibule.indexer.indexer"):
        with pytest.raises(IndexerError) as exc_info:
            indexer.index(envelope, [embedded_chunk("hello")])

    assert exc_info.value.error_code == INDEXER_INTERNAL
    assert "custom failure" in caplog.text
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.INDEXING
    assert row.last_error_code == INDEXER_INTERNAL


def test_unmapped_generic_exception_from_adapter_wrapped_as_indexer_internal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _FakeAdapter(raise_exc=RuntimeError("adapter exploded"))
    indexer, ledger, _ = _build_indexer(adapter=adapter)
    envelope = make_envelope("generic-exception")
    _seed_indexing(ledger, envelope.doc_id)

    with caplog.at_level("ERROR", logger="vestibule.indexer.indexer"):
        with pytest.raises(IndexerError) as exc_info:
            indexer.index(envelope, [embedded_chunk("hello")])

    assert exc_info.value.error_code == INDEXER_INTERNAL
    assert "adapter exploded" in caplog.text
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.INDEXING
    assert row.last_error_code == INDEXER_INTERNAL


def test_transient_failure_then_redelivery_increments_attempts_without_reaching_failed() -> (
    None
):
    adapter = _FakeAdapter(
        raise_exc=IndexerError("", "boom", error_code=INDEXER_TIMEOUT)
    )
    indexer, ledger, _ = _build_indexer(adapter=adapter)
    envelope = make_envelope("transient-redelivery")
    _seed_indexing(ledger, envelope.doc_id)

    with pytest.raises(IndexerError):
        indexer.index(envelope, [embedded_chunk("hello")])
    row1 = ledger.get(envelope.doc_id)
    assert row1 is not None

    with pytest.raises(IndexerError):
        indexer.index(envelope, [embedded_chunk("hello")])
    row2 = ledger.get(envelope.doc_id)
    assert row2 is not None
    assert row2.status == Status.INDEXING
    assert row2.attempts > row1.attempts


def test_transient_adapter_error_self_transition_benign_concurrent_loss_still_raises() -> (
    None
):
    adapter = _FakeAdapter(
        raise_exc=IndexerError("", "boom", error_code=INDEXER_TIMEOUT)
    )
    indexer, ledger, _ = _build_indexer(adapter=adapter)
    envelope = make_envelope("transient-self-transition-benign")
    _advance_to(ledger, envelope.doc_id, Status.INDEXED)
    before = ledger.get(envelope.doc_id)

    with pytest.raises(IndexerError) as exc_info:
        indexer.index(envelope, [embedded_chunk("hello")])

    assert exc_info.value.error_code == INDEXER_TIMEOUT
    after = ledger.get(envelope.doc_id)
    assert after == before
    assert after is not None
    assert after.status == Status.INDEXED


# --- exit-transition benign/non-benign redelivery -------------------------------------------------


def test_index_redelivered_after_prior_success_is_benign_noop() -> None:
    indexer, ledger, _ = _build_indexer()
    envelope = make_envelope("redelivered-past-indexed")
    _advance_to(ledger, envelope.doc_id, Status.INDEXED)
    before = ledger.get(envelope.doc_id)

    result = indexer.index(envelope, [embedded_chunk("hello world")])

    assert result.chunks_upserted == 1
    after = ledger.get(envelope.doc_id)
    assert after == before


def test_index_ledger_transition_invalid_non_benign_propagates_unchanged() -> None:
    indexer, _ledger, _ = _build_indexer()
    envelope = make_envelope("unknown-doc-id-exit")

    with pytest.raises(LedgerTransitionInvalid) as exc_info:
        indexer.index(envelope, [embedded_chunk("hello world")])

    assert exc_info.value.observed_row is None


# --- property-based (hypothesis, story) ----------------------------------------------------------


_TEXT_ALPHABET = string.ascii_letters + string.digits + " "
_CHUNK_TEXT = st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=100).map(
    lambda s: s.strip() or "x"
)
_chunk_text_lists = st.lists(_CHUNK_TEXT, min_size=1, max_size=15)


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(texts=_chunk_text_lists)
def test_property_wellformed_embedded_chunks_produce_matching_store_count(
    texts: list[str],
) -> None:
    """`InMemoryIndexer.count_by_doc_id == len(chunks)` for any well-formed batch."""
    adapter = InMemoryIndexer()
    ledger = InMemoryLedgerStore()
    indexer = Indexer(ledger, IndexerConfig(dimensions=4), adapter)
    envelope = make_envelope(f"property-{len(texts)}-{hash(tuple(texts)) % 10_000}")
    _seed_indexing(ledger, envelope.doc_id)
    chunks = [
        _embedded_chunk_at(envelope.doc_id, i, embedding_model="fixed-model")
        for i, _text in enumerate(texts)
    ]

    indexer.index(envelope, chunks)

    assert adapter.count_by_doc_id(envelope.doc_id) == len(chunks)


# --- config/code sync -------------------------------------------------------------------------


def test_config_indexer_yaml_tunables_match_indexer_config_defaults() -> None:
    config_path = Path(__file__).resolve().parents[2] / "config" / "indexer.yaml"
    text = config_path.read_text(encoding="utf-8")
    defaults = IndexerConfig()
    retry_defaults = RetryConfig()

    def _int_value(key: str) -> int:
        match = re.search(rf"{re.escape(key)}:\s*(\d+)", text)
        assert match is not None
        return int(match.group(1))

    def _str_value(key: str) -> str:
        match = re.search(rf"{re.escape(key)}:\s*(\S+)", text)
        assert match is not None
        return match.group(1)

    assert _int_value("dimensions") == defaults.dimensions
    assert _str_value("metric") == defaults.metric
    assert "batch_size: null" in text
    assert defaults.batch_size is None
    assert "delete_before_upsert_on_version_change: true" in text
    assert defaults.delete_before_upsert_on_version_change is True
    assert _int_value("max_attempts") == retry_defaults.max_attempts
    assert _int_value("backoff_base_seconds") == retry_defaults.backoff_base_seconds
    assert _int_value("backoff_max_seconds") == retry_defaults.backoff_max_seconds
