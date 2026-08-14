"""Unit tests for YamlScenarioStore (REQ-010)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vestibule.scenario.model import (
    SCENARIO_INVALID,
    SCENARIO_STORE_READ_ONLY,
    ScenarioError,
)
from vestibule.scenario.stores.yaml_store import YamlScenarioStore

_HR_YAML = """
scenario_id: hr-policies-v1
vertical: hr
config_version: "1"
index_name: vestibule-hr-v1
chunker:
  target_chunk_tokens: 512
  overlap_tokens: 64
  min_chunk_tokens: 40
  max_chunk_tokens: 800
embedder:
  adapter: azure_openai
  model: text-embedding-3-large
  target_dimensions: 1024
indexer:
  metric: cosine
  dimensions: 1024
  hnsw_m: 4
  hnsw_ef_construction: 400
  hnsw_ef_search: 500
acl_source: envelope
default_trust_tier: internal
enabled: true
created_at: "2026-08-12T00:00:00+00:00"
updated_at: "2026-08-12T00:00:00+00:00"
"""

_LEGAL_YAML = """
scenario_id: legal-contracts-v1
vertical: legal
config_version: "1"
index_name: vestibule-legal-v1
chunker:
  target_chunk_tokens: 256
  overlap_tokens: 32
  min_chunk_tokens: 20
  max_chunk_tokens: 400
embedder:
  adapter: fastembed
  model: nomic-ai/nomic-embed-text-v1.5
  target_dimensions: null
indexer:
  metric: cosine
  dimensions: 768
  hnsw_m: 8
  hnsw_ef_construction: 500
  hnsw_ef_search: 600
acl_source: blob_metadata
default_trust_tier: confidential
enabled: true
created_at: "2026-08-12T00:00:00+00:00"
updated_at: "2026-08-12T00:00:00+00:00"
"""


def _write(directory: Path, filename: str, content: str) -> Path:
    path = directory / filename
    path.write_text(content, encoding="utf-8")
    return path


def _mismatched_dimensions_yaml(scenario_id: str) -> str:
    return f"""
scenario_id: {scenario_id}
vertical: hr
config_version: "1"
index_name: {scenario_id}-idx
chunker:
  target_chunk_tokens: 512
  overlap_tokens: 64
  min_chunk_tokens: 40
  max_chunk_tokens: 800
embedder:
  adapter: azure_openai
  model: text-embedding-3-large
  target_dimensions: 1024
indexer:
  metric: cosine
  dimensions: 1536
  hnsw_m: 4
  hnsw_ef_construction: 400
  hnsw_ef_search: 500
acl_source: envelope
default_trust_tier: internal
enabled: true
created_at: "2026-08-12T00:00:00+00:00"
updated_at: "2026-08-12T00:00:00+00:00"
"""


# --- AC1: loads multiple scenario files -----------------------------------------------------


def test_yaml_store_loads_multiple_scenario_files_and_lists_both(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "hr.yaml", _HR_YAML)
    _write(tmp_path, "legal.yaml", _LEGAL_YAML)

    store = YamlScenarioStore(tmp_path)

    ids = {s.scenario_id for s in store.list_all()}
    assert ids == {"hr-policies-v1", "legal-contracts-v1"}


def test_yaml_store_get_by_vertical_returns_the_right_scenario(tmp_path: Path) -> None:
    _write(tmp_path, "hr.yaml", _HR_YAML)
    _write(tmp_path, "legal.yaml", _LEGAL_YAML)

    store = YamlScenarioStore(tmp_path)

    hr = store.get_by_vertical("hr")
    assert hr is not None
    assert hr.scenario_id == "hr-policies-v1"


# --- AC2: vertical isolation --------------------------------------------------------------


def test_hr_and_legal_scenarios_are_independently_retrievable_with_no_cross_effect(
    tmp_path: Path,
) -> None:
    """AC2: two scenarios with different chunker.target_chunk_tokens are independently
    retrievable, and reading/re-fetching one has zero effect on the other.

    This would fail if `get`/`get_by_vertical`/`list_all` shared any mutable state (a
    cache keyed wrong, aliased dict values, a bug that let one lookup's parsing mutate
    a shared buffer) between the two scenarios.
    """
    _write(tmp_path, "hr.yaml", _HR_YAML)
    _write(tmp_path, "legal.yaml", _LEGAL_YAML)
    store = YamlScenarioStore(tmp_path)

    hr_before = store.get_by_vertical("hr")
    legal_before = store.get_by_vertical("legal")
    assert hr_before is not None and legal_before is not None
    assert hr_before.chunker.target_chunk_tokens == 512
    assert legal_before.chunker.target_chunk_tokens == 256

    # Repeatedly read one vertical many times, interleaved with reads of the other.
    for _ in range(5):
        store.get_by_vertical("hr")
        store.get("legal-contracts-v1")

    hr_after = store.get_by_vertical("hr")
    legal_after = store.get_by_vertical("legal")
    assert hr_after == hr_before
    assert legal_after == legal_before
    # Byte-for-byte unchanged, not merely equal-by-coincidence field values.
    assert hr_after.chunker.target_chunk_tokens == 512
    assert legal_after.chunker.target_chunk_tokens == 256
    assert hr_after.embedder.adapter == "azure_openai"
    assert legal_after.embedder.adapter == "fastembed"
    assert hr_after.default_trust_tier == "internal"
    assert legal_after.default_trust_tier == "confidential"


def test_deleting_one_verticals_file_and_reloading_does_not_affect_the_other(
    tmp_path: Path,
) -> None:
    """AC2, the mutation half: removing the HR scenario file and reloading leaves the
    Legal scenario completely untouched."""
    hr_path = _write(tmp_path, "hr.yaml", _HR_YAML)
    _write(tmp_path, "legal.yaml", _LEGAL_YAML)
    store = YamlScenarioStore(tmp_path)
    legal_before = store.get_by_vertical("legal")

    hr_path.unlink()
    store.reload()

    assert store.get_by_vertical("hr") is None
    legal_after = store.get_by_vertical("legal")
    assert legal_after == legal_before


# --- AC3: read-only ------------------------------------------------------------------------


def test_yaml_store_is_writable_is_false(tmp_path: Path) -> None:
    store = YamlScenarioStore(tmp_path)
    assert store.is_writable is False


def test_yaml_store_upsert_raises_read_only_permanent(tmp_path: Path) -> None:
    _write(tmp_path, "hr.yaml", _HR_YAML)
    store = YamlScenarioStore(tmp_path)
    scenario = store.get("hr-policies-v1")
    assert scenario is not None

    with pytest.raises(ScenarioError) as exc_info:
        store.upsert(scenario)
    assert exc_info.value.error_code == SCENARIO_STORE_READ_ONLY


def test_yaml_store_delete_raises_read_only_permanent(tmp_path: Path) -> None:
    _write(tmp_path, "hr.yaml", _HR_YAML)
    store = YamlScenarioStore(tmp_path)

    with pytest.raises(ScenarioError) as exc_info:
        store.delete("hr-policies-v1")
    assert exc_info.value.error_code == SCENARIO_STORE_READ_ONLY


# --- AC5: dimension-consistency validation at store construction, not ingest time -----------


def test_yaml_store_construction_raises_scenario_invalid_before_any_get_call(
    tmp_path: Path,
) -> None:
    """A scenario with target_dimensions=1024/indexer.dimensions=1536 raises
    SCENARIO_INVALID at YamlScenarioStore construction itself — the failure happens
    inside `YamlScenarioStore.__init__`, before any `get`/`list_all`/ingestion call is
    even reachable."""
    _write(tmp_path, "bad.yaml", _mismatched_dimensions_yaml("bad-scenario"))

    with pytest.raises(ScenarioError) as exc_info:
        YamlScenarioStore(tmp_path)

    assert exc_info.value.error_code == SCENARIO_INVALID


def test_yaml_store_construction_fails_even_when_other_files_are_valid(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "hr.yaml", _HR_YAML)
    _write(tmp_path, "bad.yaml", _mismatched_dimensions_yaml("bad-scenario"))

    with pytest.raises(ScenarioError) as exc_info:
        YamlScenarioStore(tmp_path)
    assert exc_info.value.error_code == SCENARIO_INVALID


def test_yaml_store_reload_raises_scenario_invalid_for_malformed_yaml(
    tmp_path: Path,
) -> None:
    store = YamlScenarioStore(tmp_path)
    _write(tmp_path, "broken.yaml", "not: [valid: yaml: at: all")

    with pytest.raises(ScenarioError) as exc_info:
        store.reload()
    assert exc_info.value.error_code == SCENARIO_INVALID


def test_yaml_store_rejects_non_mapping_yaml_document(tmp_path: Path) -> None:
    _write(tmp_path, "list.yaml", "- 1\n- 2\n")

    with pytest.raises(ScenarioError) as exc_info:
        YamlScenarioStore(tmp_path)
    assert exc_info.value.error_code == SCENARIO_INVALID


# --- AC8: disabled scenarios ------------------------------------------------------------------


def test_disabled_scenario_excluded_from_get_by_vertical_and_default_list_all(
    tmp_path: Path,
) -> None:
    disabled_yaml = _HR_YAML.replace("enabled: true", "enabled: false")
    _write(tmp_path, "hr.yaml", disabled_yaml)
    store = YamlScenarioStore(tmp_path)

    assert store.get_by_vertical("hr") is None
    assert store.list_all() == []
    assert store.list_all(include_disabled=True)[0].scenario_id == "hr-policies-v1"


# --- reload() is explicit, not automatic -----------------------------------------------------


def test_yaml_store_does_not_pick_up_new_files_without_explicit_reload(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "hr.yaml", _HR_YAML)
    store = YamlScenarioStore(tmp_path)
    assert store.get("legal-contracts-v1") is None

    _write(tmp_path, "legal.yaml", _LEGAL_YAML)
    assert store.get("legal-contracts-v1") is None

    store.reload()
    assert store.get("legal-contracts-v1") is not None
