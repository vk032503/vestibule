"""Failure Taxonomy — Contract #4 canonical definition."""

from ingestion.errors.registry import (
    ErrorCode,
    ErrorCodeRegistrationInvalid,
    ErrorRegistry,
    RaggedError,
    Severity,
    all_codes,
    classify,
    register_error,
)

__all__ = [
    "ErrorCode",
    "ErrorCodeRegistrationInvalid",
    "ErrorRegistry",
    "RaggedError",
    "Severity",
    "all_codes",
    "classify",
    "register_error",
]
