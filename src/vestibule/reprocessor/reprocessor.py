"""Reprocessor — poison-queue operator tooling over `failed` ledger rows (REQ-012).

`Reprocessor` exposes five operations: `list_failed`/`summarize` (what's failed, and
why), `requeue`/`requeue_by_error_code` (reset to `pending`, reprocess from the top —
Contract #2's deterministic IDs make this safe: re-done work overwrites cleanly rather
than duplicating), and `archive` (terminal — for documents that will never succeed).

**Human-triggered only.** No automatic requeuing exists or is added anywhere by this
module — automatic retry belongs to the TRANSIENT taxonomy and queue redelivery, which
already exist. This is the tool for failures those mechanisms correctly gave up on.

**The `failed -> pending`/`failed -> archived` boundary is operator-only by convention,
not by code.** `vestibule.ledger.store.validate_transition`/`LedgerStore.transition()`
have no notion of caller identity, so they cannot themselves enforce "only an operator
tool may call this edge" — the legal-transition table simply permits it. This module
(`requeue`/`archive` below) is the only place in this codebase permitted to call
`ledger.transition(..., to_status=Status.PENDING)`/`to_status=Status.ARCHIVED)` on a row
that is currently `failed`. See `vestibule.ledger.store`'s own module docstring ("Round
4") and `src/vestibule/ledger/test_store.py`'s dedicated regression guard for what can
and cannot be mechanically enforced about this.

Every ledger write below is wrapped in `is_benign_concurrent_loss` triage, matching every
other ledger-writing component in this codebase — a losing race against a concurrent
operator action (e.g. two operators requeuing the same document at once) is not treated
as a bug. `requeue_by_error_code` additionally has its own, distinct, higher-level
concurrency handling: a document that matched `list_failed()` at listing time but has
since left `failed` (someone else already requeued or archived it) is silently skipped,
not raised — see that method's own docstring. These are two different layers, not one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from vestibule.errors.registry import classify
from vestibule.ledger.store import (
    LedgerRow,
    LedgerStore,
    LedgerTransitionInvalid,
    Status,
    is_benign_concurrent_loss,
)
from vestibule.reprocessor.model import (
    REPROCESS_DOC_NOT_FOUND,
    REPROCESS_NOT_FAILED,
    BatchRequeueResult,
    FailedItem,
    FailureSummary,
    ReprocessorError,
    RequeueResult,
)

logger = logging.getLogger(__name__)

_DEFAULT_LIST_LIMIT = 100
_DEFAULT_MAX_BATCH_REQUEUE = 500
_UNKNOWN_ERROR_CODE = "UNKNOWN"
_UNKNOWN_STAGE = "unknown"

# Effectively-unbounded raw scan size for list_failed()/summarize()'s underlying
# LedgerStore.list_by_status() read — sufficient for the in-memory backend and current
# scale (Budget: "summarize streams counts", no unbounded accumulation beyond this one
# bounded read). A future backend/scale may want a dedicated paginated/streamed
# count-only query instead; out of scope here.
_SCAN_LIMIT = 10_000

_STAGE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("ANALYZER_", Status.ANALYZING.value),
    ("PARSER_", Status.ANALYZING.value),
    ("DOCINT_", Status.ANALYZING.value),
    ("CHUNKER_", Status.CHUNKING.value),
    ("TOKENIZER_", Status.CHUNKING.value),
    ("EMBEDDER_", Status.EMBEDDING.value),
    ("INDEXER_", Status.INDEXING.value),
)


def _infer_stage(error_code: str) -> str:
    """Infers which pipeline stage `error_code` originated from, by prefix.

    Args:
        error_code: The registered error code to classify.

    Returns:
        `"analyzing"`, `"chunking"`, `"embedding"`, or `"indexing"` for a recognized
        prefix; `"unknown"` otherwise (e.g. `VERTICAL_REJECTED`, which never reached
        any of the four pipeline stages). See this package's `model.py` module
        docstring for the full spec-gap rationale.
    """
    for prefix, stage in _STAGE_PREFIXES:
        if error_code.startswith(prefix):
            return stage
    return _UNKNOWN_STAGE


def _to_failed_item(row: LedgerRow) -> FailedItem:
    """Builds a `FailedItem` from a `failed`-status `LedgerRow`.

    Args:
        row: A ledger row whose `status` is `Status.FAILED`.

    Returns:
        The corresponding `FailedItem`. `source`/`blob_path` are always `None` — see
        `Reprocessor`'s own docstring for why.
    """
    error_code = row.last_error_code or _UNKNOWN_ERROR_CODE
    return FailedItem(
        doc_id=row.doc_id,
        source=None,
        blob_path=None,
        stage=_infer_stage(error_code),
        error_code=error_code,
        error_message=row.last_error_message,
        severity=classify(error_code),
        attempts=row.attempts,
        failed_at=row.updated_at,
        first_ingested_at=row.ingested_at,
        assigned_scenario_id=row.assigned_scenario_id,
    )


@dataclass(frozen=True)
class ReprocessorConfig:
    """`Reprocessor`'s tunables — mirrors `config/reprocessor.yaml`.

    No config-loading mechanism exists in this codebase yet (established precedent,
    e.g. `vestibule.vertical.loader.VerticalLoaderConfig`); this literal default is
    this config's always-effective source of truth for a direct caller, kept in sync
    with `config/reprocessor.yaml`'s own literal by a dedicated test.

    Attributes:
        max_batch_requeue: Hard cap on how many documents
            `Reprocessor.requeue_by_error_code()` processes in one call, regardless of
            the caller-supplied `limit`.
    """

    max_batch_requeue: int = _DEFAULT_MAX_BATCH_REQUEUE


class Reprocessor:
    """Poison-queue operator tooling over `failed` ledger rows.

    `source`/`blob_path` on every `FailedItem` this class returns are always `None`:
    the story's "from the ReviewRegistry pattern where available" fallback is
    deliberately not wired in. `vestibule.vertical.review_queue.ReviewRegistry` only
    ever holds metadata for documents currently parked `needs_review` —
    `ReviewQueue.assign()`/`reject()` both discard a document's registry entry the
    moment it leaves `needs_review` (including on `reject()`'s `needs_review -> failed`
    transition, the one path that could otherwise reach `failed` with a registry entry
    still present). By the time any document is visible to `Reprocessor`, its
    `ReviewRegistry` entry (if it ever had one) is already gone — wiring the registry
    in here would only add coupling for a lookup that always misses. `None` is the
    accepted fallback per the story's own text.
    """

    def __init__(
        self, ledger: LedgerStore, config: ReprocessorConfig | None = None
    ) -> None:
        """Initializes the reprocessor.

        Args:
            ledger: Where failed rows live and transitions are written.
            config: Tunables; defaults to `ReprocessorConfig()`.
        """
        self._ledger = ledger
        self._config = config if config is not None else ReprocessorConfig()

    def list_failed(
        self,
        limit: int = _DEFAULT_LIST_LIMIT,
        error_code: str | None = None,
        stage: str | None = None,
        since: datetime | None = None,
    ) -> list[FailedItem]:
        """Lists `failed` documents, newest-failed first.

        Args:
            limit: Maximum number of items to return, applied after filtering.
                `limit <= 0` returns `[]`.
            error_code: If given, only documents that failed with exactly this code.
            stage: If given, only documents inferred to have failed at this stage (see
                `FailedItem.stage`).
            since: If given, only documents that failed at or after this time.

        Returns:
            `FailedItem`s for every matching `failed` ledger row, most-recently-failed
            first, capped at `limit`.
        """
        if limit <= 0:
            return []
        rows = self._ledger.list_by_status(Status.FAILED, limit=_SCAN_LIMIT)
        items = [_to_failed_item(row) for row in rows]
        if error_code is not None:
            items = [item for item in items if item.error_code == error_code]
        if stage is not None:
            items = [item for item in items if item.stage == stage]
        if since is not None:
            items = [item for item in items if item.failed_at >= since]
        return items[:limit]

    def summarize(self) -> FailureSummary:
        """Aggregates counts over every currently-`failed` document.

        Returns:
            A `FailureSummary` grouping the current `failed` set by `error_code` and
            by inferred stage, with the overall oldest/newest failure timestamps.
        """
        rows = self._ledger.list_by_status(Status.FAILED, limit=_SCAN_LIMIT)
        by_error_code: dict[str, int] = {}
        by_stage: dict[str, int] = {}
        oldest: datetime | None = None
        newest: datetime | None = None
        for row in rows:
            error_code = row.last_error_code or _UNKNOWN_ERROR_CODE
            stage = _infer_stage(error_code)
            by_error_code[error_code] = by_error_code.get(error_code, 0) + 1
            by_stage[stage] = by_stage.get(stage, 0) + 1
            if oldest is None or row.updated_at < oldest:
                oldest = row.updated_at
            if newest is None or row.updated_at > newest:
                newest = row.updated_at
        return FailureSummary(
            total=len(rows),
            by_error_code=by_error_code,
            by_stage=by_stage,
            oldest_failure_at=oldest,
            newest_failure_at=newest,
        )

    def requeue(self, doc_id: str) -> RequeueResult:
        """Resets a failed document to `pending` so it re-enters the pipeline.

        Resets `attempts` to `0` and clears `last_error_code`/`last_error_message` —
        the document gets a fresh run; its history remains visible via
        `first_ingested_at` and prior log output.

        Args:
            doc_id: The failed document to requeue.

        Returns:
            The outcome of the requeue.

        Raises:
            ReprocessorError: `REPROCESS_DOC_NOT_FOUND` (PERMANENT) if `doc_id` has no
                ledger row; `REPROCESS_NOT_FAILED` (PERMANENT) if `doc_id`'s row is not
                currently `failed`.
            LedgerTransitionInvalid: If the transition fails non-benignly.
        """
        current = self._require_failed(doc_id, caller="requeue")
        previous_error_code = current.last_error_code or _UNKNOWN_ERROR_CODE
        try:
            row = self._ledger.transition(
                doc_id,
                to_status=Status.PENDING,
                stage=Status.PENDING,
                error_code=None,
                error_message=None,
            )
        except LedgerTransitionInvalid as exc:
            if not is_benign_concurrent_loss(
                exc.observed_row, Status.PENDING, Status.PENDING
            ):
                raise
            assert exc.observed_row is not None  # benign loss always observed a row
            row = exc.observed_row
        logger.info(
            "doc requeued",
            extra={"doc_id": doc_id, "previous_error_code": previous_error_code},
        )
        return RequeueResult(
            doc_id=doc_id,
            previous_error_code=previous_error_code,
            requeued_at=row.updated_at,
        )

    def requeue_by_error_code(
        self, error_code: str, limit: int = _DEFAULT_LIST_LIMIT
    ) -> BatchRequeueResult:
        """Requeues every matching `failed` document, up to a hard cap.

        Processes at most `min(limit, ReprocessorConfig.max_batch_requeue)` documents.
        A document that matched at listing time but has since left `failed` — a
        concurrent operator action, e.g. someone else already requeued or archived it
        — is skipped, not treated as an error: this is a second, higher-level layer of
        concurrency handling, distinct from (and in addition to) the
        `is_benign_concurrent_loss` triage `requeue()` itself already performs around
        its own ledger write.

        Args:
            error_code: Only documents that failed with exactly this code.
            limit: Maximum number of documents to consider.

        Returns:
            Per-document outcome: which `doc_id`s were requeued, and which were
            skipped as benign concurrent losses.
        """
        effective_limit = min(limit, self._config.max_batch_requeue)
        candidates = self.list_failed(limit=effective_limit, error_code=error_code)
        requeued: list[str] = []
        skipped: list[str] = []
        for item in candidates:
            try:
                self.requeue(item.doc_id)
            except ReprocessorError as exc:
                if exc.error_code != REPROCESS_NOT_FAILED:
                    raise
                skipped.append(item.doc_id)
                continue
            requeued.append(item.doc_id)
        return BatchRequeueResult(
            error_code=error_code,
            requeued=tuple(requeued),
            skipped=tuple(skipped),
        )

    def archive(self, doc_id: str, reason: str) -> None:
        """Permanently archives a failed document that will never succeed.

        Terminal: the document no longer appears in `list_failed()`, and no further
        transition out of `archived` is ever legal.

        Args:
            doc_id: The failed document to archive.
            reason: Human-readable reason, recorded as the row's `last_error_message`.

        Raises:
            ReprocessorError: `REPROCESS_DOC_NOT_FOUND` (PERMANENT) if `doc_id` has no
                ledger row; `REPROCESS_NOT_FAILED` (PERMANENT) if `doc_id`'s row is not
                currently `failed`.
            LedgerTransitionInvalid: If the transition fails non-benignly.
        """
        current = self._require_failed(doc_id, caller="archive")
        try:
            self._ledger.transition(
                doc_id,
                to_status=Status.ARCHIVED,
                stage=Status.ARCHIVED,
                error_code=current.last_error_code,
                error_message=reason,
            )
        except LedgerTransitionInvalid as exc:
            if not is_benign_concurrent_loss(
                exc.observed_row, Status.ARCHIVED, Status.ARCHIVED
            ):
                raise
        logger.info("doc archived", extra={"doc_id": doc_id, "reason": reason})

    def _require_failed(self, doc_id: str, *, caller: str) -> LedgerRow:
        """Guards `requeue`/`archive` against a `doc_id` not currently `failed`.

        Args:
            doc_id: The document to check.
            caller: `"requeue"` or `"archive"`, for the error message only.

        Returns:
            The current `LedgerRow`, guaranteed `status == Status.FAILED`.

        Raises:
            ReprocessorError: `REPROCESS_DOC_NOT_FOUND` (PERMANENT) if `doc_id` has no
                ledger row; `REPROCESS_NOT_FAILED` (PERMANENT) if `doc_id`'s row is not
                currently `failed`.
        """
        current = self._ledger.get(doc_id)
        if current is None:
            raise ReprocessorError(
                doc_id,
                f"{caller}() called for an unknown doc_id",
                error_code=REPROCESS_DOC_NOT_FOUND,
            )
        if current.status is not Status.FAILED:
            raise ReprocessorError(
                doc_id,
                f"{caller}() called for a doc_id not currently failed "
                f"(status={current.status.value})",
                error_code=REPROCESS_NOT_FAILED,
            )
        return current
