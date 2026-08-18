"""ReviewQueue — minimal human-in-the-loop triage for parked documents (REQ-009).

`ReviewQueue` exposes exactly three operations over `needs_review`-parked ledger rows:
`list_pending` (what's waiting), `assign` (a human resolved it — re-enters the pipeline),
`reject` (a human rejected it — terminal `failed`). `ReviewRegistry` is the small,
explicitly-flagged side-table `VerticalLoader`/`ReviewQueue` share to bridge a genuine
spec gap: see `ReviewRegistry`'s own docstring and `VerticalLoader.__init__`'s docstring
for the full resolution.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from vestibule.ledger.store import (
    LedgerStore,
    LedgerTransitionInvalid,
    Status,
    is_benign_concurrent_loss,
)
from vestibule.scenario.model import SCENARIO_NOT_FOUND, ScenarioError
from vestibule.scenario.stores.base import ScenarioStore
from vestibule.vertical.model import (
    REVIEW_ITEM_NOT_FOUND,
    VERTICAL_REJECTED,
    VerticalError,
)

logger = logging.getLogger(__name__)

_DEFAULT_LIST_LIMIT = 100


@dataclass(frozen=True)
class _ParkedMetadata:
    """`ReviewRegistry`'s internal per-`doc_id` record. Not part of this package's API."""

    source: str
    blob_path: str
    suggested_verticals: tuple[str, ...]


class ReviewRegistry:
    """Thread-safe, in-memory `doc_id -> parking metadata` side-table.

    Spec-gap resolution: the story's `ReviewItem` shape (`doc_id`, `source`,
    `blob_path`, `reason`, `parked_at`, `suggested_verticals`) cannot be reconstructed
    from `LedgerRow` alone — `reason`/`parked_at` map onto `LedgerRow.last_error_message`
    /`updated_at` directly, but `LedgerRow` (REQ-003) has no `source`/`blob_path` field
    at all (Contract #3's ledger is deliberately envelope-schema-agnostic), and
    `suggested_verticals` depends on the source-mapping configuration in effect at
    parking time. `VerticalLoader.load()` records `(source, blob_path,
    suggested_verticals)` here when it parks a document; `ReviewQueue.list_pending()`
    reads it back to assemble the full `ReviewItem`. A composition root constructs one
    `ReviewRegistry` and wires the same instance into both `VerticalLoader` and
    `ReviewQueue`.

    Bounded lifetime: `ReviewQueue.assign`/`reject` discard a `doc_id`'s entry once it
    leaves `needs_review` (it is no longer needed for `list_pending`), so this registry's
    size tracks the current review queue depth, not the lifetime document count —
    unlike `InMemoryLedgerStore`'s own `_rows` dict, which is explicitly documented
    (REQ-003) as an unbounded, test/dev-only accumulation.
    """

    def __init__(self) -> None:
        """Initializes an empty registry."""
        self._lock = threading.Lock()
        self._records: dict[str, _ParkedMetadata] = {}

    def record(
        self,
        doc_id: str,
        *,
        source: str,
        blob_path: str,
        suggested_verticals: Sequence[str],
    ) -> None:
        """Records (or idempotently re-records) parking metadata for `doc_id`.

        Args:
            doc_id: The parked document's `doc_id`.
            source: `envelope.source` at parking time.
            blob_path: `envelope.blob_path` at parking time.
            suggested_verticals: Hint verticals computed at parking time.
        """
        metadata = _ParkedMetadata(
            source=source,
            blob_path=blob_path,
            suggested_verticals=tuple(suggested_verticals),
        )
        with self._lock:
            self._records[doc_id] = metadata

    def get(self, doc_id: str) -> _ParkedMetadata | None:
        """Returns `doc_id`'s recorded parking metadata, or `None` if never recorded."""
        with self._lock:
            return self._records.get(doc_id)

    def discard(self, doc_id: str) -> None:
        """Removes `doc_id`'s entry, if any. A no-op if `doc_id` was never recorded."""
        with self._lock:
            self._records.pop(doc_id, None)


class ReviewItem(BaseModel):
    """One `needs_review`-parked document, as surfaced to a human reviewer. Frozen.

    Attributes:
        doc_id: The parked document's `doc_id`.
        source: The envelope's `source` at parking time (`""` if no `ReviewRegistry`
            entry was ever recorded for `doc_id` — defensive only, see
            `ReviewQueue.list_pending`).
        blob_path: The envelope's `blob_path` at parking time (same `""` fallback).
        reason: Why the document was parked — `VerticalResolution.reason` at parking
            time, read back from the ledger row's `last_error_message`.
        parked_at: When the `pending -> needs_review` transition landed (the ledger
            row's `updated_at`).
        suggested_verticals: Convenience hint only — verticals whose source mappings
            nearly matched. Never a classifier; may be empty.
    """

    model_config = ConfigDict(frozen=True)

    doc_id: str
    source: str
    blob_path: str
    reason: str
    parked_at: datetime
    suggested_verticals: list[str]


class ReviewQueue:
    """Minimal human-in-the-loop triage for `needs_review`-parked documents."""

    def __init__(
        self,
        ledger: LedgerStore,
        scenario_store: ScenarioStore,
        review_registry: ReviewRegistry,
    ) -> None:
        """Initializes the queue.

        Args:
            ledger: Where parked rows live and transitions are written.
            scenario_store: Used by `assign()` to validate a human-supplied
                `scenario_id` actually exists before returning a document to `pending`.
            review_registry: The same `ReviewRegistry` instance the `VerticalLoader`
                that parks documents was constructed with — see `ReviewRegistry`'s own
                docstring.
        """
        self._ledger = ledger
        self._scenario_store = scenario_store
        self._registry = review_registry

    def list_pending(self, limit: int = _DEFAULT_LIST_LIMIT) -> list[ReviewItem]:
        """Lists parked documents, newest-parked first.

        Args:
            limit: Maximum number of items to return. `limit <= 0` returns `[]`
                (delegated to `LedgerStore.list_by_status`'s own semantics).

        Returns:
            `ReviewItem`s for every `needs_review` ledger row, most-recently-parked
            first, capped at `limit`.
        """
        rows = self._ledger.list_by_status(Status.NEEDS_REVIEW, limit=limit)
        items: list[ReviewItem] = []
        for row in rows:
            metadata = self._registry.get(row.doc_id)
            items.append(
                ReviewItem(
                    doc_id=row.doc_id,
                    source=metadata.source if metadata is not None else "",
                    blob_path=metadata.blob_path if metadata is not None else "",
                    reason=row.last_error_message or "",
                    parked_at=row.updated_at,
                    suggested_verticals=(
                        list(metadata.suggested_verticals)
                        if metadata is not None
                        else []
                    ),
                )
            )
        return items

    def assign(self, doc_id: str, scenario_id: str) -> None:
        """A human resolved `doc_id`'s vertical: durably records it, returns to `pending`.

        Spec-gap resolution: the story says `assign()` "sets the scenario, transitions
        back to pending." `ArrivalEnvelope` is frozen and REQ-009 must not modify it, so
        the durable record lives on the ledger row instead:
        `LedgerRow.assigned_scenario_id` (REQ-009's own extension to `LedgerRow`, see
        `vestibule.ledger.store`). This method's job is: (a) validate `scenario_id`
        actually exists in the `ScenarioStore` (a human fat-fingering a scenario_id
        during triage is the same class of error `VerticalLoader.resolve()` itself
        already guards against); (b) transition the ledger row `needs_review -> pending`,
        setting `assigned_scenario_id=scenario_id` on that same transition — no envelope
        mutation, no separate write. `VerticalLoader.resolve()` reads this field back as
        its highest-priority step, so redelivering the *original, unchanged* envelope
        after `assign()` now resolves successfully — closing the round trip without
        requiring whatever coordinator redelivers the document to construct a
        differently-shaped envelope.

        Args:
            doc_id: The parked document to reassign.
            scenario_id: The human-chosen scenario. Must exist in the `ScenarioStore`.

        Raises:
            ScenarioError: `SCENARIO_NOT_FOUND` (PERMANENT) if `scenario_id` does not
                exist.
            VerticalError: `REVIEW_ITEM_NOT_FOUND` (PERMANENT) if `doc_id` is not
                currently in `needs_review`.
            LedgerTransitionInvalid: If the transition fails non-benignly.
        """
        if self._scenario_store.get(scenario_id) is None:
            raise ScenarioError(
                scenario_id,
                f"assign() referenced scenario_id={scenario_id!r}, which does not exist",
                error_code=SCENARIO_NOT_FOUND,
            )
        self._require_needs_review(doc_id, caller="assign")
        try:
            self._ledger.transition(
                doc_id,
                to_status=Status.PENDING,
                stage=Status.PENDING,
                assigned_scenario_id=scenario_id,
            )
        except LedgerTransitionInvalid as exc:
            if not is_benign_concurrent_loss(
                exc.observed_row, Status.PENDING, Status.PENDING
            ):
                raise
        self._registry.discard(doc_id)

    def reject(self, doc_id: str, reason: str) -> None:
        """A human rejected `doc_id`: transitions it to `failed`.

        Args:
            doc_id: The parked document to reject.
            reason: Human-readable rejection reason, recorded as `last_error_message`.

        Raises:
            VerticalError: `REVIEW_ITEM_NOT_FOUND` (PERMANENT) if `doc_id` is not
                currently in `needs_review`.
            LedgerTransitionInvalid: If the transition fails non-benignly.
        """
        self._require_needs_review(doc_id, caller="reject")
        try:
            self._ledger.transition(
                doc_id,
                to_status=Status.FAILED,
                stage=Status.FAILED,
                error_code=VERTICAL_REJECTED,
                error_message=reason,
            )
        except LedgerTransitionInvalid as exc:
            if not is_benign_concurrent_loss(
                exc.observed_row, Status.FAILED, Status.FAILED
            ):
                raise
        self._registry.discard(doc_id)

    def _require_needs_review(self, doc_id: str, *, caller: str) -> None:
        """Guards `assign`/`reject` against a `doc_id` not currently parked.

        Args:
            doc_id: The document to check.
            caller: `"assign"` or `"reject"`, for the error message only.

        Raises:
            VerticalError: `REVIEW_ITEM_NOT_FOUND` (PERMANENT) if `doc_id` has no ledger
                row, or its row is not currently `needs_review`.
        """
        current = self._ledger.get(doc_id)
        if current is None or current.status is not Status.NEEDS_REVIEW:
            raise VerticalError(
                doc_id,
                f"{caller}() called for a doc_id not currently in needs_review",
                error_code=REVIEW_ITEM_NOT_FOUND,
            )
