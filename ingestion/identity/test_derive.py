"""Unit tests for Identity & Idempotency (Contract #2) — REQ-002."""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ingestion.identity.derive import (
    IdentityInvalid,
    _HEX_RE,
    derive_chunk_id,
    derive_chunk_ids_batch,
    derive_doc_id,
    validate_id_format,
)

VALID_DOC_ID = "a" * 64


# --- derive_doc_id happy paths -------------------------------------------------------


def test_derive_doc_id_deterministic() -> None:
    a = derive_doc_id("confluence", "spaces/eng/page-42")
    b = derive_doc_id("confluence", "spaces/eng/page-42")
    assert a == b


def test_derive_doc_id_differs_by_source_or_path() -> None:
    base = derive_doc_id("confluence", "spaces/eng/page-42")
    assert derive_doc_id("s3", "spaces/eng/page-42") != base
    assert derive_doc_id("confluence", "spaces/eng/page-99") != base


def test_derive_doc_id_format() -> None:
    doc_id = derive_doc_id("confluence", "spaces/eng/page-42")
    assert isinstance(doc_id, str)
    assert len(doc_id) == 64
    assert _HEX_RE.match(doc_id)
    assert doc_id == doc_id.lower()


# --- derive_chunk_id happy paths -----------------------------------------------------


def test_derive_chunk_id_deterministic() -> None:
    a = derive_chunk_id(VALID_DOC_ID, 3)
    b = derive_chunk_id(VALID_DOC_ID, 3)
    assert a == b


def test_derive_chunk_id_differs_by_index() -> None:
    base = derive_chunk_id(VALID_DOC_ID, 0)
    assert derive_chunk_id(VALID_DOC_ID, 1) != base


def test_derive_chunk_id_format() -> None:
    chunk_id = derive_chunk_id(VALID_DOC_ID, 0)
    assert isinstance(chunk_id, str)
    assert len(chunk_id) == 64
    assert _HEX_RE.match(chunk_id)


# --- derive_chunk_ids_batch happy paths ----------------------------------------------


@pytest.mark.parametrize("n", [0, 1, 50])
def test_derive_chunk_ids_batch_matches_individual(n: int) -> None:
    chunk_indices = list(range(n))
    batch = derive_chunk_ids_batch(VALID_DOC_ID, chunk_indices)
    assert batch == {i: derive_chunk_id(VALID_DOC_ID, i) for i in chunk_indices}


def test_derive_chunk_ids_batch_empty() -> None:
    assert derive_chunk_ids_batch(VALID_DOC_ID, []) == {}


# --- derive_doc_id invalid source ----------------------------------------------------


def test_derive_doc_id_invalid_source_none() -> None:
    with pytest.raises(IdentityInvalid) as exc_info:
        derive_doc_id(None, "spaces/eng/page-42")  # type: ignore[arg-type]
    assert exc_info.value.code == "IDENTITY_INVALID"
    assert exc_info.value.classification == "PERMANENT"
    assert exc_info.value.field_name == "source"


def test_derive_doc_id_invalid_source_empty() -> None:
    with pytest.raises(IdentityInvalid) as exc_info:
        derive_doc_id("", "spaces/eng/page-42")
    assert exc_info.value.field_name == "source"


def test_derive_doc_id_invalid_source_non_str() -> None:
    with pytest.raises(IdentityInvalid) as exc_info:
        derive_doc_id(123, "spaces/eng/page-42")  # type: ignore[arg-type]
    assert exc_info.value.field_name == "source"


# --- derive_doc_id invalid blob_path -------------------------------------------------


def test_derive_doc_id_invalid_blob_path_none() -> None:
    with pytest.raises(IdentityInvalid) as exc_info:
        derive_doc_id("confluence", None)  # type: ignore[arg-type]
    assert exc_info.value.field_name == "blob_path"


def test_derive_doc_id_invalid_blob_path_empty() -> None:
    with pytest.raises(IdentityInvalid) as exc_info:
        derive_doc_id("confluence", "")
    assert exc_info.value.field_name == "blob_path"


def test_derive_doc_id_invalid_blob_path_non_str() -> None:
    with pytest.raises(IdentityInvalid) as exc_info:
        derive_doc_id("confluence", 123)  # type: ignore[arg-type]
    assert exc_info.value.field_name == "blob_path"


# --- derive_chunk_id invalid doc_id --------------------------------------------------


def test_derive_chunk_id_invalid_doc_id_wrong_length() -> None:
    with pytest.raises(IdentityInvalid) as exc_info:
        derive_chunk_id("a" * 10, 0)
    assert exc_info.value.field_name == "doc_id"


def test_derive_chunk_id_invalid_doc_id_non_hex() -> None:
    with pytest.raises(IdentityInvalid) as exc_info:
        derive_chunk_id("g" * 64, 0)
    assert exc_info.value.field_name == "doc_id"


def test_derive_chunk_id_invalid_doc_id_uppercase() -> None:
    with pytest.raises(IdentityInvalid) as exc_info:
        derive_chunk_id("A" * 64, 0)
    assert exc_info.value.field_name == "doc_id"


def test_derive_chunk_id_invalid_doc_id_none() -> None:
    with pytest.raises(IdentityInvalid) as exc_info:
        derive_chunk_id(None, 0)  # type: ignore[arg-type]
    assert exc_info.value.field_name == "doc_id"


def test_derive_chunk_id_invalid_doc_id_non_str() -> None:
    with pytest.raises(IdentityInvalid) as exc_info:
        derive_chunk_id(12345, 0)  # type: ignore[arg-type]
    assert exc_info.value.field_name == "doc_id"


# --- derive_chunk_id invalid chunk_index ---------------------------------------------


@pytest.mark.parametrize("bad_index", ["3", 3.0, None])
def test_derive_chunk_id_invalid_chunk_index_type(bad_index: object) -> None:
    with pytest.raises(IdentityInvalid) as exc_info:
        derive_chunk_id(VALID_DOC_ID, bad_index)  # type: ignore[arg-type]
    assert exc_info.value.field_name == "chunk_index"


@pytest.mark.parametrize("bad_index", [True, False])
def test_derive_chunk_id_invalid_chunk_index_bool(bad_index: bool) -> None:
    with pytest.raises(IdentityInvalid) as exc_info:
        derive_chunk_id(VALID_DOC_ID, bad_index)  # type: ignore[arg-type]
    assert exc_info.value.field_name == "chunk_index"


def test_derive_chunk_id_invalid_chunk_index_negative() -> None:
    with pytest.raises(IdentityInvalid) as exc_info:
        derive_chunk_id(VALID_DOC_ID, -1)
    assert exc_info.value.field_name == "chunk_index"


# --- derive_chunk_ids_batch fail-fast -------------------------------------------------


def test_derive_chunk_ids_batch_fails_fast_on_invalid_index() -> None:
    with pytest.raises(IdentityInvalid) as exc_info:
        derive_chunk_ids_batch(VALID_DOC_ID, [0, 1, -1, 2])
    assert exc_info.value.field_name == "chunk_index"


# --- validate_id_format ---------------------------------------------------------------


def test_validate_id_format_accepts_valid_hash() -> None:
    assert validate_id_format(VALID_DOC_ID) == VALID_DOC_ID


@pytest.mark.parametrize(
    "bad_value",
    ["a" * 10, "A" * 64, "g" * 64, None, 12345],
    ids=["wrong_length", "uppercase", "non_hex", "none", "non_str"],
)
def test_validate_id_format_rejects_malformed(bad_value: object) -> None:
    with pytest.raises(IdentityInvalid) as exc_info:
        validate_id_format(bad_value, field_name="doc_id")  # type: ignore[arg-type]
    assert exc_info.value.code == "IDENTITY_INVALID"
    assert exc_info.value.classification == "PERMANENT"
    assert exc_info.value.field_name == "doc_id"


# --- IdentityInvalid constructor -------------------------------------------------------


def test_identity_invalid_constructor_sets_fixed_fields() -> None:
    exc = IdentityInvalid(field_name="chunk_index", reason="negative int")
    assert exc.code == "IDENTITY_INVALID"
    assert exc.classification == "PERMANENT"
    assert exc.field_name == "chunk_index"
    assert exc.reason == "negative int"


# --- property-based tests (hypothesis) --------------------------------------------------

_non_empty_text = st.text(min_size=1)


@given(source=_non_empty_text, blob_path=_non_empty_text)
@settings(max_examples=1000)
def test_property_derive_doc_id_determinism(source: str, blob_path: str) -> None:
    a = derive_doc_id(source, blob_path)
    b = derive_doc_id(source, blob_path)
    assert a == b
    assert _HEX_RE.match(a)


@given(salt=st.text(alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=12))
@settings(max_examples=5, suppress_health_check=[HealthCheck.too_slow])
def test_property_derive_doc_id_uniqueness(salt: str) -> None:
    """Over 1,000 distinct (source, blob_path) pairs (randomized via a hypothesis-drawn
    salt, made distinct by construction via an incrementing index), no doc_id collisions."""
    pairs = [(f"{salt}-source-{i}", f"{salt}-path-{i}") for i in range(1000)]
    doc_ids = [derive_doc_id(source, blob_path) for source, blob_path in pairs]
    assert len(doc_ids) == len(set(doc_ids))


@given(
    doc_id=st.text(alphabet="0123456789abcdef", min_size=64, max_size=64),
    chunk_index=st.integers(min_value=0, max_value=1_000_000),
)
@settings(max_examples=1000)
def test_property_derive_chunk_id_determinism(doc_id: str, chunk_index: int) -> None:
    a = derive_chunk_id(doc_id, chunk_index)
    b = derive_chunk_id(doc_id, chunk_index)
    assert a == b
    assert _HEX_RE.match(a)
