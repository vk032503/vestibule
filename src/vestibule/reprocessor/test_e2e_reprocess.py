"""End-to-end round-trip test (REQ-012, AC8) — the Poison Queue Reprocessor's own
acceptance test.

Drives a real fixture PDF through Analyzer -> Chunker, then a genuine embedding-stage
failure: `Embedder.embed(envelope, [])` is called for real, which is exactly the
`EMBEDDER_EMPTY_CHUNKS` PERMANENT path `vestibule.embedder.embedder.Embedder` itself
takes straight to `Status.FAILED` (`Embedder._terminalize`) — no manual
`LedgerRow.transition()` stand-in. (`EMBEDDER_UPSTREAM_ERROR`, by contrast, is TRANSIENT
and only ever self-transitions `Embedder`'s own row within `Status.EMBEDDING`; it is
never a code `Embedder` itself writes to `Status.FAILED`, so it would have been a
misleading choice here — see `Embedder.embed`'s own docstring.)

Then exercises the actual `Reprocessor.requeue()` and re-runs the *same* document
through the full local pipeline again from `Analyzer` onward (Analyzer -> Chunker ->
Embedder -> Indexer), asserting it ends `Status.INDEXED` with exactly one copy of each
chunk actually present in the `InMemoryIndexer`.

Because the simulated failure lands before any `Indexer.index()` call ever runs, the
single successful `index()` call below cannot by itself distinguish a correct
upsert-keyed-by-chunk_id indexer from a naive append-only one — nothing exists yet to
overwrite. The final block closes that gap directly: it re-`upsert()`s the exact records
already sitting in `InMemoryIndexer` a second time (simulating an at-least-once
redelivery of the same write), using the adapter's real, production `upsert()` — the
actual mechanism Contract #2 relies on to make Reprocessor-driven reprocessing safe —
and asserts the stored count and chunk_id set are unchanged afterward. That is the actual
proof that re-done work overwrites cleanly rather than duplicating.

Hermetic: zero cloud credentials, zero mocking of any pipeline stage. Reuses
`vestibule.indexer.test_e2e_pipeline`'s own `_fixture_pdf_bytes`/`_HashingTextEmbedding`/
`_hash_embed` hermetic-`FastEmbedEmbedder` pattern rather than inventing a new one — see
that module's own docstring for the full spec-gap rationale this borrows.
"""

from __future__ import annotations

import hashlib
import io

import pytest

from vestibule.analyzer.analyzer import Analyzer, AnalyzerConfig
from vestibule.analyzer.model import DetectedType
from vestibule.analyzer.parsers.pymupdf_parser import PyMuPDFParser
from vestibule.analyzer.registry import ParserRegistry
from vestibule.chunker.chunker import Chunker, ChunkerConfig
from vestibule.chunker.token_counter import TiktokenCounter
from vestibule.embedder.adapters.fastembed import FastEmbedEmbedder
from vestibule.embedder.embedder import Embedder, EmbedderConfig
from vestibule.embedder.model import EMBEDDER_EMPTY_CHUNKS, EmbedderError
from vestibule.envelope.model import TrustTier, build_envelope
from vestibule.indexer.adapters.in_memory import InMemoryIndexer
from vestibule.indexer.indexer import Indexer, IndexerConfig
from vestibule.indexer.test_e2e_pipeline import (
    _EMBEDDING_DIMENSIONS,
    _HashingTextEmbedding,
    _fixture_pdf_bytes,
)
from vestibule.ledger.store import EnvelopeSummary, InMemoryLedgerStore, Status
from vestibule.reprocessor.reprocessor import Reprocessor


def test_doc_failed_at_embedding_requeued_and_reprocessed_ends_indexed_no_duplicates() -> (
    None
):
    pdf_bytes = _fixture_pdf_bytes()
    content_hash = hashlib.sha256(pdf_bytes).hexdigest()
    envelope = build_envelope(
        source="reprocessor-e2e-test",
        blob_path="fixtures/wombat-regulatory-filing.pdf",
        content_hash=content_hash,
        trust_tier=TrustTier.INTERNAL,
        allowed_groups=["compliance-team"],
    )

    ledger = InMemoryLedgerStore()
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="reprocessor-e2e-v1"))

    registry = ParserRegistry()
    registry.register(DetectedType.DIGITAL_PDF, PyMuPDFParser())
    analyzer = Analyzer(ledger, registry, AnalyzerConfig())
    chunker = Chunker(ledger, ChunkerConfig(), TiktokenCounter())
    fastembed_adapter = FastEmbedEmbedder(
        _text_embedding=_HashingTextEmbedding(_EMBEDDING_DIMENSIONS)
    )
    embedder = Embedder(
        ledger, EmbedderConfig(target_dimensions=None), fastembed_adapter
    )

    # --- first attempt: real Analyzer + Chunker, then a genuine Embedder-driven failure -
    elements = analyzer.analyze(envelope, io.BytesIO(pdf_bytes))
    assert elements
    first_chunks = chunker.chunk(envelope, elements)
    assert first_chunks

    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.EMBEDDING

    # Real Embedder.embed(), called with zero chunks — its own genuine
    # EMBEDDER_EMPTY_CHUNKS PERMANENT path, which really does write Status.FAILED
    # (Embedder._terminalize), not a hand-rolled stand-in.
    with pytest.raises(EmbedderError) as exc_info:
        embedder.embed(envelope, [])
    assert exc_info.value.error_code == EMBEDDER_EMPTY_CHUNKS

    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.FAILED
    assert row.last_error_code == EMBEDDER_EMPTY_CHUNKS

    # --- requeue via the real Reprocessor -----------------------------------------------
    reprocessor = Reprocessor(ledger)
    result = reprocessor.requeue(envelope.doc_id)
    assert result.doc_id == envelope.doc_id
    assert result.previous_error_code == EMBEDDER_EMPTY_CHUNKS

    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.PENDING
    assert row.attempts == 0
    assert row.last_error_code is None
    assert row.last_error_message is None

    # --- second attempt: full pipeline, Analyzer onward, this time for real -------------
    elements_2 = analyzer.analyze(envelope, io.BytesIO(pdf_bytes))
    assert elements_2
    second_chunks = chunker.chunk(envelope, elements_2)
    assert second_chunks
    # Deterministic chunking over the same content: identical chunk_ids both attempts.
    assert {c.chunk_id for c in second_chunks} == {c.chunk_id for c in first_chunks}

    # Reuses the same Embedder instance the forced first-attempt failure was raised
    # from — its ledger row is back in Status.EMBEDDING via requeue + Chunker above.
    embedded_chunks = embedder.embed(envelope, second_chunks)
    assert embedded_chunks

    in_memory_indexer = InMemoryIndexer()
    indexer = Indexer(
        ledger,
        IndexerConfig(dimensions=_EMBEDDING_DIMENSIONS, metric="cosine"),
        in_memory_indexer,
    )
    index_result = indexer.index(envelope, embedded_chunks)
    assert index_result.chunks_upserted == len(embedded_chunks)

    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.INDEXED

    # --- independently verify the index's actual contents, not just chunks_upserted ----
    expected_chunk_ids = {ec.chunk.chunk_id for ec in embedded_chunks}
    assert len(expected_chunk_ids) == len(embedded_chunks)  # no accidental collisions

    assert in_memory_indexer.count_by_doc_id(envelope.doc_id) == len(expected_chunk_ids)

    zero_vector = [0.0] * _EMBEDDING_DIMENSIONS
    all_results = in_memory_indexer.search(
        zero_vector, top_k=len(expected_chunk_ids) + 10
    )
    stored_records_for_doc = [
        record for record, _score in all_results if record.doc_id == envelope.doc_id
    ]
    stored_chunk_ids_for_doc = {record.chunk_id for record in stored_records_for_doc}
    assert stored_chunk_ids_for_doc == expected_chunk_ids

    # --- overwrite-not-duplicate proof: re-upsert the exact same records a second time,
    # via the real InMemoryIndexer.upsert() adapter call directly (Indexer.index()
    # itself is ledger-gated and can't be called again once Status.INDEXED is terminal
    # — this exercises the same underlying, real write path Reprocessor-driven
    # reprocessing relies on for its own overwrite-not-duplicate safety, simulating an
    # at-least-once redelivery of the identical write) ---------------------------------
    in_memory_indexer.upsert(stored_records_for_doc)
    assert in_memory_indexer.count_by_doc_id(envelope.doc_id) == len(expected_chunk_ids)
    all_results_after_replay = in_memory_indexer.search(
        zero_vector, top_k=len(expected_chunk_ids) + 10
    )
    replayed_chunk_ids_for_doc = {
        record.chunk_id
        for record, _score in all_results_after_replay
        if record.doc_id == envelope.doc_id
    }
    assert replayed_chunk_ids_for_doc == expected_chunk_ids
