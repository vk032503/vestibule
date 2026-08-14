"""Unit tests for `ScenarioStore.resolve` and `select_enabled_for_vertical` (REQ-010)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from vestibule.scenario.model import (
    SCENARIO_NOT_FOUND,
    ChunkerSettings,
    EmbedderSettings,
    IndexerSettings,
    Scenario,
    ScenarioError,
)
from vestibule.scenario.stores.base import ScenarioStore, select_enabled_for_vertical

_FIXED_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


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
    """Minimal in-memory `ScenarioStore` for exercising `resolve()`'s own logic."""

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


# --- resolve() ----------------------------------------------------------------------------


def test_resolve_prefers_scenario_id_when_it_resolves() -> None:
    store = _DictScenarioStore({"hr-1": _scenario("hr-1", "hr")})
    resolved = store.resolve(scenario_id="hr-1", vertical="legal")
    assert resolved.scenario_id == "hr-1"


def test_resolve_falls_back_to_vertical_when_scenario_id_does_not_resolve() -> None:
    store = _DictScenarioStore({"hr-1": _scenario("hr-1", "hr")})
    resolved = store.resolve(scenario_id="missing", vertical="hr")
    assert resolved.scenario_id == "hr-1"


def test_resolve_falls_back_to_default_scenario_id_when_nothing_else_resolves() -> None:
    store = _DictScenarioStore({"default-1": _scenario("default-1", "other")})
    resolved = store.resolve(
        scenario_id=None, vertical="unknown-vertical", default_scenario_id="default-1"
    )
    assert resolved.scenario_id == "default-1"


def test_resolve_logs_warning_when_falling_back_to_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _DictScenarioStore({"default-1": _scenario("default-1", "other")})
    with caplog.at_level(logging.WARNING, logger="vestibule.scenario.stores.base"):
        store.resolve(default_scenario_id="default-1")
    assert any("default" in r.getMessage() for r in caplog.records)


def test_resolve_raises_scenario_not_found_permanent_when_nothing_resolves() -> None:
    store = _DictScenarioStore({})
    with pytest.raises(ScenarioError) as exc_info:
        store.resolve(scenario_id="missing", vertical="missing-vertical")
    assert exc_info.value.error_code == SCENARIO_NOT_FOUND


def test_resolve_raises_scenario_not_found_when_no_criteria_given() -> None:
    store = _DictScenarioStore({"hr-1": _scenario("hr-1", "hr")})
    with pytest.raises(ScenarioError) as exc_info:
        store.resolve()
    assert exc_info.value.error_code == SCENARIO_NOT_FOUND


# --- select_enabled_for_vertical (ambiguity tie-break) --------------------------------------


def test_select_enabled_for_vertical_returns_none_for_empty_candidates() -> None:
    assert select_enabled_for_vertical("hr", []) is None


def test_select_enabled_for_vertical_returns_sole_candidate() -> None:
    scenario = _scenario("hr-1", "hr")
    assert select_enabled_for_vertical("hr", [scenario]) == scenario


def test_select_enabled_for_vertical_picks_lexicographically_smallest_scenario_id() -> (
    None
):
    first = _scenario("hr-a", "hr")
    second = _scenario("hr-b", "hr")
    assert select_enabled_for_vertical("hr", [second, first]) == first


def test_select_enabled_for_vertical_logs_warning_on_ambiguity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = _scenario("hr-a", "hr")
    second = _scenario("hr-b", "hr")
    with caplog.at_level(logging.WARNING, logger="vestibule.scenario.stores.base"):
        select_enabled_for_vertical("hr", [first, second])
    assert any("multiple" in r.getMessage() for r in caplog.records)
