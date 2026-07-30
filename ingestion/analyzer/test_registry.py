"""Unit tests for ParserAdapter/ParserRegistry (REQ-005)."""

from __future__ import annotations

import io

from ingestion.analyzer.conftest import make_envelope
from ingestion.analyzer.model import BytesReader, DetectedType, Element, ElementType
from ingestion.analyzer.registry import ParserAdapter, ParserRegistry
from ingestion.envelope.model import ArrivalEnvelope

_ENVELOPE = make_envelope("parser-registry")


class _FakeParser(ParserAdapter):
    """A minimal ParserAdapter test double."""

    def parse(
        self, envelope: ArrivalEnvelope, bytes_reader: BytesReader
    ) -> list[Element]:
        return [Element(type=ElementType.PARAGRAPH, text="fake", metadata={})]


def test_parser_registry_register_then_get_returns_parser() -> None:
    registry = ParserRegistry()
    parser = _FakeParser()
    registry.register(DetectedType.DIGITAL_PDF, parser)
    resolved = registry.get(DetectedType.DIGITAL_PDF)
    assert resolved is parser
    assert resolved is not None
    assert resolved.parse(_ENVELOPE, io.BytesIO(b""))[0].text == "fake"


def test_parser_registry_get_missing_returns_none() -> None:
    registry = ParserRegistry()
    assert registry.get(DetectedType.DOCX) is None


def test_parser_registry_register_replaces_prior_parser_for_same_type() -> None:
    registry = ParserRegistry()
    first = _FakeParser()
    second = _FakeParser()
    registry.register(DetectedType.IMAGE, first)
    registry.register(DetectedType.IMAGE, second)
    assert registry.get(DetectedType.IMAGE) is second
