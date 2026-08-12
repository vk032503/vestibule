"""End-to-end pipeline integration test (REQ-008, AC9) — the Phase 2 acceptance test.

Drives a real fixture PDF through every Phase 2 stage for real: envelope -> Analyzer
(`PyMuPDFParser`) -> Chunker -> Embedder (`FastEmbedEmbedder`) -> Indexer
(`InMemoryIndexer`), then a `search()` call proving retrieval actually works — not just
that writes succeed — with zero cloud credentials configured anywhere in this test.

Fixture PDF: built in-memory via `pymupdf` (real PDF bytes, no committed binary fixture
file), reusing `vestibule.analyzer.conftest`'s established convention (see its own
`digital_pdf_bytes`/`_pdf_with_page_text`) rather than inventing a new fixture
mechanism — extended here with distinctive, known text on one page so `search()` has
something specific to retrieve.

Spec-gap resolution (hermetic `FastEmbedEmbedder`): the story asks for "embedder
(FastEmbed)" with "no mocking of the pipeline stages themselves" and "zero cloud
credentials configured," but `fastembed` is an optional dependency (REQ-007's `local`
extra) that is not installed in this project's test environment (`test_fastembed.py`'s
own precedent relies on exactly this fact), and even when installed, its real
`TextEmbedding` downloads ONNX weights over the network on first use — violating the
"no network" hermetic-test house rule. This test resolves the conflict the same way
`vestibule.embedder.adapters.test_fastembed` already does for *unit* tests: it
constructs a real `FastEmbedEmbedder`, but supplies a deterministic, hashing-based
`_text_embedding` via that class's own documented test-injection seam, bypassing only
the ONNX/network internals. Every other stage (`Analyzer`, `Chunker`, `Embedder`
orchestration, `Indexer`, `InMemoryIndexer`) runs completely for real, unmocked.
"""

from __future__ import annotations

import hashlib
import io
import re
from typing import Iterable

from vestibule.analyzer.analyzer import Analyzer, AnalyzerConfig
from vestibule.analyzer.model import DetectedType
from vestibule.analyzer.parsers.pymupdf_parser import PyMuPDFParser
from vestibule.analyzer.registry import ParserRegistry
from vestibule.chunker.chunker import Chunker, ChunkerConfig
from vestibule.chunker.token_counter import TiktokenCounter
from vestibule.embedder.adapters.fastembed import FastEmbedEmbedder
from vestibule.embedder.embedder import Embedder, EmbedderConfig
from vestibule.envelope.model import TrustTier, build_envelope
from vestibule.indexer.adapters.in_memory import InMemoryIndexer
from vestibule.indexer.indexer import Indexer, IndexerConfig
from vestibule.ledger.store import EnvelopeSummary, InMemoryLedgerStore, Status

_EMBEDDING_DIMENSIONS = 64
_DISTINCTIVE_PHRASE = "the wombat regulatory commission requires form 27B quarterly"


class _HashingTextEmbedding:
    """Deterministic, hermetic stand-in for `fastembed.TextEmbedding` (no ONNX, no
    network): a simple hashed bag-of-words embedding, injected via
    `FastEmbedEmbedder`'s own `_text_embedding` test seam.

    Real cosine-similarity ranking behavior falls straight out of this scheme: two
    texts sharing more words produce vectors with a larger dot product (and thus higher
    cosine similarity) than two texts sharing none — sufficient for this test's own
    "known text ranks in the top 3" assertion, without needing a real ML model.
    """

    def __init__(self, dimensions: int = _EMBEDDING_DIMENSIONS) -> None:
        self._dimensions = dimensions

    def embed(self, texts: list[str]) -> Iterable[Iterable[float]]:
        return [_hash_embed(text, self._dimensions) for text in texts]


def _hash_embed(text: str, dimensions: int) -> list[float]:
    """Hashes each word of `text` into one of `dimensions` buckets, counting occurrences."""
    vector = [0.0] * dimensions
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        digest = hashlib.sha256(word.encode("utf-8")).hexdigest()
        vector[int(digest, 16) % dimensions] += 1.0
    return vector


def _fixture_pdf_bytes() -> bytes:
    """Builds a real, in-memory multi-page PDF with several unrelated filler pages plus
    one page carrying `_DISTINCTIVE_PHRASE`, so `search()` has a specific, known chunk
    to retrieve among several plausible distractors.

    Uses `insert_textbox` (word-wrapped, unlike `insert_text`'s single-line-only
    placement) so every word of each topic's filler text actually lands in the PDF's
    extractable content — enough total content, spread across enough elements, that
    `Chunker` produces several distinct chunks rather than collapsing everything into
    one (which would make the "top 3" assertion below trivial).
    """
    import pymupdf

    topics = [
        "quarterly harvest reports for the northern orchard cooperative describe apple "
        "and pear yields across seventeen distinct growing regions this season " * 20,
        "the municipal water treatment facility upgraded its filtration membranes and "
        "reduced turbidity readings across every downstream monitoring station " * 20,
        f"{_DISTINCTIVE_PHRASE} submissions covering marsupial habitat encroachment "
        "near the eastern wetlands conservation area every three months without fail "
        * 20,
        "the regional astronomy society published updated star charts for amateur "
        "observers tracking the autumn meteor shower from suburban locations " * 20,
        "local transit authorities announced revised bus schedules affecting five "
        "major commuter routes throughout the downtown business district " * 20,
    ]
    doc = pymupdf.open()
    try:
        rect = pymupdf.Rect(72, 72, 500, 750)
        for topic in topics:
            page = doc.new_page()
            page.insert_textbox(rect, topic, fontsize=10)
        return bytes(doc.tobytes())
    finally:
        doc.close()


def test_e2e_pdf_flows_through_full_pipeline_and_is_retrievable_via_search() -> None:
    pdf_bytes = _fixture_pdf_bytes()
    content_hash = hashlib.sha256(pdf_bytes).hexdigest()
    envelope = build_envelope(
        source="e2e-test",
        blob_path="fixtures/wombat-regulatory-filing.pdf",
        content_hash=content_hash,
        trust_tier=TrustTier.INTERNAL,
        allowed_groups=["compliance-team"],
    )

    ledger = InMemoryLedgerStore()
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="e2e-v1"))

    registry = ParserRegistry()
    registry.register(DetectedType.DIGITAL_PDF, PyMuPDFParser())
    analyzer = Analyzer(ledger, registry, AnalyzerConfig())
    elements = analyzer.analyze(envelope, io.BytesIO(pdf_bytes))
    assert elements

    chunker = Chunker(ledger, ChunkerConfig(), TiktokenCounter())
    chunks = chunker.chunk(envelope, elements)
    assert chunks

    fastembed_adapter = FastEmbedEmbedder(
        _text_embedding=_HashingTextEmbedding(_EMBEDDING_DIMENSIONS)
    )
    embedder = Embedder(
        ledger, EmbedderConfig(target_dimensions=None), fastembed_adapter
    )
    embedded_chunks = embedder.embed(envelope, chunks)
    assert embedded_chunks
    assert all(
        ec.embedding_dimensions == _EMBEDDING_DIMENSIONS for ec in embedded_chunks
    )

    in_memory_indexer = InMemoryIndexer()
    indexer = Indexer(
        ledger,
        IndexerConfig(dimensions=_EMBEDDING_DIMENSIONS, metric="cosine"),
        in_memory_indexer,
    )
    result = indexer.index(envelope, embedded_chunks)
    assert result.chunks_upserted == len(embedded_chunks)

    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.INDEXED

    query_vector = _hash_embed(_DISTINCTIVE_PHRASE, _EMBEDDING_DIMENSIONS)
    top_results = in_memory_indexer.search(query_vector, top_k=3)

    assert len(top_results) >= 1
    matches = [
        record
        for record, _score in top_results
        if record.doc_id == envelope.doc_id and "wombat" in record.text.lower()
    ]
    assert matches, f"expected a wombat chunk in the top 3 results, got {top_results!r}"
    assert matches[0].allowed_groups == ["compliance-team"]
    assert matches[0].trust_tier == "internal"
