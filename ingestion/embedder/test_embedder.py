"""Unit tests for the Embedder orchestrator (REQ-007)."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Callable

import pytest

from ingestion.embedder.adapters.base import EmbedderAdapter
from ingestion.embedder.conftest import chunk, make_envelope
from ingestion.embedder.embedder import Embedder, EmbedderConfig, RetryConfig
from ingestion.embedder.model import (
    EMBEDDER_EMPTY_CHUNKS,
    EMBEDDER_INPUT_TOO_LONG,
    EMBEDDER_INTERNAL,
    EMBEDDER_MRL_UNSUPPORTED,
    EMBEDDER_RATE_LIMITED,
    EMBEDDER_TIMEOUT,
    EMBEDDER_UPSTREAM_ERROR,
    EmbedderError,
)
from ingestion.ledger.store import (
    EnvelopeSummary,
    InMemoryLedgerStore,
    LedgerTransitionInvalid,
    Status,
)


class _FakeAdapter(EmbedderAdapter):
    """A test-double `EmbedderAdapter` with fully configurable capabilities."""

    def __init__(
        self,
        *,
        native_dimensions: int = 4,
        max_batch_size: int = 32,
        max_input_tokens: int = 100,
        supports_mrl: bool = True,
        document_prefix: str | None = None,
        model_name: str = "fake-model",
        vector_fn: Callable[[str], list[float]] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._native_dimensions = native_dimensions
        self._max_batch_size = max_batch_size
        self._max_input_tokens = max_input_tokens
        self._supports_mrl = supports_mrl
        self._document_prefix = document_prefix
        self._model_name = model_name
        self._vector_fn = vector_fn or (lambda _text: [1.0] * native_dimensions)
        self._raise_exc = raise_exc
        self.batch_calls: list[list[str]] = []

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def native_dimensions(self) -> int:
        return self._native_dimensions

    @property
    def max_batch_size(self) -> int:
        return self._max_batch_size

    @property
    def max_input_tokens(self) -> int:
        return self._max_input_tokens

    @property
    def supports_mrl(self) -> bool:
        return self._supports_mrl

    @property
    def document_prefix(self) -> str | None:
        return self._document_prefix

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.batch_calls.append(list(texts))
        if self._raise_exc is not None:
            raise self._raise_exc
        return [self._vector_fn(text) for text in texts]


def _build_embedder(
    *, config: EmbedderConfig | None = None, adapter: EmbedderAdapter | None = None
) -> tuple[Embedder, InMemoryLedgerStore, _FakeAdapter]:
    ledger = InMemoryLedgerStore()
    fake_adapter = adapter if isinstance(adapter, _FakeAdapter) else _FakeAdapter()
    embedder = Embedder(
        ledger,
        config or EmbedderConfig(target_dimensions=None),
        adapter or fake_adapter,
    )
    return embedder, ledger, fake_adapter


def _seed_embedding(ledger: InMemoryLedgerStore, doc_id: str) -> None:
    """Advances a freshly-created row to Status.EMBEDDING — Embedder's entry state."""
    ledger.create(doc_id, EnvelopeSummary(config_version="v1"))
    for status in (Status.ANALYZING, Status.CHUNKING, Status.EMBEDDING):
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


# --- fake adapter test-double sanity check ----------------------------------------------------


def test_fake_adapter_reports_configured_native_dimensions() -> None:
    adapter = _FakeAdapter(native_dimensions=12)
    assert adapter.native_dimensions == 12


# --- happy path -----------------------------------------------------------------------------


def test_embed_success_transitions_embedding_to_indexing() -> None:
    embedder, ledger, _adapter = _build_embedder()
    envelope = make_envelope("happy-path")
    _seed_embedding(ledger, envelope.doc_id)

    embedded = embedder.embed(envelope, [chunk("hello world")])

    assert len(embedded) == 1
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.INDEXING


def test_embed_preserves_chunk_order_and_wraps_source_chunks() -> None:
    embedder, ledger, _adapter = _build_embedder()
    envelope = make_envelope("preserves-order")
    _seed_embedding(ledger, envelope.doc_id)
    chunks = [chunk("first"), chunk("second"), chunk("third")]

    embedded = embedder.embed(envelope, chunks)

    assert [e.chunk.text for e in embedded] == ["first", "second", "third"]
    assert all(e.embedding_model == "fake-model" for e in embedded)


# --- batching (story test plan) --------------------------------------------------------------


def test_batching_respects_max_batch_size_for_200_chunks() -> None:
    adapter = _FakeAdapter(max_batch_size=32)
    embedder, ledger, _ = _build_embedder(adapter=adapter)
    envelope = make_envelope("batching-200")
    _seed_embedding(ledger, envelope.doc_id)
    chunks = [chunk(f"chunk {i}") for i in range(200)]

    embedded = embedder.embed(envelope, chunks)

    assert len(embedded) == 200
    assert len(adapter.batch_calls) == 7  # ceil(200 / 32) == 7
    assert [len(batch) for batch in adapter.batch_calls] == [32] * 6 + [8]


def test_batch_size_config_overrides_adapter_default_when_smaller() -> None:
    adapter = _FakeAdapter(max_batch_size=32)
    embedder, ledger, _ = _build_embedder(
        adapter=adapter, config=EmbedderConfig(target_dimensions=None, batch_size=10)
    )
    envelope = make_envelope("batch-size-override")
    _seed_embedding(ledger, envelope.doc_id)
    chunks = [chunk(f"chunk {i}") for i in range(25)]

    embedder.embed(envelope, chunks)

    assert [len(batch) for batch in adapter.batch_calls] == [10, 10, 5]


def test_batch_size_config_capped_at_adapter_max_batch_size() -> None:
    adapter = _FakeAdapter(max_batch_size=8)
    embedder, ledger, _ = _build_embedder(
        adapter=adapter, config=EmbedderConfig(target_dimensions=None, batch_size=100)
    )
    envelope = make_envelope("batch-size-capped")
    _seed_embedding(ledger, envelope.doc_id)
    chunks = [chunk(f"chunk {i}") for i in range(20)]

    embedder.embed(envelope, chunks)

    assert [len(batch) for batch in adapter.batch_calls] == [8, 8, 4]


# --- MRL truncation (story test plan) ---------------------------------------------------------


def test_mrl_truncation_produces_exact_dimension_and_l2_normalized_vector() -> None:
    adapter = _FakeAdapter(
        native_dimensions=8,
        supports_mrl=True,
        vector_fn=lambda _t: [1.0, 2.0, 3.0, 4.0] * 2,
    )
    embedder, ledger, _ = _build_embedder(
        adapter=adapter, config=EmbedderConfig(target_dimensions=4)
    )
    envelope = make_envelope("mrl-truncation")
    _seed_embedding(ledger, envelope.doc_id)

    embedded = embedder.embed(envelope, [chunk("some text")])

    vector = embedded[0].vector
    assert len(vector) == 4
    norm = math.sqrt(sum(v * v for v in vector))
    assert norm == pytest.approx(1.0, abs=1e-9)
    assert embedded[0].embedding_dimensions == 4
    assert embedded[0].truncated_from == 8


def test_no_target_dimensions_leaves_native_vector_untouched() -> None:
    adapter = _FakeAdapter(native_dimensions=6, vector_fn=lambda _t: [2.0] * 6)
    embedder, ledger, _ = _build_embedder(
        adapter=adapter, config=EmbedderConfig(target_dimensions=None)
    )
    envelope = make_envelope("no-truncation")
    _seed_embedding(ledger, envelope.doc_id)

    embedded = embedder.embed(envelope, [chunk("some text")])

    assert embedded[0].vector == [2.0] * 6
    assert embedded[0].embedding_dimensions == 6
    assert embedded[0].truncated_from is None


def test_target_dimensions_with_non_mrl_adapter_raises_at_construction() -> None:
    adapter = _FakeAdapter(supports_mrl=False)
    ledger = InMemoryLedgerStore()

    with pytest.raises(EmbedderError) as exc_info:
        Embedder(ledger, EmbedderConfig(target_dimensions=512), adapter)

    assert exc_info.value.error_code == EMBEDDER_MRL_UNSUPPORTED


def test_target_dimensions_with_non_mrl_adapter_never_calls_embed_batch() -> None:
    adapter = _FakeAdapter(supports_mrl=False)
    ledger = InMemoryLedgerStore()

    with pytest.raises(EmbedderError):
        Embedder(ledger, EmbedderConfig(target_dimensions=512), adapter)

    assert adapter.batch_calls == []


def test_mrl_zero_vector_truncation_avoids_division_by_zero() -> None:
    adapter = _FakeAdapter(
        native_dimensions=4, vector_fn=lambda _t: [0.0, 0.0, 0.0, 0.0]
    )
    embedder, ledger, _ = _build_embedder(
        adapter=adapter, config=EmbedderConfig(target_dimensions=2)
    )
    envelope = make_envelope("mrl-zero-vector")
    _seed_embedding(ledger, envelope.doc_id)

    embedded = embedder.embed(envelope, [chunk("zeros")])

    assert embedded[0].vector == [0.0, 0.0]


# --- document_prefix (story test plan) ---------------------------------------------------------


def test_document_prefix_prepended_exactly_once_per_chunk_when_declared() -> None:
    adapter = _FakeAdapter(document_prefix="search_document: ")
    embedder, ledger, _ = _build_embedder(adapter=adapter)
    envelope = make_envelope("prefix-applied")
    _seed_embedding(ledger, envelope.doc_id)

    embedder.embed(envelope, [chunk("alpha"), chunk("beta")])

    assert adapter.batch_calls == [["search_document: alpha", "search_document: beta"]]


def test_document_prefix_not_applied_when_adapter_declares_none() -> None:
    adapter = _FakeAdapter(document_prefix=None)
    embedder, ledger, _ = _build_embedder(adapter=adapter)
    envelope = make_envelope("prefix-absent")
    _seed_embedding(ledger, envelope.doc_id)

    embedder.embed(envelope, [chunk("alpha"), chunk("beta")])

    assert adapter.batch_calls == [["alpha", "beta"]]


# --- F1: empty chunks (PERMANENT) --------------------------------------------------------------


def test_embed_empty_chunks_raises_embedder_empty_chunks_permanent_ledger_ends_failed() -> (
    None
):
    embedder, ledger, _ = _build_embedder()
    envelope = make_envelope("empty-chunks")
    _seed_embedding(ledger, envelope.doc_id)

    with pytest.raises(EmbedderError) as exc_info:
        embedder.embed(envelope, [])

    assert exc_info.value.error_code == EMBEDDER_EMPTY_CHUNKS
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.FAILED
    assert row.last_error_code == EMBEDDER_EMPTY_CHUNKS


def test_embed_empty_chunks_benign_concurrent_loss_skips_ledger_write_but_still_raises() -> (
    None
):
    embedder, ledger, _ = _build_embedder()
    envelope = make_envelope("empty-chunks-benign")
    _advance_to(ledger, envelope.doc_id, Status.INDEXED)
    before = ledger.get(envelope.doc_id)

    with pytest.raises(EmbedderError) as exc_info:
        embedder.embed(envelope, [])

    assert exc_info.value.error_code == EMBEDDER_EMPTY_CHUNKS
    after = ledger.get(envelope.doc_id)
    assert after == before
    assert after is not None
    assert after.status == Status.INDEXED


def test_embed_empty_chunks_unknown_doc_id_reraises_ledger_transition_invalid() -> None:
    embedder, _ledger, _ = _build_embedder()
    envelope = make_envelope("empty-chunks-unknown-doc-id")

    with pytest.raises(LedgerTransitionInvalid) as exc_info:
        embedder.embed(envelope, [])

    assert exc_info.value.observed_row is None


def test_embed_empty_chunks_already_failed_reraises_ledger_transition_invalid() -> None:
    embedder, ledger, _ = _build_embedder()
    envelope = make_envelope("empty-chunks-already-failed")
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="v1"))
    ledger.transition(
        envelope.doc_id, Status.FAILED, Status.FAILED, error_code="SOME_PRIOR_FAILURE"
    )

    with pytest.raises(LedgerTransitionInvalid) as exc_info:
        embedder.embed(envelope, [])

    assert exc_info.value.observed_row is not None
    assert exc_info.value.observed_row.status == Status.FAILED


# --- F2: input too long (PERMANENT) -------------------------------------------------------------


def test_embed_chunk_exceeding_max_input_tokens_raises_input_too_long_permanent() -> (
    None
):
    adapter = _FakeAdapter(max_input_tokens=50)
    embedder, ledger, _ = _build_embedder(adapter=adapter)
    envelope = make_envelope("input-too-long")
    _seed_embedding(ledger, envelope.doc_id)
    oversized = chunk("oversized", token_count=51)

    with pytest.raises(EmbedderError) as exc_info:
        embedder.embed(envelope, [oversized])

    assert exc_info.value.error_code == EMBEDDER_INPUT_TOO_LONG
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.FAILED
    assert row.last_error_code == EMBEDDER_INPUT_TOO_LONG


def test_embed_input_too_long_never_calls_embed_batch() -> None:
    adapter = _FakeAdapter(max_input_tokens=50)
    embedder, ledger, _ = _build_embedder(adapter=adapter)
    envelope = make_envelope("input-too-long-no-call")
    _seed_embedding(ledger, envelope.doc_id)

    with pytest.raises(EmbedderError):
        embedder.embed(
            envelope, [chunk("ok", token_count=10), chunk("bad", token_count=51)]
        )

    assert adapter.batch_calls == []


def test_embed_input_within_limit_does_not_raise() -> None:
    adapter = _FakeAdapter(max_input_tokens=50)
    embedder, ledger, _ = _build_embedder(adapter=adapter)
    envelope = make_envelope("input-within-limit")
    _seed_embedding(ledger, envelope.doc_id)

    embedded = embedder.embed(envelope, [chunk("ok", token_count=50)])

    assert len(embedded) == 1


# --- TRANSIENT adapter failures ------------------------------------------------------------------


@pytest.mark.parametrize(
    "error_code", [EMBEDDER_RATE_LIMITED, EMBEDDER_UPSTREAM_ERROR, EMBEDDER_TIMEOUT]
)
def test_embed_adapter_transient_error_self_transitions_embedding(
    error_code: str,
) -> None:
    adapter = _FakeAdapter(raise_exc=EmbedderError("", "boom", error_code=error_code))
    embedder, ledger, _ = _build_embedder(adapter=adapter)
    envelope = make_envelope(f"transient-{error_code}")
    _seed_embedding(ledger, envelope.doc_id)

    with pytest.raises(EmbedderError) as exc_info:
        embedder.embed(envelope, [chunk("hello")])

    assert exc_info.value.error_code == error_code
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.EMBEDDING
    assert row.last_error_code == error_code


def test_embed_unmapped_embedder_error_code_from_adapter_wrapped_as_embedder_internal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _FakeAdapter(
        raise_exc=EmbedderError("", "custom failure", error_code=EMBEDDER_EMPTY_CHUNKS)
    )
    embedder, ledger, _ = _build_embedder(adapter=adapter)
    envelope = make_envelope("unexpected-embedder-error")
    _seed_embedding(ledger, envelope.doc_id)

    with caplog.at_level("ERROR", logger="ingestion.embedder.embedder"):
        with pytest.raises(EmbedderError) as exc_info:
            embedder.embed(envelope, [chunk("hello")])

    assert exc_info.value.error_code == EMBEDDER_INTERNAL
    assert "custom failure" in caplog.text
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.EMBEDDING
    assert row.last_error_code == EMBEDDER_INTERNAL


def test_embed_unmapped_generic_exception_from_adapter_wrapped_as_embedder_internal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _FakeAdapter(raise_exc=RuntimeError("adapter exploded"))
    embedder, ledger, _ = _build_embedder(adapter=adapter)
    envelope = make_envelope("generic-exception")
    _seed_embedding(ledger, envelope.doc_id)

    with caplog.at_level("ERROR", logger="ingestion.embedder.embedder"):
        with pytest.raises(EmbedderError) as exc_info:
            embedder.embed(envelope, [chunk("hello")])

    assert exc_info.value.error_code == EMBEDDER_INTERNAL
    assert "adapter exploded" in caplog.text
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.EMBEDDING
    assert row.last_error_code == EMBEDDER_INTERNAL


def test_embed_transient_failure_then_redelivery_increments_attempts_without_reaching_failed() -> (
    None
):
    adapter = _FakeAdapter(
        raise_exc=EmbedderError("", "boom", error_code=EMBEDDER_TIMEOUT)
    )
    embedder, ledger, _ = _build_embedder(adapter=adapter)
    envelope = make_envelope("transient-redelivery")
    _seed_embedding(ledger, envelope.doc_id)

    with pytest.raises(EmbedderError):
        embedder.embed(envelope, [chunk("hello")])
    row1 = ledger.get(envelope.doc_id)
    assert row1 is not None

    with pytest.raises(EmbedderError):
        embedder.embed(envelope, [chunk("hello")])
    row2 = ledger.get(envelope.doc_id)
    assert row2 is not None
    assert row2.status == Status.EMBEDDING
    assert row2.attempts > row1.attempts


# --- exit-transition benign/non-benign redelivery -------------------------------------------------


def test_embed_redelivered_after_prior_success_is_benign_noop() -> None:
    embedder, ledger, _ = _build_embedder()
    envelope = make_envelope("redelivered-past-indexing")
    _advance_to(ledger, envelope.doc_id, Status.INDEXED)
    before = ledger.get(envelope.doc_id)

    embedded = embedder.embed(envelope, [chunk("hello world")])

    assert len(embedded) == 1
    after = ledger.get(envelope.doc_id)
    assert after == before


def test_embed_ledger_transition_invalid_non_benign_propagates_unchanged() -> None:
    embedder, _ledger, _ = _build_embedder()
    envelope = make_envelope("unknown-doc-id-exit")

    with pytest.raises(LedgerTransitionInvalid) as exc_info:
        embedder.embed(envelope, [chunk("hello world")])

    assert exc_info.value.observed_row is None


# --- config/code sync -------------------------------------------------------------------------


def test_config_embedder_yaml_tunables_match_embedder_config_defaults() -> None:
    config_path = Path(__file__).resolve().parents[2] / "config" / "embedder.yaml"
    text = config_path.read_text(encoding="utf-8")
    defaults = EmbedderConfig()
    retry_defaults = RetryConfig()

    def _int_value(key: str) -> int:
        match = re.search(rf"{re.escape(key)}:\s*(\d+)", text)
        assert match is not None
        return int(match.group(1))

    assert _int_value("target_dimensions") == defaults.target_dimensions
    assert "batch_size: null" in text
    assert defaults.batch_size is None
    assert _int_value("max_attempts") == retry_defaults.max_attempts
    assert _int_value("backoff_base_seconds") == retry_defaults.backoff_base_seconds
    assert _int_value("backoff_max_seconds") == retry_defaults.backoff_max_seconds
