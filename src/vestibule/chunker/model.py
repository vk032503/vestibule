"""Chunker — data model, exceptions, and error-code registration (REQ-006).

Defines the shapes every `ChunkStrategy` and the `Chunker` orchestrator (`chunker.py`)
operate on: `Chunk` (the retrieval-ready, immutable output value object), `ChunkDraft`
(the strategy-internal, pre-identity precursor), and `ChunkerError` (the single
exception type this module raises, carrying a caller-supplied `error_code`).

Failure taxonomy: this module registers exactly three error codes at import time
(Contract #4) — one PERMANENT (`CHUNKER_EMPTY_ELEMENTS`) and two TRANSIENT
(`TOKENIZER_LOAD_FAILED`, `CHUNKER_INTERNAL`). `CHUNKER_OVERSIZED_TABLE` is
deliberately *not* a fourth registered code (LLD Assumption A3, design-review BLOCKER
fix): it is a plain structured-log event name only (`OVERSIZED_TABLE_LOG_EVENT`),
never passed to `register_error`, never attached to any raised exception. See
`chunker.py` for how each registered code is raised and how the ledger is updated on
each.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from vestibule.analyzer.model import ElementType
from vestibule.errors.registry import RaggedError, Severity, register_error

CHUNKER_EMPTY_ELEMENTS = "CHUNKER_EMPTY_ELEMENTS"
TOKENIZER_LOAD_FAILED = "TOKENIZER_LOAD_FAILED"
CHUNKER_INTERNAL = "CHUNKER_INTERNAL"

OVERSIZED_TABLE_LOG_EVENT = "oversized_table"
"""Structured-log event name only — NOT an `ErrorRegistry` code (LLD Assumption A3,
design-review BLOCKER fix). Never passed to `register_error`; never attached to any
raised exception. See `vestibule.chunker.strategies.table_atomic`."""

STRATEGY_RECURSIVE = "recursive"
STRATEGY_STRUCTURE_AWARE = "structure_aware"
STRATEGY_TABLE_ATOMIC = "table_atomic"


class Chunk(BaseModel):
    """Retrieval-ready unit — a pure downstream-of-Analyzer value object.

    Contract #1's arrival boundary has no further bearing here. Immutable (frozen).

    Attributes:
        chunk_id: `derive_chunk_id(doc_id, position)` (REQ-002).
        doc_id: Unchanged from the envelope/ledger key.
        position: Ordinal in the document; equals `chunk_index` (LLD Assumption A7).
        element_types: Every source `Element.type` contributing to this chunk (usually
            one; more than one after a merge, LLD Assumption A8).
        text: Final chunk text (markdown for tables).
        token_count: Via `TokenCounter.count(text)`, recomputed after any merge.
        metadata: `strategy` (str), `section_path` (`str | None`), `page`/`pages` (when
            known), `oversized` (`bool`, tables only).
    """

    model_config = ConfigDict(frozen=True)

    chunk_id: str
    doc_id: str
    position: int
    element_types: list[ElementType]
    text: str
    token_count: int
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ChunkDraft:
    """Pre-identity chunk content produced by a `ChunkStrategy`.

    Precedes `Chunker` assigning the final document-order `position`/`chunk_id` (LLD
    Assumption A7). Never exposed to callers of `Chunker.chunk()` — internal to this
    module only.

    Attributes:
        element_types: Every source `Element.type` contributing to this draft.
        text: Draft chunk text.
        token_count: Via `TokenCounter.count(text)`.
        metadata: Same keys as `Chunk.metadata`, minus `chunk_id`/`position`/`doc_id`.
    """

    element_types: list[ElementType]
    text: str
    token_count: int
    metadata: dict[str, Any]


class ChunkerError(RaggedError):
    """Single exception type for every Chunker-raised failure.

    `error_code` varies per raise site (same shape as REQ-005's `AnalyzerError`, LLD
    Assumption A3 there).

    Attributes:
        doc_id: The `doc_id` of the document being chunked when this error occurred.
        reason: Human-readable failure reason.
    """

    def __init__(self, doc_id: str, reason: str, *, error_code: str) -> None:
        """Initializes the exception.

        Args:
            doc_id: The `doc_id` of the document being chunked.
            reason: Human-readable failure reason.
            error_code: One of this module's three registered error codes.
        """
        self.doc_id = doc_id
        self.reason = reason
        super().__init__(f"{doc_id}: {reason}", error_code=error_code)


register_error(
    CHUNKER_EMPTY_ELEMENTS,
    Severity.PERMANENT,
    "Chunker.chunk was called with an empty elements list (the Analyzer returned zero "
    "elements) — nothing to chunk (REQ-006)",
)
register_error(
    TOKENIZER_LOAD_FAILED,
    Severity.TRANSIENT,
    "a TokenCounter failed to load its underlying tokenizer resource on first use, "
    "e.g. tiktoken.get_encoding raised (REQ-006)",
)
register_error(
    CHUNKER_INTERNAL,
    Severity.TRANSIENT,
    "an exception from a ChunkStrategy call, the min-chunk merge pass, or chunk_id "
    "assignment not mapped to any other declared Chunker error code; the original "
    "exception is logged (REQ-006)",
)
