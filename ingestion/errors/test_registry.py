"""Unit tests for the Failure Taxonomy (Contract #4) — REQ-004.

Every registry-core test constructs its own fresh ``ErrorRegistry()`` instance
(Assumption A2) — none of them mutate ``_DEFAULT_REGISTRY``, the process-wide singleton
shared with the rest of the test suite, including this module's own bootstrap
self-registration and delegation tests: those read already-registered codes
(``ERROR_CODE_REGISTRATION_INVALID``, ``IdentityInvalid.code``) rather than registering
new ones, so ``_DEFAULT_REGISTRY``'s contents stay exactly the set of codes every
module's own top-level ``register_error`` call declares, for the whole test session (see
``test_config_known_codes_matches_all_codes_after_importing_all_modules``).
"""

from __future__ import annotations

import itertools
import logging
import re
import threading
from pathlib import Path

import pytest

from ingestion.errors.registry import (
    ErrorCode,
    ErrorCodeRegistrationInvalid,
    ErrorRegistry,
    RaggedError,
    Severity,
    all_codes,
    classify,
)
from ingestion.identity.derive import IdentityInvalid

_counter = itertools.count()


def _new_code(label: str) -> str:
    """Generates a fresh, collision-free error code string for a test."""
    return f"TEST_{label.upper()}_{next(_counter)}"


class _MinimalRaggedError(RaggedError):
    """Minimal concrete RaggedError subclass for constructing test instances."""


# --- register() success ----------------------------------------------------------------


def test_register_error_success_returns_error_code() -> None:
    registry = ErrorRegistry()
    code = _new_code("success")
    result = registry.register(code, Severity.TRANSIENT, "a test description")
    assert result == ErrorCode(
        code=code, severity=Severity.TRANSIENT, description="a test description"
    )


def test_register_error_then_classify_returns_registered_severity() -> None:
    registry = ErrorRegistry()
    code = _new_code("classify")
    registry.register(code, Severity.PERMANENT, "a test description")
    assert registry.classify(code) == Severity.PERMANENT


# --- register() duplicate rejection (F1, AC2) --------------------------------------------


def test_register_error_duplicate_raises_error_code_registration_invalid_permanent() -> (
    None
):
    registry = ErrorRegistry()
    code = _new_code("duplicate")
    original = registry.register(code, Severity.PERMANENT, "first registration")
    with pytest.raises(ErrorCodeRegistrationInvalid) as exc_info:
        registry.register(code, Severity.TRANSIENT, "second registration")
    err = exc_info.value
    assert err.classification == "PERMANENT"
    assert err.reason == "duplicate registration"
    assert err.existing == original


def test_register_error_duplicate_does_not_overwrite_existing_entry() -> None:
    registry = ErrorRegistry()
    code = _new_code("no-overwrite")
    registry.register(code, Severity.PERMANENT, "original description")
    with pytest.raises(ErrorCodeRegistrationInvalid):
        registry.register(code, Severity.TRANSIENT, "attempted overwrite")
    assert registry.classify(code) == Severity.PERMANENT
    entries = [entry for entry in registry.all_codes() if entry.code == code]
    assert entries == [
        ErrorCode(
            code=code, severity=Severity.PERMANENT, description="original description"
        )
    ]


# --- register() malformed code (F2) -------------------------------------------------------


def test_register_error_rejects_empty_code() -> None:
    registry = ErrorRegistry()
    with pytest.raises(ErrorCodeRegistrationInvalid) as exc_info:
        registry.register("", Severity.PERMANENT, "a description")
    assert exc_info.value.reason == "invalid code"
    assert exc_info.value.existing is None


def test_register_error_rejects_none_code() -> None:
    registry = ErrorRegistry()
    with pytest.raises(ErrorCodeRegistrationInvalid) as exc_info:
        registry.register(None, Severity.PERMANENT, "a description")  # type: ignore[arg-type]
    assert exc_info.value.reason == "invalid code"
    assert exc_info.value.existing is None


def test_register_error_rejects_non_str_code() -> None:
    registry = ErrorRegistry()
    with pytest.raises(ErrorCodeRegistrationInvalid) as exc_info:
        registry.register(12345, Severity.PERMANENT, "a description")  # type: ignore[arg-type]
    assert exc_info.value.reason == "invalid code"
    assert exc_info.value.existing is None


# --- register() malformed severity (F3) ---------------------------------------------------


def test_register_error_rejects_invalid_severity_type() -> None:
    registry = ErrorRegistry()
    code = _new_code("bad-severity")
    with pytest.raises(ErrorCodeRegistrationInvalid) as exc_info:
        registry.register(code, "PERMANENT", "a description")  # type: ignore[arg-type]
    assert exc_info.value.reason == "invalid severity"
    assert exc_info.value.existing is None


# --- register() malformed description (F4) -------------------------------------------------


def test_register_error_rejects_empty_description() -> None:
    registry = ErrorRegistry()
    code = _new_code("empty-desc")
    with pytest.raises(ErrorCodeRegistrationInvalid) as exc_info:
        registry.register(code, Severity.PERMANENT, "")
    assert exc_info.value.reason == "invalid description"
    assert exc_info.value.existing is None


def test_register_error_rejects_none_description() -> None:
    registry = ErrorRegistry()
    code = _new_code("none-desc")
    with pytest.raises(ErrorCodeRegistrationInvalid) as exc_info:
        registry.register(code, Severity.PERMANENT, None)  # type: ignore[arg-type]
    assert exc_info.value.reason == "invalid description"
    assert exc_info.value.existing is None


# --- register() mandated check order (§1, §3 F1-F4) -----------------------------------------


def test_register_error_duplicate_check_runs_before_shape_validation() -> None:
    registry = ErrorRegistry()
    code = _new_code("order")
    original = registry.register(code, Severity.PERMANENT, "original description")
    with pytest.raises(ErrorCodeRegistrationInvalid) as exc_info:
        registry.register(code, "not-a-severity-member", "")  # type: ignore[arg-type]
    err = exc_info.value
    assert err.reason == "duplicate registration"
    assert err.existing == original


# --- classify() unknown-code fallback (F5, AC3) ----------------------------------------------


def test_classify_unknown_code_returns_transient() -> None:
    registry = ErrorRegistry()
    assert registry.classify(_new_code("unknown")) == Severity.TRANSIENT


def test_classify_unknown_code_logs_warning_naming_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = ErrorRegistry()
    code = _new_code("warn")
    with caplog.at_level(logging.WARNING, logger="ingestion.errors.registry"):
        registry.classify(code)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        code in r.getMessage() or code == getattr(r, "error_code", None)
        for r in warnings
    )


@pytest.mark.parametrize(
    "bad_code",
    ["", "not-hex-garbage", "x" * 10_000, "\x00\x01", "IDENTITY_INVALID"],
    ids=[
        "empty",
        "garbage",
        "very_long",
        "control_chars",
        "plausible_but_unregistered",
    ],
)
def test_classify_never_raises_for_any_input(bad_code: str) -> None:
    registry = ErrorRegistry()
    result = registry.classify(bad_code)
    assert result in (Severity.PERMANENT, Severity.TRANSIENT)


# --- all_codes() (AC6) --------------------------------------------------------------------


def test_all_codes_empty_registry_returns_empty_list() -> None:
    registry = ErrorRegistry()
    assert registry.all_codes() == []


def test_all_codes_returns_every_registered_code_with_severity_and_description() -> (
    None
):
    registry = ErrorRegistry()
    code_a, code_b = _new_code("a"), _new_code("b")
    registry.register(code_a, Severity.PERMANENT, "description a")
    registry.register(code_b, Severity.TRANSIENT, "description b")
    codes = registry.all_codes()
    assert (
        ErrorCode(code=code_a, severity=Severity.PERMANENT, description="description a")
        in codes
    )
    assert (
        ErrorCode(code=code_b, severity=Severity.TRANSIENT, description="description b")
        in codes
    )


def test_all_codes_preserves_registration_order() -> None:
    registry = ErrorRegistry()
    codes_in_order = [_new_code(f"order-{i}") for i in range(5)]
    for code in codes_in_order:
        registry.register(code, Severity.PERMANENT, "description")
    assert [entry.code for entry in registry.all_codes()] == codes_in_order


def test_all_codes_returns_a_fresh_list_each_call() -> None:
    registry = ErrorRegistry()
    code = _new_code("fresh-list")
    registry.register(code, Severity.PERMANENT, "description")
    first_call = registry.all_codes()
    first_call.clear()
    second_call = registry.all_codes()
    assert len(second_call) == 1


# --- RaggedError (AC5, Assumption A6) -----------------------------------------------------


def test_ragged_error_carries_error_code() -> None:
    exc = _MinimalRaggedError("boom", error_code="SOME_CODE")
    assert exc.error_code == "SOME_CODE"
    assert isinstance(exc, Exception)


def test_ragged_error_severity_property_delegates_to_default_registry() -> None:
    # Uses an already-registered code (IdentityInvalid.code, registered at import time by
    # ingestion.identity.derive) rather than registering a fresh code into _DEFAULT_REGISTRY,
    # so this test never permanently mutates the process-wide registry shared with the rest
    # of the suite (see test_config_known_codes_matches_all_codes_after_importing_all_modules).
    exc = _MinimalRaggedError("boom", error_code=IdentityInvalid.code)
    assert exc.severity == classify(IdentityInvalid.code) == Severity.PERMANENT


def test_ragged_error_severity_property_ignores_local_registry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    code = _new_code("local-only")
    local_registry = ErrorRegistry()
    local_registry.register(code, Severity.PERMANENT, "registered only locally")

    exc = _MinimalRaggedError("boom", error_code=code)
    with caplog.at_level(logging.WARNING, logger="ingestion.errors.registry"):
        severity = exc.severity

    assert local_registry.classify(code) == Severity.PERMANENT
    assert severity == Severity.TRANSIENT
    assert severity != local_registry.classify(code)
    assert any(getattr(r, "error_code", None) == code for r in caplog.records)


# --- ErrorCode frozen dataclass ------------------------------------------------------------


def test_error_code_dataclass_is_frozen() -> None:
    entry = ErrorCode(code="X", severity=Severity.PERMANENT, description="d")
    with pytest.raises(AttributeError):
        entry.code = "Y"  # type: ignore[misc]


# --- thread safety (AC7, F6-F8) -------------------------------------------------------------


def test_thread_safety_concurrent_registrations_distinct_codes_all_succeed() -> None:
    registry = ErrorRegistry()
    n = 25
    codes = [_new_code(f"distinct-{i}") for i in range(n)]
    barrier = threading.Barrier(n)
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker(code: str) -> None:
        barrier.wait()
        try:
            registry.register(code, Severity.PERMANENT, f"description for {code}")
        except (
            ErrorCodeRegistrationInvalid
        ) as exc:  # pragma: no cover - failure path only
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(code,)) for code in codes]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert {entry.code for entry in registry.all_codes()} == set(codes)


def test_thread_safety_concurrent_registration_same_code_exactly_one_wins() -> None:
    registry = ErrorRegistry()
    code = _new_code("race")
    n = 25
    barrier = threading.Barrier(n)
    winners: list[ErrorCode] = []
    losers: list[ErrorCodeRegistrationInvalid] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()
        try:
            result = registry.register(code, Severity.PERMANENT, f"description {i}")
        except ErrorCodeRegistrationInvalid as exc:
            with lock:
                losers.append(exc)
        else:
            with lock:
                winners.append(result)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1
    assert len(losers) == n - 1
    assert all(loser.classification == "PERMANENT" for loser in losers)
    final_entries = [entry for entry in registry.all_codes() if entry.code == code]
    assert final_entries == [winners[0]]


def test_thread_safety_reads_never_observe_torn_state() -> None:
    registry = ErrorRegistry()
    stop = threading.Event()
    anomalies: list[str] = []
    lock = threading.Lock()

    def writer() -> None:
        for i in range(300):
            registry.register(
                _new_code(f"torn-{i}"), Severity.PERMANENT, f"description {i}"
            )
        stop.set()

    def reader() -> None:
        while not stop.is_set():
            for entry in registry.all_codes():
                is_well_formed = (
                    isinstance(entry, ErrorCode)
                    and isinstance(entry.code, str)
                    and entry.code != ""
                    and isinstance(entry.severity, Severity)
                    and isinstance(entry.description, str)
                    and entry.description != ""
                )
                if not is_well_formed:
                    with lock:
                        anomalies.append(repr(entry))

    writer_thread = threading.Thread(target=writer)
    reader_threads = [threading.Thread(target=reader) for _ in range(4)]
    writer_thread.start()
    for t in reader_threads:
        t.start()
    writer_thread.join()
    for t in reader_threads:
        t.join()

    assert anomalies == []


# --- bootstrap self-registration (§1, §3) ---------------------------------------------------


def test_error_code_registration_invalid_registered_in_default_registry() -> None:
    assert classify("ERROR_CODE_REGISTRATION_INVALID") == Severity.PERMANENT


# --- config/code sync (§6) ------------------------------------------------------------------


def test_config_known_codes_matches_all_codes_after_importing_all_modules() -> None:
    # Import every module that registers its own codes into _DEFAULT_REGISTRY before
    # comparing against config/errors.yaml's known_codes block.
    import ingestion.analyzer.model  # noqa: F401
    import ingestion.chunker.model  # noqa: F401
    import ingestion.embedder.model  # noqa: F401
    import ingestion.envelope.model  # noqa: F401
    import ingestion.identity.derive  # noqa: F401
    import ingestion.ledger.store  # noqa: F401

    config_path = Path(__file__).resolve().parents[2] / "config" / "errors.yaml"
    text = config_path.read_text(encoding="utf-8")
    match = re.search(r"known_codes:.*?\n((?:\s+[A-Z_]+:\s*\w+\n)+)", text, re.DOTALL)
    assert match is not None
    known_codes = dict(re.findall(r"([A-Z_]+):\s*(\w+)", match.group(1)))

    live_codes = {entry.code: entry.severity.value for entry in all_codes()}
    assert known_codes == live_codes
