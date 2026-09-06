"""Unit tests for PyMuPDFParser (REQ-005)."""

from __future__ import annotations

import io
import sys
from pathlib import Path

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
_SAMPLE_PDF = Path(__file__).resolve().parents[4] / "examples" / "sample.pdf"


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


class _FakeTableFinder:
    """Duck-typed stand-in for `pymupdf.table.TableFinder` — always finds no tables."""

    tables: list[object] = []


class _FakePage:
    """Duck-typed stand-in for `pymupdf.Page`, for testing `_page_elements` directly."""

    def __init__(self, blocks: list[dict[str, object]]) -> None:
        self._blocks = blocks

    def get_text(self, kind: str) -> dict[str, object]:
        return {"blocks": self._blocks}

    def find_tables(self) -> _FakeTableFinder:
        return _FakeTableFinder()


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


# --- Table detection (find_tables()) -------------------------------------------------


def _draw_grid_table(
    page: pymupdf.Page,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    cells: list[list[str]],
) -> None:
    """Draws a bordered `rows x cols` grid with `cells`' text, so `find_tables()`
    detects it (ruling lines, not just text, are required — see LLD)."""
    row_count = len(cells)
    column_count = len(cells[0])
    row_height = (y1 - y0) / row_count
    column_width = (x1 - x0) / column_count
    page.draw_rect((x0, y0, x1, y1))
    for row in range(1, row_count):
        y = y0 + row * row_height
        page.draw_line((x0, y), (x1, y))
    for column in range(1, column_count):
        x = x0 + column * column_width
        page.draw_line((x, y0), (x, y1))
    for row, row_texts in enumerate(cells):
        for column, text in enumerate(row_texts):
            page.insert_text(
                (
                    x0 + column * column_width + 8,
                    y0 + row * row_height + row_height / 2,
                ),
                text,
            )


def test_page_elements_detects_real_table_with_cells_metadata() -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    _draw_grid_table(page, 72, 200, 300, 260, [["A1", "A2"], ["B1", "B2"]])

    elements = _page_elements(page, page_index=0)
    doc.close()

    tables = [element for element in elements if element.type == ElementType.TABLE]
    assert len(tables) == 1
    table = tables[0]
    assert table.metadata["row_count"] == 2
    assert table.metadata["column_count"] == 2
    cell_index = {
        (cell["row_index"], cell["column_index"]): cell["content"]
        for cell in table.metadata["cells"]
    }
    assert set(cell_index) == {(0, 0), (0, 1), (1, 0), (1, 1)}


def test_page_elements_does_not_double_emit_table_text_as_paragraph() -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    _draw_grid_table(
        page, 72, 200, 300, 260, [["Header1", "Header2"], ["Value1", "Value2"]]
    )

    elements = _page_elements(page, page_index=0)
    doc.close()

    non_table_text = "\n".join(
        element.text for element in elements if element.type != ElementType.TABLE
    )
    assert "Header1" not in non_table_text
    assert "Value1" not in non_table_text


def test_page_elements_interleaves_table_in_reading_order() -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Section Heading", fontsize=20)
    _draw_grid_table(page, 72, 150, 300, 210, [["A1", "A2"], ["B1", "B2"]])
    page.insert_text((72, 260), "Trailing paragraph text.", fontsize=10)

    elements = _page_elements(page, page_index=0)
    doc.close()

    assert [element.type for element in elements] == [
        ElementType.HEADING,
        ElementType.TABLE,
        ElementType.PARAGRAPH,
    ]


def test_pymupdf_parser_detects_real_table_in_sample_pdf(parser: PyMuPDFParser) -> None:
    """`examples/sample.pdf`'s page-2 table is genuinely detected end-to-end."""
    elements = parser.parse(_envelope, io.BytesIO(_SAMPLE_PDF.read_bytes()))

    tables = [element for element in elements if element.type == ElementType.TABLE]
    assert len(tables) == 1
    table = tables[0]
    assert table.metadata["row_count"] == 4
    assert table.metadata["column_count"] == 3
    header_row = sorted(
        (cell for cell in table.metadata["cells"] if cell["row_index"] == 0),
        key=lambda cell: cell["column_index"],
    )
    assert [cell["content"] for cell in header_row] == [
        "Region",
        "2024 Yield (tons)",
        "2025 Yield (tons)",
    ]
    non_table_text = "\n".join(
        element.text for element in elements if element.type != ElementType.TABLE
    )
    assert "Northgate Block" not in non_table_text


def test_pymupdf_parser_fixtures_without_ruling_lines_have_no_table(
    parser: PyMuPDFParser, digital_pdf_bytes: bytes, scanned_pdf_bytes: bytes
) -> None:
    """`digital_pdf_bytes`/`scanned_pdf_bytes` draw no ruling lines, so `find_tables()`
    must genuinely find nothing — this is verified, not assumed."""
    for pdf_bytes in (digital_pdf_bytes, scanned_pdf_bytes):
        elements = parser.parse(_envelope, io.BytesIO(pdf_bytes))
        assert all(element.type != ElementType.TABLE for element in elements)


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
