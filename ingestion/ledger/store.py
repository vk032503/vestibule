"""State Ledger — Contract #3 canonical definition.

Every document has exactly one ledger row, keyed by ``doc_id`` (REQ-002), tracking its
journey through the pipeline FSM: ``pending -> analyzing -> chunking -> embedding ->
indexing -> indexed | failed``. Every stage transition is recorded (stage, attempts,
error_code, updated_at) — no silent state (CLAUDE.md Contract #3).

This module owns the FSM: the legal-transition table (``_LEGAL_TRANSITIONS``) and its
enforcement (``validate_transition``) live here, once, for every backend to reuse
(``LedgerStore`` is the persistence port; ``InMemoryLedgerStore`` is the only backend in
this phase). ``doc_id`` shape is validated by delegating to
``ingestion.identity.derive.validate_id_format`` (REQ-002) rather than reimplementing it.

Failure taxonomy: every illegal-transition failure in this module raises
``LedgerTransitionInvalid``, classified PERMANENT. See ``is_benign_concurrent_loss`` for
the required caller-side triage before treating a caught instance as a document failure
under Contract #2's at-least-once-delivery assumption (REQ-003 LLD, Round 1/Round 2).
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field

from ingestion.errors.registry import RaggedError, Severity, register_error
from ingestion.identity.derive import validate_id_format


class Status(str, Enum):
    """Pipeline lifecycle states — Contract #3's FSM.

    Fixed, code-defined enum; sole authority for legal values and legal transitions
    (``_LEGAL_TRANSITIONS`` below). Used for both ``LedgerRow.status`` and
    ``LedgerRow.stage`` — see the LLD's Assumption A1.
    """

    PENDING = "pending"
    ANALYZING = "analyzing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    INDEXED = "indexed"
    FAILED = "failed"


_TERMINAL_STATUSES: frozenset[Status] = frozenset({Status.INDEXED, Status.FAILED})

_LEGAL_TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.PENDING: frozenset({Status.ANALYZING, Status.FAILED}),
    Status.ANALYZING: frozenset({Status.CHUNKING, Status.FAILED}),
    Status.CHUNKING: frozenset({Status.EMBEDDING, Status.FAILED}),
    Status.EMBEDDING: frozenset({Status.INDEXING, Status.FAILED}),
    Status.INDEXING: frozenset({Status.INDEXED, Status.FAILED}),
    Status.INDEXED: frozenset(),
    Status.FAILED: frozenset(),
}
"""Authoritative legal-forward-transition table — a literal transcription of the story's
References table. Same-status self-transitions (retry-within-stage, Assumption A2) are
legal for every non-terminal status and are validated separately in ``validate_transition``,
not folded into this dict."""

_FORWARD_ORDER: tuple[Status, ...] = (
    Status.PENDING,
    Status.ANALYZING,
    Status.CHUNKING,
    Status.EMBEDDING,
    Status.INDEXING,
    Status.INDEXED,
)
"""The single linear non-FAILED path through ``_LEGAL_TRANSITIONS``, precomputed for
``is_benign_concurrent_loss``'s reachability check.

``Status.FAILED`` is deliberately absent: it is legal from every non-terminal status, not
part of this single linear path, so it has no well-defined position here. Any code
indexing into this tuple MUST special-case both ``attempted_to_status == Status.FAILED``
and ``observed_row.status == Status.FAILED`` before calling ``.index()`` on either —
``is_benign_concurrent_loss`` does exactly this (design-review Round 2 fix)."""


class LedgerRow(BaseModel):
    """One row per document — Contract #3.

    Immutable snapshot; ``LedgerStore`` methods return a new ``LedgerRow`` on every write,
    never mutate a returned instance in place.

    Attributes:
        doc_id: 64-char lowercase hex identifier, validated via
            ``ingestion.identity.derive.validate_id_format`` (REQ-002).
        status: Current FSM state — drives legal-transition checks.
        stage: Mirror of ``status``; must equal ``status`` on every write (Assumption A1).
        attempts: Attempts made at the current ``(status, stage)``; ``>= 0`` (Assumption A4).
        last_error_code: Most recent error code recorded at this stage, if any.
        last_error_message: Most recent error message recorded at this stage, if any.
        config_version: Opaque; from ``EnvelopeSummary``; set once at ``create()``.
        ingested_at: UTC; set once at ``create()``, never overwritten (AC1, AC5).
        updated_at: UTC; set on every ``create()``/``transition()`` write (AC5).
    """

    model_config = ConfigDict(frozen=True)

    doc_id: str
    status: Status
    stage: Status
    attempts: int = Field(ge=0)
    last_error_code: str | None = None
    last_error_message: str | None = None
    config_version: str
    ingested_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class EnvelopeSummary:
    """Minimal context ``create()`` needs beyond ``doc_id`` itself (Assumption A3).

    Attributes:
        config_version: Opaque config version string, persisted onto ``LedgerRow``.
    """

    config_version: str


class LedgerTransitionInvalid(RaggedError):
    """Raised for any illegal state transition or malformed transition request.

    Always classified PERMANENT at the exception-class level — an illegal transition
    indicates a caller/coordinator bug (wrong stage order, stale ``doc_id``, missing
    ``error_code``) in the general case.

    Concurrency note: under Contract #2's at-least-once-delivery assumption, this same
    exception is also raised for the *losing* caller of a benign, expected concurrent
    ``transition()`` race — that is not, by itself, evidence of corruption. This exception
    carries the row observed at raise time plus the caller's own original request, so
    ``is_benign_concurrent_loss()`` can triage deterministically from the exception alone,
    without a second, TOCTOU-prone ``get(doc_id)`` call.

    Attributes:
        code: Fixed error code, ``"LEDGER_TRANSITION_INVALID"``.
        classification: Fixed classification, always ``"PERMANENT"``.
        doc_id: The ``doc_id`` the caller attempted to transition.
        reason: Human-readable rejection reason.
        observed_row: The current row read at the moment this exception is raised;
            ``None`` only when no row exists for ``doc_id`` at all.
        attempted_to_status: The caller's own original ``to_status`` request.
        attempted_stage: The caller's own original ``stage`` request.
    """

    code: str = "LEDGER_TRANSITION_INVALID"
    classification: Literal["PERMANENT"] = "PERMANENT"

    def __init__(
        self,
        doc_id: str,
        reason: str,
        *,
        observed_row: LedgerRow | None,
        attempted_to_status: Status,
        attempted_stage: Status,
    ) -> None:
        """Initializes the exception. ``code``/``classification`` are fixed, not caller-supplied.

        Args:
            doc_id: The ``doc_id`` the caller attempted to transition.
            reason: Human-readable rejection reason.
            observed_row: Current row at raise time, or ``None`` if no row exists.
            attempted_to_status: The caller's own original ``to_status`` request.
            attempted_stage: The caller's own original ``stage`` request.
        """
        self.doc_id = doc_id
        self.reason = reason
        self.observed_row = observed_row
        self.attempted_to_status = attempted_to_status
        self.attempted_stage = attempted_stage
        super().__init__(f"{doc_id}: {reason}", error_code=self.code)


register_error(
    LedgerTransitionInvalid.code,
    Severity.PERMANENT,
    "illegal or malformed ledger state transition, incl. benign concurrent races "
    "requiring caller-side is_benign_concurrent_loss() triage (REQ-003)",
)


def is_benign_concurrent_loss(
    observed_row: LedgerRow | None,
    attempted_to_status: Status,
    attempted_stage: Status,
) -> bool:
    """Pure, backend-independent triage helper for a caught ``LedgerTransitionInvalid``.

    Intended call pattern: ``is_benign_concurrent_loss(exc.observed_row,
    exc.attempted_to_status, exc.attempted_stage)`` — no extra ``get(doc_id)`` needed, no
    TOCTOU gap. Never raises, for any input.

    Evaluated in this exact order (each step either returns or falls through):
      1. ``attempted_stage != attempted_to_status`` -> ``False`` (malformed request).
      2. ``observed_row is None`` -> ``False`` (unknown ``doc_id`` — nothing to observe).
      3. ``observed_row.status != observed_row.stage`` -> ``False`` (defensive only).
      4. ``attempted_to_status == Status.FAILED`` — resolved here, before any
         ``_FORWARD_ORDER.index()`` call, since ``Status.FAILED`` is deliberately absent
         from ``_FORWARD_ORDER`` and is a legal target from every non-terminal status:
         ``observed_row.status == Status.INDEXED`` -> ``True`` (the document already
         reached the other terminal state via a concurrent winner); any other observed
         status -> ``False``.
      5. (Only reached for non-``FAILED`` ``attempted_to_status``.)
         ``observed_row.status == Status.FAILED`` -> ``False`` (a distinct, separately
         handled outcome — see the LLD's §3 F7 step 2).
      6. Otherwise: ``True`` iff ``_FORWARD_ORDER.index(observed_row.status) >=
         _FORWARD_ORDER.index(attempted_to_status)`` — a concurrent winner already applied
         this exact edge or has since advanced the document further along the same linear
         path (late redelivery).

    Args:
        observed_row: The row observed at the moment ``LedgerTransitionInvalid`` was
            raised, or ``None``.
        attempted_to_status: The losing caller's own original ``to_status`` request.
        attempted_stage: The losing caller's own original ``stage`` request.

    Returns:
        ``True`` if the loss is benign concurrent progress; ``False`` otherwise. A
        ``False`` return does not by itself mean "genuine bug" — see the LLD's §3 F7
        three-step decision tree for the full caller-side triage.
    """
    if attempted_stage != attempted_to_status:
        return False
    if observed_row is None:
        return False
    if observed_row.status != observed_row.stage:
        return False
    if attempted_to_status == Status.FAILED:
        return observed_row.status == Status.INDEXED
    if observed_row.status == Status.FAILED:
        return False
    return _FORWARD_ORDER.index(observed_row.status) >= _FORWARD_ORDER.index(
        attempted_to_status
    )


def _reject(
    doc_id: str,
    reason: str,
    observed_row: LedgerRow | None,
    to_status: Status,
    stage: Status,
) -> NoReturn:
    """Raises ``LedgerTransitionInvalid`` with the given rejection details.

    Args:
        doc_id: The ``doc_id`` the caller attempted to transition.
        reason: Human-readable rejection reason.
        observed_row: Current row at raise time, or ``None``.
        to_status: The caller's own original ``to_status`` request.
        stage: The caller's own original ``stage`` request.

    Raises:
        LedgerTransitionInvalid: Always.
    """
    raise LedgerTransitionInvalid(
        doc_id,
        reason,
        observed_row=observed_row,
        attempted_to_status=to_status,
        attempted_stage=stage,
    )


def validate_transition(
    doc_id: str,
    current: LedgerRow | None,
    to_status: Status,
    stage: Status,
    error_code: str | None,
) -> None:
    """Pure, backend-independent legality check shared by every ``LedgerStore`` backend.

    No I/O, no mutation. Enforces the legal-transition table exactly once so future
    backends call this rather than reimplementing it (house rules' "adapters thin").

    Checks, in order: ``current is not None``; ``stage == to_status``; ``current.status``
    is not terminal; ``to_status`` is in ``_LEGAL_TRANSITIONS[current.status]`` or
    ``to_status == current.status`` (same-status retry, Assumption A2); if
    ``to_status == Status.FAILED``, ``error_code`` is not ``None``.

    Args:
        doc_id: The ``doc_id`` the caller is attempting to transition. Note: not part of
            the LLD's originally-drafted signature — added because the LLD's own mandated
            behavior (raising ``LedgerTransitionInvalid(doc_id, ...)`` from within this
            function, including when ``current is None``) is otherwise unimplementable;
            see this module's implementation notes / the PR description for this flagged
            deviation.
        current: The row currently in the store for ``doc_id``, or ``None`` if none exists.
        to_status: The requested target status.
        stage: The requested stage (must equal ``to_status``, Assumption A1).
        error_code: Error code supplied by the caller, if any.

    Raises:
        LedgerTransitionInvalid: If any of the checks above fails. ``observed_row`` on the
            raised exception is always ``current``, threaded straight through so every
            raise site is triage-ready via ``is_benign_concurrent_loss()``.
    """
    if current is None:
        _reject(doc_id, "unknown doc_id", None, to_status, stage)
    if stage != to_status:
        _reject(doc_id, "stage must equal to_status", current, to_status, stage)
    if current.status in _TERMINAL_STATUSES:
        _reject(
            doc_id, "no transitions out of terminal status", current, to_status, stage
        )
    legal = (
        to_status in _LEGAL_TRANSITIONS[current.status] or to_status == current.status
    )
    if not legal:
        _reject(
            doc_id,
            f"illegal transition from {current.status.value} to {to_status.value}",
            current,
            to_status,
            stage,
        )
    if to_status == Status.FAILED and error_code is None:
        _reject(
            doc_id,
            "error_code required for failed transition",
            current,
            to_status,
            stage,
        )


class LedgerStore(ABC):
    """Persistence port for Contract #3.

    Concrete backends implement all four methods, each calling ``validate_transition()``
    before any write, passing the current in-store row (read under whatever locking the
    backend uses) as ``validate_transition``'s ``current``.
    """

    @abstractmethod
    def create(self, doc_id: str, envelope_summary: EnvelopeSummary) -> LedgerRow:
        """Creates the ledger row for ``doc_id``, idempotently.

        Args:
            doc_id: 64-char lowercase hex identifier (REQ-002).
            envelope_summary: Minimal context persisted onto the new row.

        Returns:
            The (possibly pre-existing) ``LedgerRow`` for ``doc_id``. An existing row is
            returned unchanged (AC1) — every field, including ``ingested_at``, is untouched
            by a repeat call, regardless of what ``envelope_summary`` the repeat call
            carries.

        Raises:
            IdentityInvalid: If ``doc_id`` fails ``validate_id_format`` (REQ-002, PERMANENT).
        """
        raise NotImplementedError

    @abstractmethod
    def transition(
        self,
        doc_id: str,
        to_status: Status,
        stage: Status,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> LedgerRow:
        """Applies a validated state transition and returns the new row.

        Args:
            doc_id: 64-char lowercase hex identifier (REQ-002).
            to_status: Requested target status.
            stage: Requested stage; must equal ``to_status`` (Assumption A1).
            error_code: Error code to record; required if ``to_status == Status.FAILED``. On
                a same-status retry (``to_status == current.status``), passing ``None``
                preserves the row's existing ``last_error_code``/``last_error_message``
                rather than clearing them; passing a value overwrites both (Assumption A5).
            error_message: Optional human-readable error message to record; see
                ``error_code`` for same-status-retry preservation behavior.

        Returns:
            The new ``LedgerRow`` after the transition is applied.

        Raises:
            LedgerTransitionInvalid: If the transition is illegal (PERMANENT). See
                ``is_benign_concurrent_loss()`` for required caller-side triage before
                treating this as a document failure, under concurrent-race conditions.
            IdentityInvalid: If ``doc_id`` fails ``validate_id_format`` (REQ-002, PERMANENT).
        """
        raise NotImplementedError

    @abstractmethod
    def get(self, doc_id: str) -> LedgerRow | None:
        """Returns the current row for ``doc_id``.

        Args:
            doc_id: Identifier to look up. Not shape-validated (Assumption A6), so any
                string input is accepted.

        Returns:
            The current ``LedgerRow``, or ``None`` if unknown (AC8). Never raises.
        """
        raise NotImplementedError

    @abstractmethod
    def list_by_status(self, status: Status, limit: int = 100) -> list[LedgerRow]:
        """Lists rows with the given status, most-recently-updated first.

        Args:
            status: Status to filter on.
            limit: Maximum number of rows to return. ``limit <= 0`` returns ``[]``. The
                literal default (``100``) is the documented, sync-checked counterpart of
                ``config/ledger.yaml``'s ``default_list_limit``.

        Returns:
            Rows with ``status``, ordered by ``updated_at`` descending, capped at ``limit``
            (AC7). Never raises.
        """
        raise NotImplementedError


class InMemoryLedgerStore(LedgerStore):
    """Thread-safe, fully-functional ``LedgerStore`` for tests and local dev.

    Backed by a single ``dict[str, LedgerRow]`` guarded by one ``threading.RLock`` held for
    the full duration of each public method (coarse-grained; correct-under-concurrency, not
    throughput-optimized — acceptable given the in-memory, test/dev-only scope).
    """

    def __init__(self, *, default_list_limit: int = 100) -> None:
        """Initializes an empty in-memory ledger store.

        Args:
            default_list_limit: Accepted for forward compatibility with a future
                composition root that reads ``config/ledger.yaml`` and wires it in
                explicitly. Does not change ``list_by_status``'s own ``limit: int = 100``
                default parameter, which remains the always-effective default for direct
                callers.
        """
        self._default_list_limit = default_list_limit
        self._rows: dict[str, LedgerRow] = {}
        self._lock = threading.RLock()

    def create(self, doc_id: str, envelope_summary: EnvelopeSummary) -> LedgerRow:
        """See ``LedgerStore.create``."""
        validate_id_format(doc_id, field_name="doc_id")
        with self._lock:
            existing = self._rows.get(doc_id)
            if existing is not None:
                return existing
            now = datetime.now(timezone.utc)
            row = LedgerRow(
                doc_id=doc_id,
                status=Status.PENDING,
                stage=Status.PENDING,
                attempts=0,
                config_version=envelope_summary.config_version,
                ingested_at=now,
                updated_at=now,
            )
            self._rows[doc_id] = row
            return row

    def transition(
        self,
        doc_id: str,
        to_status: Status,
        stage: Status,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> LedgerRow:
        """See ``LedgerStore.transition``."""
        validate_id_format(doc_id, field_name="doc_id")
        with self._lock:
            current = self._rows.get(doc_id)
            validate_transition(doc_id, current, to_status, stage, error_code)
            assert current is not None  # validate_transition raises above when None
            new_last_error_code, new_last_error_message = _next_error_fields(
                current, to_status, error_code, error_message
            )
            new_row = LedgerRow(
                doc_id=doc_id,
                status=to_status,
                stage=stage,
                attempts=_next_attempts(current, to_status),
                last_error_code=new_last_error_code,
                last_error_message=new_last_error_message,
                config_version=current.config_version,
                ingested_at=current.ingested_at,
                updated_at=datetime.now(timezone.utc),
            )
            self._rows[doc_id] = new_row
            return new_row

    def get(self, doc_id: str) -> LedgerRow | None:
        """See ``LedgerStore.get``."""
        with self._lock:
            return self._rows.get(doc_id)

    def list_by_status(self, status: Status, limit: int = 100) -> list[LedgerRow]:
        """See ``LedgerStore.list_by_status``."""
        if limit <= 0:
            return []
        with self._lock:
            matches = [row for row in self._rows.values() if row.status == status]
        matches.sort(key=lambda row: row.updated_at, reverse=True)
        return matches[:limit]


def _next_attempts(current: LedgerRow, to_status: Status) -> int:
    """Computes the new ``attempts`` value for a transition, per Assumption A4.

    Args:
        current: The row before the transition is applied.
        to_status: The requested target status.

    Returns:
        ``current.attempts`` unchanged if transitioning to ``Status.FAILED`` (frozen);
        ``current.attempts + 1`` on a same-status retry; ``1`` on a legal forward advance
        into a new non-terminal status (first attempt at that stage).
    """
    if to_status == Status.FAILED:
        return current.attempts
    if to_status == current.status:
        return current.attempts + 1
    return 1


def _next_error_fields(
    current: LedgerRow,
    to_status: Status,
    error_code: str | None,
    error_message: str | None,
) -> tuple[str | None, str | None]:
    """Computes the new ``last_error_code``/``last_error_message`` pair, per Assumption A5.

    Args:
        current: The row before the transition is applied.
        to_status: The requested target status.
        error_code: Error code supplied by the caller, if any.
        error_message: Error message supplied by the caller, if any.

    Returns:
        ``(current.last_error_code, current.last_error_message)`` unchanged when this is a
        same-status retry (``to_status == current.status``) and no new ``error_code`` was
        supplied — the prior error remains visible via ``get()`` rather than being cleared
        by the retry attempt itself. Otherwise, ``(error_code, error_message)`` as supplied
        by the caller — clearing on a clean forward advance with no ``error_code``, or
        overwriting whenever a new ``error_code`` is explicitly supplied.
    """
    if to_status == current.status and error_code is None:
        return current.last_error_code, current.last_error_message
    return error_code, error_message
