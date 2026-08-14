"""Unit tests for PyMuPDFParser (REQ-005)."""

from __future__ import annotations

import io
import sys

import pymupdf
import pytest

from vestibule.analyzer.conftest import make_envelope
from vestibule.analyzer.model import (
    ANALYZER_DEPENDENCY_MISSING,
    AnalyzerError,
    ElementType,
)
from vestibule.analyzer.parsers.pymupdf_parser import PyMuPDFParser, _page_elements

_envelope = make_envelope("pymupdf-parser")


@pytest.fixture
def parser() -> PyMuPDFParser:
    return PyMuPDFParser()


def test_pymupdf_parser_returns_elements_in_reading_order_on_fixture(
    parser: PyMuPDFParser, digital_pdf_bytes: bytes
) -> None:
    elements = parser.parse(_envelope, io.BytesIO(digital_pdf_bytes))
    assert len(elements) >= 1
    texts = [element.text for element in elements]
    assert all(text.strip() for text in texts)


def test_pymupdf_parser_never_returns_empty_list_for_non_empty_pdf(
    parser: PyMuPDFParser, digital_pdf_bytes: bytes
) -> None:
    elements = parser.parse(_envelope, io.BytesIO(digital_pdf_bytes))
    assert elements != []


def test_pymupdf_parser_records_page_number_in_metadata(
    parser: PyMuPDFParser, digital_pdf_bytes: bytes
) -> None:
    elements = parser.parse(_envelope, io.BytesIO(digital_pdf_bytes))
    pages = {element.metadata["page"] for element in elements}
    assert pages == {0, 1, 2}


def test_pymupdf_parser_classifies_large_font_as_heading() -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Big Title", fontsize=24)
    page.insert_text((72, 120), "Body text at normal size.", fontsize=10)
    data = doc.tobytes()
    doc.close()

    elements = PyMuPDFParser().parse(_envelope, io.BytesIO(data))
    types = {element.type for element in elements}
    assert ElementType.HEADING in types
    assert ElementType.PARAGRAPH in types


def test_pymupdf_parser_returns_empty_list_for_blank_pdf(
    scanned_pdf_bytes: bytes,
) -> None:
    """A blank PDF has no non-empty text blocks — an empty list, never a raise."""
    elements = PyMuPDFParser().parse(_envelope, io.BytesIO(scanned_pdf_bytes))
    assert elements == []


class _FakePage:
    """Duck-typed stand-in for `pymupdf.Page`, for testing `_page_elements` directly."""

    def __init__(self, blocks: list[dict[str, object]]) -> None:
        self._blocks = blocks

    def get_text(self, kind: str) -> dict[str, object]:
        return {"blocks": self._blocks}


def test_page_elements_skips_blocks_with_no_extractable_text() -> None:
    """A block with no lines (e.g. an image block) contributes no Element."""
    page = _FakePage(
        [
            {"lines": []},
            {"lines": [{"spans": [{"text": "Real text", "size": 10.0}]}]},
        ]
    )
    elements = _page_elements(page, page_index=0)
    assert len(elements) == 1
    assert elements[0].text == "Real text"


# --- ANALYZER_DEPENDENCY_MISSING (issue #19): fail fast at construction, not parse() ------


def test_pymupdf_parser_construction_without_pymupdf_raises_analyzer_dependency_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulates `pymupdf` being uninstalled the same way `test_detect.py` does:
    `sys.modules["pymupdf"] = None` makes any subsequent `import pymupdf` raise
    `ImportError`. Must fail at `PyMuPDFParser()` construction, not at `.parse()` —
    raising mid-`parse()` would flow through `Analyzer._parse_with_recovery`, which
    treats every caught `AnalyzerError` as TRANSIENT regardless of its actual declared
    severity, incorrectly self-transitioning instead of terminalizing a PERMANENT
    dependency failure.
    """
    monkeypatch.setitem(sys.modules, "pymupdf", None)

    with pytest.raises(AnalyzerError) as exc_info:
        PyMuPDFParser()

    assert exc_info.value.error_code == ANALYZER_DEPENDENCY_MISSING
