"""Embedder — data model, exceptions, and error-code registration (REQ-007).

Defines the shapes every `EmbedderAdapter` and the `Embedder` orchestrator
(`embedder.py`) operate on: `EmbeddedChunk` (the retrieval-ready, immutable output value
object wrapping REQ-006's `Chunk`) and `EmbedderError` (the single exception type this
module raises, carrying a caller-supplied `error_code`).

Failure taxonomy: this module registers eight error codes at import time (Contract #4)
— three PERMANENT (`EMBEDDER_EMPTY_CHUNKS`, `EMBEDDER_MRL_UNSUPPORTED`,
`EMBEDDER_INPUT_TOO_LONG`) and five TRANSIENT (`EMBEDDER_RATE_LIMITED`,
`EMBEDDER_UPSTREAM_ERROR`, `EMBEDDER_TIMEOUT`, `EMBEDDER_INTERNAL`,
`EMBEDDER_DEPENDENCY_MISSING`). See `embedder.py` for how each is raised and how the
ledger is updated on each.

`EMBEDDER_DEPENDENCY_MISSING` is one deliberate addition beyond the story's literal
seven-code list (flagged in this REQ's PR description as an LLD-deviation-equivalent):
the story's own adapters section requires `FastEmbedEmbedder` to "raise a clear
PERMANENT-classified error if [`fastembed` is] missing" (story, adapters section), and
Contract #4 requires every raised error to be classified via a registered code —
leaving this unregistered would silently default it to TRANSIENT (`classify()`'s
documented fallback), which is actively wrong for a condition retrying can never fix.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from vestibule.chunker.model import Chunk
from vestibule.errors.registry import RaggedError, Severity, register_error

EMBEDDER_EMPTY_CHUNKS = "EMBEDDER_EMPTY_CHUNKS"
EMBEDDER_MRL_UNSUPPORTED = "EMBEDDER_MRL_UNSUPPORTED"
EMBEDDER_INPUT_TOO_LONG = "EMBEDDER_INPUT_TOO_LONG"
EMBEDDER_RATE_LIMITED = "EMBEDDER_RATE_LIMITED"
EMBEDDER_UPSTREAM_ERROR = "EMBEDDER_UPSTREAM_ERROR"
EMBEDDER_TIMEOUT = "EMBEDDER_TIMEOUT"
EMBEDDER_INTERNAL = "EMBEDDER_INTERNAL"
EMBEDDER_DEPENDENCY_MISSING = "EMBEDDER_DEPENDENCY_MISSING"


class EmbeddedChunk(BaseModel):
    """Retrieval-ready embedded unit — wraps a REQ-006 `Chunk` with its vector.

    Immutable (frozen).

    Attributes:
        chunk: The wrapped `Chunk` this embedding was derived from.
        vector: The (possibly MRL-truncated and re-normalized) embedding vector.
        embedding_model: Provenance, e.g. `"text-embedding-3-large"`
            (`EmbedderAdapter.model_name`).
        embedding_dimensions: `len(vector)` — the post-truncation dimension count.
        truncated_from: The adapter's native dimension count if MRL truncation was
            applied to produce `vector`; `None` if `vector` is the untruncated native
            embedding.
    """

    model_config = ConfigDict(frozen=True)

    chunk: Chunk
    vector: list[float]
    embedding_model: str
    embedding_dimensions: int
    truncated_from: int | None = None


class EmbedderError(RaggedError):
    """Single exception type for every Embedder-raised failure.

    `error_code` varies per raise site (same shape as REQ-006's `ChunkerError`).

    Attributes:
        doc_id: The `doc_id` of the document being embedded when this error occurred;
            `""` for failures not tied to any specific document (e.g.
            `EMBEDDER_MRL_UNSUPPORTED`/`EMBEDDER_DEPENDENCY_MISSING`, both raised at
            adapter/orchestrator construction time, before any document is in scope).
        reason: Human-readable failure reason.
    """

    def __init__(self, doc_id: str, reason: str, *, error_code: str) -> None:
        """Initializes the exception.

        Args:
            doc_id: The `doc_id` of the document being embedded, or `""` for a
                construction-time failure not tied to any document.
            reason: Human-readable failure reason.
            error_code: One of this module's eight registered error codes.
        """
        self.doc_id = doc_id
        self.reason = reason
        super().__init__(f"{doc_id}: {reason}", error_code=error_code)


register_error(
    EMBEDDER_EMPTY_CHUNKS,
    Severity.PERMANENT,
    "Embedder.embed was called with an empty chunks list (the Chunker returned zero "
    "chunks) — nothing to embed (REQ-007)",
)
register_error(
    EMBEDDER_MRL_UNSUPPORTED,
    Severity.PERMANENT,
    "EmbedderConfig.target_dimensions was set but the configured EmbedderAdapter "
    "reports supports_mrl=False — raised at construction time, fail fast (REQ-007)",
)
register_error(
    EMBEDDER_INPUT_TOO_LONG,
    Severity.PERMANENT,
    "a Chunk's token_count exceeds the configured EmbedderAdapter's max_input_tokens; "
    "indicates a Chunker/Embedder token_counter config mismatch, not a transient "
    "condition (REQ-007)",
)
register_error(
    EMBEDDER_RATE_LIMITED,
    Severity.TRANSIENT,
    "the embedding provider returned HTTP 429 after exhausting in-adapter retries "
    "(REQ-007)",
)
register_error(
    EMBEDDER_UPSTREAM_ERROR,
    Severity.TRANSIENT,
    "the embedding provider returned an HTTP 5xx error after exhausting in-adapter "
    "retries (REQ-007)",
)
register_error(
    EMBEDDER_TIMEOUT,
    Severity.TRANSIENT,
    "an EmbedderAdapter.embed_batch call did not return in time, after exhausting "
    "in-adapter retries (REQ-007)",
)
register_error(
    EMBEDDER_INTERNAL,
    Severity.TRANSIENT,
    "an exception from an EmbedderAdapter not mapped to any other declared Embedder "
    "error code; the original exception is logged (REQ-007)",
)
register_error(
    EMBEDDER_DEPENDENCY_MISSING,
    Severity.PERMANENT,
    "an EmbedderAdapter's required optional dependency (e.g. fastembed) is not "
    "installed; never retryable — the caller must install the extra (REQ-007)",
)
