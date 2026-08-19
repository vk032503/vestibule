"""Unit tests for InMemoryProvisioner (REQ-011)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from vestibule.indexer.adapters.in_memory import InMemoryIndexer
from vestibule.indexer.model import IndexRecord
from vestibule.provisioning.adapters.in_memory import InMemoryProvisioner
from vestibule.provisioning.conftest import build_index_template
from vestibule.provisioning.model import INDEX_SCHEMA_DRIFT, ProvisioningError


def test_index_exists_false_before_create_index() -> None:
    provisioner = InMemoryProvisioner(InMemoryIndexer())
    assert provisioner.index_exists("idx-1") is False


def test_index_exists_true_after_create_index() -> None:
    indexer = InMemoryIndexer()
    provisioner = InMemoryProvisioner(indexer)
    provisioner.create_index("idx-1", build_index_template(), 4, "cosine")
    assert provisioner.index_exists("idx-1") is True


def test_describe_index_returns_none_before_create_index() -> None:
    provisioner = InMemoryProvisioner(InMemoryIndexer())
    assert provisioner.describe_index("idx-1") is None


def test_describe_index_returns_description_after_create_index() -> None:
    indexer = InMemoryIndexer()
    provisioner = InMemoryProvisioner(indexer)
    provisioner.create_index("idx-1", build_index_template(), 4, "cosine")

    description = provisioner.describe_index("idx-1")

    assert description is not None
    assert description.dimensions == 4
    assert description.metric == "cosine"


def test_in_memory_provisioner_create_index_is_idempotent_second_call_with_identical_definition_succeeds() -> (
    None
):
    """Assumption A13 — exercises `InMemoryIndexer.ensure_schema`'s existing
    "already compatible — idempotent no-op" branch (REQ-008 AC8) through the
    provisioner adapter."""
    indexer = InMemoryIndexer()
    provisioner = InMemoryProvisioner(indexer)
    template = build_index_template()

    provisioner.create_index("idx-1", template, 4, "cosine")
    provisioner.create_index("idx-1", template, 4, "cosine")  # no raise


def test_create_index_conflicting_schema_raises_index_schema_drift() -> None:
    indexer = InMemoryIndexer()
    provisioner = InMemoryProvisioner(indexer)
    template = build_index_template()
    provisioner.create_index("idx-1", template, 4, "cosine")

    with pytest.raises(ProvisioningError) as exc_info:
        provisioner.create_index("idx-1", template, 8, "cosine")
    assert exc_info.value.error_code == INDEX_SCHEMA_DRIFT


def test_in_memory_provisioner_shares_state_with_wrapped_in_memory_indexer() -> None:
    """Story: "backed by the same dict the InMemoryIndexer uses, so the local
    end-to-end flow provisions and then writes" — feeds AC10."""
    indexer = InMemoryIndexer()
    provisioner = InMemoryProvisioner(indexer)
    provisioner.create_index("idx-1", build_index_template(), 2, "cosine")

    record = IndexRecord(
        chunk_id="c1",
        doc_id="d" * 64,
        doc_version="v1",
        vector=[1.0, 0.0],
        text="hello",
        position=0,
        section_path=None,
        element_types=["paragraph"],
        strategy="recursive",
        allowed_groups=["group-a"],
        trust_tier="internal",
        embedding_model="fake-model",
        embedding_dimensions=2,
        config_version="v1",
        indexed_at=datetime.now(timezone.utc),
    )
    indexer.upsert([record])

    results = indexer.search([1.0, 0.0], top_k=1)
    assert results[0][0].chunk_id == "c1"


def test_delete_index_resets_wrapped_indexer_schema() -> None:
    indexer = InMemoryIndexer()
    provisioner = InMemoryProvisioner(indexer)
    provisioner.create_index("idx-1", build_index_template(), 4, "cosine")
    assert indexer.schema is not None

    provisioner.delete_index("idx-1")

    assert indexer.schema is None
