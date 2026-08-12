"""IndexerAdapter contract (REQ-008).

`IndexerAdapter` is the thin-wrapper contract every vector-store backend implements
(house rules: "adapters thin" — implementations wrap an underlying SDK/managed service,
never implement indexing/search algorithms themselves, except `InMemoryIndexer`'s
brute-force cosine `search()`, which is the explicit point of that one adapter). The
`Indexer` orchestrator reads and respects each adapter's declared capabilities
(`max_batch_size`, `supports_hybrid`), never hard-coding a per-adapter special case.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ingestion.indexer.model import IndexRecord


@dataclass(frozen=True)
class UpsertOutcome:
    """Per-item result of one `IndexerAdapter.upsert` call.

    Internal value object handed from adapter to orchestrator — never all-or-nothing
    (story: "per-item success/failure, not all-or-nothing", since most vector stores,
    including Azure AI Search, fail per-document within a batch).

    Attributes:
        succeeded: `chunk_id`s that were successfully upserted.
        failed: `(chunk_id, reason)` pairs for records that failed to upsert; `reason`
            is a short human-readable explanation, not a declared error code.
    """

    succeeded: list[str]
    failed: list[tuple[str, str]]


class IndexerAdapter(ABC):
    """Thin wrapper contract for a single vector-store backend.

    Implementations wrap an underlying SDK or managed service, never implementing
    indexing/search algorithms themselves (see this module's docstring for the one
    documented exception).
    """

    @abstractmethod
    def upsert(self, records: list[IndexRecord]) -> UpsertOutcome:
        """Upserts one batch of records, keyed on `chunk_id`.

        Args:
            records: The records to upsert. `len(records)` is guaranteed by the caller
                (`Indexer`) to be `<= max_batch_size`; every record is guaranteed to
                share the same `embedding_model`/`embedding_dimensions`.

        Returns:
            Per-item success/failure — see `UpsertOutcome`.

        Raises:
            IndexerError: `INDEXER_RATE_LIMITED`, `INDEXER_UPSTREAM_ERROR`,
                `INDEXER_TIMEOUT` (all TRANSIENT), `INDEXER_SCHEMA_CONFLICT`, or
                `INDEXER_RECORD_TOO_LARGE` (both PERMANENT) for a call-level failure
                (as opposed to a per-item one, reported via `UpsertOutcome.failed`
                instead). Any other exception may propagate unchanged — `Indexer` wraps
                any such exception as `INDEXER_INTERNAL`.
        """
        raise NotImplementedError  # pragma: no cover

    @abstractmethod
    def delete_by_doc_id(self, doc_id: str) -> int:
        """Deletes every record for `doc_id`.

        Args:
            doc_id: The document whose records to delete.

        Returns:
            The count of records deleted (`0` if none existed).
        """
        raise NotImplementedError  # pragma: no cover

    @abstractmethod
    def count_by_doc_id(self, doc_id: str) -> int:
        """Counts records currently stored for `doc_id`.

        Args:
            doc_id: The document to count records for.

        Returns:
            The count of records currently stored for `doc_id` (`0` if none exist).
        """
        raise NotImplementedError  # pragma: no cover

    @abstractmethod
    def get_doc_version(self, doc_id: str) -> str | None:
        """Returns the `doc_version` currently stored for `doc_id`, for version-change
        detection.

        Args:
            doc_id: The document to look up.

        Returns:
            The `doc_version` of any currently-stored record for `doc_id`, or `None` if
            no record for `doc_id` exists yet.
        """
        raise NotImplementedError  # pragma: no cover

    @abstractmethod
    def ensure_schema(self, dimensions: int, metric: str) -> None:
        """Creates the index if absent; idempotent otherwise.

        Args:
            dimensions: The vector field's dimension count.
            metric: The vector similarity metric (e.g. `"cosine"`).

        Raises:
            IndexerError: `INDEXER_SCHEMA_CONFLICT` (PERMANENT) if an index already
                exists under `index_name` with an incompatible `dimensions`/`metric`.
        """
        raise NotImplementedError  # pragma: no cover

    @property
    @abstractmethod
    def index_name(self) -> str:
        """The name of the index this adapter reads/writes."""
        raise NotImplementedError  # pragma: no cover

    @property
    @abstractmethod
    def max_batch_size(self) -> int:
        """The maximum number of records this adapter accepts in one `upsert` call."""
        raise NotImplementedError  # pragma: no cover

    @property
    @abstractmethod
    def supports_hybrid(self) -> bool:
        """Whether this adapter's index supports hybrid (keyword + vector) search.

        Schema-level capability only — wiring hybrid search is out of scope for this
        REQ (story: "Out of scope").
        """
        raise NotImplementedError  # pragma: no cover
