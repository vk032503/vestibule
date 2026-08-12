"""Unit tests for InMemoryIndexer (REQ-008)."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from vestibule.indexer.adapters.in_memory import InMemoryIndexer
from vestibule.indexer.model import INDEXER_SCHEMA_CONFLICT, IndexerError, IndexRecord


def _record(
    chunk_id: str,
    doc_id: str,
    *,
    doc_version: str = "v1",
    vector: list[float] | None = None,
    text: str = "hello",
    trust_tier: str = "internal",
    allowed_groups: list[str] | None = None,
) -> IndexRecord:
    return IndexRecord(
        chunk_id=chunk_id,
        doc_id=doc_id,
        doc_version=doc_version,
        vector=vector if vector is not None else [1.0, 0.0],
        text=text,
        position=0,
        section_path=None,
        element_types=["paragraph"],
        strategy="recursive",
        allowed_groups=allowed_groups if allowed_groups is not None else ["group-a"],
        trust_tier=trust_tier,
        embedding_model="fake-model",
        embedding_dimensions=2,
        config_version="v1",
        indexed_at=datetime.now(timezone.utc),
    )


# --- declared capabilities ----------------------------------------------------------------------


def test_default_capabilities() -> None:
    adapter = InMemoryIndexer()
    assert adapter.index_name == "in-memory"
    assert adapter.max_batch_size == 1000
    assert adapter.supports_hybrid is False


def test_custom_index_name() -> None:
    adapter = InMemoryIndexer(index_name="custom-index")
    assert adapter.index_name == "custom-index"


# --- ensure_schema idempotency (AC8) -------------------------------------------------------------


def test_ensure_schema_first_call_succeeds() -> None:
    adapter = InMemoryIndexer()
    adapter.ensure_schema(4, "cosine")


def test_ensure_schema_called_twice_with_same_args_is_a_noop() -> None:
    adapter = InMemoryIndexer()
    adapter.ensure_schema(4, "cosine")
    adapter.ensure_schema(4, "cosine")  # no error, no state change


def test_ensure_schema_called_twice_with_different_args_raises_schema_conflict() -> (
    None
):
    adapter = InMemoryIndexer()
    adapter.ensure_schema(4, "cosine")

    with pytest.raises(IndexerError) as exc_info:
        adapter.ensure_schema(8, "cosine")

    assert exc_info.value.error_code == INDEXER_SCHEMA_CONFLICT


# --- upsert / delete / count / get_doc_version ----------------------------------------------------


def test_upsert_returns_all_succeeded() -> None:
    adapter = InMemoryIndexer()
    outcome = adapter.upsert([_record("c1", "d1"), _record("c2", "d1")])
    assert outcome.succeeded == ["c1", "c2"]
    assert outcome.failed == []


def test_upsert_is_idempotent_by_chunk_id() -> None:
    adapter = InMemoryIndexer()
    adapter.upsert([_record("c1", "d1", text="first")])
    adapter.upsert([_record("c1", "d1", text="second")])
    assert adapter.count_by_doc_id("d1") == 1


def test_count_by_doc_id_unknown_doc_returns_zero() -> None:
    adapter = InMemoryIndexer()
    assert adapter.count_by_doc_id("unknown") == 0


def test_get_doc_version_unknown_doc_returns_none() -> None:
    adapter = InMemoryIndexer()
    assert adapter.get_doc_version("unknown") is None


def test_get_doc_version_returns_stored_version() -> None:
    adapter = InMemoryIndexer()
    adapter.upsert([_record("c1", "d1", doc_version="v7")])
    assert adapter.get_doc_version("d1") == "v7"


def test_delete_by_doc_id_removes_all_records_and_returns_count() -> None:
    adapter = InMemoryIndexer()
    adapter.upsert([_record("c1", "d1"), _record("c2", "d1"), _record("c3", "d2")])

    deleted = adapter.delete_by_doc_id("d1")

    assert deleted == 2
    assert adapter.count_by_doc_id("d1") == 0
    assert adapter.count_by_doc_id("d2") == 1


def test_delete_by_doc_id_unknown_doc_returns_zero() -> None:
    adapter = InMemoryIndexer()
    assert adapter.delete_by_doc_id("unknown") == 0


# --- search() (story: "not just that writes succeed") ---------------------------------------------


def test_search_ranks_more_similar_vector_first() -> None:
    adapter = InMemoryIndexer()
    adapter.upsert(
        [
            _record("close", "d1", vector=[1.0, 0.0]),
            _record("far", "d1", vector=[0.0, 1.0]),
        ]
    )

    results = adapter.search([1.0, 0.0], top_k=2)

    assert [record.chunk_id for record, _score in results] == ["close", "far"]
    assert results[0][1] == pytest.approx(1.0)
    assert results[1][1] == pytest.approx(0.0)


def test_search_respects_top_k() -> None:
    adapter = InMemoryIndexer()
    adapter.upsert([_record(f"c{i}", "d1", vector=[1.0, float(i)]) for i in range(5)])

    results = adapter.search([1.0, 0.0], top_k=2)

    assert len(results) == 2


def test_search_applies_equality_filter() -> None:
    adapter = InMemoryIndexer()
    adapter.upsert(
        [
            _record("public-chunk", "d1", trust_tier="public"),
            _record("secret-chunk", "d1", trust_tier="restricted"),
        ]
    )

    results = adapter.search([1.0, 0.0], top_k=10, filters={"trust_tier": "public"})

    assert [record.chunk_id for record, _score in results] == ["public-chunk"]


def test_search_applies_list_membership_filter() -> None:
    adapter = InMemoryIndexer()
    adapter.upsert(
        [
            _record("group-a-chunk", "d1", allowed_groups=["group-a"]),
            _record("group-b-chunk", "d1", allowed_groups=["group-b"]),
        ]
    )

    results = adapter.search(
        [1.0, 0.0], top_k=10, filters={"allowed_groups": "group-a"}
    )

    assert [record.chunk_id for record, _score in results] == ["group-a-chunk"]


def test_search_zero_vector_query_returns_zero_similarity_not_an_error() -> None:
    adapter = InMemoryIndexer()
    adapter.upsert([_record("c1", "d1", vector=[1.0, 0.0])])

    results = adapter.search([0.0, 0.0], top_k=1)

    assert results[0][1] == 0.0


# --- thread safety ---------------------------------------------------------------------------------


def test_thread_safety_concurrent_upserts_for_distinct_docs_all_land() -> None:
    adapter = InMemoryIndexer()
    n = 20
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()
        adapter.upsert([_record(f"c{i}", f"d{i}")])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(adapter.count_by_doc_id(f"d{i}") for i in range(n)) == n
