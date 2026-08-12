"""Document type detection (REQ-005).

`detect_type` sniffs magic bytes to classify a document — never extension-based, per
the story's explicit requirement. PDFs are further sub-typed by a thin PyMuPDF
open + `get_text` probe of the first few pages: too little extractable text implies a
scanned/image-only PDF that needs OCR (`DocumentIntelligenceParser`) rather than direct
text extraction (`PyMuPDFParser`).

`detect_type` never raises for anything about the *document*: unrecognized, corrupt, or
encrypted input resolves to `DetectedType.UNSUPPORTED`, a return value, not an
exception. `Analyzer` is the sole raise site for `ANALYZER_UNSUPPORTED_TYPE` (LLD §3).

One deliberate, narrow exception (issue #19): `pymupdf` is imported lazily, only inside
the PDF sub-typing probe that needs it, so importing this module never hard-fails if
`pymupdf` is somehow missing (it is a core dependency, but a broken/partial install can
still lack it). A missing `pymupdf` is an *environment* misconfiguration, not a
per-document classification outcome, so it raises `AnalyzerError`
(`ANALYZER_DEPENDENCY_MISSING`, PERMANENT) rather than resolving to `UNSUPPORTED` —
`Analyzer._detect_and_parse` catches this specific case and terminalizes the ledger row,
same as `ANALYZER_UNSUPPORTED_TYPE`/`ANALYZER_NO_PARSER`.
"""

from __future__ import annotations

import logging

from vestibule.analyzer.model import (
    ANALYZER_DEPENDENCY_MISSING,
    AnalyzerError,
    BytesReader,
    DetectedType,
)
from vestibule.envelope.model import ArrivalEnvelope

logger = logging.getLogger(__name__)

_MAGIC_HEADER_BYTES = 16
_PDF_MAGIC = b"%PDF-"
_DOCX_MAGIC = b"PK\x03\x04"
_IMAGE_MAGICS: tuple[bytes, ...] = (
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"\xff\xd8\xff",  # JPEG
    b"GIF87a",  # GIF
    b"GIF89a",  # GIF
)


def detect_type(
    envelope: ArrivalEnvelope,
    bytes_reader: BytesReader,
    *,
    scanned_pdf_probe_pages: int,
    scanned_pdf_min_chars_per_page: int,
) -> DetectedType:
    """Classifies a document's content via magic-byte sniffing, never file extension.

    Args:
        envelope: The validated `ArrivalEnvelope` for this document (used only for
            `doc_id`, e.g. in log context — no source-specific field is read).
        bytes_reader: Seekable byte source positioned anywhere; this function always
            seeks to the start before reading.
        scanned_pdf_probe_pages: Number of leading PDF pages to probe for extractable
            text when sub-typing a PDF.
        scanned_pdf_min_chars_per_page: Threshold below which a PDF's average
            characters-per-page over the probed pages is classified `SCANNED_PDF`
            rather than `DIGITAL_PDF`.

    Returns:
        The detected `DetectedType`. Unrecognized magic bytes, an encrypted PDF, a
        corrupt PDF, or an empty stream all resolve to `DetectedType.UNSUPPORTED`.

    Raises:
        AnalyzerError: `ANALYZER_DEPENDENCY_MISSING` (PERMANENT) if `pymupdf` cannot be
            imported while sub-typing a PDF — the one deliberate exception to this
            function otherwise never raising (module docstring).
    """
    bytes_reader.seek(0)
    header = bytes_reader.read(_MAGIC_HEADER_BYTES)
    if header.startswith(_PDF_MAGIC):
        bytes_reader.seek(0)
        data = bytes_reader.read()
        return _detect_pdf_subtype(
            envelope.doc_id,
            data,
            scanned_pdf_probe_pages,
            scanned_pdf_min_chars_per_page,
        )
    if header.startswith(_DOCX_MAGIC):
        return DetectedType.DOCX
    if any(header.startswith(magic) for magic in _IMAGE_MAGICS):
        return DetectedType.IMAGE
    return DetectedType.UNSUPPORTED


def _detect_pdf_subtype(
    doc_id: str, data: bytes, probe_pages: int, min_chars_per_page: int
) -> DetectedType:
    """Sub-types PDF magic bytes as `DIGITAL_PDF` or `SCANNED_PDF`, or `UNSUPPORTED`.

    Args:
        doc_id: For log context only.
        data: The full PDF byte content.
        probe_pages: Number of leading pages to probe for extractable text.
        min_chars_per_page: Threshold below which the average is classified
            `SCANNED_PDF`.

    Returns:
        `DetectedType.UNSUPPORTED` if the PDF is encrypted, corrupt, or has zero pages;
        otherwise `DetectedType.SCANNED_PDF` or `DetectedType.DIGITAL_PDF` per the
        average-characters-per-page threshold.

    Raises:
        AnalyzerError: `ANALYZER_DEPENDENCY_MISSING` (PERMANENT) if `pymupdf` is not
            importable.
    """
    try:
        import pymupdf
    except ImportError as exc:
        raise AnalyzerError(
            doc_id,
            "pymupdf could not be imported to sub-type this PDF; it is a core "
            "dependency of vestibule — reinstall with `pip install -e .` (or "
            "`pip install vestibule`) to restore it",
            error_code=ANALYZER_DEPENDENCY_MISSING,
        ) from exc

    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass or doc.page_count == 0:
                return DetectedType.UNSUPPORTED
            pages_to_probe = min(probe_pages, doc.page_count)
            total_chars = sum(len(doc[i].get_text()) for i in range(pages_to_probe))
            avg_chars_per_page = total_chars / pages_to_probe
    except (RuntimeError, ValueError) as exc:
        # PyMuPDF raises RuntimeError (incl. its FileDataError subclasses) for corrupt
        # streams and ValueError for encrypted-without-auth access. Both sniffing
        # failures resolve to UNSUPPORTED, per this function's "never raises" contract
        # (LLD §1) — this is a deliberate, documented conversion, not a swallowed bug.
        logger.debug(
            "PDF sub-type probe failed; classifying UNSUPPORTED",
            extra={"doc_id": doc_id, "error": str(exc)},
        )
        return DetectedType.UNSUPPORTED
    if avg_chars_per_page < min_chars_per_page:
        return DetectedType.SCANNED_PDF
    return DetectedType.DIGITAL_PDF
