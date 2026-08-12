"""Document Analyzer — Phase 2, first pipeline component (REQ-005)."""

from vestibule.analyzer.analyzer import Analyzer, AnalyzerConfig
from vestibule.analyzer.detect import detect_type
from vestibule.analyzer.model import (
    ANALYZER_NO_PARSER,
    ANALYZER_UNSUPPORTED_TYPE,
    DOCINT_RATE_LIMITED,
    DOCINT_UPSTREAM_ERROR,
    PARSER_INTERNAL,
    PARSER_TIMEOUT,
    AnalyzerError,
    BytesReader,
    DetectedType,
    Element,
    ElementType,
)
from vestibule.analyzer.parsers.docint_parser import DocumentIntelligenceParser
from vestibule.analyzer.parsers.pymupdf_parser import PyMuPDFParser
from vestibule.analyzer.registry import ParserAdapter, ParserRegistry

__all__ = [
    "ANALYZER_NO_PARSER",
    "ANALYZER_UNSUPPORTED_TYPE",
    "DOCINT_RATE_LIMITED",
    "DOCINT_UPSTREAM_ERROR",
    "PARSER_INTERNAL",
    "PARSER_TIMEOUT",
    "Analyzer",
    "AnalyzerConfig",
    "AnalyzerError",
    "BytesReader",
    "DetectedType",
    "DocumentIntelligenceParser",
    "Element",
    "ElementType",
    "ParserAdapter",
    "ParserRegistry",
    "PyMuPDFParser",
    "detect_type",
]
