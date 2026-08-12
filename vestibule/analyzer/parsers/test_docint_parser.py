"""Unit tests for DocumentIntelligenceParser (REQ-005).

No live API calls — every test uses a recorded-response fake client in place of the real
`azure.ai.documentintelligence.DocumentIntelligenceClient` (story: "recorded response
fake"), monkeypatching the parser's private `_client` attribute since the constructor's
signature is fixed by the LLD (no client-injection parameter).
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from azure.ai.documentintelligence.models import (
    AnalyzeResult,
    DocumentParagraph,
    DocumentTable,
    DocumentTableCell,
    ParagraphRole,
)
from azure.core.exceptions import HttpResponseError

from vestibule.analyzer.conftest import make_envelope
from vestibule.analyzer.model import (
    DOCINT_RATE_LIMITED,
    DOCINT_UPSTREAM_ERROR,
    AnalyzerError,
    ElementType,
)
from vestibule.analyzer.parsers.docint_parser import DocumentIntelligenceParser

_envelope = make_envelope("docint-parser")


class _FakePoller:
    """Recorded-response fake standing in for `AnalyzeDocumentLROPoller`."""

    def __init__(
        self, result: AnalyzeResult | None = None, error: Exception | None = None
    ) -> None:
        self._result = result
        self._error = error

    def result(self, timeout: float | None = None) -> AnalyzeResult:
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class _FakeClient:
    """Recorded-response fake standing in for `DocumentIntelligenceClient`."""

    def __init__(self, poller: _FakePoller) -> None:
        self._poller = poller

    def begin_analyze_document(
        self, model_id: str, body: Any, **kwargs: Any
    ) -> _FakePoller:
        return self._poller


def _make_parser(poller: _FakePoller) -> DocumentIntelligenceParser:
    parser = DocumentIntelligenceParser(
        endpoint="https://fake.example.com",
        api_key="fake-key",
        api_version="2024-11-30",
    )
    parser._client = _FakeClient(poller)  # type: ignore[assignment]
    return parser


def _sample_result() -> AnalyzeResult:
    heading = DocumentParagraph(
        role=ParagraphRole.TITLE, content="Section Title", spans=[]
    )
    body = DocumentParagraph(role=None, content="Body paragraph.", spans=[])
    cell = DocumentTableCell(row_index=0, column_index=0, content="A1", spans=[])
    table = DocumentTable(row_count=1, column_count=1, cells=[cell], spans=[])
    return AnalyzeResult(
        api_version="2024-11-30",
        model_id="prebuilt-layout",
        string_index_type="utf16CodeUnit",
        content="Section Title\nBody paragraph.",
        pages=[],
        paragraphs=[heading, body],
        tables=[table],
    )


def test_docint_parser_returns_table_element_with_row_column_metadata() -> None:
    parser = _make_parser(_FakePoller(result=_sample_result()))
    elements = parser.parse(_envelope, io.BytesIO(b"scanned bytes"))
    tables = [element for element in elements if element.type == ElementType.TABLE]
    assert len(tables) == 1
    assert tables[0].metadata["row_count"] == 1
    assert tables[0].metadata["column_count"] == 1
    assert tables[0].metadata["cells"][0]["content"] == "A1"


def test_docint_parser_returns_heading_and_paragraph_elements() -> None:
    parser = _make_parser(_FakePoller(result=_sample_result()))
    elements = parser.parse(_envelope, io.BytesIO(b"scanned bytes"))
    headings = [element for element in elements if element.type == ElementType.HEADING]
    paragraphs = [
        element for element in elements if element.type == ElementType.PARAGRAPH
    ]
    assert len(headings) == 1
    assert headings[0].text == "Section Title"
    assert len(paragraphs) == 1
    assert paragraphs[0].text == "Body paragraph."


def test_docint_parser_maps_429_to_docint_rate_limited() -> None:
    error = HttpResponseError(message="rate limited")
    error.status_code = 429
    parser = _make_parser(_FakePoller(error=error))
    with pytest.raises(AnalyzerError) as exc_info:
        parser.parse(_envelope, io.BytesIO(b"scanned bytes"))
    assert exc_info.value.error_code == DOCINT_RATE_LIMITED


def test_docint_parser_maps_5xx_to_docint_upstream_error() -> None:
    error = HttpResponseError(message="service unavailable")
    error.status_code = 503
    parser = _make_parser(_FakePoller(error=error))
    with pytest.raises(AnalyzerError) as exc_info:
        parser.parse(_envelope, io.BytesIO(b"scanned bytes"))
    assert exc_info.value.error_code == DOCINT_UPSTREAM_ERROR


def test_docint_parser_unmapped_http_error_propagates_unchanged() -> None:
    error = HttpResponseError(message="bad request")
    error.status_code = 400
    parser = _make_parser(_FakePoller(error=error))
    with pytest.raises(HttpResponseError):
        parser.parse(_envelope, io.BytesIO(b"scanned bytes"))
