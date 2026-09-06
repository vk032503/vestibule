"""PyMuPDFParser — thin ParserAdapter wrapping PyMuPDF (fitz) for DIGITAL_PDF (REQ-005).

Extracts text with page/paragraph boundaries; maps text blocks to
`Element(HEADING | PARAGRAPH, ...)` using PyMuPDF's own block/font-size signals only —
no custom layout algorithm (house rules: "adapters thin", "never ... implement chunking
algorithms" applies equally to layout classification here). Tables are detected via
PyMuPDF's own `find_tables()` API and emitted as `Element(TABLE, ...)` in the same
`metadata["cells"]` shape `DocumentIntelligenceParser` produces (`row_index`/
`column_index`/`content`), so `TableAtomicChunkStrategy` is reachable through this
parser too, not only through the Azure Document Intelligence adapter. Text blocks whose
bounding box falls inside a detected table's bounding box are excluded from
HEADING/PARAGRAPH extraction, so a table's own text is never double-emitted as prose.

`pymupdf` is imported lazily, inside `__init__` rather than at module load (issue #19):
importing this module never hard-fails if `pymupdf` is somehow missing (it is a core
dependency, but a broken/partial install can still lack it), and constructing
`PyMuPDFParser()` fails fast with `AnalyzerError` (`ANALYZER_DEPENDENCY_MISSING`,
PERMANENT) at composition-root time — before `Analyzer.analyze()` is ever called with it
registered — rather than raising mid-`parse()`, where `Analyzer._parse_with_recovery`
would incorrectly treat any `AnalyzerError` as TRANSIENT (matches the
`FastEmbedEmbedder`/`AzureAISearchIndexer` fail-fast-at-construction pattern).
"""

from __future__ import annotations

import types
from typing import Any

from vestibule.analyzer.model import (
    ANALYZER_DEPENDENCY_MISSING,
    AnalyzerError,
    BytesReader,
    Element,
    ElementType,
)
from vestibule.analyzer.registry import ParserAdapter
from vestibule.envelope.model import ArrivalEnvelope

_HEADING_FONT_SIZE_THRESHOLD = 14.0


class PyMuPDFParser(ParserAdapter):
    """Routes for `DetectedType.DIGITAL_PDF`. Thin wrap of PyMuPDF (`fitz`)."""

    def __init__(self) -> None:
        """Fails fast if `pymupdf` is not importable.

        Raises:
            AnalyzerError: `ANALYZER_DEPENDENCY_MISSING` (PERMANENT) if `pymupdf` is
                not importable.
        """
        self._pymupdf: types.ModuleType = _import_pymupdf()

    def parse(
        self, envelope: ArrivalEnvelope, bytes_reader: BytesReader
    ) -> list[Element]:
        """See `ParserAdapter.parse`.

        Args:
            envelope: The validated `ArrivalEnvelope` (unused beyond the base contract —
                this adapter reads only the document bytes).
            bytes_reader: Seekable byte source for the PDF's content.

        Returns:
            One `Element` per non-empty text block, in reading order, across every page.
        """
        del envelope  # unused: this adapter's output depends only on the document bytes
        bytes_reader.seek(0)
        data = bytes_reader.read()
        elements: list[Element] = []
        with self._pymupdf.open(stream=data, filetype="pdf") as doc:
            for page_index in range(doc.page_count):
                elements.extend(_page_elements(doc[page_index], page_index))
        return elements


def _import_pymupdf() -> types.ModuleType:
    """Imports and returns the `pymupdf` module, or raises `AnalyzerError`.

    Raises:
        AnalyzerError: `ANALYZER_DEPENDENCY_MISSING` (PERMANENT) if `pymupdf` is not
            importable.
    """
    try:
        import pymupdf

        return pymupdf
    except ImportError as exc:
        raise AnalyzerError(
            "",
            "pymupdf could not be imported to construct PyMuPDFParser; it is a core "
            "dependency of vestibule — reinstall with `pip install -e .` (or "
            "`pip install vestibule`) to restore it",
            error_code=ANALYZER_DEPENDENCY_MISSING,
        ) from exc


def _page_elements(page: Any, page_index: int) -> list[Element]:
    """Extracts one `Element` per non-empty text block and detected table on `page`.

    Tables are detected via PyMuPDF's own `find_tables()` API. A text block whose
    bounding-box center falls inside a detected table's bounding box is excluded (its
    content is already carried by the table's own `Element`, avoiding double-emission).
    The combined list is sorted by each element's top-`y0` bbox coordinate so a table
    interleaves into true reading-order position relative to surrounding text, rather
    than trailing after every text block on the page.

    Args:
        page: A `pymupdf.Page` (typed `Any` — PyMuPDF ships no precise stubs).
        page_index: Zero-based page number, recorded in each `Element`'s metadata.

    Returns:
        One `Element` per non-empty text block not inside a table, plus one `TABLE`
        `Element` per detected table, in top-to-bottom reading order.
    """
    table_elements = _table_elements(page, page_index)
    table_bboxes = [element.metadata["bbox"] for element in table_elements]
    text_elements = [
        element
        for block in page.get_text("dict").get("blocks", [])
        if (element := _block_element(block, page_index)) is not None
        and not _center_in_any_bbox(element.metadata["bbox"], table_bboxes)
    ]
    combined = [*table_elements, *text_elements]
    combined.sort(key=lambda element: _bbox_top(element.metadata.get("bbox")))
    return combined


def _block_element(block: dict[str, Any], page_index: int) -> Element | None:
    """Maps one text block to a `HEADING`/`PARAGRAPH` `Element`, or `None` if empty.

    Args:
        block: One entry of `page.get_text("dict")["blocks"]`.
        page_index: Zero-based page number, recorded in the `Element`'s metadata.

    Returns:
        The mapped `Element`, or `None` if `block` has no extractable text.
    """
    text = _block_text(block)
    if not text.strip():
        return None
    element_type = (
        ElementType.HEADING
        if _block_max_font_size(block) >= _HEADING_FONT_SIZE_THRESHOLD
        else ElementType.PARAGRAPH
    )
    return Element(
        type=element_type,
        text=text,
        metadata={"page": page_index, "bbox": block.get("bbox")},
    )


def _table_elements(page: Any, page_index: int) -> list[Element]:
    """Detects tables on `page` via PyMuPDF's own `find_tables()` API.

    Args:
        page: A `pymupdf.Page`.
        page_index: Zero-based page number, recorded in each `Element`'s metadata.

    Returns:
        One `ElementType.TABLE` `Element` per table PyMuPDF detects on `page`, matching
        `DocumentIntelligenceParser`'s `TABLE` element shape exactly.
    """
    return [_table_element(table, page_index) for table in page.find_tables().tables]


def _table_element(table: Any, page_index: int) -> Element:
    """Maps one detected PyMuPDF table to a `TABLE` `Element` (docint_parser shape).

    Args:
        table: One entry of `page.find_tables().tables` (typed `Any` — PyMuPDF ships
            no precise stubs).
        page_index: Zero-based page number, recorded in the `Element`'s metadata.

    Returns:
        A `TABLE` `Element` with `metadata["cells"]` in the `row_index`/`column_index`/
        `content` shape, `text` a row-major reading-order join of cell content.
    """
    cells = [
        {"row_index": row_index, "column_index": column_index, "content": content}
        for row_index, row in enumerate(table.extract())
        for column_index, content in enumerate(row)
    ]
    return Element(
        type=ElementType.TABLE,
        text="\n".join(str(cell["content"]) for cell in cells),
        metadata={
            "page": page_index,
            "bbox": table.bbox,
            "row_count": table.row_count,
            "column_count": table.col_count,
            "cells": cells,
        },
    )


def _center_in_any_bbox(
    bbox: tuple[float, float, float, float] | None,
    table_bboxes: list[tuple[float, float, float, float]],
) -> bool:
    """True if `bbox`'s center point lies within any of `table_bboxes`.

    A simple bounding-box containment check (house rules: no hand-rolled layout
    algorithm) — sufficient to exclude a table's own text blocks from HEADING/PARAGRAPH
    extraction without misclassifying nearby non-table text.

    Args:
        bbox: A `(x0, y0, x1, y1)` tuple, or `None` if the block carries no bbox.
        table_bboxes: Bounding boxes of tables detected on the same page.

    Returns:
        `True` if `bbox` is not `None` and its center point falls inside any bbox in
        `table_bboxes`.
    """
    if bbox is None:
        return False
    center_x = (bbox[0] + bbox[2]) / 2
    center_y = (bbox[1] + bbox[3]) / 2
    return any(
        table_bbox[0] <= center_x <= table_bbox[2]
        and table_bbox[1] <= center_y <= table_bbox[3]
        for table_bbox in table_bboxes
    )


def _bbox_top(bbox: tuple[float, float, float, float] | None) -> float:
    """The bbox's top `y0` coordinate, or `0.0` if `bbox` is `None` (defensive default)."""
    return bbox[1] if bbox is not None else 0.0


def _block_text(block: dict[str, Any]) -> str:
    """Concatenates a text block's spans into reading-order text, one line per line."""
    lines = [
        "".join(span.get("text", "") for span in line.get("spans", []))
        for line in block.get("lines", [])
    ]
    return "\n".join(lines)


def _block_max_font_size(block: dict[str, Any]) -> float:
    """The largest font size among a text block's spans, or `0.0` if it has none."""
    sizes = [
        float(span.get("size", 0.0))
        for line in block.get("lines", [])
        for span in line.get("spans", [])
    ]
    return max(sizes) if sizes else 0.0
