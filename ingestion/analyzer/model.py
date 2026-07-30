"""Document Analyzer — data model, exceptions, and error-code registration (REQ-005).

Defines the shapes every parser adapter and the `Analyzer` orchestrator (`analyzer.py`)
operate on: `DetectedType`/`ElementType` (fixed enums), `Element` (adapter-output-only,
immutable), `BytesReader` (the minimal seekable-byte-source Protocol parsers and
`detect_type` need), and `AnalyzerError` (the single exception type this module raises,
carrying a caller-supplied `error_code`).

Failure taxonomy: this module registers all six of this REQ's error codes at import
time (Contract #4) — two PERMANENT (`ANALYZER_UNSUPPORTED_TYPE`, `ANALYZER_NO_PARSER`)
and four TRANSIENT (`PARSER_TIMEOUT`, `DOCINT_RATE_LIMITED`, `DOCINT_UPSTREAM_ERROR`,
`PARSER_INTERNAL`). See `analyzer.py` for how each is raised and how the ledger is
updated on each.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from ingestion.errors.registry import RaggedError, Severity, register_error

ANALYZER_UNSUPPORTED_TYPE = "ANALYZER_UNSUPPORTED_TYPE"
ANALYZER_NO_PARSER = "ANALYZER_NO_PARSER"
PARSER_TIMEOUT = "PARSER_TIMEOUT"
DOCINT_RATE_LIMITED = "DOCINT_RATE_LIMITED"
DOCINT_UPSTREAM_ERROR = "DOCINT_UPSTREAM_ERROR"
PARSER_INTERNAL = "PARSER_INTERNAL"


class DetectedType(str, Enum):
    """Sole authority for accepted detected-type values (LLD Assumption A7)."""

    DIGITAL_PDF = "digital_pdf"
    SCANNED_PDF = "scanned_pdf"
    DOCX = "docx"
    IMAGE = "image"
    UNSUPPORTED = "unsupported"


class ElementType(str, Enum):
    """Sole authority for accepted structured-element kinds."""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    LIST = "list"
    IMAGE_CAPTION = "image_caption"
    CODE = "code"


@dataclass(frozen=True)
class Element:
    """One structured unit extracted from a document.

    Immutable, adapter-output-only — never constructed by `Analyzer` itself, only by
    `ParserAdapter` implementations.

    Attributes:
        type: The kind of structured unit this element represents.
        text: The extracted text content.
        metadata: Adapter-specific context (page number, bbox, row/col for tables, etc.).
        confidence: Populated by OCR/layout adapters; `None` for deterministic-text
            parsers (e.g. `PyMuPDFParser`).
    """

    type: ElementType
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    confidence: float | None = None


class BytesReader(Protocol):
    """Minimal seekable-byte-source interface (LLD Assumption A1).

    Satisfied by `io.BytesIO`, a wrapped blob stream, or any equivalent. No parsing
    semantics of its own — pure I/O surface, sufficient for magic-byte sniffing and
    PDF page probing.
    """

    def read(self, size: int = -1) -> bytes:
        """Reads up to `size` bytes, or the remainder of the stream if `size < 0`."""
        ...

    def seek(self, offset: int, whence: int = 0) -> int:
        """Seeks to `offset` relative to `whence`, returning the new absolute position."""
        ...


class AnalyzerError(RaggedError):
    """Single exception type for every Analyzer-raised failure.

    Unlike REQ-004's fixed-code-per-class subclasses, `error_code` varies per raise
    site (LLD Assumption A3) — one `AnalyzerError` class, six possible `error_code`
    values.

    Attributes:
        doc_id: The `doc_id` of the document being analyzed when this error occurred.
        reason: Human-readable failure reason.
        detected_type: The `DetectedType` this failure relates to, if known/relevant
            (populated for `ANALYZER_UNSUPPORTED_TYPE`/`ANALYZER_NO_PARSER`); `None`
            otherwise.
    """

    def __init__(
        self,
        doc_id: str,
        reason: str,
        *,
        error_code: str,
        detected_type: DetectedType | None = None,
    ) -> None:
        """Initializes the exception.

        Args:
            doc_id: The `doc_id` of the document being analyzed.
            reason: Human-readable failure reason.
            error_code: One of this module's six registered error codes.
            detected_type: The `DetectedType` this failure relates to, if relevant.
        """
        self.doc_id = doc_id
        self.reason = reason
        self.detected_type = detected_type
        super().__init__(f"{doc_id}: {reason}", error_code=error_code)


register_error(
    ANALYZER_UNSUPPORTED_TYPE,
    Severity.PERMANENT,
    "detect_type could not classify the document as a supported type: unrecognized "
    "magic bytes, an encrypted PDF, or a corrupt file (REQ-005)",
)
register_error(
    ANALYZER_NO_PARSER,
    Severity.PERMANENT,
    "a supported DetectedType has no ParserAdapter registered against it in the "
    "ParserRegistry — a registry misconfiguration (REQ-005)",
)
register_error(
    PARSER_TIMEOUT,
    Severity.TRANSIENT,
    "a ParserAdapter.parse() call did not return within parser_timeout_seconds "
    "(REQ-005)",
)
register_error(
    DOCINT_RATE_LIMITED,
    Severity.TRANSIENT,
    "Azure Document Intelligence returned HTTP 429 (REQ-005)",
)
register_error(
    DOCINT_UPSTREAM_ERROR,
    Severity.TRANSIENT,
    "Azure Document Intelligence returned an HTTP 5xx error (REQ-005)",
)
register_error(
    PARSER_INTERNAL,
    Severity.TRANSIENT,
    "a ParserAdapter raised an exception not mapped to any other declared Analyzer "
    "error code; the original exception is logged (REQ-005)",
)
