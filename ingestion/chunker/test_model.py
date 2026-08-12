"""Unit tests for the Chunker data model and error-code registration (REQ-006)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ingestion.analyzer.model import ElementType
from ingestion.chunker.model import (
    CHUNKER_EMPTY_ELEMENTS,
    CHUNKER_INTERNAL,
    OVERSIZED_TABLE_LOG_EVENT,
    TOKENIZER_LOAD_FAILED,
    Chunk,
    ChunkDraft,
    ChunkerError,
)
from ingestion.errors.registry import Severity, all_codes, classify


def _make_chunk(**overrides: object) -> Chunk:
    defaults: dict[str, object] = {
        "chunk_id": "a" * 64,
        "doc_id": "b" * 64,
        "position": 0,
        "element_types": [ElementType.PARAGRAPH],
        "text": "hello world",
        "token_count": 2,
        "metadata": {"strategy": "recursive", "section_path": None},
    }
    defaults.update(overrides)
    return Chunk(**defaults)  # type: ignore[arg-type]


# --- Chunk (frozen pydantic model) -------------------------------------------------------


def test_chunk_is_frozen() -> None:
    chunk = _make_chunk()
    with pytest.raises(ValidationError):
        chunk.text = "mutated"


def test_chunk_rejects_wrong_field_type() -> None:
    with pytest.raises(ValidationError):
        Chunk(
            chunk_id="a" * 64,
            doc_id="b" * 64,
            position="not-an-int",  # type: ignore[arg-type]
            element_types=[ElementType.PARAGRAPH],
            text="hi",
            token_count=1,
            metadata={},
        )


def test_chunk_holds_supplied_field_values() -> None:
    chunk = _make_chunk(position=3, text="some text", token_count=5)
    assert chunk.position == 3
    assert chunk.text == "some text"
    assert chunk.token_count == 5


# --- ChunkDraft (frozen dataclass, internal) ----------------------------------------------


def test_chunk_draft_is_frozen() -> None:
    draft = ChunkDraft(
        element_types=[ElementType.PARAGRAPH],
        text="hi",
        token_count=1,
        metadata={},
    )
    with pytest.raises(AttributeError):
        draft.text = "mutated"  # type: ignore[misc]


# --- ChunkerError --------------------------------------------------------------------------


def test_chunker_error_carries_doc_id_reason_and_error_code() -> None:
    exc = ChunkerError("doc-1", "boom", error_code=CHUNKER_INTERNAL)
    assert exc.doc_id == "doc-1"
    assert exc.reason == "boom"
    assert exc.error_code == CHUNKER_INTERNAL
    assert "doc-1" in str(exc)
    assert "boom" in str(exc)


# --- registration (AC6, design-review BLOCKER fix) -----------------------------------------


def test_all_three_chunker_codes_registered_after_import() -> None:
    registered = {entry.code: entry.severity for entry in all_codes()}
    assert registered[CHUNKER_EMPTY_ELEMENTS] == Severity.PERMANENT
    assert registered[TOKENIZER_LOAD_FAILED] == Severity.TRANSIENT
    assert registered[CHUNKER_INTERNAL] == Severity.TRANSIENT


def test_chunker_oversized_table_is_not_a_registered_error_code() -> None:
    """Regression test guarding the design-review BLOCKER fix: CHUNKER_OVERSIZED_TABLE
    is a plain log-event-name string only, never registered in ErrorRegistry."""
    registered_codes = {entry.code for entry in all_codes()}
    assert "CHUNKER_OVERSIZED_TABLE" not in registered_codes
    assert OVERSIZED_TABLE_LOG_EVENT == "oversized_table"


def test_chunker_empty_elements_classified_permanent() -> None:
    assert classify(CHUNKER_EMPTY_ELEMENTS) == Severity.PERMANENT


def test_tokenizer_load_failed_classified_transient() -> None:
    assert classify(TOKENIZER_LOAD_FAILED) == Severity.TRANSIENT


def test_chunker_internal_classified_transient() -> None:
    assert classify(CHUNKER_INTERNAL) == Severity.TRANSIENT
