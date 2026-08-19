"""Unit tests for IndexTemplateStore (REQ-011)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vestibule.provisioning.model import (
    INDEX_TEMPLATE_INVALID,
    INDEX_TEMPLATE_NOT_FOUND,
    ProvisioningError,
)
from vestibule.provisioning.templates import IndexTemplateStore

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHIPPED_TEMPLATE_DIR = _REPO_ROOT / "config" / "index_templates"


# --- shipped standard-v1 template --------------------------------------------------------------


def test_index_template_standard_v1_loads_and_validates() -> None:
    store = IndexTemplateStore(_SHIPPED_TEMPLATE_DIR)
    template = store.get("standard-v1")
    assert template is not None
    assert template.template_version == "1"
    assert template.dimensions is None
    assert template.metric == "cosine"
    field_names = [f.name for f in template.fields]
    assert field_names == [
        "chunk_id",
        "content",
        "content_vector",
        "doc_id",
        "doc_version",
        "allowed_groups",
        "trust_tier",
        "config_version",
    ]


def test_standard_v1_field_list_matches_azure_ai_search_ensure_schema_exactly() -> None:
    """AC9's regression guard: `standard-v1`'s `fields` list must reproduce
    `vestibule.indexer.adapters._azure_search_backend._build_fields`'s exact 8-field
    key/searchable/filterable/collection shape (Assumption A12), field-for-field.

    Hermetic by construction (no real `azure-search-documents` translation is
    exercised, mirroring how `test_azure_ai_search.py`'s own indexer tests never call
    the real SDK either): compares this REQ's store-agnostic `FieldSpec` shape
    directly against `_build_fields`'s own hardcoded, read-directly field list —
    `key`/`searchable text`/`searchable vector`/`filterable`/`filterable collection`.
    """
    store = IndexTemplateStore(_SHIPPED_TEMPLATE_DIR)
    template = store.get_or_raise("standard-v1")

    # (name, type, searchable) — the exact shape
    # `_azure_search_backend._build_fields` produces today: chunk_id (key),
    # content (searchable text), content_vector (searchable vector),
    # doc_id/doc_version/trust_tier/config_version (filterable string, not
    # searchable), allowed_groups (filterable string collection, not searchable).
    expected = [
        ("chunk_id", "key", False),
        ("content", "text", True),
        ("content_vector", "vector", True),
        ("doc_id", "filterable_string", False),
        ("doc_version", "filterable_string", False),
        ("allowed_groups", "filterable_string_collection", False),
        ("trust_tier", "filterable_string", False),
        ("config_version", "filterable_string", False),
    ]
    actual = [(f.name, f.type, f.searchable) for f in template.fields]
    assert actual == expected


def test_get_or_raise_returns_loaded_template() -> None:
    store = IndexTemplateStore(_SHIPPED_TEMPLATE_DIR)
    assert store.get_or_raise("standard-v1").template_id == "standard-v1"


def test_get_returns_none_for_unknown_template_id() -> None:
    store = IndexTemplateStore(_SHIPPED_TEMPLATE_DIR)
    assert store.get("unknown-template") is None


def test_get_or_raise_raises_index_template_not_found_for_unknown_template_id() -> None:
    store = IndexTemplateStore(_SHIPPED_TEMPLATE_DIR)
    with pytest.raises(ProvisioningError) as exc_info:
        store.get_or_raise("unknown-template")
    assert exc_info.value.error_code == INDEX_TEMPLATE_NOT_FOUND


# --- fail-fast-at-load (mirrors YamlScenarioStore AC5) -----------------------------------------


def test_index_template_store_fails_fast_on_malformed_yaml_file(tmp_path: Path) -> None:
    (tmp_path / "broken.yaml").write_text(
        "template_id: [unterminated", encoding="utf-8"
    )
    with pytest.raises(ProvisioningError) as exc_info:
        IndexTemplateStore(tmp_path)
    assert exc_info.value.error_code == INDEX_TEMPLATE_INVALID


def test_index_template_store_fails_fast_on_non_mapping_yaml_document(
    tmp_path: Path,
) -> None:
    (tmp_path / "list.yaml").write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ProvisioningError) as exc_info:
        IndexTemplateStore(tmp_path)
    assert exc_info.value.error_code == INDEX_TEMPLATE_INVALID


def test_index_template_store_fails_fast_on_validation_failure(tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text(
        """
template_id: bad
template_version: "not-an-int"
dimensions: null
metric: cosine
hnsw:
  m: 4
  ef_construction: 400
  ef_search: 500
semantic_ranker_enabled: false
hybrid_enabled: false
fields:
  - name: chunk_id
    type: key
  - name: content_vector
    type: vector
""",
        encoding="utf-8",
    )
    with pytest.raises(ProvisioningError) as exc_info:
        IndexTemplateStore(tmp_path)
    assert exc_info.value.error_code == INDEX_TEMPLATE_INVALID


def test_index_template_store_fails_fast_on_filename_template_id_mismatch(
    tmp_path: Path,
) -> None:
    (tmp_path / "mismatch.yaml").write_text(
        """
template_id: different-id
template_version: "1"
dimensions: null
metric: cosine
hnsw:
  m: 4
  ef_construction: 400
  ef_search: 500
semantic_ranker_enabled: false
hybrid_enabled: false
fields:
  - name: chunk_id
    type: key
  - name: content_vector
    type: vector
""",
        encoding="utf-8",
    )
    with pytest.raises(ProvisioningError) as exc_info:
        IndexTemplateStore(tmp_path)
    assert exc_info.value.error_code == INDEX_TEMPLATE_INVALID
    assert "does not match" in exc_info.value.reason
