"""Shared fixtures and helpers for the Indexer test suite (REQ-008).

`make_envelope` is re-exported from `ingestion.analyzer.conftest` (REQ-005) rather than
re-implemented, so every test suite in this repo builds envelopes the same way.
`embedded_chunk` is a plain helper function (not a pytest fixture), building a valid
`EmbeddedChunk` (REQ-007, wrapping a REQ-006 `Chunk`) with sensible defaults, shared
across this package's test modules by direct import — mirrors
`ingestion.embedder.conftest.chunk`.
"""

from __future__ import annotations

import itertools

from ingestion.analyzer.conftest import make_envelope
from ingestion.analyzer.model import ElementType
from ingestion.chunker.model import Chunk
from ingestion.embedder.model import EmbeddedChunk
from ingestion.identity.derive import derive_chunk_id

__all__ = ["make_envelope", "embedded_chunk"]

_position_counter = itertools.count()


def embedded_chunk(
    text: str = "hello world",
    *,
    doc_id: str = "a" * 64,
    vector: list[float] | None = None,
    embedding_model: str = "fake-model",
    embedding_dimensions: int = 4,
    section_path: str | None = None,
    strategy: str = "recursive",
) -> EmbeddedChunk:
    """Builds a valid `EmbeddedChunk` for test use, with a fresh `position`/`chunk_id`
    each call.

    Args:
        text: The wrapped chunk's text content.
        doc_id: The owning document's `doc_id`.
        vector: The embedding vector; defaults to `[1.0] * embedding_dimensions`.
        embedding_model: Provenance model name.
        embedding_dimensions: `len(vector)`, unless `vector` is explicitly supplied.
        section_path: The wrapped chunk's `metadata["section_path"]`.
        strategy: The wrapped chunk's `metadata["strategy"]`.

    Returns:
        A valid `EmbeddedChunk`, with `position` incrementing on every call so repeated
        calls for the same `doc_id` never collide on `chunk_id`.
    """
    position = next(_position_counter)
    resolved_vector = vector if vector is not None else [1.0] * embedding_dimensions
    chunk = Chunk(
        chunk_id=derive_chunk_id(doc_id, position),
        doc_id=doc_id,
        position=position,
        element_types=[ElementType.PARAGRAPH],
        text=text,
        token_count=2,
        metadata={"strategy": strategy, "section_path": section_path},
    )
    return EmbeddedChunk(
        chunk=chunk,
        vector=resolved_vector,
        embedding_model=embedding_model,
        embedding_dimensions=len(resolved_vector),
        truncated_from=None,
    )
