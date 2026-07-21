"""Arrival Envelope — Contract #1 canonical definition.

Defines the single normalized envelope every document must enter the pipeline through.
No component past the arrival boundary may read raw source-specific formats; every
producer adapter (out of scope here, Phase 2) must construct an `ArrivalEnvelope` via
`build_envelope`, and every consumer reading off the queue must parse via
`envelope_from_json`.

Failure taxonomy: every validation failure in this module resolves to exactly one
error code, `ENVELOPE_INVALID`, classified PERMANENT (never retried, always acked).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

_CONTENT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class TrustTier(str, Enum):
    """Fixed, code-defined enum — sole authority for accepted trust_tier values.

    Not driven by config/envelope.yaml at runtime; the YAML `trust_tiers` key is
    documentation/audit-only (see config/envelope.yaml).
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class EnvelopeValidationError(Exception):
    """Raised whenever an ArrivalEnvelope fails validation. Always PERMANENT."""

    code: str = "ENVELOPE_INVALID"
    classification: Literal["PERMANENT"] = "PERMANENT"

    def __init__(self, field_errors: list[dict[str, str]]) -> None:
        self.field_errors = field_errors
        summary = "; ".join(f"{e['field']}: {e['reason']}" for e in field_errors)
        super().__init__(f"{self.code}: {summary or 'invalid envelope'}")


def _wrap_validation_error(exc: ValidationError) -> EnvelopeValidationError:
    """Maps a pydantic ValidationError to the module's declared PERMANENT error."""
    field_errors = [
        {"field": ".".join(str(part) for part in err["loc"]) or "__root__", "reason": err["msg"]}
        for err in exc.errors()
    ]
    return EnvelopeValidationError(field_errors)


class ArrivalEnvelope(BaseModel):
    """Canonical arrival envelope — Contract #1. Strict schema, immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_id: str
    source: str
    blob_path: str
    vertical: str | None = None
    scenario_id: str | None = None
    allowed_groups: list[str] = Field(default_factory=list)
    trust_tier: TrustTier
    content_hash: str
    received_at: datetime

    @field_validator("content_hash")
    @classmethod
    def _content_hash_is_sha256_hex(cls, v: str) -> str:
        if not _CONTENT_HASH_RE.match(v):
            raise ValueError("content_hash must be 64 lowercase hex characters")
        return v

    @field_validator("source", "blob_path")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("must be non-empty")
        return v

    @model_validator(mode="after")
    def _verify_doc_id(self) -> "ArrivalEnvelope":
        """Re-derives doc_id via compute_doc_id(source, blob_path) and rejects on mismatch.

        Runs only after all field-level validators (incl. source/blob_path) have already
        succeeded — a model-level validator, not a field-level one, because a field_validator
        on doc_id cannot reliably see source/blob_path (declared later in the model).
        """
        expected = compute_doc_id(self.source, self.blob_path)
        if self.doc_id != expected:
            raise ValueError(f"doc_id does not match sha256(source + blob_path); expected {expected}")
        return self


def compute_doc_id(source: str, blob_path: str) -> str:
    """doc_id = sha256(source + blob_path), per Contract #2. Pure, deterministic."""
    return hashlib.sha256((source + blob_path).encode("utf-8")).hexdigest()


def build_envelope(
    *,
    source: str,
    blob_path: str,
    content_hash: str,
    trust_tier: TrustTier,
    allowed_groups: list[str] | None = None,
    vertical: str | None = None,
    scenario_id: str | None = None,
    received_at: datetime | None = None,
) -> ArrivalEnvelope:
    """Producer-facing factory: computes doc_id, applies default-deny, validates."""
    try:
        return ArrivalEnvelope(
            doc_id=compute_doc_id(source, blob_path),
            source=source,
            blob_path=blob_path,
            vertical=vertical,
            scenario_id=scenario_id,
            allowed_groups=allowed_groups if allowed_groups is not None else [],
            trust_tier=trust_tier,
            content_hash=content_hash,
            received_at=received_at if received_at is not None else datetime.now(timezone.utc),
        )
    except ValidationError as exc:
        raise _wrap_validation_error(exc) from exc


def envelope_to_json(envelope: ArrivalEnvelope) -> str:
    """Lossless serialization for queue message body."""
    return envelope.model_dump_json()


def envelope_from_json(raw: str | bytes) -> ArrivalEnvelope:
    """Deserialize + re-validate (including doc_id cross-check). Raises EnvelopeValidationError."""
    try:
        data: Any = json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        raise EnvelopeValidationError([{"field": "__root__", "reason": f"malformed JSON: {exc}"}]) from exc

    try:
        return ArrivalEnvelope.model_validate(data)
    except ValidationError as exc:
        raise _wrap_validation_error(exc) from exc
