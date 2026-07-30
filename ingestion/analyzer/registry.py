"""Parser adapter contract and registry (REQ-005).

`ParserAdapter` is the thin-wrapper contract every document parser implements (house
rules: "adapters thin" — implementations wrap an underlying library/service, never
implement parsing/OCR algorithms themselves). `ParserRegistry` is the in-process
`DetectedType -> ParserAdapter` lookup `Analyzer` uses to dispatch; swapping or adding a
parser (e.g. a future Docling/Unstructured/VLM adapter, see `benchmarks/README.md`)
requires only a `register()` call, never a change to `Analyzer` itself (story AC6).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ingestion.analyzer.model import BytesReader, DetectedType, Element
from ingestion.envelope.model import ArrivalEnvelope


class ParserAdapter(ABC):
    """Thin wrapper contract for a single-`DetectedType` document parser.

    Implementations wrap an underlying library or managed service, never implementing
    parsing/OCR/layout algorithms themselves.
    """

    @abstractmethod
    def parse(
        self, envelope: ArrivalEnvelope, bytes_reader: BytesReader
    ) -> list[Element]:
        """Parses a document into structured `Element`s.

        Args:
            envelope: The validated `ArrivalEnvelope` for this document.
            bytes_reader: Seekable byte source for the document's content.

        Returns:
            The extracted `Element`s.

        Raises:
            AnalyzerError: With a code from this module's registered set (currently
                `DOCINT_RATE_LIMITED`/`DOCINT_UPSTREAM_ERROR`, raised by
                `DocumentIntelligenceParser`). An unmapped exception may also propagate
                unchanged — `Analyzer` wraps any such exception as `PARSER_INTERNAL`.
        """
        raise NotImplementedError  # pragma: no cover


class ParserRegistry:
    """`DetectedType -> ParserAdapter` lookup. No I/O; a plain in-process dict."""

    def __init__(self) -> None:
        """Initializes an empty registry."""
        self._parsers: dict[DetectedType, ParserAdapter] = {}

    def register(self, detected_type: DetectedType, parser: ParserAdapter) -> None:
        """Registers `parser` as the adapter for `detected_type`.

        A repeat call for the same `detected_type` replaces the previously registered
        parser — there is no immutability guarantee here (unlike
        `ingestion.errors.registry.ErrorRegistry`); this is a plain routing table.

        Args:
            detected_type: The `DetectedType` to route to `parser`.
            parser: The adapter to register.
        """
        self._parsers[detected_type] = parser

    def get(self, detected_type: DetectedType) -> ParserAdapter | None:
        """Looks up the registered parser for `detected_type`.

        Args:
            detected_type: The `DetectedType` to look up.

        Returns:
            The registered `ParserAdapter`, or `None` if nothing is registered.
        """
        return self._parsers.get(detected_type)
