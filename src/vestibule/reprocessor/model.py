"""Poison Queue Reprocessor — data model and error-code registration (REQ-012).

Defines the read models `Reprocessor.list_failed()`/`summarize()` return
(`FailedItem`, `FailureSummary`), the outcome models `requeue()`/`requeue_by_error_code()`
return (`RequeueResult`, `BatchRequeueResult`), and `ReprocessorError` — the single
exception type `vestibule.reprocessor.reprocessor` raises, carrying a caller-supplied
`error_code` (same shape as `vestibule.vertical.model.VerticalError`).

Failure taxonomy: this module registers two error codes at import time (Contract #4),
both PERMANENT — `REPROCESS_NOT_FAILED` (a `requeue()`/`archive()` call targeted a
`doc_id` whose current ledger row is not `failed`) and `REPROCESS_DOC_NOT_FOUND` (the
`doc_id` has no ledger row at all). These are deliberately two distinct codes, unlike
`vestibule.vertical.review_queue`'s single `REVIEW_ITEM_NOT_FOUND` for both cases — the
story calls for `requeue()` to raise a different, more specific code for "unknown
doc_id" than for "known doc_id, wrong status" (AC5 vs. the unknown-doc_id test case).

`FailedItem.stage` spec-gap resolution: the story asks for a `stage: str` field
recording "where it failed (analyzing, chunking, embedding, indexing)", but
`vestibule.ledger.store.LedgerRow` cannot carry this directly — Assumption A1 (`stage ==
status` on every write, enforced by `validate_transition`) means a row's `stage` field is
always literally `Status.FAILED` once it has failed, never the stage it failed *at*. This
module resolves the gap by inferring the originating stage from the row's
`last_error_code` prefix (`ANALYZER_`/`PARSER_`/`DOCINT_` -> analyzing,
`CHUNKER_`/`TOKENIZER_` -> chunking, `EMBEDDER_` -> embedding, `INDEXER_` -> indexing),
since every error code this codebase's pipeline stages register already follows exactly
this prefix convention (see `config/errors.yaml`). A code matching no known prefix (e.g.
`VERTICAL_REJECTED`, from a human rejecting a `needs_review`-parked document, which never
reached any of the four listed stages) maps to `_UNKNOWN_STAGE`. See
`vestibule.reprocessor.reprocessor._infer_stage` for the implementation.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from vestibule.errors.registry import RaggedError, Severity, register_error

REPROCESS_NOT_FAILED = "REPROCESS_NOT_FAILED"
REPROCESS_DOC_NOT_FOUND = "REPROCESS_DOC_NOT_FOUND"


class FailedItem(BaseModel):
    """One `failed`-status document, as surfaced to an operator. Frozen.

    Attributes:
        doc_id: The failed document's `doc_id`.
        source: The envelope's `source` at ingestion time, if still known via the
            `ReviewRegistry` pattern; `None` otherwise. See this module's own docstring
            and `Reprocessor`'s docstring for why this is `None` in practice today.
        blob_path: The envelope's `blob_path`, same `None`-when-unavailable fallback.
        stage: Where the document failed — `"analyzing"`, `"chunking"`, `"embedding"`,
            `"indexing"`, or `"unknown"` if `error_code` matches none of those stages'
            registered prefixes. Inferred from `error_code`; see this module's own
            docstring.
        error_code: The registered error code the document failed with.
        error_message: The human-readable error message recorded at failure time, if
            any.
        severity: `error_code`'s classification, looked up from the process-wide
            `ErrorRegistry` (Contract #4).
        attempts: Attempts made at the stage the document failed at, before failing.
        failed_at: When the `-> failed` transition landed (the ledger row's
            `updated_at`).
        first_ingested_at: When the document was first ingested (the ledger row's
            `ingested_at`) — preserved across the document's full failure history.
        assigned_scenario_id: The ledger row's `assigned_scenario_id`, if a human ever
            assigned one via `vestibule.vertical.review_queue.ReviewQueue.assign()`
            before the document ultimately failed.
    """

    model_config = ConfigDict(frozen=True)

    doc_id: str
    source: str | None
    blob_path: str | None
    stage: str
    error_code: str
    error_message: str | None
    severity: Severity
    attempts: int
    failed_at: datetime
    first_ingested_at: datetime
    assigned_scenario_id: str | None


class FailureSummary(BaseModel):
    """Aggregate counts over every currently-`failed` document. Frozen.

    Attributes:
        total: Total number of `failed` documents.
        by_error_code: `error_code -> count`, over the same document set as `total`.
        by_stage: Inferred `stage -> count` (see `FailedItem.stage`), over the same
            document set as `total`.
        oldest_failure_at: The earliest `failed_at` among all failed documents, or
            `None` if there are none.
        newest_failure_at: The latest `failed_at` among all failed documents, or `None`
            if there are none.
    """

    model_config = ConfigDict(frozen=True)

    total: int
    by_error_code: dict[str, int]
    by_stage: dict[str, int]
    oldest_failure_at: datetime | None
    newest_failure_at: datetime | None


class RequeueResult(BaseModel):
    """Outcome of one `Reprocessor.requeue()` call. Frozen.

    Attributes:
        doc_id: The document requeued.
        previous_error_code: The `error_code` the document had failed with immediately
            before this requeue — observability only; the ledger row's own
            `last_error_code` is cleared by the requeue itself.
        requeued_at: When the `failed -> pending` transition landed.
    """

    model_config = ConfigDict(frozen=True)

    doc_id: str
    previous_error_code: str
    requeued_at: datetime


class BatchRequeueResult(BaseModel):
    """Outcome of one `Reprocessor.requeue_by_error_code()` call. Frozen.

    Attributes:
        error_code: The `error_code` this batch call filtered on.
        requeued: `doc_id`s successfully requeued, in the order processed.
        skipped: `doc_id`s that matched at listing time but had already left `failed`
            (a concurrent operator action — requeued, archived, or otherwise) by the
            time this call attempted to requeue them. Not an error — see this
            component's docstring for why this is benign.
    """

    model_config = ConfigDict(frozen=True)

    error_code: str
    requeued: tuple[str, ...]
    skipped: tuple[str, ...]


class ReprocessorError(RaggedError):
    """Single exception type for every `Reprocessor`-raised failure.

    `error_code` varies per raise site — same shape as
    `vestibule.vertical.model.VerticalError`.

    Attributes:
        doc_id: The `doc_id` involved.
        reason: Human-readable failure reason.
    """

    def __init__(self, doc_id: str, reason: str, *, error_code: str) -> None:
        """Initializes the exception.

        Args:
            doc_id: The `doc_id` involved.
            reason: Human-readable failure reason.
            error_code: One of this module's two registered codes.
        """
        self.doc_id = doc_id
        self.reason = reason
        super().__init__(f"{doc_id}: {reason}", error_code=error_code)


register_error(
    REPROCESS_NOT_FAILED,
    Severity.PERMANENT,
    "Reprocessor.requeue()/archive() was called for a doc_id whose ledger row is not "
    "currently failed (REQ-012)",
)
register_error(
    REPROCESS_DOC_NOT_FOUND,
    Severity.PERMANENT,
    "Reprocessor.requeue()/archive() was called for a doc_id with no ledger row at all "
    "(REQ-012)",
)
