"""Identity & Idempotency — Contract #2 canonical definition.

This is the single, canonical implementation of the doc_id/chunk_id derivation rule:
`doc_id = sha256(source + blob_path)`, `chunk_id = sha256(doc_id + str(chunk_index))`.
Pure functions, zero I/O, zero external dependencies beyond the standard library.

`ingestion.envelope.model.compute_doc_id` (REQ-001) delegates to `derive_doc_id` here so
there is exactly one hashing implementation in the codebase (see REQ-002 LLD §1a).

Failure taxonomy: every failure in this module raises `IdentityInvalid`, classified
PERMANENT — malformed identity inputs are a producer/caller bug, never transient.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable, Literal

from ingestion.errors.registry import RaggedError, Severity, register_error

ID_HEX_LENGTH: int = 64
_HEX_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{64}$")


class IdentityInvalid(RaggedError):
    """Raised for any malformed identity input. Always PERMANENT — malformed identity
    inputs are a producer/caller bug, never a transient condition worth retrying."""

    code: str = "IDENTITY_INVALID"
    classification: Literal["PERMANENT"] = "PERMANENT"

    def __init__(self, field_name: str, reason: str) -> None:
        """code and classification are fixed by this class, not caller-supplied."""
        self.field_name = field_name
        self.reason = reason
        super().__init__(f"{field_name}: {reason}", error_code=self.code)


register_error(
    IdentityInvalid.code,
    Severity.PERMANENT,
    "malformed identity input to doc_id/chunk_id derivation (REQ-002)",
)


def derive_doc_id(source: str, blob_path: str) -> str:
    """doc_id = sha256(source + blob_path), per Contract #2. Pure, deterministic, no I/O.

    Canonical implementation of the doc_id derivation rule; ingestion.envelope.model's
    compute_doc_id (REQ-001) delegates to this function. Raises IdentityInvalid (PERMANENT)
    if source or blob_path is None, non-str, or empty.
    """
    _validate_non_empty_str(source, field_name="source")
    _validate_non_empty_str(blob_path, field_name="blob_path")
    return hashlib.sha256((source + blob_path).encode("utf-8")).hexdigest()


def derive_chunk_id(doc_id: str, chunk_index: int) -> str:
    """chunk_id = sha256(doc_id + str(chunk_index)), per Contract #2. Pure, deterministic.

    Raises IdentityInvalid (PERMANENT) if doc_id fails validate_id_format, or chunk_index
    is not a non-negative int. Booleans are always rejected regardless of value, per F4.
    """
    validated_doc_id = validate_id_format(doc_id, field_name="doc_id")
    _validate_chunk_index(chunk_index)
    return hashlib.sha256(
        (validated_doc_id + str(chunk_index)).encode("utf-8")
    ).hexdigest()


def derive_chunk_ids_batch(doc_id: str, chunk_indices: Iterable[int]) -> dict[int, str]:
    """Batch helper: {chunk_index: chunk_id, ...}, one entry per input index.

    Each value is identical to calling derive_chunk_id(doc_id, i) individually.
    Fails fast: raises IdentityInvalid on the first invalid chunk_index encountered and
    returns no partial mapping (all-or-nothing).
    """
    result: dict[int, str] = {}
    for chunk_index in chunk_indices:
        result[chunk_index] = derive_chunk_id(doc_id, chunk_index)
    return result


def validate_id_format(value: str, *, field_name: str = "id") -> str:
    """Validates that value is a 64-character lowercase hex string (sha256 hex digest shape).

    Returns value unchanged on success. Raises IdentityInvalid (PERMANENT) otherwise.
    """
    if not isinstance(value, str):
        raise IdentityInvalid(field_name, "not a string")
    if not _HEX_RE.match(value):
        raise IdentityInvalid(field_name, "not 64 hex chars")
    return value


def _validate_non_empty_str(value: str, *, field_name: str) -> None:
    if value is None or not isinstance(value, str):
        raise IdentityInvalid(field_name, "not a string")
    if value == "":
        raise IdentityInvalid(field_name, "empty string")


def _validate_chunk_index(chunk_index: int) -> None:
    if not isinstance(chunk_index, int) or isinstance(chunk_index, bool):
        raise IdentityInvalid("chunk_index", "not an int")
    if chunk_index < 0:
        raise IdentityInvalid("chunk_index", "negative int")
