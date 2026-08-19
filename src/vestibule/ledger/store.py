"""State Ledger — Contract #3 canonical definition.

Every document has exactly one ledger row, keyed by ``doc_id`` (REQ-002), tracking its
journey through the pipeline FSM: ``pending -> analyzing -> chunking -> embedding ->
indexing -> indexed | failed``. Every stage transition is recorded (stage, attempts,
error_code, updated_at) — no silent state (CLAUDE.md Contract #3).

REQ-009 (Vertical Loader) additively extends this FSM with ``Status.NEEDS_REVIEW`` and
its transitions (``pending -> needs_review``, ``needs_review -> pending``,
``needs_review -> failed``) — a document whose vertical/scenario cannot be confidently
determined is parked for human review rather than guessed at. This is a real, in-place
edit to this already-merged module's transition table, not new code in a new module; see
``docs/designs/REQ-003-lld.md``'s ``## Revision note`` (third entry) and
``docs/stories/REQ-009.md`` for the full rationale. ``NEEDS_REVIEW`` is deliberately
**not** added to ``_TERMINAL_STATUSES`` — a parked document can still return to the
pipeline, which is the entire point of a review queue.

REQ-009 additionally extends ``LedgerRow`` with ``assigned_scenario_id`` (and threads a
matching optional ``assigned_scenario_id`` keyword through ``LedgerStore.transition()``)
so a human's ``vestibule.vertical.review_queue.ReviewQueue.assign()`` decision is recorded
durably on the row itself, rather than nowhere — see ``LedgerRow``'s own docstring for
this field's lifecycle and ``transition()``'s docstring for how every other, unrelated
caller in this codebase (Analyzer, Chunker, Embedder, Indexer, ``VerticalLoader`` itself)
leaves it untouched by omitting the parameter.

This module owns the FSM: the legal-transition table (``_LEGAL_TRANSITIONS``) and its
enforcement (``validate_transition``) live here, once, for every backend to reuse
(``LedgerStore`` is the persistence port; ``InMemoryLedgerStore`` is the only backend in
this phase). ``doc_id`` shape is validated by delegating to
``vestibule.identity.derive.validate_id_format`` (REQ-002) rather than reimplementing it.

Failure taxonomy: every illegal-transition failure in this module raises
``LedgerTransitionInvalid``, classified PERMANENT. See ``is_benign_concurrent_loss`` for
the required caller-side triage before treating a caught instance as a document failure
under Contract #2's at-least-once-delivery assumption (REQ-003 LLD, Round 1/Round 2;
Round-2-equivalent fix repeated for ``Status.NEEDS_REVIEW`` by REQ-009, since it is
absent from ``_FORWARD_ORDER`` for the same structural reason ``Status.FAILED`` is).

Round 4 (REQ-012, Poison Queue Reprocessor): additively extends this FSM a second time,
the same way REQ-009 did, with ``Status.ARCHIVED`` and two new outgoing edges from
``Status.FAILED``: ``failed -> pending`` (requeue) and ``failed -> archived`` (archive).
Before this change, ``Status.FAILED`` was in every practical sense fully terminal:
``_TERMINAL_STATUSES`` already included it (alongside ``Status.INDEXED``), and
``_LEGAL_TRANSITIONS[Status.FAILED]`` was ``frozenset()`` — no edge out of ``failed`` was
legal, full stop. That is genuinely different from ``Status.NEEDS_REVIEW``'s situation:
``NEEDS_REVIEW`` was never terminal (deliberately excluded from ``_TERMINAL_STATUSES``
from the start) — REQ-009 gave it its two return edges without touching terminality at
all. REQ-012 instead re-opens a status that *was* fully terminal, which
``validate_transition``'s original "``current.status in _TERMINAL_STATUSES`` -> always
reject" check could not accommodate as written: that check ran unconditionally, before
the legal-transitions-table lookup, so simply adding ``Status.FAILED: frozenset({PENDING,
ARCHIVED})`` to ``_LEGAL_TRANSITIONS`` would have had no effect — the terminal check would
still reject both edges before the table was ever consulted. ``validate_transition`` is
therefore changed (this revision) to reject a terminal-status transition only when
``to_status`` is not itself one of that status's own explicitly-legal targets — so
``Status.INDEXED``/``Status.ARCHIVED`` (both still mapped to ``frozenset()``) remain fully
blocked exactly as before, while ``Status.FAILED`` now permits precisely its two new
named edges and nothing else (in particular, ``failed -> failed`` self-retry is still
rejected — ``Status.FAILED`` is deliberately absent from its own legal-target set, unlike
non-terminal statuses' generic same-status-retry allowance).

Crucially, **the exit from ``failed`` is real at the table level but is a human-triggered
exception path, not a pipeline one**: ``validate_transition``/``LedgerStore.transition()``
have no notion of caller identity, so they cannot themselves distinguish "an operator
called this via ``vestibule.reprocessor.reprocessor.Reprocessor.requeue()``/``archive()``"
from "pipeline code called this" — both would be equally legal at this layer. The
boundary is enforced entirely by convention: only ``Reprocessor.requeue()``/``archive()``
are permitted, anywhere in this codebase, to call ``ledger.transition(...,
to_status=Status.PENDING)``/``to_status=Status.ARCHIVED`` on a row that is currently
``failed``. No pipeline component (``Analyzer``, ``Chunker``, ``Embedder``, ``Indexer``,
``VerticalLoader``) does this today, and none may be changed to. See
``src/vestibule/ledger/test_store.py``'s dedicated regression guard for what can and
cannot actually be mechanically tested about this boundary, and
``vestibule.reprocessor.reprocessor`` for the only legitimate callers.

``Status.ARCHIVED`` itself is a genuinely new terminal status (added to
``_TERMINAL_STATUSES``, mapped to ``frozenset()`` in ``_LEGAL_TRANSITIONS`` exactly like
``Status.INDEXED``) — reachable only from ``failed``, and permitting no further
transitions at all, including no self-retry. ``is_benign_concurrent_loss`` gains a
matching Round-4 special case for ``Status.ARCHIVED``, for the identical structural
reason ``Status.FAILED``/``Status.NEEDS_REVIEW`` already have one: ``Status.ARCHIVED`` is
absent from ``_FORWARD_ORDER`` (it has no position on the ordinary pipeline's linear
path), so it must be resolved before any ``_FORWARD_ORDER.index()`` call touches it — see
that function's own docstring for the exact new steps. See
``docs/designs/REQ-003-lld.md``'s ``## Revision note`` (fourth entry) and
``docs/stories/REQ-012.md`` for the full rationale.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field

from vestibule.errors.registry import RaggedError, Severity, register_error
from vestibule.identity.derive import validate_id_format


class Status(str, Enum):
    """Pipeline lifecycle states — Contract #3's FSM.

    Fixed, code-defined enum; sole authority for legal values and legal transitions
    (``_LEGAL_TRANSITIONS`` below). Used for both ``LedgerRow.status`` and
    ``LedgerRow.stage`` — see the LLD's Assumption A1.

    ``NEEDS_REVIEW`` (REQ-009): a document parked for human review because its
    vertical/scenario could not be confidently determined. Reachable only from
    ``PENDING``; returns to either ``PENDING`` (a human assigned a scenario) or
    ``FAILED`` (a human rejected it). Deliberately non-terminal — a parked document can
    still return to the pipeline, which is the entire point of a review queue. See this
    module's own docstring and ``docs/stories/REQ-009.md``.

    ``ARCHIVED`` (REQ-012): terminal. Reachable only from ``FAILED``, via a human's
    explicit ``vestibule.reprocessor.reprocessor.Reprocessor.archive()`` call — for a
    failed document that will never succeed and should stop appearing in
    ``Reprocessor.list_failed()``. No transition out of ``ARCHIVED`` is ever legal
    (``_LEGAL_TRANSITIONS[Status.ARCHIVED] == frozenset()``, same as ``INDEXED``). See
    this module's own docstring's "Round 4" entry and ``docs/stories/REQ-012.md``.
    """

    PENDING = "pending"
    ANALYZING = "analyzing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    INDEXED = "indexed"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    ARCHIVED = "archived"


_TERMINAL_STATUSES: frozenset[Status] = frozenset(
    {Status.INDEXED, Status.FAILED, Status.ARCHIVED}
)
"""``Status.FAILED`` remains here after REQ-012 — it is still rejected for self-retry and
every mid-pipeline target; it merely gains two explicitly-named legal exits, checked
against ``_LEGAL_TRANSITIONS`` before this set is consulted (see
``validate_transition``). ``Status.ARCHIVED`` (REQ-012) joins as a second, ordinary fully-
closed terminal status, exactly like ``Status.INDEXED`` — no exceptions carved out for it
at all."""

_LEGAL_TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.PENDING: frozenset({Status.ANALYZING, Status.FAILED, Status.NEEDS_REVIEW}),
    Status.ANALYZING: frozenset({Status.CHUNKING, Status.FAILED}),
    Status.CHUNKING: frozenset({Status.EMBEDDING, Status.FAILED}),
    Status.EMBEDDING: frozenset({Status.INDEXING, Status.FAILED}),
    Status.INDEXING: frozenset({Status.INDEXED, Status.FAILED}),
    Status.INDEXED: frozenset(),
    Status.FAILED: frozenset({Status.PENDING, Status.ARCHIVED}),
    Status.NEEDS_REVIEW: frozenset({Status.PENDING, Status.FAILED}),
    Status.ARCHIVED: frozenset(),
}
"""Authoritative legal-forward-transition table — a literal transcription of the story's
References table, additively extended by REQ-009's ``NEEDS_REVIEW`` entries (``PENDING``
gains ``NEEDS_REVIEW`` as an outgoing edge; ``NEEDS_REVIEW`` itself is only ever reachable
from, and only ever returns to, ``PENDING``/``FAILED`` — no other status gains
``NEEDS_REVIEW`` as a legal target) and by REQ-012's two ``Status.FAILED`` exits
(``PENDING`` — requeue; ``ARCHIVED`` — archive) plus ``Status.ARCHIVED``'s own empty
entry (fully terminal, same shape as ``Status.INDEXED``). Same-status self-transitions
(retry-within-stage, Assumption A2) are legal for every non-terminal status and are
validated separately in ``validate_transition``, not folded into this dict —
``Status.FAILED`` is deliberately excluded from that generic allowance despite no longer
being fully closed: only its two explicitly-named targets above are legal, never
``Status.FAILED`` itself (see ``validate_transition``'s Round-4 revision note)."""

_FORWARD_ORDER: tuple[Status, ...] = (
    Status.PENDING,
    Status.ANALYZING,
    Status.CHUNKING,
    Status.EMBEDDING,
    Status.INDEXING,
    Status.INDEXED,
)
"""The single linear non-FAILED, non-NEEDS_REVIEW path through ``_LEGAL_TRANSITIONS``,
precomputed for ``is_benign_concurrent_loss``'s reachability check.

``Status.FAILED`` is deliberately absent: it is legal from every non-terminal status, not
part of this single linear path, so it has no well-defined position here. Any code
indexing into this tuple MUST special-case both ``attempted_to_status == Status.FAILED``
and ``observed_row.status == Status.FAILED`` before calling ``.index()`` on either —
``is_benign_concurrent_loss`` does exactly this (design-review Round 2 fix).

``Status.NEEDS_REVIEW`` (REQ-009) is absent for the identical structural reason: it
branches only off ``PENDING`` and rejoins the pipeline at ``PENDING``, so it is not a
point *on* this linear path either. Any code indexing into this tuple MUST equally
special-case both ``attempted_to_status == Status.NEEDS_REVIEW`` and
``observed_row.status == Status.NEEDS_REVIEW`` before calling ``.index()`` on either —
``is_benign_concurrent_loss`` does this too (the Round-2-equivalent fix REQ-009 required
to keep this function's "never raises, for any input" contract true after
``NEEDS_REVIEW`` was added; see ``docs/designs/REQ-003-lld.md``'s Revision note, third
entry).

``Status.ARCHIVED`` (REQ-012) is absent for a related but distinct reason: unlike
``NEEDS_REVIEW`` (a side-branch that rejoins the pipeline) it is a genuine dead end
reachable only from ``Status.FAILED`` — but ``Status.FAILED`` itself has never had a
position on this tuple either (see the first paragraph above), so ``ARCHIVED``, one hop
further from an already-absent status, cannot have one. Any code indexing into this tuple
MUST equally special-case both ``attempted_to_status == Status.ARCHIVED`` and
``observed_row.status == Status.ARCHIVED`` before calling ``.index()`` on either —
``is_benign_concurrent_loss`` does this too (the Round-4-equivalent fix; see this
module's own docstring, "Round 4" entry, and ``docs/designs/REQ-003-lld.md``'s Revision
note, fourth entry)."""


class LedgerRow(BaseModel):
    """One row per document — Contract #3.

    Immutable snapshot; ``LedgerStore`` methods return a new ``LedgerRow`` on every write,
    never mutate a returned instance in place.

    Attributes:
        doc_id: 64-char lowercase hex identifier, validated via
            ``vestibule.identity.derive.validate_id_format`` (REQ-002).
        status: Current FSM state — drives legal-transition checks.
        stage: Mirror of ``status``; must equal ``status`` on every write (Assumption A1).
        attempts: Attempts made at the current ``(status, stage)``; ``>= 0`` (Assumption A4).
        last_error_code: Most recent error code recorded at this stage, if any.
        last_error_message: Most recent error message recorded at this stage, if any.
        config_version: Opaque; from ``EnvelopeSummary``; set once at ``create()``.
        ingested_at: UTC; set once at ``create()``, never overwritten (AC1, AC5).
        updated_at: UTC; set on every ``create()``/``transition()`` write (AC5).
        assigned_scenario_id: ``None`` until a human resolves a ``needs_review``-parked
            document via ``vestibule.vertical.review_queue.ReviewQueue.assign()``
            (REQ-009), which sets it as part of its ``needs_review -> pending``
            transition. **Set once, never overwritten** — the same "set once" lifecycle
            as ``config_version``/``ingested_at`` above, deliberately chosen over
            clear-on-consumption: ``vestibule.vertical.loader.VerticalLoader.resolve()``
            reads this field as its highest-priority resolution step, but never clears
            it, because no entry in ``_LEGAL_TRANSITIONS`` ever returns a row to
            ``PENDING`` once it has left ``PENDING`` via the ordinary forward path (the
            only way back to ``PENDING`` is ``NEEDS_REVIEW -> PENDING``, i.e. another
            ``assign()`` call, which would overwrite this field with a fresh value
            anyway). A stale value sitting on an already-advanced row is therefore
            provably inert, not a latent bug — see ``VerticalLoader.resolve()``'s own
            docstring for the consuming side of this contract. Any unrelated
            ``transition()`` call that does not pass ``assigned_scenario_id`` explicitly
            leaves this field unchanged (never silently clears it) — see
            ``LedgerStore.transition()``'s own docstring.
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
    assigned_scenario_id: str | None = None


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
      6. ``attempted_to_status == Status.NEEDS_REVIEW`` (REQ-009) — resolved here, for the
         identical structural reason as step 4: ``Status.NEEDS_REVIEW`` is deliberately
         absent from ``_FORWARD_ORDER``. ``observed_row.status != Status.PENDING`` ->
         ``True`` (the document already left ``PENDING`` by some other means — a
         concurrent winner parked it first, or it legitimately advanced through the
         ordinary pipeline instead); ``observed_row.status == Status.PENDING`` -> ``False``
         (this exact combination is never actually raised in practice, since
         ``PENDING -> NEEDS_REVIEW`` is itself always a legal transition — but this
         function must still return a definite, non-raising answer for every input, per
         its own "never raises" contract).
      7. ``observed_row.status == Status.NEEDS_REVIEW`` (only reached for
         ``attempted_to_status`` values not already resolved by steps 4/6) -> ``False``.
         ``NEEDS_REVIEW`` has no well-defined position relative to any status on the
         ordinary pipeline's forward path (it is a side-branch off ``PENDING``, not a
         point beyond it), so a race against a concurrent parking is never treated as
         benign forward progress — the conservative default, consistent with step 8's own
         off-path -> ``False`` precedent.
      8. ``attempted_to_status == Status.ARCHIVED`` (REQ-012) — resolved here, for the
         identical structural reason as steps 4/6: ``Status.ARCHIVED`` is deliberately
         absent from ``_FORWARD_ORDER``. ``observed_row.status == Status.ARCHIVED`` ->
         ``True`` (a concurrent winner already archived it — the exact same edge);
         any other observed status -> ``False`` (conservative: unlike ``FAILED -> ...``'s
         "already reached a later terminal state" case, no other status is treated as
         "further along" than ``archived``, since ``archived`` has no linear relationship
         to anything but ``failed``).
      9. ``observed_row.status == Status.ARCHIVED`` (only reached for
         ``attempted_to_status`` values not already resolved by steps 4/6/8) -> ``False``,
         for the identical conservative reason as step 7.
      10. Otherwise: ``True`` iff ``_FORWARD_ORDER.index(observed_row.status) >=
         _FORWARD_ORDER.index(attempted_to_status)`` — a concurrent winner already applied
         this exact edge or has since advanced the document further along the same linear
         path (late redelivery). Both operands are now guaranteed to be in
         ``_FORWARD_ORDER`` (steps 4-9 have already resolved every ``FAILED``/
         ``NEEDS_REVIEW``/``ARCHIVED`` case on either side).

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
    if attempted_to_status == Status.NEEDS_REVIEW:
        return observed_row.status != Status.PENDING
    if observed_row.status == Status.NEEDS_REVIEW:
        return False
    if attempted_to_status == Status.ARCHIVED:
        return observed_row.status == Status.ARCHIVED
    if observed_row.status == Status.ARCHIVED:
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
    is not terminal *unless* ``to_status`` is one of that terminal status's own
    explicitly-legal targets (REQ-012: lets ``Status.FAILED``'s two named exits —
    ``PENDING``/``ARCHIVED`` — through, while ``Status.INDEXED``/``Status.ARCHIVED``
    themselves, whose legal-target sets are both empty, remain fully blocked exactly as
    before); ``to_status`` is in ``_LEGAL_TRANSITIONS[current.status]`` or
    ``to_status == current.status`` (same-status retry, Assumption A2 — note
    ``Status.FAILED`` never satisfies this itself, since it is excluded from its own
    legal-target set); if ``to_status == Status.FAILED``, ``error_code`` is not ``None``.

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
    if (
        current.status in _TERMINAL_STATUSES
        and to_status not in _LEGAL_TRANSITIONS[current.status]
    ):
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
        assigned_scenario_id: str | None = None,
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
            assigned_scenario_id: REQ-009. When non-``None``, overwrites the row's
                ``LedgerRow.assigned_scenario_id`` with this value (the shape
                ``vestibule.vertical.review_queue.ReviewQueue.assign()`` uses to durably
                record a human's scenario assignment on its ``needs_review -> pending``
                transition). When ``None`` (the default — every caller in this codebase
                other than ``ReviewQueue.assign()`` omits this parameter entirely), the
                row's existing ``assigned_scenario_id`` is preserved unchanged; it is
                never implicitly cleared by an unrelated ``transition()`` call. See
                ``LedgerRow``'s own docstring for this field's full "set once, never
                overwritten" lifecycle.

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
        assigned_scenario_id: str | None = None,
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
                assigned_scenario_id=_next_assigned_scenario_id(
                    current, assigned_scenario_id
                ),
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
        ``0`` on REQ-012's ``failed -> pending`` requeue edge specifically (a deliberate,
        narrowly-scoped exception — see below); ``current.attempts + 1`` on a same-status
        retry; ``1`` on any other legal forward advance into a new non-terminal status
        (first attempt at that stage).

        The ``failed -> pending`` case is intentionally distinct from every other
        "advance into a new status" edge, which otherwise always yields ``1`` (see
        below): ``Reprocessor.requeue()`` (REQ-012) resets a requeued document to look
        exactly like a freshly-created row (``LedgerStore.create()`` also starts
        ``attempts`` at ``0``) — its whole history at the stage it failed at is being
        discarded, not continued, which ``1`` (first attempt already counted) would
        misrepresent. This is the one and only place ``_next_attempts`` inspects
        ``current.status`` on its own, rather than only comparing it to ``to_status``.
    """
    if to_status == Status.FAILED:
        return current.attempts
    if current.status == Status.FAILED and to_status == Status.PENDING:
        return 0
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


def _next_assigned_scenario_id(
    current: LedgerRow, assigned_scenario_id: str | None
) -> str | None:
    """Computes the new ``assigned_scenario_id`` value, per its "set once" lifecycle.

    Args:
        current: The row before the transition is applied.
        assigned_scenario_id: The value supplied to this ``transition()`` call, if any.

    Returns:
        ``assigned_scenario_id`` itself if the caller supplied a non-``None`` value
        (``ReviewQueue.assign()`` durably recording a human's assignment); otherwise
        ``current.assigned_scenario_id`` unchanged — every other caller in this codebase
        omits this parameter and must never silently clear a prior assignment.
    """
    if assigned_scenario_id is not None:
        return assigned_scenario_id
    return current.assigned_scenario_id
