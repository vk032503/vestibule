"""Unit tests for Analyzer (REQ-005)."""

from __future__ import annotations

import io
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path

import pytest

import vestibule.analyzer.analyzer as analyzer_module
from vestibule.analyzer.analyzer import Analyzer, AnalyzerConfig
from vestibule.analyzer.conftest import make_envelope
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
from vestibule.analyzer.registry import ParserAdapter, ParserRegistry
from vestibule.envelope.model import ArrivalEnvelope
from vestibule.errors.registry import Severity
from vestibule.ledger.store import (
    EnvelopeSummary,
    InMemoryLedgerStore,
    LedgerTransitionInvalid,
    Status,
)

_SAMPLE_ELEMENTS = [Element(type=ElementType.PARAGRAPH, text="hello", metadata={})]


class _FakeParser(ParserAdapter):
    """Configurable ParserAdapter test double: returns fixed elements, raises, or sleeps."""

    def __init__(
        self,
        elements: list[Element] | None = None,
        *,
        raises: Exception | None = None,
        sleep_seconds: float = 0.0,
    ) -> None:
        self._elements = elements if elements is not None else []
        self._raises = raises
        self._sleep_seconds = sleep_seconds

    def parse(
        self, envelope: ArrivalEnvelope, bytes_reader: BytesReader
    ) -> list[Element]:
        if self._sleep_seconds:
            time.sleep(self._sleep_seconds)
        if self._raises is not None:
            raise self._raises
        return self._elements


class _SideEffectParser(ParserAdapter):
    """A ParserAdapter test double that runs a callback before returning elements.

    Used to simulate a concurrent duplicate worker mutating the ledger row while this
    Analyzer's own `parser.parse()` call is in flight (F8).
    """

    def __init__(
        self, elements: list[Element], side_effect: Callable[[], None]
    ) -> None:
        self._elements = elements
        self._side_effect = side_effect

    def parse(
        self, envelope: ArrivalEnvelope, bytes_reader: BytesReader
    ) -> list[Element]:
        self._side_effect()
        return self._elements


def _build_analyzer(
    parser: ParserAdapter | None,
    *,
    config: AnalyzerConfig | None = None,
    detected_type: DetectedType = DetectedType.DIGITAL_PDF,
) -> tuple[Analyzer, InMemoryLedgerStore]:
    ledger = InMemoryLedgerStore()
    registry = ParserRegistry()
    if parser is not None:
        registry.register(detected_type, parser)
    analyzer = Analyzer(ledger, registry, config or AnalyzerConfig())
    return analyzer, ledger


def _seed_pending(ledger: InMemoryLedgerStore, envelope: ArrivalEnvelope) -> None:
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="v1"))


def _advance_to(ledger: InMemoryLedgerStore, doc_id: str, target: Status) -> None:
    """Advances a freshly-created row through legal forward edges up to `target`."""
    ledger.create(doc_id, EnvelopeSummary(config_version="v1"))
    if target == Status.PENDING:
        return
    for status in (
        Status.ANALYZING,
        Status.CHUNKING,
        Status.EMBEDDING,
        Status.INDEXING,
        Status.INDEXED,
    ):
        ledger.transition(doc_id, status, status)
        if status == target:
            return


def _reader(data: bytes) -> BytesReader:
    return io.BytesIO(data)


# --- happy paths (AC1, AC2, AC6) --------------------------------------------------------


def test_analyzer_analyze_digital_pdf_transitions_pending_to_analyzing_to_chunking(
    digital_pdf_bytes: bytes,
) -> None:
    envelope = make_envelope("digital-pdf-happy-path")
    analyzer, ledger = _build_analyzer(_FakeParser(elements=_SAMPLE_ELEMENTS))
    _seed_pending(ledger, envelope)

    elements = analyzer.analyze(envelope, _reader(digital_pdf_bytes))

    assert elements == _SAMPLE_ELEMENTS
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.CHUNKING


def test_analyzer_analyze_swaps_in_fake_parser_with_no_analyzer_code_changes(
    digital_pdf_bytes: bytes,
) -> None:
    """AC6: registering a FakeParser is the only change needed to swap parsers."""
    envelope = make_envelope("fake-parser-swap")
    fake_elements = [Element(type=ElementType.CODE, text="print(1)", metadata={})]
    analyzer, ledger = _build_analyzer(_FakeParser(elements=fake_elements))
    _seed_pending(ledger, envelope)

    elements = analyzer.analyze(envelope, _reader(digital_pdf_bytes))

    assert elements == fake_elements


def test_analyzer_analyze_scanned_pdf_with_recorded_docint_response_returns_table_element(
    scanned_pdf_bytes: bytes,
) -> None:
    envelope = make_envelope("scanned-pdf-table")
    table_element = Element(
        type=ElementType.TABLE, text="A1", metadata={"row_count": 1, "column_count": 1}
    )
    analyzer, ledger = _build_analyzer(
        _FakeParser(elements=[table_element]), detected_type=DetectedType.SCANNED_PDF
    )
    _seed_pending(ledger, envelope)

    elements = analyzer.analyze(envelope, _reader(scanned_pdf_bytes))

    assert any(element.type == ElementType.TABLE for element in elements)
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.CHUNKING


# --- F1/F2 PERMANENT failure paths (AC3, AC4) -------------------------------------------


def test_analyzer_analyze_unsupported_type_raises_analyzer_error_permanent_ledger_ends_failed(
    non_pdf_bytes_disguised_as_pdf: bytes,
) -> None:
    envelope = make_envelope("unsupported-type")
    analyzer, ledger = _build_analyzer(_FakeParser())
    _seed_pending(ledger, envelope)

    with pytest.raises(AnalyzerError) as exc_info:
        analyzer.analyze(envelope, _reader(non_pdf_bytes_disguised_as_pdf))

    assert exc_info.value.error_code == ANALYZER_UNSUPPORTED_TYPE
    assert exc_info.value.severity == Severity.PERMANENT
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.FAILED
    assert row.last_error_code == ANALYZER_UNSUPPORTED_TYPE


def test_analyzer_analyze_no_registered_parser_raises_analyzer_error_permanent_ledger_ends_failed(
    digital_pdf_bytes: bytes,
) -> None:
    envelope = make_envelope("no-parser")
    analyzer, ledger = _build_analyzer(None)
    _seed_pending(ledger, envelope)

    with pytest.raises(AnalyzerError) as exc_info:
        analyzer.analyze(envelope, _reader(digital_pdf_bytes))

    assert exc_info.value.error_code == ANALYZER_NO_PARSER
    assert exc_info.value.severity == Severity.PERMANENT
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.FAILED
    assert row.last_error_code == ANALYZER_NO_PARSER


# --- F3-F6 TRANSIENT failure paths (AC5) ------------------------------------------------


def test_analyzer_analyze_parser_timeout_raises_parser_timeout_transient_ledger_self_transitions_analyzing(
    digital_pdf_bytes: bytes,
) -> None:
    envelope = make_envelope("parser-timeout")
    config = AnalyzerConfig(parser_timeout_seconds=0.05)
    analyzer, ledger = _build_analyzer(_FakeParser(sleep_seconds=0.5), config=config)
    _seed_pending(ledger, envelope)

    with pytest.raises(AnalyzerError) as exc_info:
        analyzer.analyze(envelope, _reader(digital_pdf_bytes))

    assert exc_info.value.error_code == PARSER_TIMEOUT
    assert exc_info.value.severity == Severity.TRANSIENT
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.ANALYZING
    assert row.attempts == 2
    assert row.last_error_code == PARSER_TIMEOUT


def test_analyzer_analyze_docint_429_raises_docint_rate_limited_transient_ledger_self_transitions_analyzing(
    digital_pdf_bytes: bytes,
) -> None:
    envelope = make_envelope("docint-429")
    raised = AnalyzerError(
        envelope.doc_id, "rate limited", error_code=DOCINT_RATE_LIMITED
    )
    analyzer, ledger = _build_analyzer(_FakeParser(raises=raised))
    _seed_pending(ledger, envelope)

    with pytest.raises(AnalyzerError) as exc_info:
        analyzer.analyze(envelope, _reader(digital_pdf_bytes))

    assert exc_info.value.error_code == DOCINT_RATE_LIMITED
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.ANALYZING
    assert row.attempts == 2
    assert row.last_error_code == DOCINT_RATE_LIMITED


def test_analyzer_analyze_docint_5xx_raises_docint_upstream_error_transient_ledger_self_transitions_analyzing(
    digital_pdf_bytes: bytes,
) -> None:
    envelope = make_envelope("docint-5xx")
    raised = AnalyzerError(
        envelope.doc_id, "upstream error", error_code=DOCINT_UPSTREAM_ERROR
    )
    analyzer, ledger = _build_analyzer(_FakeParser(raises=raised))
    _seed_pending(ledger, envelope)

    with pytest.raises(AnalyzerError) as exc_info:
        analyzer.analyze(envelope, _reader(digital_pdf_bytes))

    assert exc_info.value.error_code == DOCINT_UPSTREAM_ERROR
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.ANALYZING
    assert row.attempts == 2
    assert row.last_error_code == DOCINT_UPSTREAM_ERROR


def test_analyzer_analyze_unmapped_parser_exception_wrapped_as_parser_internal_transient_ledger_self_transitions_analyzing(
    digital_pdf_bytes: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    envelope = make_envelope("parser-internal")
    analyzer, ledger = _build_analyzer(
        _FakeParser(raises=RuntimeError("pymupdf exploded"))
    )
    _seed_pending(ledger, envelope)

    with caplog.at_level(logging.ERROR, logger="vestibule.analyzer.analyzer"):
        with pytest.raises(AnalyzerError) as exc_info:
            analyzer.analyze(envelope, _reader(digital_pdf_bytes))

    assert exc_info.value.error_code == PARSER_INTERNAL
    assert "pymupdf exploded" in caplog.text
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.ANALYZING
    assert row.attempts == 2
    assert row.last_error_code == PARSER_INTERNAL


def test_analyzer_analyze_transient_failure_then_redelivery_increments_attempts_without_reaching_failed(
    digital_pdf_bytes: bytes,
) -> None:
    """Two consecutive analyze() calls, each hitting PARSER_TIMEOUT: attempts strictly
    increases and status never reaches Status.FAILED (the row stays re-enterable).

    Each failing call performs two ledger writes once past the initial PENDING->ANALYZING
    forward advance: step 2's own entry self-transition (LLD §3 step 2's note — a
    redelivered call's `to_status == current.status` is itself a legal self-transition,
    bumping attempts before parsing even starts), and the F3 failure self-transition. Both
    keep the row at Status.ANALYZING; neither ever writes Status.FAILED.
    """
    envelope = make_envelope("transient-redelivery")
    config = AnalyzerConfig(parser_timeout_seconds=0.05)
    analyzer, ledger = _build_analyzer(_FakeParser(sleep_seconds=0.5), config=config)
    _seed_pending(ledger, envelope)

    with pytest.raises(AnalyzerError):
        analyzer.analyze(envelope, _reader(digital_pdf_bytes))
    row1 = ledger.get(envelope.doc_id)
    assert row1 is not None
    assert row1.status == Status.ANALYZING
    assert (
        row1.attempts == 2
    )  # PENDING->ANALYZING (reset to 1) then F3 self-transition (+1)

    with pytest.raises(AnalyzerError):
        analyzer.analyze(envelope, _reader(digital_pdf_bytes))
    row2 = ledger.get(envelope.doc_id)
    assert row2 is not None
    assert row2.status == Status.ANALYZING
    assert row2.attempts > row1.attempts  # step 2 entry self-transition, then F3 again


_CONTRAST_SCENARIOS: dict[str, tuple[str, Status]] = {
    "unsupported": (ANALYZER_UNSUPPORTED_TYPE, Status.FAILED),
    "no_parser": (ANALYZER_NO_PARSER, Status.FAILED),
    "timeout": (PARSER_TIMEOUT, Status.ANALYZING),
    "docint_429": (DOCINT_RATE_LIMITED, Status.ANALYZING),
    "docint_5xx": (DOCINT_UPSTREAM_ERROR, Status.ANALYZING),
    "parser_internal": (PARSER_INTERNAL, Status.ANALYZING),
}


def _build_contrast_scenario(
    scenario: str, envelope: ArrivalEnvelope, digital_pdf_bytes: bytes
) -> tuple[Analyzer, InMemoryLedgerStore, bytes]:
    """Builds the (analyzer, ledger, bytes) triple for one contrast-test scenario."""
    config = AnalyzerConfig(parser_timeout_seconds=0.05)
    if scenario == "unsupported":
        return *_build_analyzer(_FakeParser()), b"not a pdf at all"
    if scenario == "no_parser":
        return *_build_analyzer(None), digital_pdf_bytes
    if scenario == "timeout":
        return *_build_analyzer(
            _FakeParser(sleep_seconds=0.5), config=config
        ), digital_pdf_bytes
    if scenario == "docint_429":
        raised = AnalyzerError(envelope.doc_id, "rl", error_code=DOCINT_RATE_LIMITED)
        return *_build_analyzer(_FakeParser(raises=raised)), digital_pdf_bytes
    if scenario == "docint_5xx":
        raised = AnalyzerError(envelope.doc_id, "5xx", error_code=DOCINT_UPSTREAM_ERROR)
        return *_build_analyzer(_FakeParser(raises=raised)), digital_pdf_bytes
    raised_internal = RuntimeError("boom")
    return *_build_analyzer(_FakeParser(raises=raised_internal)), digital_pdf_bytes


@pytest.mark.parametrize("scenario", sorted(_CONTRAST_SCENARIOS))
def test_analyzer_analyze_permanent_vs_transient_ledger_terminalization_contrast(
    scenario: str, digital_pdf_bytes: bytes
) -> None:
    envelope = make_envelope(f"contrast-{scenario}")
    expected_code, expected_status = _CONTRAST_SCENARIOS[scenario]
    analyzer, ledger, data = _build_contrast_scenario(
        scenario, envelope, digital_pdf_bytes
    )
    _seed_pending(ledger, envelope)

    with pytest.raises(AnalyzerError) as exc_info:
        analyzer.analyze(envelope, _reader(data))

    assert exc_info.value.error_code == expected_code
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == expected_status


# --- F7/F8 benign-concurrent-redelivery paths (Assumption A2) --------------------------


def test_analyzer_analyze_redelivered_after_prior_success_is_benign_noop(
    digital_pdf_bytes: bytes,
) -> None:
    envelope = make_envelope("redelivered-past-analyzing")
    analyzer, ledger = _build_analyzer(_FakeParser(elements=_SAMPLE_ELEMENTS))
    _advance_to(ledger, envelope.doc_id, Status.CHUNKING)
    before = ledger.get(envelope.doc_id)

    elements = analyzer.analyze(envelope, _reader(digital_pdf_bytes))

    assert elements == _SAMPLE_ELEMENTS
    after = ledger.get(envelope.doc_id)
    assert after == before


def test_analyzer_analyze_step6_chunking_transition_benign_concurrent_advance_treated_as_success(
    digital_pdf_bytes: bytes,
) -> None:
    """F8 benign: a concurrent duplicate worker finishes the whole pipeline while this
    Analyzer's own parser.parse() call is still in flight; step 6's own CHUNKING write
    then loses the race but is treated as success, not re-raised."""
    envelope = make_envelope("f8-benign-concurrent-advance")
    ledger = InMemoryLedgerStore()
    registry = ParserRegistry()

    def _advance_past_chunking() -> None:
        ledger.transition(envelope.doc_id, Status.CHUNKING, Status.CHUNKING)
        ledger.transition(envelope.doc_id, Status.EMBEDDING, Status.EMBEDDING)
        ledger.transition(envelope.doc_id, Status.INDEXING, Status.INDEXING)
        ledger.transition(envelope.doc_id, Status.INDEXED, Status.INDEXED)

    registry.register(
        DetectedType.DIGITAL_PDF,
        _SideEffectParser(_SAMPLE_ELEMENTS, _advance_past_chunking),
    )
    analyzer = Analyzer(ledger, registry, AnalyzerConfig())
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="v1"))

    elements = analyzer.analyze(envelope, _reader(digital_pdf_bytes))

    assert elements == _SAMPLE_ELEMENTS
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.INDEXED


def test_analyzer_analyze_step6_chunking_transition_non_benign_concurrent_failure_propagates_unchanged(
    digital_pdf_bytes: bytes,
) -> None:
    """F8 non-benign: a concurrent path independently marks the row FAILED while this
    Analyzer's own parser.parse() call is still in flight; step 6's CHUNKING write then
    fails non-benignly and LedgerTransitionInvalid propagates unchanged."""
    envelope = make_envelope("f8-non-benign-concurrent-failure")
    ledger = InMemoryLedgerStore()
    registry = ParserRegistry()

    def _fail_concurrently() -> None:
        ledger.transition(
            envelope.doc_id,
            Status.FAILED,
            Status.FAILED,
            error_code="CONCURRENT_FAILURE",
        )

    registry.register(
        DetectedType.DIGITAL_PDF,
        _SideEffectParser(_SAMPLE_ELEMENTS, _fail_concurrently),
    )
    analyzer = Analyzer(ledger, registry, AnalyzerConfig())
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="v1"))

    with pytest.raises(LedgerTransitionInvalid) as exc_info:
        analyzer.analyze(envelope, _reader(digital_pdf_bytes))

    assert exc_info.value.observed_row is not None
    assert exc_info.value.observed_row.status == Status.FAILED


def test_analyzer_analyze_redelivered_while_still_analyzing_is_plain_self_transition_not_f7(
    digital_pdf_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope = make_envelope("redelivered-still-analyzing")
    analyzer, ledger = _build_analyzer(_FakeParser(elements=_SAMPLE_ELEMENTS))
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="v1"))
    ledger.transition(
        envelope.doc_id, Status.ANALYZING, Status.ANALYZING
    )  # attempts == 1

    calls: list[object] = []
    original = analyzer_module.is_benign_concurrent_loss  # type: ignore[attr-defined]

    def _tracking_is_benign(*args: object, **kwargs: object) -> bool:
        calls.append(args)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        analyzer_module, "is_benign_concurrent_loss", _tracking_is_benign
    )

    elements = analyzer.analyze(envelope, _reader(digital_pdf_bytes))

    assert elements == _SAMPLE_ELEMENTS
    assert (
        calls == []
    )  # step 2's same-status retry never raises, so triage is never invoked
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.CHUNKING
    assert row.attempts == 1  # reset on the forward advance to CHUNKING


def test_analyzer_analyze_ledger_transition_invalid_non_benign_unknown_doc_id_propagates_unchanged(
    digital_pdf_bytes: bytes,
) -> None:
    envelope = make_envelope("unknown-doc-id")
    analyzer, _ledger = _build_analyzer(_FakeParser(elements=_SAMPLE_ELEMENTS))
    # deliberately no create(): no ledger row exists for this doc_id.

    with pytest.raises(LedgerTransitionInvalid) as exc_info:
        analyzer.analyze(envelope, _reader(digital_pdf_bytes))

    assert exc_info.value.observed_row is None


def test_analyzer_analyze_ledger_transition_invalid_non_benign_already_failed_propagates_unchanged(
    digital_pdf_bytes: bytes,
) -> None:
    envelope = make_envelope("already-failed")
    analyzer, ledger = _build_analyzer(_FakeParser(elements=_SAMPLE_ELEMENTS))
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="v1"))
    ledger.transition(
        envelope.doc_id, Status.FAILED, Status.FAILED, error_code="SOME_PRIOR_FAILURE"
    )

    with pytest.raises(LedgerTransitionInvalid) as exc_info:
        analyzer.analyze(envelope, _reader(digital_pdf_bytes))

    assert exc_info.value.observed_row is not None
    assert exc_info.value.observed_row.status == Status.FAILED


# --- config/code sync -------------------------------------------------------------------


def test_config_analyzer_yaml_tunables_match_analyzer_config_defaults() -> None:
    config_path = Path(__file__).resolve().parents[2] / "config" / "analyzer.yaml"
    text = config_path.read_text(encoding="utf-8")
    defaults = AnalyzerConfig()

    def _int_value(key: str) -> int:
        match = re.search(rf"{key}:\s*(\d+)", text)
        assert match is not None
        return int(match.group(1))

    assert _int_value("scanned_pdf_probe_pages") == defaults.scanned_pdf_probe_pages
    assert (
        _int_value("scanned_pdf_min_chars_per_page")
        == defaults.scanned_pdf_min_chars_per_page
    )
    assert _int_value("parser_timeout_seconds") == int(defaults.parser_timeout_seconds)
