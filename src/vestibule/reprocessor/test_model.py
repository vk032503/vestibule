"""Unit tests for `vestibule.reprocessor.model` (REQ-012)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from vestibule.errors.registry import Severity, classify
from vestibule.reprocessor.model import (
    REPROCESS_DOC_NOT_FOUND,
    REPROCESS_NOT_FAILED,
    BatchRequeueResult,
    FailedItem,
    FailureSummary,
    ReprocessorError,
    RequeueResult,
)

_NOW = datetime.now(timezone.utc)


def _failed_item(**overrides: object) -> FailedItem:
    defaults: dict[str, object] = {
        "doc_id": "a" * 64,
        "source": None,
        "blob_path": None,
        "stage": "embedding",
        "error_code": "EMBEDDER_UPSTREAM_ERROR",
        "error_message": "upstream outage",
        "severity": Severity.TRANSIENT,
        "attempts": 3,
        "failed_at": _NOW,
        "first_ingested_at": _NOW,
        "assigned_scenario_id": None,
    }
    defaults.update(overrides)
    return FailedItem(**defaults)  # type: ignore[arg-type]


# --- FailedItem --------------------------------------------------------------------------


def test_failed_item_is_frozen() -> None:
    item = _failed_item()
    with pytest.raises(ValidationError):
        item.error_code = "OTHER"


def test_failed_item_allows_none_source_and_blob_path() -> None:
    item = _failed_item(source=None, blob_path=None)
    assert item.source is None
    assert item.blob_path is None


# --- FailureSummary ------------------------------------------------------------------------


def test_failure_summary_is_frozen() -> None:
    summary = FailureSummary(
        total=0,
        by_error_code={},
        by_stage={},
        oldest_failure_at=None,
        newest_failure_at=None,
    )
    with pytest.raises(ValidationError):
        summary.total = 5


def test_failure_summary_allows_none_timestamps_when_empty() -> None:
    summary = FailureSummary(
        total=0,
        by_error_code={},
        by_stage={},
        oldest_failure_at=None,
        newest_failure_at=None,
    )
    assert summary.oldest_failure_at is None
    assert summary.newest_failure_at is None


# --- RequeueResult / BatchRequeueResult -----------------------------------------------------


def test_requeue_result_is_frozen() -> None:
    result = RequeueResult(
        doc_id="a" * 64, previous_error_code="EMBEDDER_UPSTREAM_ERROR", requeued_at=_NOW
    )
    with pytest.raises(ValidationError):
        result.doc_id = "b" * 64


def test_batch_requeue_result_is_frozen() -> None:
    result = BatchRequeueResult(
        error_code="EMBEDDER_UPSTREAM_ERROR",
        requeued=("a" * 64,),
        skipped=(),
    )
    with pytest.raises(ValidationError):
        result.requeued = ()


# --- ReprocessorError ------------------------------------------------------------------------


def test_reprocessor_error_carries_error_code_and_doc_id() -> None:
    exc = ReprocessorError("doc-1", "not failed", error_code=REPROCESS_NOT_FAILED)
    assert exc.error_code == REPROCESS_NOT_FAILED
    assert exc.doc_id == "doc-1"
    assert exc.reason == "not failed"


# --- error registration (Contract #4) -------------------------------------------------------


@pytest.mark.parametrize("code", [REPROCESS_NOT_FAILED, REPROCESS_DOC_NOT_FOUND])
def test_both_codes_registered_permanent(code: str) -> None:
    assert classify(code) == Severity.PERMANENT
