"""PyMuPDFParser — thin ParserAdapter wrapping PyMuPDF (fitz) for DIGITAL_PDF (REQ-005).

Extracts text with page/paragraph boundaries; maps text blocks to
`Element(HEADING | PARAGRAPH, ...)` using PyMuPDF's own block/font-size signals only —
no custom layout algorithm (house rules: "adapters thin", "never ... implement chunking
algorithms" applies equally to layout classification here).

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
