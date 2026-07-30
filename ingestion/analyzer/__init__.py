"""Document Analyzer — Phase 2, first pipeline component (REQ-005)."""

from ingestion.analyzer.analyzer import Analyzer, AnalyzerConfig
from ingestion.analyzer.detect import detect_type
from ingestion.analyzer.model import (
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
from ingestion.analyzer.parsers.docint_parser import DocumentIntelligenceParser
from ingestion.analyzer.parsers.pymupdf_parser import PyMuPDFParser
from ingestion.analyzer.registry import ParserAdapter, ParserRegistry

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
