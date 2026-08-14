"""Unit tests for the Indexer data model (REQ-008)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from vestibule.errors.registry import Severity, classify
from vestibule.indexer.model import (
    INDEXER_DEPENDENCY_MISSING,
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


def _record(**overrides: object) -> IndexRecord:
    defaults: dict[str, object] = {
        "chunk_id": "a" * 64,
        "doc_id": "b" * 64,
        "doc_version": "0" * 64,
        "vector": [1.0, 2.0],
        "text": "hello",
        "position": 0,
        "section_path": None,
        "element_types": ["paragraph"],
        "strategy": "recursive",
        "allowed_groups": ["group-a"],
        "trust_tier": "internal",
        "embedding_model": "fake-model",
        "embedding_dimensions": 2,
        "config_version": "v1",
        "indexed_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    return IndexRecord(**defaults)  # type: ignore[arg-type]


# --- IndexRecord -----------------------------------------------------------------------------


def test_index_record_is_frozen() -> None:
    record = _record()
    with pytest.raises(ValidationError):
        record.chunk_id = "c" * 64


def test_index_record_carries_allowed_groups_and_trust_tier() -> None:
    record = _record(allowed_groups=["group-a", "group-b"], trust_tier="confidential")
    assert record.allowed_groups == ["group-a", "group-b"]
    assert record.trust_tier == "confidential"


# --- IndexResult -----------------------------------------------------------------------------


def test_index_result_is_frozen() -> None:
    result = IndexResult(
        doc_id="a" * 64,
        chunks_upserted=1,
        chunks_deleted=0,
        index_name="idx",
        embedding_model="fake-model",
        embedding_dimensions=4,
    )
    with pytest.raises(ValidationError):
        result.chunks_upserted = 2


# --- IndexerError ----------------------------------------------------------------------------


def test_indexer_error_carries_doc_id_reason_and_error_code() -> None:
    exc = IndexerError("a" * 64, "boom", error_code=INDEXER_INTERNAL)
    assert exc.doc_id == "a" * 64
    assert exc.reason == "boom"
    assert exc.error_code == INDEXER_INTERNAL
    assert str(exc) == f"{'a' * 64}: boom"


# --- error registration (Contract #4) ---------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected_severity",
    [
        (INDEXER_EMPTY_RECORDS, Severity.PERMANENT),
        (INDEXER_DIMENSION_MISMATCH, Severity.PERMANENT),
        (INDEXER_MIXED_MODEL_BATCH, Severity.PERMANENT),
        (INDEXER_SCHEMA_CONFLICT, Severity.PERMANENT),
        (INDEXER_RECORD_TOO_LARGE, Severity.PERMANENT),
        (INDEXER_DEPENDENCY_MISSING, Severity.PERMANENT),
        (INDEXER_RATE_LIMITED, Severity.TRANSIENT),
        (INDEXER_UPSTREAM_ERROR, Severity.TRANSIENT),
        (INDEXER_TIMEOUT, Severity.TRANSIENT),
        (INDEXER_PARTIAL_BATCH_FAILURE, Severity.TRANSIENT),
        (INDEXER_INTERNAL, Severity.TRANSIENT),
    ],
)
def test_error_code_registered_with_expected_severity(
    code: str, expected_severity: Severity
) -> None:
    assert classify(code) == expected_severity
