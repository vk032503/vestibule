"""Unit tests for `vestibule.vertical.loader.VerticalLoader` (REQ-009)."""

from __future__ import annotations

import inspect
import itertools
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vestibule.envelope.model import ArrivalEnvelope, TrustTier, build_envelope
from vestibule.errors.registry import Severity
from vestibule.ledger.store import (
    EnvelopeSummary,
    InMemoryLedgerStore,
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
from vestibule.vertical.loader import (
    SourceMappingRule,
    VerticalLoader,
    VerticalLoaderConfig,
)
from vestibule.vertical.model import (
    VERTICAL_MAPPING_INVALID,
    VERTICAL_UNRESOLVED,
    ResolutionSource,
    VerticalError,
    VerticalResolution,
)
from vestibule.vertical.review_queue import ReviewRegistry

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
    """Minimal in-memory `ScenarioStore` for exercising `VerticalLoader` hermetically."""

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


def _envelope(
    *,
    source: str = "blob:misc",
    vertical: str | None = None,
    scenario_id: str | None = None,
) -> ArrivalEnvelope:
    return build_envelope(
        source=source,
        blob_path=f"path-{next(_counter)}",
        content_hash="a" * 64,
        trust_tier=TrustTier.INTERNAL,
        vertical=vertical,
        scenario_id=scenario_id,
    )


def _loader(
    store: ScenarioStore,
    ledger: InMemoryLedgerStore,
    *,
    source_mappings: tuple[SourceMappingRule, ...] = (),
    park_unresolved: bool = True,
    suggest_verticals: bool = True,
    review_registry: ReviewRegistry | None = None,
) -> VerticalLoader:
    config = VerticalLoaderConfig(
        source_mappings=source_mappings,
        park_unresolved=park_unresolved,
        suggest_verticals=suggest_verticals,
    )
    return VerticalLoader(
        store,
        ledger,
        config,
        review_registry=(
            review_registry if review_registry is not None else ReviewRegistry()
        ),
    )


# --- resolution order: review assignment, step 0 (REQ-009 review-queue round-trip fix) ------


def test_resolve_review_assignment_takes_priority_over_envelope_scenario_id() -> None:
    """Step 0 (`LedgerRow.assigned_scenario_id`) outranks even an explicit
    `envelope.scenario_id` when both are present and disagree — a human's assignment via
    the review queue is a strictly more authoritative signal than whatever the envelope
    itself declares."""
    store = _DictScenarioStore(
        {"hr-1": _scenario("hr-1", "hr"), "legal-1": _scenario("legal-1", "legal")}
    )
    ledger = InMemoryLedgerStore()
    envelope = _envelope(scenario_id="legal-1")
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="v1"))
    ledger.transition(
        envelope.doc_id,
        Status.NEEDS_REVIEW,
        Status.NEEDS_REVIEW,
        error_code=VERTICAL_UNRESOLVED,
    )
    ledger.transition(
        envelope.doc_id,
        Status.PENDING,
        Status.PENDING,
        assigned_scenario_id="hr-1",
    )
    loader = _loader(store, ledger)

    resolution = loader.resolve(envelope)

    assert resolution.scenario_id == "hr-1"
    assert resolution.vertical == "hr"
    assert resolution.source is ResolutionSource.REVIEW_ASSIGNMENT
    assert resolution.confidence == "explicit"


def test_resolve_review_assignment_missing_scenario_raises_scenario_not_found() -> None:
    """A `LedgerRow.assigned_scenario_id` that no longer resolves (e.g. the scenario was
    deleted after `ReviewQueue.assign()` was called) is a config error, raised
    immediately — never silently downgraded to falling through to the envelope."""
    store = _DictScenarioStore({})
    ledger = InMemoryLedgerStore()
    envelope = _envelope(source="blob:unrouted")
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="v1"))
    ledger.transition(
        envelope.doc_id,
        Status.NEEDS_REVIEW,
        Status.NEEDS_REVIEW,
        error_code=VERTICAL_UNRESOLVED,
    )
    ledger.transition(
        envelope.doc_id,
        Status.PENDING,
        Status.PENDING,
        assigned_scenario_id="does-not-exist",
    )
    loader = _loader(store, ledger)

    with pytest.raises(ScenarioError) as exc_info:
        loader.resolve(envelope)
    assert exc_info.value.error_code == SCENARIO_NOT_FOUND


# --- resolution order: scenario_id (AC1) --------------------------------------------------


def test_resolve_scenario_id_returns_explicit_envelope_scenario_id() -> None:
    store = _DictScenarioStore({"hr-1": _scenario("hr-1", "hr")})
    loader = _loader(store, InMemoryLedgerStore())
    envelope = _envelope(scenario_id="hr-1")
    resolution = loader.resolve(envelope)
    assert resolution.scenario_id == "hr-1"
    assert resolution.vertical == "hr"
    assert resolution.source is ResolutionSource.ENVELOPE_SCENARIO_ID
    assert resolution.confidence == "explicit"
    assert resolution.reason


def test_load_scenario_id_returns_scenario_and_resolution() -> None:
    scenario = _scenario("hr-1", "hr")
    store = _DictScenarioStore({"hr-1": scenario})
    ledger = InMemoryLedgerStore()
    loader = _loader(store, ledger)
    envelope = _envelope(scenario_id="hr-1")
    result_scenario, resolution = loader.load(envelope)
    assert result_scenario == scenario
    assert resolution.scenario_id == "hr-1"


# --- resolution order: vertical (AC2) -------------------------------------------------------


def test_resolve_vertical_only_returns_explicit_envelope_vertical() -> None:
    store = _DictScenarioStore({"legal-1": _scenario("legal-1", "legal")})
    loader = _loader(store, InMemoryLedgerStore())
    envelope = _envelope(vertical="legal")
    resolution = loader.resolve(envelope)
    assert resolution.scenario_id == "legal-1"
    assert resolution.source is ResolutionSource.ENVELOPE_VERTICAL
    assert resolution.confidence == "explicit"


# --- resolution order: source mapping (AC3) --------------------------------------------------


def test_resolve_source_mapping_glob_match_is_inferred() -> None:
    store = _DictScenarioStore({"hr-1": _scenario("hr-1", "hr")})
    loader = _loader(
        store,
        InMemoryLedgerStore(),
        source_mappings=(SourceMappingRule(match="blob:hr-*", vertical="hr"),),
    )
    envelope = _envelope(source="blob:hr-uploads")
    resolution = loader.resolve(envelope)
    assert resolution.scenario_id == "hr-1"
    assert resolution.source is ResolutionSource.SOURCE_MAPPING
    assert resolution.confidence == "inferred"


def test_resolve_exact_source_mapping_wins_over_matching_glob() -> None:
    store = _DictScenarioStore(
        {"hr-1": _scenario("hr-1", "hr"), "legal-1": _scenario("legal-1", "legal")}
    )
    loader = _loader(
        store,
        InMemoryLedgerStore(),
        source_mappings=(
            SourceMappingRule(match="blob:*", vertical="hr"),
            SourceMappingRule(match="blob:legal-site", vertical="legal"),
        ),
    )
    envelope = _envelope(source="blob:legal-site")
    resolution = loader.resolve(envelope)
    assert resolution.vertical == "legal"


def test_resolve_among_matching_globs_first_listed_wins() -> None:
    store = _DictScenarioStore(
        {"hr-1": _scenario("hr-1", "hr"), "legal-1": _scenario("legal-1", "legal")}
    )
    loader = _loader(
        store,
        InMemoryLedgerStore(),
        source_mappings=(
            SourceMappingRule(match="blob:hr-*", vertical="hr"),
            SourceMappingRule(match="blob:*-uploads", vertical="legal"),
        ),
    )
    envelope = _envelope(source="blob:hr-uploads")
    resolution = loader.resolve(envelope)
    assert resolution.vertical == "hr"


# --- unresolved: parking (AC4) ---------------------------------------------------------------


def test_load_unresolved_parks_and_raises_vertical_unresolved() -> None:
    store = _DictScenarioStore({})
    ledger = InMemoryLedgerStore()
    doc_id_envelope = _envelope(source="blob:unknown")
    ledger.create(doc_id_envelope.doc_id, EnvelopeSummary(config_version="v1"))
    loader = _loader(store, ledger)

    with pytest.raises(VerticalError) as exc_info:
        loader.load(doc_id_envelope)
    assert exc_info.value.error_code == VERTICAL_UNRESOLVED
    assert exc_info.value.severity == Severity.PERMANENT

    row = ledger.get(doc_id_envelope.doc_id)
    assert row is not None
    # NEEDS_REVIEW, never FAILED — a parked document is not marked failed (see
    # VerticalLoader._park's own docstring for the PERMANENT-but-not-dead reasoning).
    assert row.status is Status.NEEDS_REVIEW


def test_load_unresolved_records_review_registry_metadata() -> None:
    store = _DictScenarioStore({})
    ledger = InMemoryLedgerStore()
    registry = ReviewRegistry()
    envelope = _envelope(source="blob:unknown")
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="v1"))
    loader = _loader(store, ledger, review_registry=registry)

    with pytest.raises(VerticalError):
        loader.load(envelope)

    metadata = registry.get(envelope.doc_id)
    assert metadata is not None
    assert metadata.source == envelope.source
    assert metadata.blob_path == envelope.blob_path


def test_load_unresolved_park_benign_concurrent_progress_still_raises() -> None:
    """Mirrors the chunker/embedder/indexer precedent for `is_benign_concurrent_loss`:
    pre-advancing the row past PENDING before a losing park attempt is a benign lost
    race (a concurrent winner legitimately advanced the document some other way), not a
    genuine bug — VerticalError is still raised so the caller stops on this exact
    redelivery, but the ledger write itself is skipped (no downgrade of real progress)."""
    store = _DictScenarioStore({})
    ledger = InMemoryLedgerStore()
    envelope = _envelope(source="blob:unknown")
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="v1"))
    ledger.transition(envelope.doc_id, Status.ANALYZING, Status.ANALYZING)
    loader = _loader(store, ledger)

    with pytest.raises(VerticalError) as exc_info:
        loader.load(envelope)
    assert exc_info.value.error_code == VERTICAL_UNRESOLVED

    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status is Status.ANALYZING


def test_load_unresolved_park_non_benign_reraises_ledger_transition_invalid() -> None:
    store = _DictScenarioStore({})
    ledger = InMemoryLedgerStore()
    envelope = _envelope(source="blob:unknown")
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="v1"))
    ledger.transition(
        envelope.doc_id, Status.FAILED, Status.FAILED, error_code="PRIOR_FAILURE"
    )
    loader = _loader(store, ledger)

    with pytest.raises(LedgerTransitionInvalid):
        loader.load(envelope)


def test_park_unresolved_false_raises_without_ledger_transition() -> None:
    store = _DictScenarioStore({})
    ledger = InMemoryLedgerStore()
    envelope = _envelope(source="blob:unknown")
    created = ledger.create(envelope.doc_id, EnvelopeSummary(config_version="v1"))
    loader = _loader(store, ledger, park_unresolved=False)

    with pytest.raises(VerticalError) as exc_info:
        loader.load(envelope)
    assert exc_info.value.error_code == VERTICAL_UNRESOLVED

    row = ledger.get(envelope.doc_id)
    assert row == created
    assert row is not None
    assert row.status is Status.PENDING


def test_park_unresolved_false_with_no_seeded_row_still_raises_without_write() -> None:
    store = _DictScenarioStore({})
    ledger = InMemoryLedgerStore()
    envelope = _envelope(source="blob:unknown")
    loader = _loader(store, ledger, park_unresolved=False)

    with pytest.raises(VerticalError) as exc_info:
        loader.load(envelope)
    assert exc_info.value.error_code == VERTICAL_UNRESOLVED
    assert ledger.get(envelope.doc_id) is None


# --- AC5: ambiguous vs. misconfigured (the core distinction) --------------------------------


def test_stated_vertical_with_no_scenario_raises_scenario_not_found_and_does_not_park() -> (
    None
):
    """AC5, core of the design: a *stated* vertical with no matching scenario is a config
    error, not an ambiguity — must raise SCENARIO_NOT_FOUND and must NOT transition the
    ledger row to needs_review. This test is written so it would fail if the
    ambiguous-vs-misconfigured paths were ever collapsed into one behavior."""
    store = _DictScenarioStore({})
    ledger = InMemoryLedgerStore()
    envelope = _envelope(vertical="marketing")
    created = ledger.create(envelope.doc_id, EnvelopeSummary(config_version="v1"))
    loader = _loader(store, ledger)

    with pytest.raises(ScenarioError) as exc_info:
        loader.load(envelope)
    assert exc_info.value.error_code == SCENARIO_NOT_FOUND

    row = ledger.get(envelope.doc_id)
    assert row == created
    assert row is not None
    # Still PENDING, never NEEDS_REVIEW — a stated-but-missing vertical is a config
    # error (AC5), not an ambiguity to park.
    assert row.status is Status.PENDING


def test_stated_scenario_id_with_no_scenario_raises_scenario_not_found_and_does_not_park() -> (
    None
):
    store = _DictScenarioStore({})
    ledger = InMemoryLedgerStore()
    envelope = _envelope(scenario_id="nonexistent-scenario")
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="v1"))
    loader = _loader(store, ledger)

    with pytest.raises(ScenarioError) as exc_info:
        loader.load(envelope)
    assert exc_info.value.error_code == SCENARIO_NOT_FOUND

    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status is Status.PENDING


def test_stated_vertical_missing_scenario_error_is_distinct_from_unresolved_park() -> (
    None
):
    """Directly contrasts both AC5 paths against the SAME loader/store, proving they are
    provably different code paths, not the same behavior tested twice."""
    store = _DictScenarioStore({})
    ledger = InMemoryLedgerStore()
    loader = _loader(store, ledger)

    stated_envelope = _envelope(vertical="marketing")
    ledger.create(stated_envelope.doc_id, EnvelopeSummary(config_version="v1"))
    with pytest.raises(ScenarioError):
        loader.load(stated_envelope)
    stated_row = ledger.get(stated_envelope.doc_id)
    assert stated_row is not None
    assert stated_row.status is Status.PENDING

    unresolved_envelope = _envelope(source="blob:no-signal-at-all")
    ledger.create(unresolved_envelope.doc_id, EnvelopeSummary(config_version="v1"))
    with pytest.raises(VerticalError):
        loader.load(unresolved_envelope)
    unresolved_row = ledger.get(unresolved_envelope.doc_id)
    assert unresolved_row is not None
    assert unresolved_row.status is Status.NEEDS_REVIEW


# --- VERTICAL_MAPPING_INVALID at construction (AC6) ------------------------------------------


def test_construction_rejects_mapping_referencing_unknown_vertical() -> None:
    store = _DictScenarioStore({})
    with pytest.raises(VerticalError) as exc_info:
        _loader(
            store,
            InMemoryLedgerStore(),
            source_mappings=(SourceMappingRule(match="blob:hr-*", vertical="hr"),),
        )
    assert exc_info.value.error_code == VERTICAL_MAPPING_INVALID
    assert exc_info.value.severity == Severity.PERMANENT


def test_construction_validates_mappings_before_any_document_processed() -> None:
    """No document should ever reach resolve()/load() if construction itself fails."""
    store = _DictScenarioStore({})
    with pytest.raises(VerticalError):
        _loader(
            store,
            InMemoryLedgerStore(),
            source_mappings=(SourceMappingRule(match="blob:hr-*", vertical="hr"),),
        )
    # No assertions possible on a loader that failed to construct — the raise itself,
    # before any resolve()/load() call could occur, is the guarantee under test.


# --- defensive TOCTOU guards (scenario disabled/deleted after construction/resolve) ---------


class _DisablesAfterFirstLookupScenarioStore(_DictScenarioStore):
    """`get_by_vertical` succeeds once (construction-time validation), then always
    misses afterward — simulates a scenario being disabled after `VerticalLoader`
    construction but before a matching document arrives."""

    def __init__(self, scenarios: dict[str, Scenario]) -> None:
        super().__init__(scenarios)
        self._calls = 0

    def get_by_vertical(self, vertical: str) -> Scenario | None:
        self._calls += 1
        if self._calls == 1:
            return super().get_by_vertical(vertical)
        return None


def test_source_mapping_scenario_disabled_after_construction_raises_scenario_not_found() -> (
    None
):
    store = _DisablesAfterFirstLookupScenarioStore({"hr-1": _scenario("hr-1", "hr")})
    loader = _loader(
        store,
        InMemoryLedgerStore(),
        source_mappings=(SourceMappingRule(match="blob:hr-*", vertical="hr"),),
    )
    with pytest.raises(ScenarioError) as exc_info:
        loader.resolve(_envelope(source="blob:hr-uploads"))
    assert exc_info.value.error_code == SCENARIO_NOT_FOUND


class _VerticalOnlyScenarioStore(_DictScenarioStore):
    """`get()` always misses; `get_by_vertical()` succeeds — simulates a scenario
    deleted between `resolve()` and `load()`'s own follow-up `get()` call."""

    def get(self, scenario_id: str) -> Scenario | None:
        return None


def test_load_toctou_scenario_deleted_between_resolve_and_get_raises_scenario_not_found() -> (
    None
):
    store = _VerticalOnlyScenarioStore({"hr-1": _scenario("hr-1", "hr")})
    loader = _loader(store, InMemoryLedgerStore())
    envelope = _envelope(vertical="hr")

    with pytest.raises(ScenarioError) as exc_info:
        loader.load(envelope)
    assert exc_info.value.error_code == SCENARIO_NOT_FOUND


# --- suggested_verticals (parking hint) -------------------------------------------------------


def test_parking_suggests_verticals_with_matching_prefix() -> None:
    store = _DictScenarioStore({"hr-1": _scenario("hr-1", "hr")})
    ledger = InMemoryLedgerStore()
    registry = ReviewRegistry()
    envelope = _envelope(source="blob:unknown-uploads")
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="v1"))
    loader = _loader(
        store,
        ledger,
        source_mappings=(SourceMappingRule(match="blob:hr-*", vertical="hr"),),
        review_registry=registry,
    )

    with pytest.raises(VerticalError):
        loader.load(envelope)

    metadata = registry.get(envelope.doc_id)
    assert metadata is not None
    assert metadata.suggested_verticals == ("hr",)


def test_parking_suggest_verticals_disabled_yields_empty_list() -> None:
    store = _DictScenarioStore({"hr-1": _scenario("hr-1", "hr")})
    ledger = InMemoryLedgerStore()
    registry = ReviewRegistry()
    envelope = _envelope(source="blob:unknown-uploads")
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="v1"))
    loader = _loader(
        store,
        ledger,
        source_mappings=(SourceMappingRule(match="blob:hr-*", vertical="hr"),),
        suggest_verticals=False,
        review_registry=registry,
    )

    with pytest.raises(VerticalError):
        loader.load(envelope)

    metadata = registry.get(envelope.doc_id)
    assert metadata is not None
    assert metadata.suggested_verticals == ()


def test_suggested_verticals_dedupes_and_excludes_different_prefix_rules() -> None:
    """Only rules sharing the source's prefix-before-first-`:` are suggested, deduped by
    vertical; a rule with a different prefix must never appear."""
    store = _DictScenarioStore(
        {"hr-1": _scenario("hr-1", "hr"), "legal-1": _scenario("legal-1", "legal")}
    )
    ledger = InMemoryLedgerStore()
    registry = ReviewRegistry()
    envelope = _envelope(source="blob:unmatched-suffix")
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="v1"))
    loader = _loader(
        store,
        ledger,
        source_mappings=(
            SourceMappingRule(match="blob:hr-*", vertical="hr"),
            SourceMappingRule(match="blob:hr-legacy-*", vertical="hr"),
            SourceMappingRule(match="sharepoint:legal-site", vertical="legal"),
        ),
        review_registry=registry,
    )

    with pytest.raises(VerticalError):
        loader.load(envelope)

    metadata = registry.get(envelope.doc_id)
    assert metadata is not None
    assert metadata.suggested_verticals == ("hr",)


# --- property-based: resolve() always returns exactly one non-empty-reason resolution --------


@given(
    vertical=st.sampled_from([None, "hr"]),
    scenario_id=st.sampled_from([None, "hr-1"]),
    source=st.text(min_size=1, max_size=40),
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_resolve_always_returns_exactly_one_resolution_with_non_empty_reason(
    vertical: str | None, scenario_id: str | None, source: str
) -> None:
    store = _DictScenarioStore({"hr-1": _scenario("hr-1", "hr")})
    loader = _loader(store, InMemoryLedgerStore())
    envelope = build_envelope(
        source=source,
        blob_path=f"path-{next(_counter)}",
        content_hash="a" * 64,
        trust_tier=TrustTier.INTERNAL,
        vertical=vertical,
        scenario_id=scenario_id,
    )
    resolution = loader.resolve(envelope)
    assert isinstance(resolution, VerticalResolution)
    assert resolution.reason != ""


# --- config/code sync -------------------------------------------------------------------


def test_config_vertical_loader_yaml_matches_code_defaults() -> None:
    config_path = (
        Path(__file__).resolve().parents[3] / "config" / "vertical_loader.yaml"
    )
    text = config_path.read_text(encoding="utf-8")

    defaults = inspect.signature(VerticalLoaderConfig).parameters
    assert defaults["park_unresolved"].default is True
    assert defaults["suggest_verticals"].default is True
    assert defaults["source_mappings"].default == ()

    assert re.search(r"park_unresolved:\s*true", text) is not None
    assert re.search(r"suggest_verticals:\s*true", text) is not None
    assert re.search(r"source_mappings:\s*\[\]", text) is not None


def test_config_known_codes_includes_vertical_loader_codes() -> None:
    config_path = Path(__file__).resolve().parents[3] / "config" / "errors.yaml"
    text = config_path.read_text(encoding="utf-8")
    for code in (
        VERTICAL_UNRESOLVED,
        VERTICAL_MAPPING_INVALID,
        "VERTICAL_REJECTED",
        "REVIEW_ITEM_NOT_FOUND",
    ):
        assert f"{code}: PERMANENT" in text
