"""Unit tests for the Embedder data model and error-code registration (REQ-007)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ingestion.embedder.conftest import chunk
from ingestion.embedder.model import (
    EMBEDDER_DEPENDENCY_MISSING,
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
from ingestion.errors.registry import Severity, all_codes, classify


def _make_embedded_chunk(**overrides: object) -> EmbeddedChunk:
    defaults: dict[str, object] = {
        "chunk": chunk(),
        "vector": [0.1, 0.2, 0.3],
        "embedding_model": "text-embedding-3-large",
        "embedding_dimensions": 3,
        "truncated_from": None,
    }
    defaults.update(overrides)
    return EmbeddedChunk(**defaults)  # type: ignore[arg-type]


# --- EmbeddedChunk (frozen pydantic model) -------------------------------------------------


def test_embedded_chunk_is_frozen() -> None:
    embedded = _make_embedded_chunk()
    with pytest.raises(ValidationError):
        embedded.vector = [9.9]


def test_embedded_chunk_rejects_wrong_field_type() -> None:
    with pytest.raises(ValidationError):
        EmbeddedChunk(
            chunk=chunk(),
            vector="not-a-list",  # type: ignore[arg-type]
            embedding_model="m",
            embedding_dimensions=3,
            truncated_from=None,
        )


def test_embedded_chunk_holds_supplied_field_values() -> None:
    embedded = _make_embedded_chunk(
        vector=[1.0, 2.0], embedding_dimensions=2, truncated_from=4
    )
    assert embedded.vector == [1.0, 2.0]
    assert embedded.embedding_dimensions == 2
    assert embedded.truncated_from == 4


def test_embedded_chunk_wraps_the_source_chunk() -> None:
    source = chunk(text="wrapped chunk text")
    embedded = _make_embedded_chunk(chunk=source)
    assert embedded.chunk == source
    assert embedded.chunk.text == "wrapped chunk text"


def test_embedded_chunk_truncated_from_defaults_to_none() -> None:
    embedded = EmbeddedChunk(
        chunk=chunk(),
        vector=[0.1, 0.2],
        embedding_model="m",
        embedding_dimensions=2,
    )
    assert embedded.truncated_from is None


# --- EmbedderError -------------------------------------------------------------------------


def test_embedder_error_carries_doc_id_reason_and_error_code() -> None:
    exc = EmbedderError("doc-1", "boom", error_code=EMBEDDER_INTERNAL)
    assert exc.doc_id == "doc-1"
    assert exc.reason == "boom"
    assert exc.error_code == EMBEDDER_INTERNAL
    assert "doc-1" in str(exc)
    assert "boom" in str(exc)


# --- registration (Contract #4) -------------------------------------------------------------


def test_all_eight_embedder_codes_registered_after_import() -> None:
    registered = {entry.code: entry.severity for entry in all_codes()}
    assert registered[EMBEDDER_EMPTY_CHUNKS] == Severity.PERMANENT
    assert registered[EMBEDDER_MRL_UNSUPPORTED] == Severity.PERMANENT
    assert registered[EMBEDDER_INPUT_TOO_LONG] == Severity.PERMANENT
    assert registered[EMBEDDER_RATE_LIMITED] == Severity.TRANSIENT
    assert registered[EMBEDDER_UPSTREAM_ERROR] == Severity.TRANSIENT
    assert registered[EMBEDDER_TIMEOUT] == Severity.TRANSIENT
    assert registered[EMBEDDER_INTERNAL] == Severity.TRANSIENT
    assert registered[EMBEDDER_DEPENDENCY_MISSING] == Severity.PERMANENT


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (EMBEDDER_EMPTY_CHUNKS, Severity.PERMANENT),
        (EMBEDDER_MRL_UNSUPPORTED, Severity.PERMANENT),
        (EMBEDDER_INPUT_TOO_LONG, Severity.PERMANENT),
        (EMBEDDER_RATE_LIMITED, Severity.TRANSIENT),
        (EMBEDDER_UPSTREAM_ERROR, Severity.TRANSIENT),
        (EMBEDDER_TIMEOUT, Severity.TRANSIENT),
        (EMBEDDER_INTERNAL, Severity.TRANSIENT),
        (EMBEDDER_DEPENDENCY_MISSING, Severity.PERMANENT),
    ],
)
def test_classify_matches_declared_severity(code: str, expected: Severity) -> None:
    assert classify(code) == expected
