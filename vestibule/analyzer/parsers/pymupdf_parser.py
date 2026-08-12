"""PyMuPDFParser — thin ParserAdapter wrapping PyMuPDF (fitz) for DIGITAL_PDF (REQ-005).

Extracts text with page/paragraph boundaries; maps text blocks to
`Element(HEADING | PARAGRAPH, ...)` using PyMuPDF's own block/font-size signals only —
no custom layout algorithm (house rules: "adapters thin", "never ... implement chunking
algorithms" applies equally to layout classification here).
"""

from __future__ import annotations

from typing import Any

import pymupdf

from vestibule.analyzer.model import BytesReader, Element, ElementType
from vestibule.analyzer.registry import ParserAdapter
from vestibule.envelope.model import ArrivalEnvelope

_HEADING_FONT_SIZE_THRESHOLD = 14.0


class PyMuPDFParser(ParserAdapter):
    """Routes for `DetectedType.DIGITAL_PDF`. Thin wrap of PyMuPDF (`fitz`)."""

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
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            for page_index in range(doc.page_count):
                elements.extend(_page_elements(doc[page_index], page_index))
        return elements


def _page_elements(page: Any, page_index: int) -> list[Element]:
    """Extracts one `Element` per non-empty text block on `page`.

    Args:
        page: A `pymupdf.Page` (typed `Any` — PyMuPDF ships no precise stubs).
        page_index: Zero-based page number, recorded in each `Element`'s metadata.

    Returns:
        One `Element` per non-empty text block on `page`, in reading order.
    """
    elements: list[Element] = []
    for block in page.get_text("dict").get("blocks", []):
        text = _block_text(block)
        if not text.strip():
            continue
        element_type = (
            ElementType.HEADING
            if _block_max_font_size(block) >= _HEADING_FONT_SIZE_THRESHOLD
            else ElementType.PARAGRAPH
        )
        elements.append(
            Element(
                type=element_type,
                text=text,
                metadata={"page": page_index, "bbox": block.get("bbox")},
            )
        )
    return elements


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
