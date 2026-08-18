"""Vertical Loader — data model, resolution result, and error-code registration (REQ-009).

Defines `VerticalResolution` (the pure decision `vestibule.vertical.loader.VerticalLoader
.resolve()` returns) and `VerticalError` (the single exception type this package's
`loader.py`/`review_queue.py` raise, carrying a caller-supplied `error_code`).

Failure taxonomy: this module registers four error codes at import time (Contract #4),
all four PERMANENT — `VERTICAL_UNRESOLVED`, `VERTICAL_MAPPING_INVALID`,
`VERTICAL_REJECTED`, `REVIEW_ITEM_NOT_FOUND`. A fifth code this package raises,
`SCENARIO_NOT_FOUND`, is deliberately *not* redefined here — `vestibule.vertical.loader`
imports and reuses it as-is from `vestibule.scenario.model` (REQ-010), since a
stated-but-missing `scenario_id`/`vertical` is exactly the same failure `ScenarioStore`
already declares. See `vestibule.vertical.loader`'s module docstring for why this is
raised directly rather than via `ScenarioStore.resolve()`.

`VERTICAL_UNRESOLVED` is PERMANENT even though the parked document is not dead — the one
non-obvious classification in this package, so it is documented here in full (and
mirrored at its raise site, `vestibule.vertical.loader.VerticalLoader._park`). CLAUDE.md's
Contract #4 reads PERMANENT as "mark failed, ack, never retry," but that is not what
happens here: a parked document is *not* marked failed — its ledger row moves to
`needs_review` (`vestibule.ledger.store.Status`), a deliberately non-terminal status that
can still return to the pipeline. What PERMANENT means in this one case is narrower:
"raise so the caller's at-least-once-delivery retry loop stops redelivering this exact
message." Resolution is a pure function of the envelope plus configuration (Contract #2),
so redelivering the identical, unchanged envelope deterministically reaches the same
`unresolved` outcome every time — retrying buys nothing. Only a human's
`vestibule.vertical.review_queue.ReviewQueue.assign()` call, which supplies information
(a chosen `scenario_id`) the original envelope never carried, can change the outcome.
Classifying this TRANSIENT instead would make a well-behaved retry/poison consumer keep
redelivering the message until it poisons — silently turning a clean, single
park-and-stop into queue-poisoning noise.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from vestibule.errors.registry import RaggedError, Severity, register_error

VERTICAL_UNRESOLVED = "VERTICAL_UNRESOLVED"
VERTICAL_MAPPING_INVALID = "VERTICAL_MAPPING_INVALID"
VERTICAL_REJECTED = "VERTICAL_REJECTED"
REVIEW_ITEM_NOT_FOUND = "REVIEW_ITEM_NOT_FOUND"


class ResolutionSource(str, Enum):
    """How a `VerticalResolution` was determined — REQ-009's resolution order.

    `REVIEW_ASSIGNMENT` is a deliberate, documented spec extension beyond the story's
    original four-member enum (`ENVELOPE_SCENARIO_ID | ENVELOPE_VERTICAL |
    SOURCE_MAPPING | UNRESOLVED`): the story's resolution order has no member describing
    "a human assigned this via `vestibule.vertical.review_queue.ReviewQueue.assign()`",
    because the story did not anticipate `assign()` needing a durable, replayable
    record of its own decision (see `LedgerRow.assigned_scenario_id`'s docstring in
    `vestibule.ledger.store` for the full gap this closes). It is the new
    highest-priority resolution step — see
    `vestibule.vertical.loader.VerticalLoader.resolve()`'s own docstring.
    """

    REVIEW_ASSIGNMENT = "review_assignment"
    ENVELOPE_SCENARIO_ID = "envelope_scenario_id"
    ENVELOPE_VERTICAL = "envelope_vertical"
    SOURCE_MAPPING = "source_mapping"
    UNRESOLVED = "unresolved"


class VerticalResolution(BaseModel):
    """The pure decision `VerticalLoader.resolve()` returns. Frozen, immutable.

    Attributes:
        vertical: The resolved vertical, or `None` if unresolved.
        scenario_id: The resolved scenario's id, or `None` if unresolved. Populated
            whenever a `Scenario` was actually found (resolution steps 1-3), so
            `VerticalLoader.load()` does not need to re-derive it.
        source: Which resolution step produced this result.
        confidence: `"explicit"` (envelope declared `scenario_id`/`vertical`),
            `"inferred"` (a source mapping matched), or `"unresolved"` (parked).
        reason: Human-readable explanation, surfaced in the review queue. Always
            non-empty (enforced below) — every resolution, including a successful one,
            carries a reason so `ReviewItem.reason` is never fabricated after the fact.
    """

    model_config = ConfigDict(frozen=True)

    vertical: str | None
    scenario_id: str | None
    source: ResolutionSource
    confidence: Literal["explicit", "inferred", "unresolved"]
    reason: str

    @field_validator("reason")
    @classmethod
    def _non_empty_reason(cls, value: str) -> str:
        if not value:
            raise ValueError("reason must be non-empty")
        return value


class VerticalError(RaggedError):
    """Single exception type for every `VerticalLoader`/`ReviewQueue`-raised failure.

    `error_code` varies per raise site — same shape as REQ-010's `ScenarioError`.

    Attributes:
        doc_id: The `doc_id` involved, or `""` for a failure not tied to any specific
            document (e.g. `VERTICAL_MAPPING_INVALID`, raised at `VerticalLoader`
            construction, before any document is in scope).
        reason: Human-readable failure reason.
    """

    def __init__(self, doc_id: str, reason: str, *, error_code: str) -> None:
        """Initializes the exception.

        Args:
            doc_id: The `doc_id` involved, or `""`.
            reason: Human-readable failure reason.
            error_code: One of this module's four registered codes, or the reused
                `vestibule.scenario.model.SCENARIO_NOT_FOUND`.
        """
        self.doc_id = doc_id
        self.reason = reason
        super().__init__(f"{doc_id}: {reason}", error_code=error_code)


register_error(
    VERTICAL_UNRESOLVED,
    Severity.PERMANENT,
    "no vertical/scenario_id signal on the envelope and no source mapping matched; "
    "document parked to needs_review for human review — PERMANENT here means 'stop "
    "redelivering this exact message,' not 'the document is dead': see this module's "
    "own docstring (REQ-009)",
)
register_error(
    VERTICAL_MAPPING_INVALID,
    Severity.PERMANENT,
    "a configured source mapping references a vertical with no enabled scenario in the "
    "ScenarioStore; raised at VerticalLoader construction, before any document is ever "
    "processed (REQ-009)",
)
register_error(
    VERTICAL_REJECTED,
    Severity.PERMANENT,
    "a human reviewer rejected a needs_review-parked document via ReviewQueue.reject "
    "(REQ-009)",
)
register_error(
    REVIEW_ITEM_NOT_FOUND,
    Severity.PERMANENT,
    "ReviewQueue.assign/reject was called for a doc_id not currently in needs_review "
    "(REQ-009)",
)
