#!/usr/bin/env python3
"""Vestibule quickstart — the full ingestion pipeline, end to end, zero cloud credentials.

Runs one PDF through every Phase 2 stage exactly as REQ-008's acceptance test does:

    envelope -> ledger -> Analyzer (PyMuPDFParser) -> Chunker -> Embedder (FastEmbed)
    -> Indexer (in-memory) -> search()

Every stage below is the real production code from `vestibule/` — nothing here is
mocked or simplified. The only reason this can run without an Azure account is that
`FastEmbedEmbedder` and `InMemoryIndexer` are the local, credentials-free adapters
Vestibule ships specifically so the pipeline is runnable by anyone.

Prerequisites:
    pip install -e ".[local]"

    This installs `fastembed` (ONNX runtime, no PyTorch) and its dependencies. The
    first run downloads the `nomic-embed-text-v1.5` model weights (~274MB) from
    Hugging Face over the network; every run after that reads from the on-disk cache
    and needs no network access at all. No Azure endpoint, API key, or other secret
    is read anywhere in this script.

Usage:
    python examples/quickstart.py [PDF_PATH] [SEARCH_QUERY]

    PDF_PATH      Defaults to examples/sample.pdf, a small bundled fixture with
                  headings, paragraphs, and a table.
    SEARCH_QUERY  Defaults to a question the fixture's "Pest Management" section
                  answers. Quote it if it contains spaces.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import math
import sys
from collections import Counter
from pathlib import Path

# The project isn't installed as a package (no pip/uv editable install; pytest's
# rootdir-based discovery is what normally puts `vestibule` on sys.path) — replicate
# that here so `python examples/quickstart.py` works from a plain clone.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vestibule.analyzer.analyzer import Analyzer, AnalyzerConfig  # noqa: E402
from vestibule.analyzer.model import DetectedType, Element, ElementType
from vestibule.analyzer.parsers.pymupdf_parser import PyMuPDFParser
from vestibule.analyzer.registry import ParserRegistry
from vestibule.chunker.chunker import Chunker, ChunkerConfig
from vestibule.chunker.token_counter import TiktokenCounter
from vestibule.embedder.adapters.fastembed import FastEmbedEmbedder
from vestibule.embedder.embedder import Embedder, EmbedderConfig
from vestibule.embedder.model import EMBEDDER_DEPENDENCY_MISSING, EmbedderError
from vestibule.envelope.model import TrustTier, build_envelope
from vestibule.indexer.adapters.in_memory import InMemoryIndexer
from vestibule.indexer.indexer import Indexer, IndexerConfig
from vestibule.ledger.store import EnvelopeSummary, InMemoryLedgerStore

_DEFAULT_PDF = Path(__file__).parent / "sample.pdf"
_DEFAULT_QUERY = "How are pests controlled in the orchard?"

# The Embedder MRL-truncates FastEmbed's native 768-dim vectors down to this many
# dimensions (and L2 re-normalizes), demonstrating REQ-007's Matryoshka truncation.
# The Indexer's schema is built for this same dimension count.
_TARGET_DIMENSIONS = 256

# nomic-embed-text-v1.5 is trained with an *asymmetric* prefix scheme: document text
# gets "search_document: " (FastEmbedEmbedder.document_prefix, applied by Embedder to
# every chunk automatically) and queries get "search_query: " instead. REQ-007's story
# calls this out explicitly: "a mismatched prefix scheme between ingest and query
# silently degrades retrieval with no error." Query-side prefixing is out of scope for
# the Embedder itself (REQ-007) — a future retrieval-API REQ owns it — so this script
# applies it by hand, and embeds the query directly via the adapter rather than through
# `Embedder.embed` (which would apply the *document*-side prefix instead).
_QUERY_PREFIX = "search_query: "


def _print_header(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def _load_fastembed_adapter() -> FastEmbedEmbedder:
    """Constructs `FastEmbedEmbedder`, with a friendly message if the optional
    `local` extra isn't installed. `FastEmbedEmbedder` itself always imports (this
    module has no top-level dependency on the third-party `fastembed` package); only
    *constructing* an instance triggers its lazy import, surfaced as an `EmbedderError`
    (`EMBEDDER_DEPENDENCY_MISSING`) rather than a raw `ImportError` — see
    `vestibule/embedder/adapters/fastembed.py`."""
    print(
        "Loading FastEmbed model 'nomic-ai/nomic-embed-text-v1.5' (first run "
        "downloads ~274MB from Hugging Face and caches it; later runs are offline)..."
    )
    try:
        return FastEmbedEmbedder()
    except EmbedderError as exc:
        if exc.error_code == EMBEDDER_DEPENDENCY_MISSING:
            sys.exit(
                'fastembed is not installed. Run:\n\n    pip install -e ".[local]"\n\n'
                "then re-run this script."
            )
        raise


def _inject_demo_table_element(elements: list[Element]) -> list[Element]:
    """Appends one synthetic `ElementType.TABLE` element so the Chunker's
    table-atomic strategy runs, alongside the structure-aware/recursive strategies
    the real parser output already exercises.

    `PyMuPDFParser` (REQ-005) classifies every text block as HEADING or PARAGRAPH
    only — it has no table-detection logic; only `DocumentIntelligenceParser` (the
    Azure Document Intelligence adapter, REQ-005) extracts `ElementType.TABLE`
    elements, via its `prebuilt-layout` model's cell grid. Since this script is
    explicitly the zero-cloud-credentials path, that adapter isn't available here.

    The element below uses the exact `metadata["cells"]` shape
    (`row_index`/`column_index`/`content`) `DocumentIntelligenceParser` produces, so
    `TableAtomicChunkStrategy` (REQ-006) serializes it exactly as it would a real
    Document-Intelligence-extracted table — the only thing simulated is *detection*,
    not chunking.
    """
    cells: list[dict[str, int | str]] = [
        {"row_index": 0, "column_index": 0, "content": "Region"},
        {"row_index": 0, "column_index": 1, "content": "2024 Yield (tons)"},
        {"row_index": 0, "column_index": 2, "content": "2025 Yield (tons)"},
        {"row_index": 1, "column_index": 0, "content": "Northgate Block"},
        {"row_index": 1, "column_index": 1, "content": "412"},
        {"row_index": 1, "column_index": 2, "content": "438"},
        {"row_index": 2, "column_index": 0, "content": "Millbrook Block"},
        {"row_index": 2, "column_index": 1, "content": "356"},
        {"row_index": 2, "column_index": 2, "content": "371"},
    ]
    table_element = Element(
        type=ElementType.TABLE,
        text="\n".join(str(cell["content"]) for cell in cells),
        metadata={"cells": cells, "page": 2},
    )
    return [*elements, table_element]


def _l2_normalize(vector: list[float]) -> list[float]:
    """Truncates to `_TARGET_DIMENSIONS` and L2-normalizes, mirroring exactly what
    `Embedder` does internally (REQ-007) — the query vector must land in the same
    truncated-and-renormalized space as the indexed vectors for cosine similarity to
    mean anything."""
    truncated = vector[:_TARGET_DIMENSIONS]
    norm = math.sqrt(sum(value * value for value in truncated))
    return truncated if norm == 0.0 else [value / norm for value in truncated]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf_path", nargs="?", default=str(_DEFAULT_PDF))
    parser.add_argument("query", nargs="?", default=_DEFAULT_QUERY)
    args = parser.parse_args()

    pdf_path = Path(args.pdf_path)
    pdf_bytes = pdf_path.read_bytes()
    print(f"Ingesting {pdf_path} ({len(pdf_bytes):,} bytes)")

    # --- 1-2. Arrival envelope (Contract #1) -----------------------------------
    envelope = build_envelope(
        source="examples",
        blob_path=f"examples/{pdf_path.name}",
        content_hash=hashlib.sha256(pdf_bytes).hexdigest(),
        trust_tier=TrustTier.INTERNAL,
        allowed_groups=["ops-team"],
    )
    print(f"doc_id = {envelope.doc_id}")

    # --- 3. State ledger (Contract #3) ------------------------------------------
    ledger = InMemoryLedgerStore()
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="quickstart-v1"))

    # --- 4. Analyzer -------------------------------------------------------------
    _print_header("1. ANALYZER")
    registry = ParserRegistry()
    registry.register(DetectedType.DIGITAL_PDF, PyMuPDFParser())
    analyzer = Analyzer(ledger, registry, AnalyzerConfig())
    elements = analyzer.analyze(envelope, io.BytesIO(pdf_bytes))
    counts = Counter(e.type.value for e in elements)
    print(f"Extracted {len(elements)} elements: {dict(counts)}")

    elements = _inject_demo_table_element(elements)
    print(
        "Added 1 synthetic TABLE element (PyMuPDFParser has no table detection — "
        "see _inject_demo_table_element's docstring for why)."
    )

    # --- 5. Chunker ----------------------------------------------------------------
    _print_header("2. CHUNKER")
    chunker = Chunker(ledger, ChunkerConfig(), TiktokenCounter())
    chunks = chunker.chunk(envelope, elements)
    strategy_counts = Counter(chunk.metadata.get("strategy") for chunk in chunks)
    print(f"Produced {len(chunks)} chunks: {dict(strategy_counts)}")
    if "recursive" not in strategy_counts:
        print(
            "(No 'recursive' chunks: Chunker._dispatch_region routes every "
            "non-table region through structure-aware once *any* heading exists "
            "anywhere in the document — recursive only activates for documents "
            "with zero headings at all. This fixture has headings.)"
        )
    for chunk in chunks:
        section = chunk.metadata.get("section_path") or "(no section)"
        preview = chunk.text.strip().replace("\n", " ")[:60]
        print(
            f"  #{chunk.position:<2} [{chunk.metadata.get('strategy'):<15}] "
            f"{section:<25} {chunk.token_count:>4} tok  {preview!r}"
        )

    # --- 6. Embedder -----------------------------------------------------------
    _print_header("3. EMBEDDER")
    fastembed_adapter = _load_fastembed_adapter()
    embedder = Embedder(
        ledger, EmbedderConfig(target_dimensions=_TARGET_DIMENSIONS), fastembed_adapter
    )
    embedded_chunks = embedder.embed(envelope, chunks)
    sample = embedded_chunks[0]
    print(
        f"Embedded {len(embedded_chunks)} chunks with '{sample.embedding_model}': "
        f"{sample.truncated_from} native dims -> {sample.embedding_dimensions} dims "
        f"(MRL-truncated + L2-renormalized)"
    )

    # --- 7. Indexer ----------------------------------------------------------------
    _print_header("4. INDEXER")
    in_memory_indexer = InMemoryIndexer()
    indexer = Indexer(
        ledger,
        IndexerConfig(dimensions=_TARGET_DIMENSIONS, metric="cosine"),
        in_memory_indexer,
    )
    result = indexer.index(envelope, embedded_chunks)
    print(
        f"Upserted {result.chunks_upserted} records into '{result.index_name}' "
        f"({result.chunks_deleted} deleted, {result.embedding_dimensions} dims, "
        f"model={result.embedding_model!r})"
    )

    # --- 8. Ledger status --------------------------------------------------------
    row = ledger.get(envelope.doc_id)
    assert row is not None
    print(f"Final ledger status for {envelope.doc_id[:12]}...: {row.status.value}")

    # --- 9. Search -----------------------------------------------------------------
    _print_header(f"5. SEARCH: {args.query!r}")
    query_vector_native = fastembed_adapter.embed_batch([_QUERY_PREFIX + args.query])[0]
    query_vector = _l2_normalize(query_vector_native)
    results = in_memory_indexer.search(query_vector, top_k=3)
    for rank, (record, score) in enumerate(results, start=1):
        section = record.section_path or "(no section)"
        preview = record.text.strip().replace("\n", " ")[:70]
        print(f"  #{rank}  score={score:.4f}  section={section!r}")
        print(
            f"       allowed_groups={record.allowed_groups}  trust_tier={record.trust_tier!r}"
        )
        print(f"       {preview!r}")


if __name__ == "__main__":
    main()
