"""Unit tests for the Document Analyzer data model (REQ-005)."""

from __future__ import annotations

import dataclasses

import pytest

from vestibule.analyzer.model import (
    ANALYZER_NO_PARSER,
    ANALYZER_UNSUPPORTED_TYPE,
    DOCINT_RATE_LIMITED,
    DOCINT_UPSTREAM_ERROR,
    PARSER_INTERNAL,
    PARSER_TIMEOUT,
    AnalyzerError,
    DetectedType,
    Element,
    ElementType,
)
from vestibule.errors.registry import RaggedError, Severity, all_codes, classify

_ALL_CODES_AND_SEVERITIES: tuple[tuple[str, Severity], ...] = (
    (ANALYZER_UNSUPPORTED_TYPE, Severity.PERMANENT),
    (ANALYZER_NO_PARSER, Severity.PERMANENT),
    (PARSER_TIMEOUT, Severity.TRANSIENT),
    (DOCINT_RATE_LIMITED, Severity.TRANSIENT),
    (DOCINT_UPSTREAM_ERROR, Severity.TRANSIENT),
    (PARSER_INTERNAL, Severity.TRANSIENT),
)


def test_detected_type_values_are_lowercase_snake() -> None:
    assert DetectedType.DIGITAL_PDF.value == "digital_pdf"
    assert DetectedType.SCANNED_PDF.value == "scanned_pdf"
    assert DetectedType.DOCX.value == "docx"
    assert DetectedType.IMAGE.value == "image"
    assert DetectedType.UNSUPPORTED.value == "unsupported"


def test_element_type_values_are_lowercase_snake() -> None:
    assert ElementType.HEADING.value == "heading"
    assert ElementType.PARAGRAPH.value == "paragraph"
    assert ElementType.TABLE.value == "table"
    assert ElementType.LIST.value == "list"
    assert ElementType.IMAGE_CAPTION.value == "image_caption"
    assert ElementType.CODE.value == "code"


def test_element_is_frozen() -> None:
    element = Element(type=ElementType.PARAGRAPH, text="hello", metadata={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        element.text = "changed"  # type: ignore[misc]


def test_element_defaults_empty_metadata_and_no_confidence() -> None:
    element = Element(type=ElementType.PARAGRAPH, text="hello")
    assert element.metadata == {}
    assert element.confidence is None


def test_analyzer_error_carries_doc_id_reason_error_code_detected_type() -> None:
    exc = AnalyzerError(
        "doc-1",
        "boom",
        error_code=ANALYZER_UNSUPPORTED_TYPE,
        detected_type=DetectedType.UNSUPPORTED,
    )
    assert exc.doc_id == "doc-1"
    assert exc.reason == "boom"
    assert exc.error_code == ANALYZER_UNSUPPORTED_TYPE
    assert exc.detected_type == DetectedType.UNSUPPORTED
    assert isinstance(exc, RaggedError)


def test_analyzer_error_detected_type_defaults_to_none() -> None:
    exc = AnalyzerError("doc-1", "boom", error_code=PARSER_TIMEOUT)
    assert exc.detected_type is None


@pytest.mark.parametrize("code,severity", _ALL_CODES_AND_SEVERITIES)
def test_all_six_analyzer_codes_registered_after_import(
    code: str, severity: Severity
) -> None:
    assert classify(code) == severity


def test_all_six_analyzer_codes_present_in_all_codes() -> None:
    registered = {entry.code for entry in all_codes()}
    for code, _ in _ALL_CODES_AND_SEVERITIES:
        assert code in registered
