"""Indexer orchestrator (REQ-008).

`Indexer.index` owns exactly the `indexing` stage of Contract #3's pipeline FSM:
`IndexRecord` assembly, dimension/mixed-model validation, batching per adapter,
delete-before-upsert on version change, partial-batch retry, and the
`indexing -> indexed` ledger transition. It owns no indexing/search algorithm itself —
delegates entirely to `IndexerAdapter` implementations (house rules: "adapters thin").

Failure-path/ledger-write contract (mirrors REQ-006/007's `Chunker`/`Embedder`,
following the issue #9 lesson): the PERMANENT failure paths (`INDEXER_EMPTY_RECORDS`,
`INDEXER_DIMENSION_MISMATCH`, `INDEXER_MIXED_MODEL_BATCH`, `INDEXER_SCHEMA_CONFLICT`,
`INDEXER_RECORD_TOO_LARGE`) terminalize the ledger row to `Status.FAILED` — each
terminalize path triages its own ledger write via `is_benign_concurrent_loss` before
treating it as a genuine failure. Every TRANSIENT failure path (`INDEXER_RATE_LIMITED`,
`INDEXER_UPSTREAM_ERROR`, `INDEXER_TIMEOUT`, `INDEXER_PARTIAL_BATCH_FAILURE`,
`INDEXER_INTERNAL`) performs a same-status `indexing -> indexing` self-transition
instead — never `Status.FAILED` — so a redelivered `index()` call remains re-enterable.
`Indexer` performs no *entry* transition of its own: the row is already `indexing` on
entry (written by `Embedder`'s own `embedding -> indexing` transition), so there is
exactly one ledger write on the happy path (the exit).

Spec-gap resolution (`config_version`): `IndexRecord.config_version` has no REQ-006/007
precedent to follow (`Chunk`/`EmbeddedChunk` carry no such field). This module resolves
it by reading the current ledger row's own `config_version` (`LedgerRow.config_version`,
set once at `create()` from `EnvelopeSummary`, REQ-003) via `_config_version_for` — the
ledger is already the canonical, single-write-at-creation carrier of this value, so
reusing it avoids introducing a second, potentially-drifting source of truth. Falls back
to `"unknown"` only if no ledger row exists for `doc_id` at all (defensive; the pipeline
convention is that a row already exists in `Status.INDEXING` by the time `index()` runs).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, NoReturn, TypeVar

from vestibule.embedder.model import EmbeddedChunk
from vestibule.envelope.model import ArrivalEnvelope
from vestibule.indexer.adapters.base import IndexerAdapter
from vestibule.indexer.model import (
    INDEXER_DIMENSION_MISMATCH,
    INDEXER_EMPTY_RECORDS,
    INDEXER_INTERNAL,
    INDEXER_MIXED_MODEL_BATCH,
    INDEXER_PARTIAL_BATCH_FAILURE,
    INDEXER_RATE_LIMITED,
    INDEXER_RECORD_TOO_LARGE,
    INDEXER_SCHEMA_CONFLICT,
    INDEXER_TIMEOUT,
    INDEXER_UPSTREAM_ERROR,
    IndexerError,
    IndexRecord,
    IndexResult,
)
from vestibule.ledger.store import (
    LedgerStore,
    LedgerTransitionInvalid,
    Status,
    is_benign_concurrent_loss,
)

logger = logging.getLogger(__name__)

_DEFAULT_DIMENSIONS = 1024
"""Must match the configured `EmbedderConfig.target_dimensions` (or the adapter's
native output dimensions if no MRL truncation is applied) — see this module's
docstring and `config/indexer.yaml`'s NOTE for why this is a code-authoritative
`IndexerConfig` field despite not appearing in the story's literal `indexer.yaml` key
list (a second, explicitly-flagged spec gap, alongside `config_version`)."""
_DEFAULT_METRIC = "cosine"
_DEFAULT_BATCH_SIZE = None
_DEFAULT_DELETE_ON_VERSION_CHANGE = True
_DEFAULT_MAX_ATTEMPTS = 5
_DEFAULT_BACKOFF_BASE_SECONDS = 2.0
_DEFAULT_BACKOFF_MAX_SECONDS = 60.0
_UNKNOWN_CONFIG_VERSION = "unknown"

_PERMANENT_UPSERT_CODES = frozenset({INDEXER_SCHEMA_CONFLICT, INDEXER_RECORD_TOO_LARGE})
_TRANSIENT_UPSERT_CODES = frozenset(
    {
        INDEXER_RATE_LIMITED,
        INDEXER_UPSTREAM_ERROR,
        INDEXER_TIMEOUT,
        INDEXER_PARTIAL_BATCH_FAILURE,
    }
)

_T = TypeVar("_T")


@dataclass(frozen=True)
class RetryConfig:
    """In-adapter retry tunables (story: "Retries").

    Documentation-authoritative for `config/indexer.yaml`'s `retry.*` block — actual
    retry execution happens inside each `IndexerAdapter` implementation, which accepts
    its own equivalent constructor parameters directly (REQ-007 `RetryConfig`
    precedent).

    Attributes:
        max_attempts: Maximum attempts (first call plus retries) before an adapter
            raises its exhausted-retry TRANSIENT `IndexerError`.
        backoff_base_seconds: Exponential-backoff-with-jitter multiplier between
            retries.
        backoff_max_seconds: Cap on the computed backoff delay.
    """

    max_attempts: int = _DEFAULT_MAX_ATTEMPTS
    backoff_base_seconds: float = _DEFAULT_BACKOFF_BASE_SECONDS
    backoff_max_seconds: float = _DEFAULT_BACKOFF_MAX_SECONDS


@dataclass(frozen=True)
class IndexerConfig:
    """Documentation-authoritative-but-code-defaults tunables (REQ-006/007 precedent).

    Code-authoritative defaults, kept in sync with `config/indexer.yaml` via a
    dedicated test. `adapter`/`index_name` (which `IndexerAdapter` to construct) are
    composition-root concerns documented in `config/indexer.yaml` only — not fields
    here, mirroring `config/embedder.yaml`'s `adapter`/`model` keys.

    Attributes:
        dimensions: The expected vector dimension count, passed to
            `adapter.ensure_schema` at `Indexer.__init__` time and checked against
            every record's `embedding_dimensions` before any write. See this module's
            docstring for why this is a code-authoritative field (a flagged spec gap).
        metric: The vector similarity metric, passed to `adapter.ensure_schema`.
        batch_size: Overrides the configured adapter's own `max_batch_size` when set
            (and not larger than it); `None` means use the adapter's `max_batch_size`
            unmodified.
        delete_before_upsert_on_version_change: When `True` (default), a detected
            `doc_version` change for `doc_id` deletes all of its existing records
            before upserting the new ones (story: "Delete-before-upsert on version
            change").
        retry: In-adapter retry tunables (documentation-only here; see `RetryConfig`).
    """

    dimensions: int = _DEFAULT_DIMENSIONS
    metric: str = _DEFAULT_METRIC
    batch_size: int | None = _DEFAULT_BATCH_SIZE
    delete_before_upsert_on_version_change: bool = _DEFAULT_DELETE_ON_VERSION_CHANGE
    retry: RetryConfig = field(default_factory=RetryConfig)


class Indexer:
    """Orchestrates `IndexRecord` assembly, batching, delete-before-upsert, and the
    `indexing` ledger stage transitions. Owns no indexing/search algorithm itself."""

    def __init__(
        self, ledger: LedgerStore, config: IndexerConfig, adapter: IndexerAdapter
    ) -> None:
        """Initializes the Indexer, ensuring the adapter's schema exists.

        Args:
            ledger: The `LedgerStore` this Indexer reads/writes `LedgerRow`s through.
            config: Operational tunables.
            adapter: The `IndexerAdapter` every `index()` call dispatches to.

        Raises:
            IndexerError: `INDEXER_SCHEMA_CONFLICT` (PERMANENT) if `adapter`'s existing
                index has an incompatible schema — fail fast, before any document is
                ever in scope.
        """
        adapter.ensure_schema(config.dimensions, config.metric)
        self._ledger = ledger
        self._config = config
        self._adapter = adapter

    def index(
        self, envelope: ArrivalEnvelope, embedded_chunks: list[EmbeddedChunk]
    ) -> IndexResult:
        """Indexes `embedded_chunks` and advances the ledger accordingly.

        Args:
            envelope: The validated `ArrivalEnvelope` for this document. A `LedgerRow`
                for `envelope.doc_id` must already be in `Status.INDEXING` (written by
                `Embedder`'s own `embedding -> indexing` transition).
            embedded_chunks: The `EmbeddedChunk`s to index, e.g. from `Embedder.embed`.

        Returns:
            The `IndexResult` receipt for what landed in the store.

        Raises:
            IndexerError: `INDEXER_EMPTY_RECORDS`/`INDEXER_DIMENSION_MISMATCH`/
                `INDEXER_MIXED_MODEL_BATCH`/`INDEXER_SCHEMA_CONFLICT`/
                `INDEXER_RECORD_TOO_LARGE` (PERMANENT, ledger row ends `Status.FAILED`),
                or `INDEXER_RATE_LIMITED`/`INDEXER_UPSTREAM_ERROR`/`INDEXER_TIMEOUT`/
                `INDEXER_PARTIAL_BATCH_FAILURE`/`INDEXER_INTERNAL` (TRANSIENT, ledger
                row self-transitions and stays `Status.INDEXING`).
            LedgerTransitionInvalid: Propagated unchanged when a ledger transition
                fails for a reason `is_benign_concurrent_loss` classifies as not benign.
        """
        doc_id = envelope.doc_id
        if not embedded_chunks:
            self._terminalize(
                doc_id,
                INDEXER_EMPTY_RECORDS,
                "Embedder returned zero chunks; nothing to index",
            )
        self._validate_batch(doc_id, embedded_chunks)
        records = self._build_records(doc_id, envelope, embedded_chunks)
        result = self._run_with_recovery(doc_id, records)
        return self._transition_to_indexed(doc_id, result)

    def _validate_batch(
        self, doc_id: str, embedded_chunks: list[EmbeddedChunk]
    ) -> None:
        """Rejects mixed-model batches and dimension mismatches (fail fast).

        Args:
            doc_id: The document's `doc_id` (ledger key).
            embedded_chunks: The `EmbeddedChunk`s to validate.

        Raises:
            IndexerError: `INDEXER_MIXED_MODEL_BATCH` if more than one distinct
                `embedding_model` is present, checked before `INDEXER_DIMENSION_MISMATCH`
                (both PERMANENT).
        """
        models = {chunk.embedding_model for chunk in embedded_chunks}
        if len(models) > 1:
            self._terminalize(
                doc_id,
                INDEXER_MIXED_MODEL_BATCH,
                f"batch contains {len(models)} distinct embedding_model values: "
                f"{sorted(models)}",
            )
        for chunk in embedded_chunks:
            if chunk.embedding_dimensions != self._config.dimensions:
                self._terminalize(
                    doc_id,
                    INDEXER_DIMENSION_MISMATCH,
                    f"chunk_id={chunk.chunk.chunk_id} "
                    f"embedding_dimensions={chunk.embedding_dimensions} does not "
                    f"match configured dimensions={self._config.dimensions}",
                )

    def _build_records(
        self,
        doc_id: str,
        envelope: ArrivalEnvelope,
        embedded_chunks: list[EmbeddedChunk],
    ) -> list[IndexRecord]:
        """Assembles one `IndexRecord` per `EmbeddedChunk` (guaranteed valid by
        `_validate_batch`).

        Args:
            doc_id: The document's `doc_id`.
            envelope: The validated `ArrivalEnvelope`; supplies `content_hash`
                (`doc_version`, see this module's docstring), `allowed_groups`,
                `trust_tier`.
            embedded_chunks: The `EmbeddedChunk`s to convert.

        Returns:
            One `IndexRecord` per input chunk, in the same order.
        """
        config_version = self._config_version_for(doc_id)
        indexed_at = datetime.now(timezone.utc)
        allowed_groups = list(envelope.allowed_groups)
        trust_tier = envelope.trust_tier.value
        return [
            IndexRecord(
                chunk_id=ec.chunk.chunk_id,
                doc_id=doc_id,
                doc_version=envelope.content_hash,
                vector=ec.vector,
                text=ec.chunk.text,
                position=ec.chunk.position,
                section_path=ec.chunk.metadata.get("section_path"),
                element_types=[et.value for et in ec.chunk.element_types],
                strategy=str(ec.chunk.metadata.get("strategy", "")),
                allowed_groups=allowed_groups,
                trust_tier=trust_tier,
                embedding_model=ec.embedding_model,
                embedding_dimensions=ec.embedding_dimensions,
                config_version=config_version,
                indexed_at=indexed_at,
            )
            for ec in embedded_chunks
        ]

    def _config_version_for(self, doc_id: str) -> str:
        """Resolves `IndexRecord.config_version` — see this module's docstring.

        Args:
            doc_id: The document's `doc_id` (ledger key).

        Returns:
            The current ledger row's `config_version`, or `_UNKNOWN_CONFIG_VERSION` if
            no row exists (defensive; should not occur in normal pipeline operation).
        """
        row = self._ledger.get(doc_id)
        return row.config_version if row is not None else _UNKNOWN_CONFIG_VERSION

    def _terminalize(self, doc_id: str, error_code: str, reason: str) -> NoReturn:
        """Marks the ledger row `Status.FAILED` (triaged) and raises `IndexerError`.

        Args:
            doc_id: The document's `doc_id` (ledger key).
            error_code: One of this module's five PERMANENT error codes.
            reason: Human-readable failure reason.

        Raises:
            IndexerError: Always.
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
        raise IndexerError(doc_id, reason, error_code=error_code)

    def _run_with_recovery(
        self, doc_id: str, records: list[IndexRecord]
    ) -> IndexResult:
        """Runs the delete/upsert pipeline, routing any failure to the right ledger edge.

        Args:
            doc_id: The document's `doc_id` (ledger key).
            records: The `IndexRecord`s to index (guaranteed non-empty and validated
                by `index`).

        Returns:
            The `IndexResult` receipt.

        Raises:
            IndexerError: `INDEXER_SCHEMA_CONFLICT`/`INDEXER_RECORD_TOO_LARGE`
                (PERMANENT, terminalizes), or `INDEXER_RATE_LIMITED`/
                `INDEXER_UPSTREAM_ERROR`/`INDEXER_TIMEOUT`/
                `INDEXER_PARTIAL_BATCH_FAILURE`/`INDEXER_INTERNAL` (TRANSIENT,
                self-transitions).
        """
        try:
            return self._upsert_all(doc_id, records)
        except IndexerError as exc:
            if exc.error_code in _PERMANENT_UPSERT_CODES:
                self._terminalize(doc_id, exc.error_code, exc.reason)
            if exc.error_code in _TRANSIENT_UPSERT_CODES:
                self._self_transition_and_raise(
                    doc_id, exc.error_code, exc.reason, cause=exc
                )
            logger.exception(
                "indexer_internal_unexpected_indexer_error",
                extra={"doc_id": doc_id, "error_code": exc.error_code},
            )
            self._self_transition_and_raise(
                doc_id, INDEXER_INTERNAL, str(exc), cause=exc
            )
        except Exception as exc:  # any other unmapped exception -> INDEXER_INTERNAL.
            logger.exception("indexer_internal", extra={"doc_id": doc_id})
            self._self_transition_and_raise(
                doc_id, INDEXER_INTERNAL, str(exc), cause=exc
            )

    def _self_transition_and_raise(
        self, doc_id: str, error_code: str, reason: str, *, cause: BaseException
    ) -> NoReturn:
        """Same-status `indexing -> indexing` self-transition, then raises.

        Args:
            doc_id: The document's `doc_id` (ledger key).
            error_code: One of the five TRANSIENT Indexer error codes.
            reason: Human-readable failure reason.
            cause: The original exception to chain via `raise ... from cause`.

        Raises:
            IndexerError: Always.
            LedgerTransitionInvalid: If the ledger write fails non-benignly.
        """
        try:
            self._ledger.transition(
                doc_id,
                to_status=Status.INDEXING,
                stage=Status.INDEXING,
                error_code=error_code,
                error_message=reason,
            )
        except LedgerTransitionInvalid as exc:
            if not is_benign_concurrent_loss(
                exc.observed_row, Status.INDEXING, Status.INDEXING
            ):
                raise
            # Benign: a concurrent duplicate worker already advanced doc_id past
            # `indexing` (e.g. to a terminal state) before this straggler's
            # self-transition landed. The retry this raise triggers is still
            # correct: upsert idempotency (Contract #2) makes it safe even though
            # the row itself has already moved on.
        raise IndexerError(doc_id, reason, error_code=error_code) from cause

    def _upsert_all(self, doc_id: str, records: list[IndexRecord]) -> IndexResult:
        """Delete-before-upsert-on-version-change, then batched upsert with retry.

        Args:
            doc_id: The document's `doc_id`.
            records: The `IndexRecord`s to index; all share `doc_id`/`embedding_model`/
                `embedding_dimensions`/`doc_version` by construction.

        Returns:
            The `IndexResult` receipt.
        """
        chunks_deleted = self._maybe_delete_stale_version(
            doc_id, records[0].doc_version
        )
        chunks_upserted = 0
        for batch in _batched(records, self._effective_batch_size()):
            chunks_upserted += self._upsert_batch_with_retry(batch)
        return IndexResult(
            doc_id=doc_id,
            chunks_upserted=chunks_upserted,
            chunks_deleted=chunks_deleted,
            index_name=self._adapter.index_name,
            embedding_model=records[0].embedding_model,
            embedding_dimensions=records[0].embedding_dimensions,
        )

    def _maybe_delete_stale_version(self, doc_id: str, new_version: str) -> int:
        """Story: "Delete-before-upsert on version change".

        Args:
            doc_id: The document's `doc_id`.
            new_version: The `doc_version` of the records about to be upserted.

        Returns:
            The count of stale records deleted; `0` if the feature is disabled by
            config, no prior version is stored, or the stored version already matches.
        """
        if not self._config.delete_before_upsert_on_version_change:
            return 0
        existing_version = self._adapter.get_doc_version(doc_id)
        if existing_version is not None and existing_version != new_version:
            return self._adapter.delete_by_doc_id(doc_id)
        return 0

    def _upsert_batch_with_retry(self, batch: list[IndexRecord]) -> int:
        """Upserts one batch; on partial failure, retries only the failed `chunk_id`s.

        Story: "Partial-batch retry ... retry only the failed chunk_ids, not the whole
        batch." Exactly one in-orchestrator retry pass; a `chunk_id` still failing
        after that raises `INDEXER_PARTIAL_BATCH_FAILURE` (TRANSIENT) for the caller's
        own queue-level redelivery to retry further (upsert idempotency makes this
        safe).

        Args:
            batch: The records to upsert, `len(batch) <= adapter.max_batch_size`.

        Returns:
            The count of records that ended up successfully upserted.

        Raises:
            IndexerError: `INDEXER_PARTIAL_BATCH_FAILURE` if any `chunk_id` still
                fails after the retry pass.
        """
        outcome = self._adapter.upsert(batch)
        if not outcome.failed:
            return len(outcome.succeeded)
        failed_ids = {chunk_id for chunk_id, _reason in outcome.failed}
        retry_batch = [record for record in batch if record.chunk_id in failed_ids]
        retry_outcome = self._adapter.upsert(retry_batch)
        if retry_outcome.failed:
            reasons = "; ".join(
                f"{chunk_id}: {reason}" for chunk_id, reason in retry_outcome.failed
            )
            raise IndexerError(
                "",
                f"partial batch failure persisted after retry: {reasons}",
                error_code=INDEXER_PARTIAL_BATCH_FAILURE,
            )
        return len(outcome.succeeded) + len(retry_outcome.succeeded)

    def _effective_batch_size(self) -> int:
        """The batch size to split `upsert` calls into (story: "Batching").

        Returns:
            `config.batch_size` capped at `adapter.max_batch_size` when configured;
            `adapter.max_batch_size` unmodified otherwise.
        """
        if self._config.batch_size is None:
            return self._adapter.max_batch_size
        return min(self._config.batch_size, self._adapter.max_batch_size)

    def _transition_to_indexed(self, doc_id: str, result: IndexResult) -> IndexResult:
        """The happy-path `indexing -> indexed` exit transition.

        Args:
            doc_id: The document's `doc_id` (ledger key).
            result: The `IndexResult` to return on success/benign redelivery.

        Returns:
            `result`, unchanged.

        Raises:
            LedgerTransitionInvalid: If the transition fails non-benignly.
        """
        try:
            self._ledger.transition(
                doc_id, to_status=Status.INDEXED, stage=Status.INDEXED
            )
        except LedgerTransitionInvalid as exc:
            if not is_benign_concurrent_loss(
                exc.observed_row, Status.INDEXED, Status.INDEXED
            ):
                raise
            # Benign: a concurrent duplicate worker already made this exact edge (or
            # this is the terminal state itself) happen — deterministic recomputation
            # (Contract #2) makes returning the freshly-computed result safe.
        return result


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
