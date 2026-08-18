"""Unit tests for `vestibule.vertical.review_queue.ReviewQueue` (REQ-009)."""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest

from vestibule.envelope.model import ArrivalEnvelope, TrustTier, build_envelope
from vestibule.errors.registry import Severity
from vestibule.ledger.store import (
    EnvelopeSummary,
    InMemoryLedgerStore,
    LedgerRow,
    LedgerStore,
    LedgerTransitionInvalid,
    Status,
)
from vestibule.scenario.model import (
    SCENARIO_NOT_FOUND,
    ChunkerSettings,
    EmbedderSettings,
    IndexerSettings,
    Scenario,
    ScenarioError,
)
from vestibule.scenario.stores.base import ScenarioStore, select_enabled_for_vertical
from vestibule.vertical.loader import VerticalLoader, VerticalLoaderConfig
from vestibule.vertical.model import (
    REVIEW_ITEM_NOT_FOUND,
    VERTICAL_REJECTED,
    ResolutionSource,
    VerticalError,
)
from vestibule.vertical.review_queue import ReviewQueue, ReviewRegistry

_FIXED_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)
_counter = itertools.count()


def _scenario(scenario_id: str, vertical: str, *, enabled: bool = True) -> Scenario:
    return Scenario(
        scenario_id=scenario_id,
        vertical=vertical,
        config_version="1",
        index_name=f"{scenario_id}-idx",
        chunker=ChunkerSettings(
            target_chunk_tokens=512,
            overlap_tokens=64,
            min_chunk_tokens=40,
            max_chunk_tokens=800,
        ),
        embedder=EmbedderSettings(
            adapter="azure_openai",
            model="text-embedding-3-large",
            target_dimensions=1024,
        ),
        indexer=IndexerSettings(
            metric="cosine",
            dimensions=1024,
            hnsw_m=4,
            hnsw_ef_construction=400,
            hnsw_ef_search=500,
        ),
        acl_source="envelope",
        default_trust_tier="internal",
        enabled=enabled,
        created_at=_FIXED_TIME,
        updated_at=_FIXED_TIME,
    )


class _DictScenarioStore(ScenarioStore):
    """Minimal in-memory `ScenarioStore` for exercising `ReviewQueue` hermetically."""

    def __init__(self, scenarios: dict[str, Scenario]) -> None:
        self._scenarios = scenarios

    @property
    def is_writable(self) -> bool:
        return False

    def get(self, scenario_id: str) -> Scenario | None:
        return self._scenarios.get(scenario_id)

    def get_by_vertical(self, vertical: str) -> Scenario | None:
        candidates = [
            s for s in self._scenarios.values() if s.vertical == vertical and s.enabled
        ]
        return select_enabled_for_vertical(vertical, candidates)

    def list_all(self, include_disabled: bool = False) -> list[Scenario]:
        values = self._scenarios.values()
        return list(values) if include_disabled else [s for s in values if s.enabled]

    def upsert(self, scenario: Scenario) -> Scenario:
        raise NotImplementedError

    def delete(self, scenario_id: str) -> bool:
        raise NotImplementedError


def _row(status: Status, *, doc_id: str = "a" * 64) -> LedgerRow:
    now = datetime.now(timezone.utc)
    return LedgerRow(
        doc_id=doc_id,
        status=status,
        stage=status,
        attempts=1,
        config_version="v1",
        ingested_at=now,
        updated_at=now,
    )


class _RacedLedgerStore(LedgerStore):
    """`get()` always returns `precheck_row` (so `ReviewQueue`'s own precondition check
    passes); `transition()` always raises `LedgerTransitionInvalid` carrying
    `observed_row` — simulating a race won by a concurrent caller strictly between the
    precheck and the `transition()` call itself."""

    def __init__(self, precheck_row: LedgerRow, observed_row: LedgerRow) -> None:
        self._precheck_row = precheck_row
        self._observed_row = observed_row

    def create(self, doc_id: str, envelope_summary: object) -> LedgerRow:
        raise NotImplementedError

    def transition(
        self,
        doc_id: str,
        to_status: Status,
        stage: Status,
        error_code: str | None = None,
        error_message: str | None = None,
        assigned_scenario_id: str | None = None,
    ) -> LedgerRow:
        raise LedgerTransitionInvalid(
            doc_id,
            "raced",
            observed_row=self._observed_row,
            attempted_to_status=to_status,
            attempted_stage=stage,
        )

    def get(self, doc_id: str) -> LedgerRow | None:
        return self._precheck_row

    def list_by_status(self, status: Status, limit: int = 100) -> list[LedgerRow]:
        raise NotImplementedError


def _envelope(
    *,
    source: str = "blob:unrouted",
    blob_path: str | None = None,
    vertical: str | None = None,
    scenario_id: str | None = None,
) -> ArrivalEnvelope:
    return build_envelope(
        source=source,
        blob_path=blob_path if blob_path is not None else f"path-{next(_counter)}",
        content_hash="a" * 64,
        trust_tier=TrustTier.INTERNAL,
        vertical=vertical,
        scenario_id=scenario_id,
    )


def _park(
    loader: VerticalLoader, ledger: InMemoryLedgerStore, envelope: ArrivalEnvelope
) -> None:
    """Seeds a pending row for `envelope` and parks it via `loader.load()`."""
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="v1"))
    with pytest.raises(VerticalError):
        loader.load(envelope)


# --- list_pending ----------------------------------------------------------------------


def test_list_pending_returns_parked_items_newest_first() -> None:
    store = _DictScenarioStore({})
    ledger = InMemoryLedgerStore()
    registry = ReviewRegistry()
    loader = VerticalLoader(
        store, ledger, VerticalLoaderConfig(), review_registry=registry
    )
    queue = ReviewQueue(ledger, store, registry)

    first = _envelope(source="blob:first")
    second = _envelope(source="blob:second")
    _park(loader, ledger, first)
    _park(loader, ledger, second)

    items = queue.list_pending()
    assert [item.doc_id for item in items] == [second.doc_id, first.doc_id]
    assert items[0].source == second.source
    assert items[0].blob_path == second.blob_path
    assert items[0].reason


def test_list_pending_respects_limit() -> None:
    store = _DictScenarioStore({})
    ledger = InMemoryLedgerStore()
    registry = ReviewRegistry()
    loader = VerticalLoader(
        store, ledger, VerticalLoaderConfig(), review_registry=registry
    )
    queue = ReviewQueue(ledger, store, registry)

    for i in range(3):
        _park(loader, ledger, _envelope(source=f"blob:item-{i}"))

    assert len(queue.list_pending(limit=2)) == 2


# --- assign --------------------------------------------------------------------------------


def test_assign_transitions_needs_review_to_pending() -> None:
    store = _DictScenarioStore({"hr-1": _scenario("hr-1", "hr")})
    ledger = InMemoryLedgerStore()
    registry = ReviewRegistry()
    loader = VerticalLoader(
        store, ledger, VerticalLoaderConfig(), review_registry=registry
    )
    queue = ReviewQueue(ledger, store, registry)

    envelope = _envelope(source="blob:needs-a-human")
    _park(loader, ledger, envelope)

    queue.assign(envelope.doc_id, "hr-1")

    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status is Status.PENDING


def test_assign_unknown_scenario_id_raises_scenario_not_found() -> None:
    store = _DictScenarioStore({})
    ledger = InMemoryLedgerStore()
    registry = ReviewRegistry()
    loader = VerticalLoader(
        store, ledger, VerticalLoaderConfig(), review_registry=registry
    )
    queue = ReviewQueue(ledger, store, registry)

    envelope = _envelope(source="blob:needs-a-human")
    _park(loader, ledger, envelope)

    with pytest.raises(ScenarioError) as exc_info:
        queue.assign(envelope.doc_id, "does-not-exist")
    assert exc_info.value.error_code == SCENARIO_NOT_FOUND

    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status is Status.NEEDS_REVIEW


def test_assign_for_doc_id_not_in_needs_review_raises_review_item_not_found() -> None:
    store = _DictScenarioStore({"hr-1": _scenario("hr-1", "hr")})
    ledger = InMemoryLedgerStore()
    registry = ReviewRegistry()
    queue = ReviewQueue(ledger, store, registry)

    envelope = _envelope()
    ledger.create(
        envelope.doc_id, EnvelopeSummary(config_version="v1")
    )  # still pending

    with pytest.raises(VerticalError) as exc_info:
        queue.assign(envelope.doc_id, "hr-1")
    assert exc_info.value.error_code == REVIEW_ITEM_NOT_FOUND
    assert exc_info.value.severity == Severity.PERMANENT


def test_assign_for_completely_unknown_doc_id_raises_review_item_not_found() -> None:
    store = _DictScenarioStore({"hr-1": _scenario("hr-1", "hr")})
    ledger = InMemoryLedgerStore()
    registry = ReviewRegistry()
    queue = ReviewQueue(ledger, store, registry)

    with pytest.raises(VerticalError) as exc_info:
        queue.assign("a" * 64, "hr-1")
    assert exc_info.value.error_code == REVIEW_ITEM_NOT_FOUND


def test_assign_benign_concurrent_progress_is_a_no_op() -> None:
    """Mirrors the chunker/embedder/indexer precedent: a race lost strictly between
    `assign()`'s own precondition check and its `transition()` call is benign whenever
    the concurrent winner already advanced the document further (here: all the way to
    INDEXED) — `assign()` completes without raising."""
    doc_id = "b" * 64
    ledger = _RacedLedgerStore(
        _row(Status.NEEDS_REVIEW, doc_id=doc_id), _row(Status.INDEXED, doc_id=doc_id)
    )
    store = _DictScenarioStore({"hr-1": _scenario("hr-1", "hr")})
    queue = ReviewQueue(ledger, store, ReviewRegistry())

    queue.assign(doc_id, "hr-1")  # must not raise


def test_assign_non_benign_reraises_ledger_transition_invalid() -> None:
    doc_id = "c" * 64
    ledger = _RacedLedgerStore(
        _row(Status.NEEDS_REVIEW, doc_id=doc_id), _row(Status.FAILED, doc_id=doc_id)
    )
    store = _DictScenarioStore({"hr-1": _scenario("hr-1", "hr")})
    queue = ReviewQueue(ledger, store, ReviewRegistry())

    with pytest.raises(LedgerTransitionInvalid):
        queue.assign(doc_id, "hr-1")


# --- reject --------------------------------------------------------------------------------


def test_reject_transitions_needs_review_to_failed_with_vertical_rejected() -> None:
    store = _DictScenarioStore({})
    ledger = InMemoryLedgerStore()
    registry = ReviewRegistry()
    loader = VerticalLoader(
        store, ledger, VerticalLoaderConfig(), review_registry=registry
    )
    queue = ReviewQueue(ledger, store, registry)

    envelope = _envelope(source="blob:reject-me")
    _park(loader, ledger, envelope)

    queue.reject(envelope.doc_id, "not a real vertical")

    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status is Status.FAILED
    assert row.last_error_code == VERTICAL_REJECTED
    assert row.last_error_message == "not a real vertical"


def test_reject_for_doc_id_not_in_needs_review_raises_review_item_not_found() -> None:
    store = _DictScenarioStore({})
    ledger = InMemoryLedgerStore()
    registry = ReviewRegistry()
    queue = ReviewQueue(ledger, store, registry)

    envelope = _envelope()
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="v1"))

    with pytest.raises(VerticalError) as exc_info:
        queue.reject(envelope.doc_id, "irrelevant")
    assert exc_info.value.error_code == REVIEW_ITEM_NOT_FOUND


def test_reject_benign_concurrent_progress_is_a_no_op() -> None:
    doc_id = "d" * 64
    ledger = _RacedLedgerStore(
        _row(Status.NEEDS_REVIEW, doc_id=doc_id), _row(Status.INDEXED, doc_id=doc_id)
    )
    store = _DictScenarioStore({})
    queue = ReviewQueue(ledger, store, ReviewRegistry())

    queue.reject(doc_id, "not a real vertical")  # must not raise


def test_reject_non_benign_reraises_ledger_transition_invalid() -> None:
    doc_id = "e" * 64
    ledger = _RacedLedgerStore(
        _row(Status.NEEDS_REVIEW, doc_id=doc_id), _row(Status.ANALYZING, doc_id=doc_id)
    )
    store = _DictScenarioStore({})
    queue = ReviewQueue(ledger, store, ReviewRegistry())

    with pytest.raises(LedgerTransitionInvalid):
        queue.reject(doc_id, "not a real vertical")


# --- round trip: assign + redelivery resolves successfully (the queue is not a dead end) ----


def test_assigned_document_redelivered_with_original_envelope_resolves_successfully() -> (
    None
):
    """AC7, genuinely: a document parked with **zero** vertical signal (no `vertical`,
    no `scenario_id`, no matching source mapping — the exact case the review queue
    exists for) that a human resolves via `assign()` must resolve successfully when the
    literal SAME original envelope object is redelivered to `load()` — not a freshly
    constructed envelope with `scenario_id` pre-set. `assign()` durably records the
    assignment on `LedgerRow.assigned_scenario_id`; `VerticalLoader.resolve()` reads it
    back as step 0. Without that step, redelivering the unchanged original envelope
    would resolve identically to the first attempt and park it again — an infinite
    human-assign/re-park loop with no way out (the exact bug this fix closes)."""
    store = _DictScenarioStore({"hr-1": _scenario("hr-1", "hr")})
    ledger = InMemoryLedgerStore()
    registry = ReviewRegistry()
    loader = VerticalLoader(
        store, ledger, VerticalLoaderConfig(), review_registry=registry
    )
    queue = ReviewQueue(ledger, store, registry)

    original = _envelope(
        source="blob:completely-unrouted", blob_path="docs/some-file.pdf"
    )
    assert original.vertical is None
    assert original.scenario_id is None
    _park(loader, ledger, original)

    queue.assign(original.doc_id, "hr-1")

    # The literal same envelope object, redelivered unchanged — no new envelope with
    # scenario_id pre-set is constructed anywhere in this test.
    scenario, resolution = loader.load(original)
    assert scenario.scenario_id == "hr-1"
    assert resolution.source is ResolutionSource.REVIEW_ASSIGNMENT
    assert resolution.confidence == "explicit"

    # The ledger reaches a non-needs_review state (still PENDING — VerticalLoader.load()
    # never itself advances the ledger on a successful resolution; that is the next
    # pipeline stage's job, per the story's own "wiring the loader into the pipeline
    # orchestration is the caller's job").
    row = ledger.get(original.doc_id)
    assert row is not None
    assert row.status is not Status.NEEDS_REVIEW
    assert row.status is Status.PENDING


def test_assigned_scenario_id_survives_unrelated_subsequent_transition() -> None:
    """Lifecycle decision: `assigned_scenario_id` is set once by `assign()` and persists
    unchanged for the remainder of the row's lifetime (mirrors `config_version`'s own
    "set once, never overwritten" contract) — a completely unrelated subsequent
    `transition()` call (here: standing in for the next pipeline stage's own
    `pending -> analyzing` write) must not silently wipe it back to `None`."""
    store = _DictScenarioStore({"hr-1": _scenario("hr-1", "hr")})
    ledger = InMemoryLedgerStore()
    registry = ReviewRegistry()
    loader = VerticalLoader(
        store, ledger, VerticalLoaderConfig(), review_registry=registry
    )
    queue = ReviewQueue(ledger, store, registry)

    envelope = _envelope(source="blob:needs-a-human-2")
    _park(loader, ledger, envelope)
    queue.assign(envelope.doc_id, "hr-1")

    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.assigned_scenario_id == "hr-1"

    # An unrelated caller (e.g. the Analyzer) transitions the row forward, without any
    # knowledge of assigned_scenario_id.
    advanced = ledger.transition(envelope.doc_id, Status.ANALYZING, Status.ANALYZING)
    assert advanced.assigned_scenario_id == "hr-1"
