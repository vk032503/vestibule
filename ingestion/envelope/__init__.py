"""Arrival Envelope — Contract #1 canonical definition."""

from ingestion.envelope.model import (
    ArrivalEnvelope,
    EnvelopeValidationError,
    TrustTier,
    build_envelope,
    compute_doc_id,
    envelope_from_json,
    envelope_to_json,
)

__all__ = [
    "ArrivalEnvelope",
    "EnvelopeValidationError",
    "TrustTier",
    "build_envelope",
    "compute_doc_id",
    "envelope_from_json",
    "envelope_to_json",
]
