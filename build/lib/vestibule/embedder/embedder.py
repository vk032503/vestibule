"""Embedder orchestrator (REQ-007).

`Embedder.embed` owns exactly the `embedding` stage of Contract #3's pipeline FSM:
per-adapter batching, task-type document-prefix application, Matryoshka (MRL) dimension
truncation/re-normalization, and the `embedding -> indexing` ledger transition. It owns
no embedding algorithm itself — delegates entirely to `EmbedderAdapter` implementations
(house rules: "never embed in-house").

Failure-path/ledger-write contract (mirrors REQ-006's `Chunker`, following the issue #9
lesson): the two PERMANENT failure paths (`EMBEDDER_EMPTY_CHUNKS`,
`EMBEDDER_INPUT_TOO_LONG`) terminalize the ledger row to `Status.FAILED` — and, exactly
like `Chunker._terminalize_empty_elements`, each terminalize path triages its own ledger
write via `is_benign_concurrent_loss` before treating it as a genuine failure. Every
TRANSIENT failure path (`EMBEDDER_RATE_LIMITED`, `EMBEDDER_UPSTREAM_ERROR`,
`EMBEDDER_TIMEOUT`, `EMBEDDER_INTERNAL`) performs a same-status `embedding -> embedding`
self-transition instead — never `Status.FAILED` — so a redelivered `embed()` call
remains re-enterable. `EMBEDDER_MRL_UNSUPPORTED` is raised by `__init__`, before any
document or ledger row is in scope (fail fast, story: "not at embed time"). `Embedder`
performs no *entry* transition of its own: the row is already `embedding` on entry
(written by `Chunker`'s own `chunking -> embedding` transition), so there is exactly one
ledger write on the happy path (the exit).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Iterator, NoReturn, TypeVar

from vestibule.chunker.model import Chunk
from vestibule.embedder.adapters.base import EmbedderAdapter
from vestibule.embedder.model import (
    EMBEDDER_EMPTY_CHUNKS,
    EMBEDDER_INPUT_TOO_LONG,
    EMBEDDER_INTERNAL,
    EMBEDDER_MRL_UNSUPPORTED,
    EMBEDDER_RATE_LIMITED,
    EMBEDDER_TIMEOUT,
    EMBEDDER_UPSTREAM_ERROR,
    EmbeddedChunk,
    EmbedderError,
)
from vestibule.envelope.model import ArrivalEnvelope
from vestibule.ledger.store import (
    LedgerStore,
    LedgerTransitionInvalid,
    Status,
    is_benign_concurrent_loss,
)

logger = logging.getLogger(__name__)

_DEFAULT_TARGET_DIMENSIONS = 1024
_DEFAULT_BATCH_SIZE = None
_DEFAULT_MAX_ATTEMPTS = 5
_DEFAULT_BACKOFF_BASE_SECONDS = 2.0
_DEFAULT_BACKOFF_MAX_SECONDS = 60.0

_KNOWN_ADAPTER_ERROR_CODES = frozenset(
    {EMBEDDER_RATE_LIMITED, EMBEDDER_UPSTREAM_ERROR, EMBEDDER_TIMEOUT}
)

_T = TypeVar("_T")


@dataclass(frozen=True)
class RetryConfig:
    """In-adapter retry tunables (story: "Retries").

    Documentation-authoritative for `config/embedder.yaml`'s `retry.*` block — actual
    retry execution happens inside each `EmbedderAdapter` implementation, which each
    accept their own equivalent constructor parameters directly (no config-loading
    mechanism exists anywhere in this codebase yet, REQ-005/006 precedent).

    Attributes:
        max_attempts: Maximum attempts (first call plus retries) before an adapter
            raises its exhausted-retry TRANSIENT `EmbedderError`.
        backoff_base_seconds: Exponential-backoff-with-jitter multiplier between
            retries.
        backoff_max_seconds: Cap on the computed backoff delay.
    """

    max_attempts: int = _DEFAULT_MAX_ATTEMPTS
    backoff_base_seconds: float = _DEFAULT_BACKOFF_BASE_SECONDS
    backoff_max_seconds: float = _DEFAULT_BACKOFF_MAX_SECONDS


@dataclass(frozen=True)
class EmbedderConfig:
    """Documentation-authoritative-but-code-defaults tunables (REQ-006 §6 precedent).

    Code-authoritative defaults, kept in sync with `config/embedder.yaml` via a
    dedicated test. `adapter`/`model` (which `EmbedderAdapter` to construct) are
    composition-root concerns documented in `config/embedder.yaml` only — not fields
    here, mirroring `config/analyzer.yaml`'s `docint.*` block never appearing on
    `AnalyzerConfig`.

    Attributes:
        target_dimensions: Matryoshka (MRL) truncation target. When not `None`, every
            adapter's vector is truncated to this many leading dimensions and
            re-normalized (L2). Requires the configured `EmbedderAdapter.supports_mrl`
            to be `True` — checked once, at `Embedder.__init__` time.
        batch_size: Overrides the configured adapter's own `max_batch_size` when set
            (and not larger than it); `None` means use the adapter's `max_batch_size`
            unmodified.
        retry: In-adapter retry tunables (documentation-only here; see `RetryConfig`).
    """

    target_dimensions: int | None = _DEFAULT_TARGET_DIMENSIONS
    batch_size: int | None = _DEFAULT_BATCH_SIZE
    retry: RetryConfig = field(default_factory=RetryConfig)


class Embedder:
    """Orchestrates batching, prefixing, MRL truncation, and the `embedding` ledger
    stage transitions. Owns no embedding algorithm itself."""

    def __init__(
        self, ledger: LedgerStore, config: EmbedderConfig, adapter: EmbedderAdapter
    ) -> None:
        """Initializes the Embedder.

        Args:
            ledger: The `LedgerStore` this Embedder reads/writes `LedgerRow`s through.
            config: Operational tunables.
            adapter: The `EmbedderAdapter` every `embed()` call dispatches to.

        Raises:
            EmbedderError: `EMBEDDER_MRL_UNSUPPORTED` (PERMANENT) if
                `config.target_dimensions` is set but `adapter.supports_mrl` is
                `False` — fail fast, before any document is ever in scope.
        """
        if config.target_dimensions is not None and not adapter.supports_mrl:
            raise EmbedderError(
                "",
                f"target_dimensions={config.target_dimensions} was configured but "
                f"adapter {adapter.model_name!r} does not support MRL truncation",
                error_code=EMBEDDER_MRL_UNSUPPORTED,
            )
        self._ledger = ledger
        self._config = config
        self._adapter = adapter

    def embed(
        self, envelope: ArrivalEnvelope, chunks: list[Chunk]
    ) -> list[EmbeddedChunk]:
        """Embeds `chunks` and advances the ledger accordingly.

        Args:
            envelope: The validated `ArrivalEnvelope` for this document. Only
                `envelope.doc_id` is read (Contract #1 touchpoint). A `LedgerRow` for
                `envelope.doc_id` must already be in `Status.EMBEDDING` (written by
                `Chunker`'s own `chunking -> embedding` transition).
            chunks: The `Chunk`s to embed, e.g. from `Chunker.chunk`.

        Returns:
            One `EmbeddedChunk` per input chunk, in the same order.

        Raises:
            EmbedderError: `EMBEDDER_EMPTY_CHUNKS`/`EMBEDDER_INPUT_TOO_LONG`
                (PERMANENT, ledger row ends `Status.FAILED`), or
                `EMBEDDER_RATE_LIMITED`/`EMBEDDER_UPSTREAM_ERROR`/`EMBEDDER_TIMEOUT`/
                `EMBEDDER_INTERNAL` (TRANSIENT, ledger row self-transitions and stays
                `Status.EMBEDDING`).
            LedgerTransitionInvalid: Propagated unchanged when a ledger transition
                fails for a reason `is_benign_concurrent_loss` classifies as not benign.
        """
        doc_id = envelope.doc_id
        if not chunks:
            self._terminalize(
                doc_id,
                EMBEDDER_EMPTY_CHUNKS,
                "Chunker returned zero chunks; nothing to embed",
            )
        self._validate_input_lengths(doc_id, chunks)
        embedded = self._run_with_recovery(doc_id, chunks)
        return self._transition_to_indexing(doc_id, embedded)

    def _validate_input_lengths(self, doc_id: str, chunks: list[Chunk]) -> None:
        """Rejects any chunk exceeding the adapter's `max_input_tokens` (fail fast).

        Args:
            doc_id: The document's `doc_id` (ledger key).
            chunks: The `Chunk`s to validate.

        Raises:
            EmbedderError: `EMBEDDER_INPUT_TOO_LONG` (PERMANENT), on the first
                offending chunk found.
        """
        limit = self._adapter.max_input_tokens
        for chunk in chunks:
            if chunk.token_count > limit:
                self._terminalize(
                    doc_id,
                    EMBEDDER_INPUT_TOO_LONG,
                    f"chunk position={chunk.position} token_count={chunk.token_count} "
                    f"exceeds adapter {self._adapter.model_name!r}'s "
                    f"max_input_tokens={limit}",
                )

    def _terminalize(self, doc_id: str, error_code: str, reason: str) -> NoReturn:
        """Marks the ledger row `Status.FAILED` (triaged) and raises `EmbedderError`.

        Args:
            doc_id: The document's `doc_id` (ledger key).
            error_code: `EMBEDDER_EMPTY_CHUNKS` or `EMBEDDER_INPUT_TOO_LONG`.
            reason: Human-readable failure reason.

        Raises:
            EmbedderError: Always.
            LedgerTransitionInvalid: If the ledger write fails non-benignly.
        """
        try:
            self._ledger.transition(
                doc_id,
                to_status=Status.FAILED,
                stage=Status.FAILED,
                error_code=error_code,
                error_message=reason,
            )
        except LedgerTransitionInvalid as exc:
            if not is_benign_concurrent_loss(
                exc.observed_row, Status.FAILED, Status.FAILED
            ):
                raise
            # Benign: a concurrent duplicate worker already completed the whole
            # pipeline for doc_id. A terminal INDEXED row must never be downgraded to
            # FAILED, but this call's own observation is still worth surfacing.
        raise EmbedderError(doc_id, reason, error_code=error_code)

    def _run_with_recovery(
        self, doc_id: str, chunks: list[Chunk]
    ) -> list[EmbeddedChunk]:
        """Runs the batching/embedding pipeline, mapping any failure to a TRANSIENT code.

        Args:
            doc_id: The document's `doc_id` (ledger key).
            chunks: The `Chunk`s to embed (guaranteed non-empty and within
                `max_input_tokens` by `embed`).

        Returns:
            One `EmbeddedChunk` per input chunk, in the same order.

        Raises:
            EmbedderError: `EMBEDDER_RATE_LIMITED`/`EMBEDDER_UPSTREAM_ERROR`/
                `EMBEDDER_TIMEOUT`/`EMBEDDER_INTERNAL` (all TRANSIENT).
        """
        try:
            return self._embed_all(chunks)
        except EmbedderError as exc:
            if exc.error_code in _KNOWN_ADAPTER_ERROR_CODES:
                self._self_transition_and_raise(
                    doc_id, exc.error_code, exc.reason, cause=exc
                )
            logger.exception(
                "embedder_internal_unexpected_embedder_error",
                extra={"doc_id": doc_id, "error_code": exc.error_code},
            )
            self._self_transition_and_raise(
                doc_id, EMBEDDER_INTERNAL, str(exc), cause=exc
            )
        except Exception as exc:  # any other unmapped exception -> EMBEDDER_INTERNAL.
            logger.exception("embedder_internal", extra={"doc_id": doc_id})
            self._self_transition_and_raise(
                doc_id, EMBEDDER_INTERNAL, str(exc), cause=exc
            )

    def _self_transition_and_raise(
        self, doc_id: str, error_code: str, reason: str, *, cause: BaseException
    ) -> NoReturn:
        """Same-status `embedding -> embedding` self-transition, then raises.

        Args:
            doc_id: The document's `doc_id` (ledger key).
            error_code: One of the four TRANSIENT Embedder error codes.
            reason: Human-readable failure reason.
            cause: The original exception to chain via `raise ... from cause`.

        Raises:
            EmbedderError: Always.
        """
        self._ledger.transition(
            doc_id,
            to_status=Status.EMBEDDING,
            stage=Status.EMBEDDING,
            error_code=error_code,
            error_message=reason,
        )
        raise EmbedderError(doc_id, reason, error_code=error_code) from cause

    def _embed_all(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
        """Prefixes, batches, and embeds every chunk, then applies MRL truncation.

        Args:
            chunks: The `Chunk`s to embed.

        Returns:
            One `EmbeddedChunk` per input chunk, in the same order.
        """
        prefix = self._adapter.document_prefix
        texts = [_apply_prefix(chunk.text, prefix) for chunk in chunks]
        vectors: list[list[float]] = []
        for batch in _batched(texts, self._effective_batch_size()):
            vectors.extend(self._adapter.embed_batch(batch))
        return [
            self._build_embedded_chunk(chunk, vector)
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]

    def _effective_batch_size(self) -> int:
        """The batch size to split `embed_batch` calls into (story: "Batching").

        Returns:
            `config.batch_size` capped at `adapter.max_batch_size` when configured;
            `adapter.max_batch_size` unmodified otherwise.
        """
        if self._config.batch_size is None:
            return self._adapter.max_batch_size
        return min(self._config.batch_size, self._adapter.max_batch_size)

    def _build_embedded_chunk(self, chunk: Chunk, vector: list[float]) -> EmbeddedChunk:
        """Builds one `EmbeddedChunk`, applying MRL truncation if configured.

        Args:
            chunk: The source `Chunk`.
            vector: The adapter-returned native-dimension embedding vector.

        Returns:
            An `EmbeddedChunk` wrapping `chunk`, with `vector` truncated/re-normalized
            to `config.target_dimensions` when set; the native `vector` unchanged
            otherwise.
        """
        target = self._config.target_dimensions
        if target is None:
            return EmbeddedChunk(
                chunk=chunk,
                vector=vector,
                embedding_model=self._adapter.model_name,
                embedding_dimensions=len(vector),
                truncated_from=None,
            )
        truncated = _truncate_and_renormalize(vector, target)
        return EmbeddedChunk(
            chunk=chunk,
            vector=truncated,
            embedding_model=self._adapter.model_name,
            embedding_dimensions=len(truncated),
            truncated_from=len(vector),
        )

    def _transition_to_indexing(
        self, doc_id: str, embedded: list[EmbeddedChunk]
    ) -> list[EmbeddedChunk]:
        """The happy-path `embedding -> indexing` exit transition.

        Args:
            doc_id: The document's `doc_id` (ledger key).
            embedded: The assembled `EmbeddedChunk`s to return on success/benign
                redelivery.

        Returns:
            `embedded`, unchanged.

        Raises:
            LedgerTransitionInvalid: If the transition fails non-benignly.
        """
        try:
            self._ledger.transition(
                doc_id, to_status=Status.INDEXING, stage=Status.INDEXING
            )
        except LedgerTransitionInvalid as exc:
            if not is_benign_concurrent_loss(
                exc.observed_row, Status.INDEXING, Status.INDEXING
            ):
                raise
            # Benign: a concurrent duplicate worker already made this exact edge (or
            # later) happen — deterministic recomputation (Contract #2) makes returning
            # the freshly-computed EmbeddedChunks safe, not a correctness risk.
        return embedded


def _apply_prefix(text: str, prefix: str | None) -> str:
    """Prepends `prefix` to `text` exactly once, or returns `text` unchanged if `None`.

    Args:
        text: The chunk text to prefix.
        prefix: The adapter's declared `document_prefix`, or `None`.

    Returns:
        `f"{prefix}{text}"` if `prefix` is not `None`; `text` unchanged otherwise.
    """
    return text if prefix is None else f"{prefix}{text}"


def _batched(items: list[_T], size: int) -> Iterator[list[_T]]:
    """Splits `items` into consecutive, order-preserving chunks of at most `size`.

    Args:
        items: The items to split.
        size: The maximum size of each yielded batch; must be `> 0`.

    Yields:
        Successive slices of `items`, each of length `size` except possibly the last.
    """
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _truncate_and_renormalize(
    vector: list[float], target_dimensions: int
) -> list[float]:
    """Matryoshka (MRL) truncation: first `target_dimensions` dims, then L2-normalized.

    Re-normalization is required for cosine similarity to remain valid after
    truncation (story: "Matryoshka dimension truncation").

    Args:
        vector: The native-dimension embedding vector.
        target_dimensions: The number of leading dimensions to keep.

    Returns:
        `vector[:target_dimensions]`, divided by its own L2 norm; returned unscaled
        (all-zero) if that norm is `0.0`, to avoid a division by zero.
    """
    truncated = vector[:target_dimensions]
    norm = math.sqrt(sum(value * value for value in truncated))
    if norm == 0.0:
        return truncated
    return [value / norm for value in truncated]
