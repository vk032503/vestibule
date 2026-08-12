"""Shared fixtures and helpers for the Embedder test suite (REQ-007).

`make_envelope` is re-exported from `vestibule.analyzer.conftest` (REQ-005) rather than
re-implemented, so every test suite in this repo builds envelopes the same way. `chunk`
is a plain helper function (not a pytest fixture), building a valid `Chunk` (REQ-006)
with sensible defaults, shared across this package's test modules by direct import.
"""

from __future__ import annotations

import itertools

from vestibule.analyzer.conftest import make_envelope
from vestibule.analyzer.model import ElementType
from vestibule.chunker.model import Chunk
from vestibule.identity.derive import derive_chunk_id

__all__ = ["make_envelope", "chunk"]

_position_counter = itertools.count()


def chunk(
    text: str = "hello world", *, doc_id: str = "a" * 64, token_count: int = 2
) -> Chunk:
    """Builds a valid `Chunk` for test use, with a fresh `position`/`chunk_id` each call.

    Args:
        text: The chunk's text content.
        doc_id: The owning document's `doc_id`.
        token_count: The chunk's token count.

    Returns:
        A valid `Chunk`, with `position` incrementing on every call so repeated calls
        for the same `doc_id` never collide on `chunk_id`.
    """
    position = next(_position_counter)
    return Chunk(
        chunk_id=derive_chunk_id(doc_id, position),
        doc_id=doc_id,
        position=position,
        element_types=[ElementType.PARAGRAPH],
        text=text,
        token_count=token_count,
        metadata={"strategy": "recursive", "section_path": None},
    )
