"""Unit tests for CachedScenarioStore (REQ-010)."""

from __future__ import annotations

import inspect
import re
from datetime import datetime, timezone
from pathlib import Path

from vestibule.scenario.model import (
    ChunkerSettings,
    EmbedderSettings,
    IndexerSettings,
    Scenario,
)
from vestibule.scenario.stores.base import ScenarioStore
from vestibule.scenario.stores.cached_store import CachedScenarioStore

_FIXED_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _scenario(scenario_id: str = "hr-1", vertical: str = "hr") -> Scenario:
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
        enabled=True,
        created_at=_FIXED_TIME,
        updated_at=_FIXED_TIME,
    )


class _FakeClock:
    """Deterministic, manually-advanced stand-in for `time.monotonic`."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _CountingStore(ScenarioStore):
    """Wraps a fixed set of scenarios, counting calls to each method."""

    def __init__(self, scenarios: dict[str, Scenario]) -> None:
        self._scenarios = scenarios
        self.get_calls = 0
        self.get_by_vertical_calls = 0
        self.list_all_calls = 0
        self.upsert_calls = 0
        self.delete_calls = 0

    @property
    def is_writable(self) -> bool:
        return True

    def get(self, scenario_id: str) -> Scenario | None:
        self.get_calls += 1
        return self._scenarios.get(scenario_id)

    def get_by_vertical(self, vertical: str) -> Scenario | None:
        self.get_by_vertical_calls += 1
        for scenario in self._scenarios.values():
            if scenario.vertical == vertical:
                return scenario
        return None

    def list_all(self, include_disabled: bool = False) -> list[Scenario]:
        self.list_all_calls += 1
        return list(self._scenarios.values())

    def upsert(self, scenario: Scenario) -> Scenario:
        self.upsert_calls += 1
        self._scenarios[scenario.scenario_id] = scenario
        return scenario

    def delete(self, scenario_id: str) -> bool:
        self.delete_calls += 1
        return self._scenarios.pop(scenario_id, None) is not None


# --- serves from cache within TTL --------------------------------------------------------------


def test_get_serves_from_cache_within_ttl_wrapped_get_called_once() -> None:
    scenario = _scenario()
    wrapped = _CountingStore({scenario.scenario_id: scenario})
    clock = _FakeClock()
    cached = CachedScenarioStore(wrapped, ttl_seconds=60, _now=clock)

    first = cached.get(scenario.scenario_id)
    second = cached.get(scenario.scenario_id)

    assert first == second == scenario
    assert wrapped.get_calls == 1


def test_get_by_vertical_serves_from_cache_within_ttl() -> None:
    scenario = _scenario()
    wrapped = _CountingStore({scenario.scenario_id: scenario})
    cached = CachedScenarioStore(wrapped, ttl_seconds=60, _now=_FakeClock())

    cached.get_by_vertical("hr")
    cached.get_by_vertical("hr")

    assert wrapped.get_by_vertical_calls == 1


def test_list_all_serves_from_cache_within_ttl() -> None:
    scenario = _scenario()
    wrapped = _CountingStore({scenario.scenario_id: scenario})
    cached = CachedScenarioStore(wrapped, ttl_seconds=60, _now=_FakeClock())

    cached.list_all()
    cached.list_all()

    assert wrapped.list_all_calls == 1


# --- re-reads after TTL elapses (AC7, ttl_seconds=1) --------------------------------------------


def test_get_re_reads_wrapped_store_after_ttl_elapses() -> None:
    scenario = _scenario()
    wrapped = _CountingStore({scenario.scenario_id: scenario})
    clock = _FakeClock()
    cached = CachedScenarioStore(wrapped, ttl_seconds=1, _now=clock)

    cached.get(scenario.scenario_id)
    cached.get(scenario.scenario_id)
    assert wrapped.get_calls == 1

    clock.advance(1.0)
    cached.get(scenario.scenario_id)
    assert wrapped.get_calls == 2


def test_get_still_cached_just_under_ttl() -> None:
    scenario = _scenario()
    wrapped = _CountingStore({scenario.scenario_id: scenario})
    clock = _FakeClock()
    cached = CachedScenarioStore(wrapped, ttl_seconds=1, _now=clock)

    cached.get(scenario.scenario_id)
    clock.advance(0.99)
    cached.get(scenario.scenario_id)

    assert wrapped.get_calls == 1


# --- invalidate() forces a re-read ----------------------------------------------------------


def test_invalidate_forces_a_re_read() -> None:
    scenario = _scenario()
    wrapped = _CountingStore({scenario.scenario_id: scenario})
    cached = CachedScenarioStore(wrapped, ttl_seconds=60, _now=_FakeClock())

    cached.get(scenario.scenario_id)
    assert wrapped.get_calls == 1

    cached.invalidate(scenario.scenario_id)
    cached.get(scenario.scenario_id)

    assert wrapped.get_calls == 2


def test_upsert_invalidates_cache_so_next_read_sees_the_write() -> None:
    scenario = _scenario()
    wrapped = _CountingStore({scenario.scenario_id: scenario})
    cached = CachedScenarioStore(wrapped, ttl_seconds=60, _now=_FakeClock())
    cached.get(scenario.scenario_id)

    updated = scenario.model_copy(update={"config_version": "2"})
    cached.upsert(updated)

    assert cached.get(scenario.scenario_id) == updated
    assert wrapped.get_calls == 2


def test_delete_invalidates_cache() -> None:
    scenario = _scenario()
    wrapped = _CountingStore({scenario.scenario_id: scenario})
    cached = CachedScenarioStore(wrapped, ttl_seconds=60, _now=_FakeClock())
    cached.get(scenario.scenario_id)

    assert cached.delete(scenario.scenario_id) is True
    assert cached.get(scenario.scenario_id) is None
    assert wrapped.get_calls == 2


# --- delegation --------------------------------------------------------------------------------


def test_is_writable_delegates_to_wrapped_store() -> None:
    wrapped = _CountingStore({})
    cached = CachedScenarioStore(wrapped)
    assert cached.is_writable == wrapped.is_writable is True


# --- config/code sync ----------------------------------------------------------------------


def test_config_scenario_store_yaml_tunables_match_code_defaults() -> None:
    config_path = Path(__file__).resolve().parents[4] / "config" / "scenario_store.yaml"
    text = config_path.read_text(encoding="utf-8")
    default_ttl = (
        inspect.signature(CachedScenarioStore.__init__)
        .parameters["ttl_seconds"]
        .default
    )

    match = re.search(r"ttl_seconds:\s*(\d+)", text)
    assert match is not None
    assert int(match.group(1)) == default_ttl
    assert "enabled: true" in text
    assert "backend: yaml" in text
