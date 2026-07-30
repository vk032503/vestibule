"""Document Analyzer orchestrator (REQ-005).

`Analyzer.analyze` owns exactly the `analyzing` stage of Contract #3's pipeline FSM:
type detection, parser dispatch, and the `pending -> analyzing -> {chunking | failed}`
ledger transitions. It owns no parsing logic itself — delegates entirely to
`ParserAdapter` implementations (house rules: "never parse documents ... in-house").

Failure-path/ledger-write contract (LLD §3, §4 — the design-review-fixed core of this
module): the two PERMANENT failure paths (`ANALYZER_UNSUPPORTED_TYPE`,
`ANALYZER_NO_PARSER`) terminalize the ledger row to `Status.FAILED`. The four TRANSIENT
failure paths (`PARSER_TIMEOUT`, `DOCINT_RATE_LIMITED`, `DOCINT_UPSTREAM_ERROR`,
`PARSER_INTERNAL`) perform a same-status `analyzing -> analyzing` self-transition
instead — never `Status.FAILED` — so a redelivered `analyze()` call remains re-enterable
and `attempts` can keep incrementing for a coordinator's "poison after max dequeue"
policy (out of scope here). Conflating the two would strand a `doc_id` at a terminal
`failed` row after a merely-transient failure, defeating REQ-003's retry mechanism.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from typing import NoReturn

from ingestion.analyzer.detect import detect_type
from ingestion.analyzer.model import (
    ANALYZER_NO_PARSER,
    ANALYZER_UNSUPPORTED_TYPE,
    PARSER_INTERNAL,
    PARSER_TIMEOUT,
    AnalyzerError,
    BytesReader,
    DetectedType,
    Element,
)
from ingestion.analyzer.registry import ParserAdapter, ParserRegistry
from ingestion.envelope.model import ArrivalEnvelope
from ingestion.ledger.store import (
    LedgerStore,
    LedgerTransitionInvalid,
    Status,
    is_benign_concurrent_loss,
)

logger = logging.getLogger(__name__)

_DEFAULT_SCANNED_PDF_PROBE_PAGES = 3
_DEFAULT_SCANNED_PDF_MIN_CHARS_PER_PAGE = 50
_DEFAULT_PARSER_TIMEOUT_SECONDS = 240.0


@dataclass(frozen=True)
class AnalyzerConfig:
    """Operational tunables (LLD Assumption A6).

    Code-authoritative defaults, kept in sync with `config/analyzer.yaml` via a
    dedicated test.

    Attributes:
        scanned_pdf_probe_pages: Number of leading PDF pages `detect_type` probes for
            extractable text.
        scanned_pdf_min_chars_per_page: Threshold below which a PDF is classified
            `SCANNED_PDF` rather than `DIGITAL_PDF`.
        parser_timeout_seconds: Maximum time to wait for a single `parser.parse()` call
            before raising `PARSER_TIMEOUT` (LLD Assumption A5).
    """

    scanned_pdf_probe_pages: int = _DEFAULT_SCANNED_PDF_PROBE_PAGES
    scanned_pdf_min_chars_per_page: int = _DEFAULT_SCANNED_PDF_MIN_CHARS_PER_PAGE
    parser_timeout_seconds: float = _DEFAULT_PARSER_TIMEOUT_SECONDS


class Analyzer:
    """Orchestrates type detection, parser dispatch, and the `analyzing` ledger stage."""

    def __init__(
        self, ledger: LedgerStore, registry: ParserRegistry, config: AnalyzerConfig
    ) -> None:
        """Initializes the Analyzer.

        Args:
            ledger: The `LedgerStore` this Analyzer reads/writes `LedgerRow`s through.
            registry: The `ParserRegistry` used to dispatch by `DetectedType`.
            config: Operational tunables.
        """
        self._ledger = ledger
        self._registry = registry
        self._config = config

    def analyze(
        self, envelope: ArrivalEnvelope, bytes_reader: BytesReader
    ) -> list[Element]:
        """Detects type, dispatches to a parser, and advances the ledger accordingly.

        See the LLD's §3 for the full numbered sequence, including the F7/F8
        benign-concurrent-redelivery paths.

        Args:
            envelope: The validated `ArrivalEnvelope` for this document. A `LedgerRow`
                for `envelope.doc_id` must already exist in `Status.PENDING` (created by
                an upstream coordinator, out of scope here).
            bytes_reader: Seekable byte source for the document's content.

        Returns:
            The extracted `Element`s.

        Raises:
            AnalyzerError: `ANALYZER_UNSUPPORTED_TYPE`/`ANALYZER_NO_PARSER` (PERMANENT,
                ledger row ends `Status.FAILED`), or `PARSER_TIMEOUT`/
                `DOCINT_RATE_LIMITED`/`DOCINT_UPSTREAM_ERROR`/`PARSER_INTERNAL`
                (TRANSIENT, ledger row self-transitions and stays `Status.ANALYZING`).
            LedgerTransitionInvalid: Propagated unchanged when the first ledger
                transition fails for a reason `is_benign_concurrent_loss` classifies as
                not benign (unknown `doc_id`, or a row already `Status.FAILED`).
        """
        doc_id = envelope.doc_id
        try:
            self._ledger.transition(
                doc_id, to_status=Status.ANALYZING, stage=Status.ANALYZING
            )
        except LedgerTransitionInvalid as exc:
            if not is_benign_concurrent_loss(
                exc.observed_row, Status.ANALYZING, Status.ANALYZING
            ):
                raise
            # F7 benign: a concurrent duplicate worker already advanced this doc_id past
            # `analyzing`. Re-run detection/parsing (deterministic, Contract #2) but skip
            # every further ledger write — the row is already at or past `chunking`.
            return self._detect_and_parse(doc_id, envelope, bytes_reader)

        elements = self._detect_and_parse(doc_id, envelope, bytes_reader)

        try:
            self._ledger.transition(
                doc_id, to_status=Status.CHUNKING, stage=Status.CHUNKING
            )
        except LedgerTransitionInvalid as exc:
            if not is_benign_concurrent_loss(
                exc.observed_row, Status.CHUNKING, Status.CHUNKING
            ):
                raise
            # F8 benign: treat as success — a concurrent duplicate worker already made
            # this exact edge (or later) happen.
        return elements

    def _detect_and_parse(
        self, doc_id: str, envelope: ArrivalEnvelope, bytes_reader: BytesReader
    ) -> list[Element]:
        """Runs steps 3-5 of the LLD's §3 sequence: detect, dispatch, parse.

        Args:
            doc_id: The document's `doc_id` (ledger key).
            envelope: The validated `ArrivalEnvelope`.
            bytes_reader: Seekable byte source for the document's content.

        Returns:
            The extracted `Element`s.

        Raises:
            AnalyzerError: See `analyze`.
        """
        detected_type = detect_type(
            envelope,
            bytes_reader,
            scanned_pdf_probe_pages=self._config.scanned_pdf_probe_pages,
            scanned_pdf_min_chars_per_page=self._config.scanned_pdf_min_chars_per_page,
        )
        if detected_type is DetectedType.UNSUPPORTED:
            self._terminalize(
                doc_id,
                ANALYZER_UNSUPPORTED_TYPE,
                "document could not be classified as a supported type",
                detected_type,
            )
        parser = self._registry.get(detected_type)
        if parser is None:
            self._terminalize(
                doc_id,
                ANALYZER_NO_PARSER,
                f"no parser registered for detected type {detected_type.value}",
                detected_type,
            )
        return self._parse_with_recovery(
            doc_id, parser, envelope, bytes_reader, detected_type
        )

    def _terminalize(
        self, doc_id: str, error_code: str, reason: str, detected_type: DetectedType
    ) -> NoReturn:
        """F1/F2: marks the ledger row `Status.FAILED` and raises `AnalyzerError`.

        Args:
            doc_id: The document's `doc_id` (ledger key).
            error_code: `ANALYZER_UNSUPPORTED_TYPE` or `ANALYZER_NO_PARSER` (PERMANENT).
            reason: Human-readable failure reason.
            detected_type: The `DetectedType` this failure relates to.

        Raises:
            AnalyzerError: Always.
        """
        self._ledger.transition(
            doc_id,
            to_status=Status.FAILED,
            stage=Status.FAILED,
            error_code=error_code,
            error_message=reason,
        )
        raise AnalyzerError(
            doc_id, reason, error_code=error_code, detected_type=detected_type
        )

    def _parse_with_recovery(
        self,
        doc_id: str,
        parser: ParserAdapter,
        envelope: ArrivalEnvelope,
        bytes_reader: BytesReader,
        detected_type: DetectedType,
    ) -> list[Element]:
        """Calls `parser.parse()` under a per-call timeout, handling F3-F6.

        Wraps the call in a single-worker `ThreadPoolExecutor` (LLD Assumption A5):
        Python cannot forcibly kill a running thread, so on timeout the future is
        abandoned (the parser thread may continue running in the background) while this
        method raises immediately.

        Args:
            doc_id: The document's `doc_id` (ledger key).
            parser: The dispatched `ParserAdapter`.
            envelope: The validated `ArrivalEnvelope`.
            bytes_reader: Seekable byte source for the document's content.
            detected_type: The `DetectedType` dispatched to `parser`.

        Returns:
            The extracted `Element`s.

        Raises:
            AnalyzerError: `PARSER_TIMEOUT`, or whatever code `parser.parse()` itself
                raised, or `PARSER_INTERNAL` for any other exception (all TRANSIENT).
        """
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(parser.parse, envelope, bytes_reader)
            try:
                return future.result(timeout=self._config.parser_timeout_seconds)
            except FuturesTimeoutError:
                reason = f"parser did not return within {self._config.parser_timeout_seconds}s"
                self._self_transition_and_raise(
                    doc_id, PARSER_TIMEOUT, reason, detected_type
                )
            except AnalyzerError as exc:
                self._self_transition_and_raise(
                    doc_id, exc.error_code, exc.reason, detected_type, cause=exc
                )
            except (
                Exception
            ) as exc:  # F6: any unmapped parser exception -> PARSER_INTERNAL.
                logger.exception(
                    "Parser raised an exception not mapped to a declared error code; "
                    "wrapping as PARSER_INTERNAL",
                    extra={"doc_id": doc_id, "detected_type": detected_type.value},
                )
                self._self_transition_and_raise(
                    doc_id, PARSER_INTERNAL, str(exc), detected_type, cause=exc
                )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _self_transition_and_raise(
        self,
        doc_id: str,
        error_code: str,
        reason: str,
        detected_type: DetectedType,
        *,
        cause: BaseException | None = None,
    ) -> NoReturn:
        """F3-F6: same-status `analyzing -> analyzing` self-transition, then raises.

        Args:
            doc_id: The document's `doc_id` (ledger key).
            error_code: One of `PARSER_TIMEOUT`/`DOCINT_RATE_LIMITED`/
                `DOCINT_UPSTREAM_ERROR`/`PARSER_INTERNAL` (all TRANSIENT).
            reason: Human-readable failure reason.
            detected_type: The `DetectedType` this failure relates to.
            cause: The original exception to chain via `raise ... from cause`, if any.

        Raises:
            AnalyzerError: Always.
        """
        self._ledger.transition(
            doc_id,
            to_status=Status.ANALYZING,
            stage=Status.ANALYZING,
            error_code=error_code,
            error_message=reason,
        )
        raise AnalyzerError(
            doc_id, reason, error_code=error_code, detected_type=detected_type
        ) from cause
