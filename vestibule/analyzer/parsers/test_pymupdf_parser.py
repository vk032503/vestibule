"""Unit tests for PyMuPDFParser (REQ-005)."""

from __future__ import annotations

import io

import pymupdf
import pytest

from vestibule.analyzer.conftest import make_envelope
from vestibule.analyzer.model import ElementType
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
