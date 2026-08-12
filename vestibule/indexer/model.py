"""Indexer — data model, exceptions, and error-code registration (REQ-008).

Defines the shapes every `IndexerAdapter` and the `Indexer` orchestrator (`indexer.py`)
operate on: `IndexRecord` (the flat, store-agnostic unit that gets written),
`IndexResult` (the receipt for what actually landed), and `IndexerError` (the single
exception type this module raises, carrying a caller-supplied `error_code`).

Failure taxonomy: this module registers eleven error codes at import time (Contract #4)
— six PERMANENT (`INDEXER_EMPTY_RECORDS`, `INDEXER_DIMENSION_MISMATCH`,
`INDEXER_MIXED_MODEL_BATCH`, `INDEXER_SCHEMA_CONFLICT`, `INDEXER_RECORD_TOO_LARGE`,
`INDEXER_DEPENDENCY_MISSING`) and five TRANSIENT (`INDEXER_RATE_LIMITED`,
`INDEXER_UPSTREAM_ERROR`, `INDEXER_TIMEOUT`, `INDEXER_PARTIAL_BATCH_FAILURE`,
`INDEXER_INTERNAL`). See `indexer.py` for how each is raised and how the ledger is
updated on each.

Spec-gap resolution (`doc_version`): the story requires `IndexRecord.doc_version` and
says it is "carried from the envelope," but `ArrivalEnvelope` (REQ-001, strict/frozen,
`extra="forbid"`) has no `doc_version` field, and adding one is out of scope for this
REQ (a Contract #1 change belongs to its own REQ). This module's orchestrator
(`Indexer._build_records`, `indexer.py`) resolves this by using
`envelope.content_hash` as `IndexRecord.doc_version`: `content_hash` already changes if
and only if the document's content changes — exactly the condition under which
delete-before-upsert must trigger — so this requires no envelope-contract change.
Mirrors REQ-006's LLD Assumption A7/A8 documentation pattern for a comparable gap.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from vestibule.errors.registry import RaggedError, Severity, register_error

INDEXER_EMPTY_RECORDS = "INDEXER_EMPTY_RECORDS"
INDEXER_DIMENSION_MISMATCH = "INDEXER_DIMENSION_MISMATCH"
INDEXER_MIXED_MODEL_BATCH = "INDEXER_MIXED_MODEL_BATCH"
INDEXER_SCHEMA_CONFLICT = "INDEXER_SCHEMA_CONFLICT"
INDEXER_RECORD_TOO_LARGE = "INDEXER_RECORD_TOO_LARGE"
INDEXER_RATE_LIMITED = "INDEXER_RATE_LIMITED"
INDEXER_UPSTREAM_ERROR = "INDEXER_UPSTREAM_ERROR"
INDEXER_TIMEOUT = "INDEXER_TIMEOUT"
INDEXER_PARTIAL_BATCH_FAILURE = "INDEXER_PARTIAL_BATCH_FAILURE"
INDEXER_INTERNAL = "INDEXER_INTERNAL"
INDEXER_DEPENDENCY_MISSING = "INDEXER_DEPENDENCY_MISSING"


class IndexRecord(BaseModel):
    """Flat, store-agnostic representation of what gets written to a vector store.

    Immutable (frozen). Built by the `Indexer` orchestrator from one `EmbeddedChunk`
    (REQ-007, itself wrapping a REQ-006 `Chunk`) plus the owning `ArrivalEnvelope`
    (REQ-001).

    Attributes:
        chunk_id: The store's primary key (REQ-002) — upsert semantics mean retries
            overwrite, never duplicate.
        doc_id: The owning document's `doc_id` (REQ-002).
        doc_version: `envelope.content_hash` — see this module's docstring for why.
        vector: The (possibly MRL-truncated) embedding vector.
        text: The chunk's text content.
        position: The chunk's ordinal position within its document.
        section_path: The chunk's `metadata["section_path"]`, if known.
        element_types: The chunk's `element_types`, as plain strings (store-agnostic —
            never a REQ-005 `ElementType` enum instance past this boundary).
        strategy: The chunk's `metadata["strategy"]` (which `ChunkStrategy` produced it).
        allowed_groups: Security-trimming metadata carried from the envelope.
        trust_tier: Security-trimming metadata carried from the envelope.
        embedding_model: Provenance — which model produced `vector`.
        embedding_dimensions: `len(vector)`.
        config_version: Provenance — the ledger row's `config_version` for `doc_id` at
            index time (see `indexer.py`'s `_config_version_for`).
        indexed_at: UTC timestamp this record was assembled for indexing.
    """

    model_config = ConfigDict(frozen=True)

    chunk_id: str
    doc_id: str
    doc_version: str
    vector: list[float]
    text: str
    position: int
    section_path: str | None
    element_types: list[str]
    strategy: str
    allowed_groups: list[str]
    trust_tier: str
    embedding_model: str
    embedding_dimensions: int
    config_version: str
    indexed_at: datetime


class IndexResult(BaseModel):
    """The receipt for what actually landed in the store on one `Indexer.index` call.

    Immutable (frozen).

    Attributes:
        doc_id: The indexed document's `doc_id`.
        chunks_upserted: Count of chunks successfully upserted.
        chunks_deleted: Count of stale chunks purged by delete-before-upsert, `0` when
            no version change was detected (or the feature is disabled by config).
        index_name: The adapter's `index_name` this call wrote to.
        embedding_model: The single `embedding_model` shared by every upserted record.
        embedding_dimensions: The single `embedding_dimensions` shared by every
            upserted record.
    """

    model_config = ConfigDict(frozen=True)

    doc_id: str
    chunks_upserted: int
    chunks_deleted: int
    index_name: str
    embedding_model: str
    embedding_dimensions: int


class IndexerError(RaggedError):
    """Single exception type for every Indexer-raised failure.

    `error_code` varies per raise site (same shape as REQ-006/007's `ChunkerError`/
    `EmbedderError`).

    Attributes:
        doc_id: The `doc_id` of the document being indexed when this error occurred;
            `""` for failures not tied to any specific document (e.g. adapter-level
            failures raised below the orchestrator, before `doc_id` context is
            reattached).
        reason: Human-readable failure reason.
    """

    def __init__(self, doc_id: str, reason: str, *, error_code: str) -> None:
        """Initializes the exception.

        Args:
            doc_id: The `doc_id` of the document being indexed, or `""`.
            reason: Human-readable failure reason.
            error_code: One of this module's eleven registered error codes.
        """
        self.doc_id = doc_id
        self.reason = reason
        super().__init__(f"{doc_id}: {reason}", error_code=error_code)


register_error(
    INDEXER_EMPTY_RECORDS,
    Severity.PERMANENT,
    "Indexer.index was called with an empty embedded_chunks list (the Embedder "
    "returned zero chunks) — nothing to index (REQ-008)",
)
register_error(
    INDEXER_DIMENSION_MISMATCH,
    Severity.PERMANENT,
    "a record's embedding_dimensions does not match the schema's configured "
    "dimension count; raised before any write (REQ-008)",
)
register_error(
    INDEXER_MIXED_MODEL_BATCH,
    Severity.PERMANENT,
    "a single Indexer.index call carried records from more than one distinct "
    "embedding_model; raised before any write (REQ-008)",
)
register_error(
    INDEXER_SCHEMA_CONFLICT,
    Severity.PERMANENT,
    "the store's existing index has a schema incompatible with the configured "
    "dimensions/metric; requires human intervention, never retried (REQ-008)",
)
register_error(
    INDEXER_RECORD_TOO_LARGE,
    Severity.PERMANENT,
    "a single record exceeds the store's size limit, typically an oversized table "
    "chunk from REQ-006 (REQ-008)",
)
register_error(
    INDEXER_RATE_LIMITED,
    Severity.TRANSIENT,
    "the vector store returned HTTP 429 after exhausting in-adapter retries (REQ-008)",
)
register_error(
    INDEXER_UPSTREAM_ERROR,
    Severity.TRANSIENT,
    "the vector store returned an HTTP 5xx error after exhausting in-adapter retries "
    "(REQ-008)",
)
register_error(
    INDEXER_TIMEOUT,
    Severity.TRANSIENT,
    "an IndexerAdapter call did not return in time, after exhausting in-adapter "
    "retries (REQ-008)",
)
register_error(
    INDEXER_PARTIAL_BATCH_FAILURE,
    Severity.TRANSIENT,
    "some records in a batch failed to upsert after one in-orchestrator retry of "
    "only the failed chunk_ids (REQ-008)",
)
register_error(
    INDEXER_INTERNAL,
    Severity.TRANSIENT,
    "an exception from an IndexerAdapter not mapped to any other declared Indexer "
    "error code; the original exception is logged (REQ-008)",
)
register_error(
    INDEXER_DEPENDENCY_MISSING,
    Severity.PERMANENT,
    "an IndexerAdapter's required optional dependency (e.g. azure-search-documents) "
    "is not installed; never retryable — the caller must install the extra (REQ-008)",
)
