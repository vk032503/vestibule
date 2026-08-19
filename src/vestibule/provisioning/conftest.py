"""Shared fixtures and helpers for the provisioning test suite (REQ-011).

Plain helper functions (not pytest fixtures), shared across this package's test
modules by direct import — mirrors `vestibule.indexer.conftest`/
`vestibule.embedder.conftest`'s established convention.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from vestibule.provisioning.model import (
    FieldSpec,
    HnswSettings,
    IndexRegistryEntry,
    IndexTemplate,
)
from vestibule.provisioning.templates import IndexTemplateStore
from vestibule.scenario.model import (
    ChunkerSettings,
    EmbedderSettings,
    IndexerSettings,
    Scenario,
)

__all__ = [
    "FIXED_TIME",
    "build_index_registry_entry",
    "build_index_template",
    "build_scenario",
    "build_template_store",
]

FIXED_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def build_index_template(**overrides: Any) -> IndexTemplate:
    """Builds a valid `IndexTemplate` for test use, mirroring `standard-v1.yaml`."""
    defaults: dict[str, Any] = {
        "template_id": "standard-v1",
        "template_version": "1",
        "dimensions": None,
        "metric": "cosine",
        "hnsw": HnswSettings(m=4, ef_construction=400, ef_search=500),
        "fields": [
            FieldSpec(name="chunk_id", type="key"),
            FieldSpec(name="content", type="text", searchable=True),
            FieldSpec(name="content_vector", type="vector", searchable=True),
            FieldSpec(name="doc_id", type="filterable_string"),
            FieldSpec(name="doc_version", type="filterable_string"),
            FieldSpec(name="allowed_groups", type="filterable_string_collection"),
            FieldSpec(name="trust_tier", type="filterable_string"),
            FieldSpec(name="config_version", type="filterable_string"),
        ],
        "semantic_ranker_enabled": False,
        "hybrid_enabled": False,
    }
    defaults.update(overrides)
    return IndexTemplate(**defaults)


def build_index_registry_entry(**overrides: Any) -> IndexRegistryEntry:
    """Builds a valid, `provisioning`-status `IndexRegistryEntry` for test use."""
    defaults: dict[str, Any] = {
        "index_name": "vestibule-hr-v1",
        "vertical": "hr",
        "scenario_id": "hr-policies-v1",
        "template_id": "standard-v1",
        "template_version": "1",
        "dimensions": 1024,
        "metric": "cosine",
        "embedding_model": "text-embedding-3-large",
        "status": "provisioning",
        "created_at": FIXED_TIME,
        "last_verified_at": None,
        "document_count": None,
        "claim_token": "claim-token-1",
        "claimed_at": FIXED_TIME,
        "last_error_message": None,
        "resolved_template": build_index_template(),
    }
    defaults.update(overrides)
    return IndexRegistryEntry(**defaults)


class _DictTemplateStore(IndexTemplateStore):
    """An `IndexTemplateStore` seeded from an in-memory dict, bypassing the real
    constructor's directory/YAML I/O entirely — for hermetic `IndexProvisioner`
    tests that need deterministic template content without touching disk."""

    def __init__(self, templates: dict[str, IndexTemplate]) -> None:
        self._templates = templates


def build_template_store(*templates: IndexTemplate) -> IndexTemplateStore:
    """Builds an `IndexTemplateStore` seeded with `templates`, keyed by
    `template_id`. Defaults to a single `build_index_template()` entry when called
    with no arguments."""
    seeded = templates or (build_index_template(),)
    return _DictTemplateStore({t.template_id: t for t in seeded})


def build_scenario(**overrides: Any) -> Scenario:
    """Builds a valid `Scenario` for test use (REQ-010 model, reused here since
    `IndexProvisioner.ensure` takes a `Scenario`)."""
    defaults: dict[str, Any] = {
        "scenario_id": "hr-policies-v1",
        "vertical": "hr",
        "config_version": "1",
        "index_name": "vestibule-hr-v1",
        "chunker": ChunkerSettings(
            target_chunk_tokens=512,
            overlap_tokens=64,
            min_chunk_tokens=40,
            max_chunk_tokens=800,
        ),
        "embedder": EmbedderSettings(
            adapter="azure_openai",
            model="text-embedding-3-large",
            target_dimensions=1024,
        ),
        "indexer": IndexerSettings(
            metric="cosine",
            dimensions=1024,
            hnsw_m=4,
            hnsw_ef_construction=400,
            hnsw_ef_search=500,
        ),
        "acl_source": "envelope",
        "default_trust_tier": "internal",
        "enabled": True,
        "created_at": FIXED_TIME,
        "updated_at": FIXED_TIME,
        "index_template_id": None,
    }
    defaults.update(overrides)
    return Scenario(**defaults)
