"""Unit tests for the State Ledger (Contract #3) — REQ-003."""

from __future__ import annotations

import inspect
import re
import threading
from pathlib import Path

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from ingestion.identity.derive import IdentityInvalid, derive_doc_id
from ingestion.ledger.store import (
    _FORWARD_ORDER,
    _LEGAL_TRANSITIONS,
    EnvelopeSummary,
    InMemoryLedgerStore,
    LedgerRow,
    LedgerStore,
    LedgerTransitionInvalid,
    Status,
    is_benign_concurrent_loss,
    validate_transition,
)

_NON_TERMINAL: tuple[Status, ...] = (
    Status.PENDING,
    Status.ANALYZING,
    Status.CHUNKING,
    Status.EMBEDDING,
    Status.INDEXING,
)
_TERMINAL: tuple[Status, ...] = (Status.INDEXED, Status.FAILED)
_ALL_STATUSES: tuple[Status, ...] = tuple(Status)

_counter = iter(range(10_000_000))


def _new_doc_id(label: str) -> str:
    """Generates a fresh, valid, collision-free doc_id for a test."""
    return derive_doc_id("test-source", f"{label}-{next(_counter)}")


def _build_row_at(store: InMemoryLedgerStore, doc_id: str, target: Status) -> LedgerRow:
    """Creates a row and advances it to `target` via legal transitions."""
    row = store.create(doc_id, EnvelopeSummary(config_version="v1"))
    if target == Status.PENDING:
        return row
    if target == Status.FAILED:
        return store.transition(doc_id, Status.FAILED, Status.FAILED, error_code="E")
    for status in _FORWARD_ORDER[1:]:
        row = store.transition(doc_id, status, status)
        if status == target:
            return row
    return row


# --- create() -------------------------------------------------------------------------


def test_create_new_doc_returns_pending_row() -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id("create-fresh")
    row = store.create(doc_id, EnvelopeSummary(config_version="v1"))
    assert row.status == Status.PENDING
    assert row.stage == Status.PENDING
    assert row.attempts == 0
    assert row.ingested_at == row.updated_at


def test_create_idempotent_returns_same_row_same_ingested_at() -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id("create-idempotent")
    first = store.create(doc_id, EnvelopeSummary(config_version="v1"))
    second = store.create(doc_id, EnvelopeSummary(config_version="v1"))
    assert first == second
    assert first.ingested_at == second.ingested_at


def test_create_idempotent_ignores_new_envelope_summary_on_repeat() -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id("create-idempotent-summary")
    first = store.create(doc_id, EnvelopeSummary(config_version="v1"))
    second = store.create(doc_id, EnvelopeSummary(config_version="v2-different"))
    assert second.config_version == "v1"
    assert first.config_version == "v1"


def test_create_rejects_malformed_doc_id() -> None:
    store = InMemoryLedgerStore()
    with pytest.raises(IdentityInvalid) as exc_info:
        store.create("not-a-valid-hex-id", EnvelopeSummary(config_version="v1"))
    assert exc_info.value.code == "IDENTITY_INVALID"
    assert exc_info.value.classification == "PERMANENT"


# --- transition() legal edges -----------------------------------------------------------


_LEGAL_EDGES: list[tuple[Status, Status]] = [
    (frm, to) for frm, targets in _LEGAL_TRANSITIONS.items() for to in targets
]


@pytest.mark.parametrize("frm,to", _LEGAL_EDGES)
def test_transition_each_legal_edge_in_table(frm: Status, to: Status) -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id(f"legal-edge-{frm.value}-{to.value}")
    _build_row_at(store, doc_id, frm)
    error_code = "E" if to == Status.FAILED else None
    row = store.transition(doc_id, to, to, error_code=error_code)
    assert row.status == to
    assert row.stage == to


_ILLEGAL_EDGES: list[tuple[Status, Status]] = [
    (frm, to)
    for frm in _NON_TERMINAL
    for to in Status
    if to not in _LEGAL_TRANSITIONS[frm] and to != frm
]


@pytest.mark.parametrize("frm,to", _ILLEGAL_EDGES)
def test_transition_illegal_edge_rejected(frm: Status, to: Status) -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id(f"illegal-edge-{frm.value}-{to.value}")
    _build_row_at(store, doc_id, frm)
    with pytest.raises(LedgerTransitionInvalid) as exc_info:
        store.transition(doc_id, to, to)
    assert exc_info.value.classification == "PERMANENT"


@pytest.mark.parametrize("status", _NON_TERMINAL)
def test_transition_same_status_retry_legal_for_non_terminal(status: Status) -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id(f"self-retry-{status.value}")
    _build_row_at(store, doc_id, status)
    row = store.transition(doc_id, status, status)
    assert row.status == status
    assert row.stage == status


@pytest.mark.parametrize("status", _TERMINAL)
def test_transition_same_status_retry_rejected_for_terminal(status: Status) -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id(f"self-retry-terminal-{status.value}")
    _build_row_at(store, doc_id, status)
    error_code = "E" if status == Status.FAILED else None
    with pytest.raises(LedgerTransitionInvalid) as exc_info:
        store.transition(doc_id, status, status, error_code=error_code)
    assert exc_info.value.classification == "PERMANENT"


def test_transition_from_terminal_indexed_rejected() -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id("from-indexed")
    _build_row_at(store, doc_id, Status.INDEXED)
    with pytest.raises(LedgerTransitionInvalid):
        store.transition(doc_id, Status.FAILED, Status.FAILED, error_code="E")


def test_transition_from_terminal_failed_rejected() -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id("from-failed")
    _build_row_at(store, doc_id, Status.FAILED)
    with pytest.raises(LedgerTransitionInvalid):
        store.transition(doc_id, Status.ANALYZING, Status.ANALYZING)


def test_transition_to_failed_without_error_code_rejected() -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id("failed-no-error-code")
    _build_row_at(store, doc_id, Status.PENDING)
    with pytest.raises(LedgerTransitionInvalid) as exc_info:
        store.transition(doc_id, Status.FAILED, Status.FAILED, error_code=None)
    assert exc_info.value.classification == "PERMANENT"


def test_transition_to_failed_with_error_code_succeeds_and_stores_it() -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id("failed-with-error-code")
    _build_row_at(store, doc_id, Status.PENDING)
    row = store.transition(
        doc_id, Status.FAILED, Status.FAILED, error_code="BOOM", error_message="kaboom"
    )
    assert row.last_error_code == "BOOM"
    assert row.last_error_message == "kaboom"


def test_transition_stage_mismatch_rejected() -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id("stage-mismatch")
    _build_row_at(store, doc_id, Status.PENDING)
    with pytest.raises(LedgerTransitionInvalid) as exc_info:
        store.transition(doc_id, Status.ANALYZING, Status.CHUNKING)
    assert exc_info.value.classification == "PERMANENT"


def test_transition_unknown_doc_id_rejected() -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id("unknown")
    with pytest.raises(LedgerTransitionInvalid) as exc_info:
        store.transition(doc_id, Status.ANALYZING, Status.ANALYZING)
    assert exc_info.value.observed_row is None
    assert exc_info.value.classification == "PERMANENT"


def test_transition_rejects_malformed_doc_id() -> None:
    store = InMemoryLedgerStore()
    with pytest.raises(IdentityInvalid) as exc_info:
        store.transition("not-hex", Status.ANALYZING, Status.ANALYZING)
    assert exc_info.value.code == "IDENTITY_INVALID"


# --- validate_transition() (pure helper) -------------------------------------------------


def test_validate_transition_returns_none_on_success() -> None:
    """A legal transition request does not raise (the function's only observable effect)."""
    current = _row(Status.PENDING)
    validate_transition("doc", current, Status.ANALYZING, Status.ANALYZING, None)


def test_validate_transition_raises_for_unknown_doc_id() -> None:
    with pytest.raises(LedgerTransitionInvalid) as exc_info:
        validate_transition("doc", None, Status.ANALYZING, Status.ANALYZING, None)
    assert exc_info.value.observed_row is None


def test_validate_transition_raises_for_stage_mismatch() -> None:
    current = _row(Status.PENDING)
    with pytest.raises(LedgerTransitionInvalid):
        validate_transition("doc", current, Status.ANALYZING, Status.CHUNKING, None)


def test_validate_transition_raises_for_terminal_current() -> None:
    current = _row(Status.INDEXED)
    with pytest.raises(LedgerTransitionInvalid):
        validate_transition("doc", current, Status.INDEXED, Status.INDEXED, None)


def test_validate_transition_raises_for_illegal_edge() -> None:
    current = _row(Status.PENDING)
    with pytest.raises(LedgerTransitionInvalid):
        validate_transition("doc", current, Status.EMBEDDING, Status.EMBEDDING, None)


def test_validate_transition_raises_for_failed_without_error_code() -> None:
    current = _row(Status.PENDING)
    with pytest.raises(LedgerTransitionInvalid):
        validate_transition("doc", current, Status.FAILED, Status.FAILED, None)


# --- attempts semantics (A4) ------------------------------------------------------------


def test_attempts_increment_on_same_stage_retry() -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id("attempts-retry")
    _build_row_at(store, doc_id, Status.ANALYZING)  # attempts == 1
    row = store.transition(doc_id, Status.ANALYZING, Status.ANALYZING)
    assert row.attempts == 2
    row = store.transition(doc_id, Status.ANALYZING, Status.ANALYZING)
    assert row.attempts == 3


def test_attempts_reset_on_advance_to_new_stage() -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id("attempts-reset")
    _build_row_at(store, doc_id, Status.ANALYZING)
    store.transition(doc_id, Status.ANALYZING, Status.ANALYZING)  # attempts == 2
    row = store.transition(doc_id, Status.CHUNKING, Status.CHUNKING)
    assert row.attempts == 1


def test_attempts_frozen_at_failed_value() -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id("attempts-frozen")
    _build_row_at(store, doc_id, Status.ANALYZING)
    row = store.transition(doc_id, Status.ANALYZING, Status.ANALYZING)  # attempts == 2
    assert row.attempts == 2
    failed_row = store.transition(doc_id, Status.FAILED, Status.FAILED, error_code="E")
    assert failed_row.attempts == 2


def test_attempts_has_no_enforced_upper_cap() -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id("attempts-no-cap")
    _build_row_at(store, doc_id, Status.ANALYZING)
    row = store.create(doc_id, EnvelopeSummary(config_version="v1"))
    for expected in range(2, 202):
        row = store.transition(doc_id, Status.ANALYZING, Status.ANALYZING)
        assert row.attempts == expected


# --- timestamps (AC5) --------------------------------------------------------------------


def test_updated_at_set_on_every_transition_ingested_at_only_on_create() -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id("timestamps")
    created = store.create(doc_id, EnvelopeSummary(config_version="v1"))
    row1 = store.transition(doc_id, Status.ANALYZING, Status.ANALYZING)
    row2 = store.transition(doc_id, Status.CHUNKING, Status.CHUNKING)
    assert created.ingested_at == row1.ingested_at == row2.ingested_at
    assert row1.updated_at >= created.updated_at
    assert row2.updated_at >= row1.updated_at


def test_last_error_cleared_on_clean_forward_advance() -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id("error-cleared")
    _build_row_at(store, doc_id, Status.ANALYZING)
    stale = store.transition(
        doc_id, Status.ANALYZING, Status.ANALYZING, error_code="TRANSIENT_X"
    )
    assert stale.last_error_code == "TRANSIENT_X"
    clean = store.transition(doc_id, Status.CHUNKING, Status.CHUNKING)
    assert clean.last_error_code is None
    assert clean.last_error_message is None


def test_last_error_preserved_on_same_status_retry_without_error_code() -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id("error-preserved")
    _build_row_at(store, doc_id, Status.ANALYZING)
    stale = store.transition(
        doc_id,
        Status.ANALYZING,
        Status.ANALYZING,
        error_code="TRANSIENT_X",
        error_message="transient failure",
    )
    assert stale.last_error_code == "TRANSIENT_X"
    assert stale.last_error_message == "transient failure"
    retried = store.transition(doc_id, Status.ANALYZING, Status.ANALYZING)
    assert retried.last_error_code == "TRANSIENT_X"
    assert retried.last_error_message == "transient failure"


# --- get() ---------------------------------------------------------------------------


def test_get_unknown_doc_id_returns_none_no_raise() -> None:
    store = InMemoryLedgerStore()
    assert store.get(_new_doc_id("unknown-get")) is None
    assert store.get("not-even-a-valid-shape") is None


def test_get_known_doc_id_returns_row() -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id("known-get")
    created = store.create(doc_id, EnvelopeSummary(config_version="v1"))
    assert store.get(doc_id) == created


# --- list_by_status() (AC7, F8) --------------------------------------------------------


def test_list_by_status_orders_by_updated_at_desc() -> None:
    store = InMemoryLedgerStore()
    doc_ids = [_new_doc_id(f"list-order-{i}") for i in range(3)]
    for doc_id in doc_ids:
        _build_row_at(store, doc_id, Status.ANALYZING)
    rows = store.list_by_status(Status.ANALYZING, limit=10)
    updated_ats = [row.updated_at for row in rows]
    assert updated_ats == sorted(updated_ats, reverse=True)
    assert {row.doc_id for row in rows} == set(doc_ids)


def test_list_by_status_respects_limit() -> None:
    store = InMemoryLedgerStore()
    for i in range(5):
        _build_row_at(store, _new_doc_id(f"list-limit-{i}"), Status.ANALYZING)
    rows = store.list_by_status(Status.ANALYZING, limit=2)
    assert len(rows) == 2


def test_list_by_status_filters_correct_status_only() -> None:
    store = InMemoryLedgerStore()
    analyzing_id = _new_doc_id("list-filter-analyzing")
    chunking_id = _new_doc_id("list-filter-chunking")
    _build_row_at(store, analyzing_id, Status.ANALYZING)
    _build_row_at(store, chunking_id, Status.CHUNKING)
    rows = store.list_by_status(Status.ANALYZING, limit=10)
    assert {row.doc_id for row in rows} == {analyzing_id}


def test_list_by_status_empty_when_no_matches() -> None:
    store = InMemoryLedgerStore()
    assert store.list_by_status(Status.INDEXED, limit=10) == []


def test_list_by_status_limit_non_positive_returns_empty() -> None:
    store = InMemoryLedgerStore()
    _build_row_at(store, _new_doc_id("list-limit-zero"), Status.ANALYZING)
    assert store.list_by_status(Status.ANALYZING, limit=0) == []
    assert store.list_by_status(Status.ANALYZING, limit=-5) == []


# --- thread safety (AC6, F7) ------------------------------------------------------------


def test_thread_safety_concurrent_transitions_consistent_final_state() -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id("thread-safety-transitions")
    _build_row_at(store, doc_id, Status.ANALYZING)  # attempts == 1
    n = 25
    barrier = threading.Barrier(n)
    results: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        row = store.transition(doc_id, Status.ANALYZING, Status.ANALYZING)
        with lock:
            results.append(row.attempts)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == list(range(2, n + 2))
    final = store.get(doc_id)
    assert final is not None
    assert final.status == final.stage == Status.ANALYZING
    assert final.attempts == n + 1


def test_thread_safety_concurrent_create_same_doc_id() -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id("thread-safety-create")
    n = 25
    barrier = threading.Barrier(n)
    results: list[LedgerRow] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        row = store.create(doc_id, EnvelopeSummary(config_version="v1"))
        with lock:
            results.append(row)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == n
    ingested_ats = {row.ingested_at for row in results}
    assert len(ingested_ats) == 1


def test_concurrent_loss_exception_carries_observed_row_and_attempted_request() -> None:
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id("concurrent-loss")
    _build_row_at(store, doc_id, Status.INDEXING)
    barrier = threading.Barrier(2)
    winners: list[LedgerRow] = []
    losers: list[LedgerTransitionInvalid] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            row = store.transition(doc_id, Status.INDEXED, Status.INDEXED)
        except LedgerTransitionInvalid as exc:
            with lock:
                losers.append(exc)
        else:
            with lock:
                winners.append(row)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1
    assert len(losers) == 1
    exc = losers[0]
    assert exc.observed_row is not None
    assert exc.observed_row.status == Status.INDEXED
    assert exc.attempted_to_status == Status.INDEXED
    assert exc.attempted_stage == Status.INDEXED


# --- is_benign_concurrent_loss ----------------------------------------------------------


def _row(status: Status, stage: Status | None = None) -> LedgerRow:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return LedgerRow(
        doc_id="a" * 64,
        status=status,
        stage=stage if stage is not None else status,
        attempts=1,
        config_version="v1",
        ingested_at=now,
        updated_at=now,
    )


def test_is_benign_concurrent_loss_true_when_observed_equals_attempted() -> None:
    observed = _row(Status.ANALYZING)
    assert (
        is_benign_concurrent_loss(observed, Status.ANALYZING, Status.ANALYZING) is True
    )


def test_is_benign_concurrent_loss_true_when_observed_further_along_same_path() -> None:
    observed = _row(Status.INDEXED)
    assert (
        is_benign_concurrent_loss(observed, Status.ANALYZING, Status.ANALYZING) is True
    )


def test_is_benign_concurrent_loss_false_when_observed_row_is_none() -> None:
    assert is_benign_concurrent_loss(None, Status.ANALYZING, Status.ANALYZING) is False


def test_is_benign_concurrent_loss_false_when_observed_row_status_stage_mismatched() -> (
    None
):
    """Defensive-only branch (§1 step 3): an internally-inconsistent observed_row (should
    be unreachable via any real store) is never treated as benign."""
    observed = _row(Status.ANALYZING, stage=Status.CHUNKING)
    assert (
        is_benign_concurrent_loss(observed, Status.ANALYZING, Status.ANALYZING) is False
    )


def test_is_benign_concurrent_loss_false_when_observed_earlier_or_off_path() -> None:
    observed = _row(Status.ANALYZING)
    assert (
        is_benign_concurrent_loss(observed, Status.CHUNKING, Status.CHUNKING) is False
    )
    assert (
        is_benign_concurrent_loss(observed, Status.CHUNKING, Status.EMBEDDING) is False
    )


def test_is_benign_concurrent_loss_false_when_attempted_non_failed_and_observed_failed() -> (
    None
):
    observed = _row(Status.FAILED)
    assert (
        is_benign_concurrent_loss(observed, Status.ANALYZING, Status.ANALYZING) is False
    )


def test_is_benign_concurrent_loss_true_when_attempted_failed_and_observed_indexed() -> (
    None
):
    observed = _row(Status.INDEXED)
    result = is_benign_concurrent_loss(observed, Status.FAILED, Status.FAILED)
    assert result is True


@pytest.mark.parametrize(
    "mid_status", [Status.ANALYZING, Status.CHUNKING, Status.EMBEDDING, Status.INDEXING]
)
def test_is_benign_concurrent_loss_false_when_attempted_failed_and_observed_mid_pipeline(
    mid_status: Status,
) -> None:
    observed = _row(mid_status)
    result = is_benign_concurrent_loss(observed, Status.FAILED, Status.FAILED)
    assert result is False


def test_is_benign_concurrent_loss_false_when_attempted_failed_and_observed_failed() -> (
    None
):
    observed = _row(Status.FAILED)
    result = is_benign_concurrent_loss(observed, Status.FAILED, Status.FAILED)
    assert result is False


@given(
    observed_status=st.sampled_from([None, *_ALL_STATUSES]),
    attempted_to_status=st.sampled_from(_ALL_STATUSES),
    attempted_stage=st.sampled_from(_ALL_STATUSES),
)
@settings(max_examples=500)
def test_is_benign_concurrent_loss_never_raises(
    observed_status: Status | None,
    attempted_to_status: Status,
    attempted_stage: Status,
) -> None:
    observed_row = None if observed_status is None else _row(observed_status)
    result = is_benign_concurrent_loss(
        observed_row, attempted_to_status, attempted_stage
    )
    assert isinstance(result, bool)


# --- config/code sync ------------------------------------------------------------------


def test_config_default_list_limit_matches_code_default() -> None:
    config_path = Path(__file__).resolve().parents[2] / "config" / "ledger.yaml"
    text = config_path.read_text(encoding="utf-8")
    match = re.search(r"default_list_limit:\s*(\d+)", text)
    assert match is not None
    config_value = int(match.group(1))

    list_by_status_default = (
        inspect.signature(LedgerStore.list_by_status).parameters["limit"].default
    )
    init_default = (
        inspect.signature(InMemoryLedgerStore.__init__)
        .parameters["default_list_limit"]
        .default
    )

    assert config_value == list_by_status_default == init_default


# --- property-based tests (hypothesis) --------------------------------------------------


@given(moves=st.lists(st.booleans(), min_size=0, max_size=15))
@settings(max_examples=200)
def test_property_legal_transition_sequences_end_consistent(moves: list[bool]) -> None:
    """A random walk of same-status retries (True) and forward advances (False) through
    the legal graph, starting from PENDING, always leaves the row's status/stage/attempts
    mutually consistent with the last-applied edge (A1/A4)."""
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id("property-legal-sequence")
    store.create(doc_id, EnvelopeSummary(config_version="v1"))
    idx = 0
    attempts = 0
    for retry in moves:
        status = _FORWARD_ORDER[idx]
        if status == Status.INDEXED:
            break  # terminal: no further legal transition() call is possible
        if retry:
            store.transition(doc_id, status, status)
            attempts += 1
        else:
            idx += 1
            next_status = _FORWARD_ORDER[idx]
            store.transition(doc_id, next_status, next_status)
            attempts = 1

    row = store.get(doc_id)
    assert row is not None
    assert row.status == row.stage == _FORWARD_ORDER[idx]
    assert row.attempts == attempts


@given(
    current_status=st.sampled_from(_ALL_STATUSES),
    to_status=st.sampled_from(_ALL_STATUSES),
)
@settings(max_examples=200)
def test_property_illegal_edges_never_silently_applied(
    current_status: Status, to_status: Status
) -> None:
    """For sampled (current_status, to_status) pairs outside the legal set, transition()
    always raises and the row is provably unmutated (compared before/after)."""
    assume(
        to_status not in _LEGAL_TRANSITIONS[current_status]
        and to_status != current_status
    )
    store = InMemoryLedgerStore()
    doc_id = _new_doc_id(f"property-illegal-{current_status.value}-{to_status.value}")
    _build_row_at(store, doc_id, current_status)
    before = store.get(doc_id)
    with pytest.raises(LedgerTransitionInvalid):
        store.transition(doc_id, to_status, to_status)
    after = store.get(doc_id)
    assert before == after
