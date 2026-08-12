"""InMemoryIndexer — local, credentials-free IndexerAdapter (REQ-008).

Local dev/test path: no network, no credentials, holds every record in-process by
design (story: "Memory: in-memory adapter holds all records by design (dev/test only,
documented as such)"). Included specifically so the full ingestion pipeline can run,
and prove retrieval works via `search()`, with zero cloud credentials configured (story:
AC9, the Phase 2 acceptance test).

Thread-safe: a single `threading.RLock` guards both backing dicts, held for the full
duration of each public method — the same coarse-grained, correct-under-concurrency
pattern as `ingestion.ledger.store.InMemoryLedgerStore`.
"""

from __future__ import annotations

import math
import threading
from typing import Any

from ingestion.indexer.adapters.base import IndexerAdapter, UpsertOutcome
from ingestion.indexer.model import INDEXER_SCHEMA_CONFLICT, IndexerError, IndexRecord

_DEFAULT_INDEX_NAME = "in-memory"
_MAX_BATCH_SIZE = 1000
_SUPPORTS_HYBRID = False


class InMemoryIndexer(IndexerAdapter):
    """Thread-safe, fully-functional `IndexerAdapter` for tests and local dev.

    Backed by a `dict[chunk_id, IndexRecord]` plus a `dict[doc_id, set[chunk_id]]`
    secondary index for `delete_by_doc_id`/`count_by_doc_id`/`get_doc_version`. Never
    implements indexing/search algorithms itself except this class's own brute-force
    cosine `search()` — the explicit point of this adapter (story: "not just that
    writes succeed").
    """

    def __init__(self, index_name: str = _DEFAULT_INDEX_NAME) -> None:
        """Initializes an empty in-memory index.

        Args:
            index_name: The name reported via `index_name`.
        """
        self._index_name = index_name
        self._lock = threading.RLock()
        self._records: dict[str, IndexRecord] = {}
        self._by_doc: dict[str, set[str]] = {}
        self._schema: tuple[int, str] | None = None

    @property
    def index_name(self) -> str:
        """See `IndexerAdapter.index_name`."""
        return self._index_name

    @property
    def max_batch_size(self) -> int:
        """See `IndexerAdapter.max_batch_size`."""
        return _MAX_BATCH_SIZE

    @property
    def supports_hybrid(self) -> bool:
        """See `IndexerAdapter.supports_hybrid`. Always `False`."""
        return _SUPPORTS_HYBRID

    def ensure_schema(self, dimensions: int, metric: str) -> None:
        """See `IndexerAdapter.ensure_schema`.

        Idempotent (AC8): a repeat call with the same `(dimensions, metric)` is a
        no-op. A repeat call with different values raises `INDEXER_SCHEMA_CONFLICT`.
        """
        with self._lock:
            requested = (dimensions, metric)
            if self._schema is None:
                self._schema = requested
                return
            if self._schema != requested:
                raise IndexerError(
                    "",
                    f"index {self._index_name!r} already has schema "
                    f"dimensions={self._schema[0]} metric={self._schema[1]!r}; "
                    f"requested dimensions={dimensions} metric={metric!r}",
                    error_code=INDEXER_SCHEMA_CONFLICT,
                )

    def upsert(self, records: list[IndexRecord]) -> UpsertOutcome:
        """See `IndexerAdapter.upsert`. Always all-succeed (no store-side failure mode)."""
        succeeded: list[str] = []
        with self._lock:
            for record in records:
                self._records[record.chunk_id] = record
                self._by_doc.setdefault(record.doc_id, set()).add(record.chunk_id)
                succeeded.append(record.chunk_id)
        return UpsertOutcome(succeeded=succeeded, failed=[])

    def delete_by_doc_id(self, doc_id: str) -> int:
        """See `IndexerAdapter.delete_by_doc_id`."""
        with self._lock:
            chunk_ids = self._by_doc.pop(doc_id, set())
            for chunk_id in chunk_ids:
                self._records.pop(chunk_id, None)
            return len(chunk_ids)

    def count_by_doc_id(self, doc_id: str) -> int:
        """See `IndexerAdapter.count_by_doc_id`."""
        with self._lock:
            return len(self._by_doc.get(doc_id, ()))

    def get_doc_version(self, doc_id: str) -> str | None:
        """See `IndexerAdapter.get_doc_version`."""
        with self._lock:
            chunk_ids = self._by_doc.get(doc_id)
            if not chunk_ids:
                return None
            any_chunk_id = next(iter(chunk_ids))
            return self._records[any_chunk_id].doc_version

    def search(
        self,
        query_vector: list[float],
        top_k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[tuple[IndexRecord, float]]:
        """Brute-force cosine-similarity search over every stored record.

        Not part of `IndexerAdapter` (out of scope for other adapters, story: "Query-
        side retrieval API beyond the in-memory search() used for the E2E test") — this
        method exists specifically so the end-to-end pipeline test can prove retrieval
        works, not just that writes succeed.

        Args:
            query_vector: The query embedding, in the same vector space as every
                stored `IndexRecord.vector`.
            top_k: The maximum number of results to return.
            filters: Optional equality filters, e.g. `{"trust_tier": "internal"}`. For
                a list-valued `IndexRecord` field (e.g. `allowed_groups`), the filter
                value is checked for membership in that list instead of equality.

        Returns:
            Up to `top_k` `(record, cosine_similarity)` pairs, sorted by similarity
            descending.
        """
        with self._lock:
            candidates = list(self._records.values())
        if filters:
            candidates = [c for c in candidates if _matches_filters(c, filters)]
        scored = [(c, _cosine_similarity(query_vector, c.vector)) for c in candidates]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_k]


def _matches_filters(record: IndexRecord, filters: dict[str, Any]) -> bool:
    """Checks whether `record` satisfies every `(field, value)` pair in `filters`.

    Args:
        record: The candidate record.
        filters: Equality (or, for list-valued fields, membership) filters.

    Returns:
        `True` if `record` satisfies every filter; `False` otherwise.
    """
    for field_name, value in filters.items():
        attribute = getattr(record, field_name, None)
        if isinstance(attribute, list):
            if value not in attribute:
                return False
        elif attribute != value:
            return False
    return True


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Computes the cosine similarity between two equal-length vectors.

    Args:
        a: The first vector.
        b: The second vector.

    Returns:
        The cosine similarity in `[-1.0, 1.0]`, or `0.0` if either vector has zero
        magnitude (avoids a division by zero).
    """
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
