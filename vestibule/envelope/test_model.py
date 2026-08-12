"""Unit tests for the Arrival Envelope (Contract #1) — REQ-001."""

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from vestibule.envelope.model import (
    ArrivalEnvelope,
    EnvelopeValidationError,
    TrustTier,
    build_envelope,
    compute_doc_id,
    envelope_from_json,
    envelope_to_json,
)
from vestibule.errors.registry import RaggedError, Severity, classify
from vestibule.identity.derive import derive_doc_id

VALID_CONTENT_HASH = "a" * 64


def _valid_payload() -> dict:
    """Full, valid raw envelope payload (as it would arrive off the queue)."""
    source = "confluence"
    blob_path = "spaces/eng/page-42"
    return {
        "doc_id": compute_doc_id(source, blob_path),
        "source": source,
        "blob_path": blob_path,
        "vertical": "engineering",
        "scenario_id": "scn-1",
        "allowed_groups": ["team-eng"],
        "trust_tier": "internal",
        "content_hash": VALID_CONTENT_HASH,
        "received_at": "2026-07-20T12:00:00+00:00",
    }


def _field_names(field_errors: list[dict[str, str]]) -> list[str]:
    return [e["field"] for e in field_errors]


# --- compute_doc_id -----------------------------------------------------------------


def test_compute_doc_id_deterministic() -> None:
    a = compute_doc_id("confluence", "spaces/eng/page-42")
    b = compute_doc_id("confluence", "spaces/eng/page-42")
    assert a == b
    assert len(a) == 64


def test_compute_doc_id_differs_by_source_or_path() -> None:
    base = compute_doc_id("confluence", "spaces/eng/page-42")
    assert compute_doc_id("s3", "spaces/eng/page-42") != base
    assert compute_doc_id("confluence", "spaces/eng/page-99") != base


# --- build_envelope happy paths ------------------------------------------------------


def test_build_envelope_valid_minimal() -> None:
    env = build_envelope(
        source="confluence",
        blob_path="spaces/eng/page-42",
        content_hash=VALID_CONTENT_HASH,
        trust_tier=TrustTier.INTERNAL,
    )
    assert env.doc_id == compute_doc_id("confluence", "spaces/eng/page-42")
    assert env.allowed_groups == []
    assert env.vertical is None
    assert env.scenario_id is None


def test_build_envelope_valid_full() -> None:
    received_at = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    env = build_envelope(
        source="confluence",
        blob_path="spaces/eng/page-42",
        content_hash=VALID_CONTENT_HASH,
        trust_tier=TrustTier.RESTRICTED,
        allowed_groups=["team-eng", "team-sec"],
        vertical="engineering",
        scenario_id="scn-1",
        received_at=received_at,
    )
    assert env.allowed_groups == ["team-eng", "team-sec"]
    assert env.vertical == "engineering"
    assert env.scenario_id == "scn-1"
    assert env.received_at == received_at
    assert env.trust_tier == TrustTier.RESTRICTED


# --- missing required fields (via envelope_from_json, the untrusted-payload path) ---


@pytest.mark.parametrize(
    "missing_field",
    ["source", "blob_path", "trust_tier", "content_hash", "received_at"],
)
def test_missing_required_field(missing_field: str) -> None:
    payload = _valid_payload()
    del payload[missing_field]
    with pytest.raises(EnvelopeValidationError) as exc_info:
        envelope_from_json(json.dumps(payload))
    err = exc_info.value
    assert err.code == "ENVELOPE_INVALID"
    assert err.classification == "PERMANENT"
    assert missing_field in _field_names(err.field_errors)


# --- strict schema --------------------------------------------------------------------


def test_unknown_extra_field_rejected() -> None:
    payload = _valid_payload()
    payload["unexpected_field"] = "not allowed"
    with pytest.raises(EnvelopeValidationError) as exc_info:
        envelope_from_json(json.dumps(payload))
    err = exc_info.value
    assert err.code == "ENVELOPE_INVALID"
    assert "unexpected_field" in _field_names(err.field_errors)


# --- trust_tier -------------------------------------------------------------------


def test_invalid_trust_tier_value() -> None:
    payload = _valid_payload()
    payload["trust_tier"] = "top-secret"
    with pytest.raises(EnvelopeValidationError) as exc_info:
        envelope_from_json(json.dumps(payload))
    assert exc_info.value.code == "ENVELOPE_INVALID"
    assert exc_info.value.classification == "PERMANENT"


# --- content_hash -------------------------------------------------------------------


def test_invalid_content_hash_format() -> None:
    with pytest.raises(EnvelopeValidationError) as exc_info:
        build_envelope(
            source="confluence",
            blob_path="spaces/eng/page-42",
            content_hash="not-a-valid-hash",
            trust_tier=TrustTier.PUBLIC,
        )
    assert exc_info.value.code == "ENVELOPE_INVALID"
    assert exc_info.value.classification == "PERMANENT"


# --- doc_id cross-check (model-level validator) --------------------------------------


def test_doc_id_mismatch_rejected() -> None:
    payload = _valid_payload()
    payload["doc_id"] = "0" * 64  # does not match sha256(source + blob_path)
    with pytest.raises(EnvelopeValidationError) as exc_info:
        envelope_from_json(json.dumps(payload))
    err = exc_info.value
    assert err.code == "ENVELOPE_INVALID"
    assert err.classification == "PERMANENT"


def test_doc_id_verified_only_after_source_and_blob_path_valid() -> None:
    """Regression guard for the doc_id field-ordering bug fixed pre-approval.

    An empty (invalid) source combined with a mismatched doc_id must report the
    source field error, not a spurious/undefined doc_id check — confirming the
    model-level `_verify_doc_id` validator only runs after field-level validation
    succeeds.
    """
    payload = _valid_payload()
    payload["source"] = ""
    payload["doc_id"] = "0" * 64
    with pytest.raises(EnvelopeValidationError) as exc_info:
        envelope_from_json(json.dumps(payload))
    field_names = _field_names(exc_info.value.field_errors)
    assert "source" in field_names
    assert not any("doc_id" in name for name in field_names)


# --- allowed_groups (default-deny) ---------------------------------------------------


def test_allowed_groups_omitted_defaults_to_deny() -> None:
    payload = _valid_payload()
    del payload["allowed_groups"]
    env = envelope_from_json(json.dumps(payload))
    assert env.allowed_groups == []


def test_allowed_groups_explicit_null_rejected() -> None:
    payload = _valid_payload()
    payload["allowed_groups"] = None
    with pytest.raises(EnvelopeValidationError) as exc_info:
        envelope_from_json(json.dumps(payload))
    assert exc_info.value.code == "ENVELOPE_INVALID"
    assert "allowed_groups" in _field_names(exc_info.value.field_errors)


def test_allowed_groups_wrong_type_rejected() -> None:
    payload = _valid_payload()
    payload["allowed_groups"] = "team-eng"
    with pytest.raises(EnvelopeValidationError) as exc_info:
        envelope_from_json(json.dumps(payload))
    assert exc_info.value.code == "ENVELOPE_INVALID"
    assert "allowed_groups" in _field_names(exc_info.value.field_errors)


# --- received_at ---------------------------------------------------------------------


def test_received_at_invalid_format_rejected() -> None:
    payload = _valid_payload()
    payload["received_at"] = "not-a-timestamp"
    with pytest.raises(EnvelopeValidationError) as exc_info:
        envelope_from_json(json.dumps(payload))
    assert exc_info.value.code == "ENVELOPE_INVALID"
    assert "received_at" in _field_names(exc_info.value.field_errors)


# --- malformed JSON --------------------------------------------------------------------


def test_malformed_json_rejected() -> None:
    with pytest.raises(EnvelopeValidationError) as exc_info:
        envelope_from_json("{not json")
    assert exc_info.value.code == "ENVELOPE_INVALID"
    assert exc_info.value.classification == "PERMANENT"


# --- round-trip serialization ---------------------------------------------------------


def test_round_trip_lossless() -> None:
    original = build_envelope(
        source="confluence",
        blob_path="spaces/eng/page-42",
        content_hash=VALID_CONTENT_HASH,
        trust_tier=TrustTier.CONFIDENTIAL,
        allowed_groups=["team-eng", "team-sec"],
        vertical="engineering",
        scenario_id="scn-1",
        received_at=datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc),
    )
    round_tripped = envelope_from_json(envelope_to_json(original))
    assert round_tripped == original


def test_round_trip_lossless_minimal() -> None:
    original = build_envelope(
        source="confluence",
        blob_path="spaces/eng/page-42",
        content_hash=VALID_CONTENT_HASH,
        trust_tier=TrustTier.PUBLIC,
        received_at=datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc),
    )
    round_tripped = envelope_from_json(envelope_to_json(original))
    assert round_tripped == original


# --- immutability -----------------------------------------------------------------------


def test_envelope_is_immutable() -> None:
    env = build_envelope(
        source="confluence",
        blob_path="spaces/eng/page-42",
        content_hash=VALID_CONTENT_HASH,
        trust_tier=TrustTier.PUBLIC,
    )
    with pytest.raises(ValidationError):
        env.source = "s3"  # type: ignore[misc]


def test_direct_model_construction_still_valid() -> None:
    """Sanity check: ArrivalEnvelope itself is constructible directly with valid data."""
    source, blob_path = "confluence", "spaces/eng/page-42"
    env = ArrivalEnvelope(
        doc_id=compute_doc_id(source, blob_path),
        source=source,
        blob_path=blob_path,
        trust_tier=TrustTier.PUBLIC,
        content_hash=VALID_CONTENT_HASH,
        received_at=datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert env.allowed_groups == []


# --- REQ-002 cross-module regression guards (Contract #2 drift protection) -----------


@pytest.mark.parametrize(
    "source,blob_path",
    [
        ("confluence", "spaces/eng/page-42"),
        ("s3", "bucket/key.pdf"),
        ("sharepoint", "sites/hr/doc-1"),
        ("a", "b"),
        ("x" * 500, "y" * 500),
    ],
)
def test_compute_doc_id_delegates_to_derive_doc_id(source: str, blob_path: str) -> None:
    """Guards against the two doc_id implementations silently diverging (REQ-002 §1a)."""
    assert compute_doc_id(source, blob_path) == derive_doc_id(source, blob_path)


def test_build_envelope_empty_source_raises_envelope_validation_error() -> None:
    with pytest.raises(EnvelopeValidationError) as exc_info:
        build_envelope(
            source="",
            blob_path="spaces/eng/page-42",
            content_hash=VALID_CONTENT_HASH,
            trust_tier=TrustTier.PUBLIC,
        )
    err = exc_info.value
    assert err.code == "ENVELOPE_INVALID"
    assert err.classification == "PERMANENT"
    assert "source" in _field_names(err.field_errors)


def test_build_envelope_empty_blob_path_raises_envelope_validation_error() -> None:
    with pytest.raises(EnvelopeValidationError) as exc_info:
        build_envelope(
            source="confluence",
            blob_path="",
            content_hash=VALID_CONTENT_HASH,
            trust_tier=TrustTier.PUBLIC,
        )
    err = exc_info.value
    assert err.code == "ENVELOPE_INVALID"
    assert err.classification == "PERMANENT"
    assert "blob_path" in _field_names(err.field_errors)


def test_build_envelope_default_received_at_is_tz_aware_utc() -> None:
    env = build_envelope(
        source="confluence",
        blob_path="spaces/eng/page-42",
        content_hash=VALID_CONTENT_HASH,
        trust_tier=TrustTier.PUBLIC,
    )
    assert env.received_at.tzinfo is not None


# --- REQ-004 cross-module regression guards (Contract #4 migration) -------------------


def test_envelope_validation_error_is_ragged_error() -> None:
    exc = EnvelopeValidationError([{"field": "source", "reason": "empty string"}])
    assert isinstance(exc, RaggedError)
    assert exc.error_code == exc.code == "ENVELOPE_INVALID"


def test_envelope_invalid_registered_in_default_registry() -> None:
    assert classify("ENVELOPE_INVALID") == Severity.PERMANENT


def test_existing_classification_attr_matches_registry_severity() -> None:
    """Drift guard (Assumption A7): the hardcoded `classification` literal must never
    silently diverge from what the registry now reports for the same code."""
    assert (
        classify(EnvelopeValidationError.code).value
        == EnvelopeValidationError.classification
    )


def test_existing_constructor_signature_unchanged() -> None:
    """Regression guard: EnvelopeValidationError(field_errors) still constructs with the
    exact call shape used by this module's own pre-REQ-004 tests (migration note)."""
    field_errors = [{"field": "source", "reason": "empty string"}]
    exc = EnvelopeValidationError(field_errors)
    assert exc.field_errors == field_errors
