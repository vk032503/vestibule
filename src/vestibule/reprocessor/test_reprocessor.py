"""Unit tests for `vestibule.reprocessor.reprocessor.Reprocessor` (REQ-012)."""

from __future__ import annotations

import itertools
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vestibule.errors.registry import Severity
from vestibule.identity.derive import derive_doc_id
from vestibule.ledger.store import (
    EnvelopeSummary,
    InMemoryLedgerStore,
    LedgerRow,
    LedgerStore,
    LedgerTransitionInvalid,
    Status,
)
from vestibule.reprocessor.model import (
    REPROCESS_DOC_NOT_FOUND,
    REPROCESS_NOT_FAILED,
    ReprocessorError,
)
from vestibule.reprocessor.reprocessor import Reprocessor, ReprocessorConfig

_counter = itertools.count()


def _new_doc_id(label: str) -> str:
    return derive_doc_id("test-source", f"{label}-{next(_counter)}")


def _seed_failed(
    ledger: InMemoryLedgerStore,
    *,
    label: str,
    error_code: str = "EMBEDDER_UPSTREAM_ERROR",
    error_message: str | None = "boom",
) -> str:
    """Creates a fresh ledger row and drives it straight to `failed`."""
    doc_id = _new_doc_id(label)
    ledger.create(doc_id, EnvelopeSummary(config_version="v1"))
    ledger.transition(
        doc_id,
        to_status=Status.FAILED,
        stage=Status.FAILED,
        error_code=error_code,
        error_message=error_message,
    )
    return doc_id


def _seed_status(ledger: InMemoryLedgerStore, *, label: str, target: Status) -> str:
    """Creates a fresh ledger row and drives it to `target` (non-`FAILED`)."""
    doc_id = _new_doc_id(label)
    ledger.create(doc_id, EnvelopeSummary(config_version="v1"))
    if target is Status.PENDING:
        return doc_id
    if target is Status.NEEDS_REVIEW:
        ledger.transition(
            doc_id, to_status=Status.NEEDS_REVIEW, stage=Status.NEEDS_REVIEW
        )
        return doc_id
    for status in (
        Status.ANALYZING,
        Status.CHUNKING,
        Status.EMBEDDING,
        Status.INDEXING,
        Status.INDEXED,
    ):
        ledger.transition(doc_id, to_status=status, stage=status)
        if status is target:
            return doc_id
    return doc_id


def _row(status: Status, *, doc_id: str = "a" * 64) -> LedgerRow:
    now = datetime.now(timezone.utc)
    return LedgerRow(
        doc_id=doc_id,
        status=status,
        stage=status,
        attempts=1,
        last_error_code="EMBEDDER_UPSTREAM_ERROR" if status == Status.FAILED else None,
        config_version="v1",
        ingested_at=now,
        updated_at=now,
    )


class _RacedLedgerStore(LedgerStore):
    """`get()` always returns `precheck_row`; `transition()` always raises
    `LedgerTransitionInvalid` carrying `observed_row` — simulates a race won by a
    concurrent caller strictly between `Reprocessor`'s own precondition check and its
    `transition()` call. Mirrors `vestibule.vertical.test_review_queue`'s own
    `_RacedLedgerStore`."""

    def __init__(self, precheck_row: LedgerRow, observed_row: LedgerRow) -> None:
        self._precheck_row = precheck_row
        self._observed_row = observed_row

    def create(self, doc_id: str, envelope_summary: object) -> LedgerRow:
        raise NotImplementedError

    def transition(
        self,
        doc_id: str,
        to_status: Status,
        stage: Status,
        error_code: str | None = None,
        error_message: str | None = None,
        assigned_scenario_id: str | None = None,
    ) -> LedgerRow:
        raise LedgerTransitionInvalid(
            doc_id,
            "raced",
            observed_row=self._observed_row,
            attempted_to_status=to_status,
            attempted_stage=stage,
        )

    def get(self, doc_id: str) -> LedgerRow | None:
        return self._precheck_row

    def list_by_status(self, status: Status, limit: int = 100) -> list[LedgerRow]:
        raise NotImplementedError


class _ConcurrentDepartureLedgerStore(LedgerStore):
    """Wraps a real `InMemoryLedgerStore`; the first `get()` call for
    `departing_doc_id` causes that document to concurrently leave `failed`
    (transitioned to `archived` on the real, underlying store) before returning —
    simulating a race between `requeue_by_error_code`'s listing snapshot and its
    later, per-doc `requeue()` call for that same document."""

    def __init__(self, real: InMemoryLedgerStore, departing_doc_id: str) -> None:
        self._real = real
        self._departing_doc_id = departing_doc_id
        self._departed = False

    def create(self, doc_id: str, envelope_summary: EnvelopeSummary) -> LedgerRow:
        return self._real.create(doc_id, envelope_summary)

    def transition(
        self,
        doc_id: str,
        to_status: Status,
        stage: Status,
        error_code: str | None = None,
        error_message: str | None = None,
        assigned_scenario_id: str | None = None,
    ) -> LedgerRow:
        return self._real.transition(
            doc_id,
            to_status,
            stage,
            error_code=error_code,
            error_message=error_message,
            assigned_scenario_id=assigned_scenario_id,
        )

    def get(self, doc_id: str) -> LedgerRow | None:
        if doc_id == self._departing_doc_id and not self._departed:
            self._departed = True
            self._real.transition(
                doc_id,
                to_status=Status.ARCHIVED,
                stage=Status.ARCHIVED,
                error_message="concurrently archived by another operator",
            )
        return self._real.get(doc_id)

    def list_by_status(self, status: Status, limit: int = 100) -> list[LedgerRow]:
        return self._real.list_by_status(status, limit=limit)


class _VanishingLedgerStore(LedgerStore):
    """Wraps a real `InMemoryLedgerStore`; the first `get()` call for
    `vanishing_doc_id` returns `None` (as if the row were deleted entirely) instead of
    delegating — an implausible-in-practice but still-handled input, used to exercise
    `requeue_by_error_code`'s non-`REPROCESS_NOT_FAILED` re-raise path."""

    def __init__(self, real: InMemoryLedgerStore, vanishing_doc_id: str) -> None:
        self._real = real
        self._vanishing_doc_id = vanishing_doc_id
        self._vanished = False

    def create(self, doc_id: str, envelope_summary: EnvelopeSummary) -> LedgerRow:
        return self._real.create(doc_id, envelope_summary)

    def transition(
        self,
        doc_id: str,
        to_status: Status,
        stage: Status,
        error_code: str | None = None,
        error_message: str | None = None,
        assigned_scenario_id: str | None = None,
    ) -> LedgerRow:
        return self._real.transition(
            doc_id,
            to_status,
            stage,
            error_code=error_code,
            error_message=error_message,
            assigned_scenario_id=assigned_scenario_id,
        )

    def get(self, doc_id: str) -> LedgerRow | None:
        if doc_id == self._vanishing_doc_id and not self._vanished:
            self._vanished = True
            return None
        return self._real.get(doc_id)

    def list_by_status(self, status: Status, limit: int = 100) -> list[LedgerRow]:
        return self._real.list_by_status(status, limit=limit)


# --- list_failed (AC1, AC2) --------------------------------------------------------------


def test_list_failed_returns_only_failed_rows_newest_first() -> None:
    ledger = InMemoryLedgerStore()
    reprocessor = Reprocessor(ledger)

    first = _seed_failed(ledger, label="first")
    second = _seed_failed(ledger, label="second")
    _seed_status(ledger, label="indexed", target=Status.INDEXED)
    _seed_status(ledger, label="needs-review", target=Status.NEEDS_REVIEW)

    items = reprocessor.list_failed()
    assert [item.doc_id for item in items] == [second, first]


def test_list_failed_filters_by_error_code() -> None:
    ledger = InMemoryLedgerStore()
    reprocessor = Reprocessor(ledger)
    matching = _seed_failed(
        ledger, label="matching", error_code="DOCINT_UPSTREAM_ERROR"
    )
    _seed_failed(ledger, label="other", error_code="EMBEDDER_UPSTREAM_ERROR")

    items = reprocessor.list_failed(error_code="DOCINT_UPSTREAM_ERROR")
    assert [item.doc_id for item in items] == [matching]


def test_list_failed_filters_by_stage() -> None:
    ledger = InMemoryLedgerStore()
    reprocessor = Reprocessor(ledger)
    embedding_doc = _seed_failed(
        ledger, label="embedding", error_code="EMBEDDER_UPSTREAM_ERROR"
    )
    _seed_failed(ledger, label="indexing", error_code="INDEXER_UPSTREAM_ERROR")

    items = reprocessor.list_failed(stage="embedding")
    assert [item.doc_id for item in items] == [embedding_doc]


def test_list_failed_filters_by_since() -> None:
    ledger = InMemoryLedgerStore()
    reprocessor = Reprocessor(ledger)
    _seed_failed(ledger, label="old")
    recent = _seed_failed(ledger, label="recent")

    cutoff = ledger.get(recent).updated_at  # type: ignore[union-attr]
    items = reprocessor.list_failed(since=cutoff)
    assert [item.doc_id for item in items] == [recent]


def test_list_failed_respects_limit() -> None:
    ledger = InMemoryLedgerStore()
    reprocessor = Reprocessor(ledger)
    for i in range(5):
        _seed_failed(ledger, label=f"doc-{i}")

    assert len(reprocessor.list_failed(limit=2)) == 2


def test_list_failed_limit_non_positive_returns_empty() -> None:
    ledger = InMemoryLedgerStore()
    reprocessor = Reprocessor(ledger)
    _seed_failed(ledger, label="doc")

    assert reprocessor.list_failed(limit=0) == []
    assert reprocessor.list_failed(limit=-1) == []


def test_list_failed_item_severity_looked_up_from_registry() -> None:
    ledger = InMemoryLedgerStore()
    reprocessor = Reprocessor(ledger)
    _seed_failed(ledger, label="doc", error_code="EMBEDDER_UPSTREAM_ERROR")

    item = reprocessor.list_failed()[0]
    assert item.severity == Severity.TRANSIENT
    assert item.stage == "embedding"


def test_list_failed_item_stage_unknown_for_unrecognized_error_code_prefix() -> None:
    ledger = InMemoryLedgerStore()
    reprocessor = Reprocessor(ledger)
    _seed_failed(ledger, label="doc", error_code="VERTICAL_REJECTED")

    item = reprocessor.list_failed()[0]
    assert item.stage == "unknown"


# --- summarize (AC3) ----------------------------------------------------------------------


def test_summarize_groups_by_error_code_and_stage() -> None:
    ledger = InMemoryLedgerStore()
    reprocessor = Reprocessor(ledger)

    # 10 failures across 3 error codes and 2 stages.
    for i in range(5):
        _seed_failed(
            ledger, label=f"analyzer-{i}", error_code="ANALYZER_UNSUPPORTED_TYPE"
        )
    for i in range(3):
        _seed_failed(ledger, label=f"docint-{i}", error_code="DOCINT_UPSTREAM_ERROR")
    for i in range(2):
        _seed_failed(
            ledger, label=f"embedder-{i}", error_code="EMBEDDER_UPSTREAM_ERROR"
        )

    summary = reprocessor.summarize()
    assert summary.total == 10
    assert summary.by_error_code == {
        "ANALYZER_UNSUPPORTED_TYPE": 5,
        "DOCINT_UPSTREAM_ERROR": 3,
        "EMBEDDER_UPSTREAM_ERROR": 2,
    }
    assert summary.by_stage == {"analyzing": 8, "embedding": 2}
    assert summary.oldest_failure_at is not None
    assert summary.newest_failure_at is not None
    assert summary.oldest_failure_at <= summary.newest_failure_at


def test_summarize_empty_ledger_returns_zero_total_and_none_timestamps() -> None:
    reprocessor = Reprocessor(InMemoryLedgerStore())
    summary = reprocessor.summarize()
    assert summary.total == 0
    assert summary.by_error_code == {}
    assert summary.by_stage == {}
    assert summary.oldest_failure_at is None
    assert summary.newest_failure_at is None


# --- requeue (AC4, AC5) ---------------------------------------------------------------------


def test_requeue_transitions_failed_to_pending_resets_attempts_and_clears_errors() -> (
    None
):
    ledger = InMemoryLedgerStore()
    reprocessor = Reprocessor(ledger)
    doc_id = _seed_failed(ledger, label="doc")

    result = reprocessor.requeue(doc_id)

    row = ledger.get(doc_id)
    assert row is not None
    assert row.status == Status.PENDING
    assert row.attempts == 0
    assert row.last_error_code is None
    assert row.last_error_message is None
    assert result.doc_id == doc_id
    assert result.previous_error_code == "EMBEDDER_UPSTREAM_ERROR"
    assert result.requeued_at == row.updated_at


@pytest.mark.parametrize(
    "target", [Status.PENDING, Status.INDEXED, Status.NEEDS_REVIEW]
)
def test_requeue_on_non_failed_status_raises_not_failed(target: Status) -> None:
    ledger = InMemoryLedgerStore()
    reprocessor = Reprocessor(ledger)
    doc_id = _seed_status(ledger, label=f"not-failed-{target.value}", target=target)

    with pytest.raises(ReprocessorError) as exc_info:
        reprocessor.requeue(doc_id)
    assert exc_info.value.error_code == REPROCESS_NOT_FAILED


def test_requeue_unknown_doc_id_raises_doc_not_found() -> None:
    reprocessor = Reprocessor(InMemoryLedgerStore())
    with pytest.raises(ReprocessorError) as exc_info:
        reprocessor.requeue("a" * 64)
    assert exc_info.value.error_code == REPROCESS_DOC_NOT_FOUND


def test_requeue_benign_concurrent_loss_returns_without_raising() -> None:
    doc_id = "b" * 64
    ledger = _RacedLedgerStore(
        _row(Status.FAILED, doc_id=doc_id), _row(Status.ANALYZING, doc_id=doc_id)
    )
    reprocessor = Reprocessor(ledger)

    result = reprocessor.requeue(doc_id)  # must not raise
    assert result.doc_id == doc_id


def test_requeue_non_benign_reraises_ledger_transition_invalid() -> None:
    doc_id = "c" * 64
    ledger = _RacedLedgerStore(
        _row(Status.FAILED, doc_id=doc_id), _row(Status.ARCHIVED, doc_id=doc_id)
    )
    reprocessor = Reprocessor(ledger)

    with pytest.raises(LedgerTransitionInvalid):
        reprocessor.requeue(doc_id)


# --- archive (AC6) ------------------------------------------------------------------------


def test_archive_transitions_failed_to_archived_and_removed_from_list_failed() -> None:
    ledger = InMemoryLedgerStore()
    reprocessor = Reprocessor(ledger)
    doc_id = _seed_failed(ledger, label="doc")

    reprocessor.archive(doc_id, "will never succeed")

    row = ledger.get(doc_id)
    assert row is not None
    assert row.status == Status.ARCHIVED
    assert [item.doc_id for item in reprocessor.list_failed()] == []


def test_archive_no_transition_out_of_archived_is_legal() -> None:
    ledger = InMemoryLedgerStore()
    reprocessor = Reprocessor(ledger)
    doc_id = _seed_failed(ledger, label="doc")
    reprocessor.archive(doc_id, "dead")

    with pytest.raises(LedgerTransitionInvalid):
        ledger.transition(doc_id, to_status=Status.PENDING, stage=Status.PENDING)


@pytest.mark.parametrize(
    "target", [Status.PENDING, Status.INDEXED, Status.NEEDS_REVIEW]
)
def test_archive_on_non_failed_status_raises_not_failed(target: Status) -> None:
    ledger = InMemoryLedgerStore()
    reprocessor = Reprocessor(ledger)
    doc_id = _seed_status(ledger, label=f"not-failed-{target.value}", target=target)

    with pytest.raises(ReprocessorError) as exc_info:
        reprocessor.archive(doc_id, "reason")
    assert exc_info.value.error_code == REPROCESS_NOT_FAILED


def test_archive_unknown_doc_id_raises_doc_not_found() -> None:
    reprocessor = Reprocessor(InMemoryLedgerStore())
    with pytest.raises(ReprocessorError) as exc_info:
        reprocessor.archive("a" * 64, "reason")
    assert exc_info.value.error_code == REPROCESS_DOC_NOT_FOUND


def test_archive_benign_concurrent_loss_is_a_no_op() -> None:
    doc_id = "d" * 64
    ledger = _RacedLedgerStore(
        _row(Status.FAILED, doc_id=doc_id), _row(Status.ARCHIVED, doc_id=doc_id)
    )
    reprocessor = Reprocessor(ledger)

    reprocessor.archive(doc_id, "reason")  # must not raise


def test_archive_non_benign_reraises_ledger_transition_invalid() -> None:
    doc_id = "e" * 64
    ledger = _RacedLedgerStore(
        _row(Status.FAILED, doc_id=doc_id), _row(Status.PENDING, doc_id=doc_id)
    )
    reprocessor = Reprocessor(ledger)

    with pytest.raises(LedgerTransitionInvalid):
        reprocessor.archive(doc_id, "reason")


# --- requeue_by_error_code (AC7) -------------------------------------------------------------


def test_requeue_by_error_code_requeues_only_matching_docs() -> None:
    ledger = InMemoryLedgerStore()
    reprocessor = Reprocessor(ledger)
    matching_1 = _seed_failed(ledger, label="m1", error_code="DOCINT_UPSTREAM_ERROR")
    matching_2 = _seed_failed(ledger, label="m2", error_code="DOCINT_UPSTREAM_ERROR")
    other = _seed_failed(ledger, label="other", error_code="EMBEDDER_UPSTREAM_ERROR")

    result = reprocessor.requeue_by_error_code("DOCINT_UPSTREAM_ERROR")

    assert set(result.requeued) == {matching_1, matching_2}
    assert result.skipped == ()
    assert ledger.get(matching_1).status == Status.PENDING  # type: ignore[union-attr]
    assert ledger.get(matching_2).status == Status.PENDING  # type: ignore[union-attr]
    assert ledger.get(other).status == Status.FAILED  # type: ignore[union-attr]


def test_requeue_by_error_code_respects_max_batch_requeue() -> None:
    ledger = InMemoryLedgerStore()
    reprocessor = Reprocessor(ledger, ReprocessorConfig(max_batch_requeue=2))
    for i in range(5):
        _seed_failed(ledger, label=f"doc-{i}", error_code="DOCINT_UPSTREAM_ERROR")

    result = reprocessor.requeue_by_error_code("DOCINT_UPSTREAM_ERROR", limit=100)
    assert len(result.requeued) == 2


def test_requeue_by_error_code_skips_docs_that_concurrently_left_failed() -> None:
    real_ledger = InMemoryLedgerStore()
    reprocessor_seed = Reprocessor(real_ledger)  # only used to seed via helper below
    del reprocessor_seed
    departing = _seed_failed(
        real_ledger, label="departing", error_code="DOCINT_UPSTREAM_ERROR"
    )
    staying = _seed_failed(
        real_ledger, label="staying", error_code="DOCINT_UPSTREAM_ERROR"
    )

    wrapped_ledger = _ConcurrentDepartureLedgerStore(real_ledger, departing)
    reprocessor = Reprocessor(wrapped_ledger)

    result = reprocessor.requeue_by_error_code("DOCINT_UPSTREAM_ERROR")

    assert result.requeued == (staying,)
    assert result.skipped == (departing,)
    assert real_ledger.get(departing).status == Status.ARCHIVED  # type: ignore[union-attr]
    assert real_ledger.get(staying).status == Status.PENDING  # type: ignore[union-attr]


def test_requeue_by_error_code_reraises_non_not_failed_reprocessor_errors() -> None:
    """A per-doc `requeue()` failure other than `REPROCESS_NOT_FAILED` is a genuine
    bug/anomaly, not a benign concurrent departure — it must propagate, not be silently
    folded into `skipped`."""
    real_ledger = InMemoryLedgerStore()
    vanishing = _seed_failed(
        real_ledger, label="vanishing", error_code="DOCINT_UPSTREAM_ERROR"
    )
    wrapped_ledger = _VanishingLedgerStore(real_ledger, vanishing)
    reprocessor = Reprocessor(wrapped_ledger)

    with pytest.raises(ReprocessorError) as exc_info:
        reprocessor.requeue_by_error_code("DOCINT_UPSTREAM_ERROR")
    assert exc_info.value.error_code == REPROCESS_DOC_NOT_FOUND


# --- config/code sync ------------------------------------------------------------------


def test_config_reprocessor_yaml_matches_code_defaults() -> None:
    config_path = Path(__file__).resolve().parents[3] / "config" / "reprocessor.yaml"
    text = config_path.read_text(encoding="utf-8")

    list_limit_match = re.search(r"default_list_limit:\s*(\d+)", text)
    batch_match = re.search(r"max_batch_requeue:\s*(\d+)", text)
    assert list_limit_match is not None
    assert batch_match is not None

    from vestibule.reprocessor.reprocessor import _DEFAULT_LIST_LIMIT

    assert int(list_limit_match.group(1)) == _DEFAULT_LIST_LIMIT
    assert int(batch_match.group(1)) == ReprocessorConfig().max_batch_requeue


# --- property-based: consistent ledger state under any requeue/archive sequence -----------


@given(
    ops=st.lists(
        st.tuples(st.integers(min_value=0, max_value=3), st.booleans()),
        min_size=0,
        max_size=20,
    )
)
@settings(max_examples=100)
def test_property_requeue_archive_sequence_leaves_ledger_consistent(
    ops: list[tuple[int, bool]],
) -> None:
    """Any sequence of `requeue`/`archive` calls over a fixed set of initially-`failed`
    documents leaves every row in a legal status: `failed` (untouched, or already
    non-failed when the op was attempted and thus rejected), `pending` (requeued), or
    `archived` (archived) — never anything else, and never silently corrupted."""
    ledger = InMemoryLedgerStore()
    reprocessor = Reprocessor(ledger)
    doc_ids = [_seed_failed(ledger, label=f"prop-{i}") for i in range(4)]
    expected: dict[str, Status] = {doc_id: Status.FAILED for doc_id in doc_ids}

    for index, use_requeue in ops:
        doc_id = doc_ids[index]
        if expected[doc_id] is not Status.FAILED:
            with pytest.raises(ReprocessorError) as exc_info:
                if use_requeue:
                    reprocessor.requeue(doc_id)
                else:
                    reprocessor.archive(doc_id, "reason")
            assert exc_info.value.error_code == REPROCESS_NOT_FAILED
            continue
        if use_requeue:
            reprocessor.requeue(doc_id)
            expected[doc_id] = Status.PENDING
        else:
            reprocessor.archive(doc_id, "reason")
            expected[doc_id] = Status.ARCHIVED

    for doc_id in doc_ids:
        row = ledger.get(doc_id)
        assert row is not None
        assert row.status == expected[doc_id]
        assert row.status in {Status.FAILED, Status.PENDING, Status.ARCHIVED}
