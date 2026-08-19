"""InMemoryProvisioner — local, credentials-free IndexProvisionerAdapter (REQ-011).

Story: "backed by the same dict the InMemoryIndexer uses, so the local end-to-end flow
provisions and then writes." Concretely: wraps a live `InMemoryIndexer` instance and
delegates to its own already-idempotent `ensure_schema()` (REQ-008) — no duplicated
schema-state logic (house rules: DRY, adapters thin).
"""

from __future__ import annotations

from vestibule.indexer.adapters.in_memory import InMemoryIndexer
from vestibule.indexer.model import INDEXER_SCHEMA_CONFLICT, IndexerError
from vestibule.provisioning.adapters.base import IndexProvisionerAdapter
from vestibule.provisioning.model import (
    INDEX_SCHEMA_DRIFT,
    IndexDescription,
    IndexTemplate,
    ProvisioningError,
)


class InMemoryProvisioner(IndexProvisionerAdapter):
    """Local, credentials-free `IndexProvisionerAdapter` for tests and local dev."""

    def __init__(self, indexer: InMemoryIndexer) -> None:
        """Initializes the adapter.

        Args:
            indexer: The same `InMemoryIndexer` instance the local composition root
                wires as the write-path `IndexerAdapter` — this is what makes
                "provision, then write" observable in one process without any
                adapter-to-adapter coupling beyond this constructor injection.
        """
        self._indexer = indexer

    def index_exists(self, index_name: str) -> bool:
        """See `IndexProvisionerAdapter.index_exists`.

        Uses `InMemoryIndexer.schema` (REQ-011 Assumption A15) — `schema is not None`.
        """
        return self._indexer.schema is not None

    def create_index(
        self, index_name: str, template: IndexTemplate, dimensions: int, metric: str
    ) -> None:
        """See `IndexProvisionerAdapter.create_index`.

        Delegates to `indexer.ensure_schema(dimensions, metric)` (REQ-008) — reuses its
        existing set-if-none/conflict-if-mismatched idempotency rather than
        reimplementing it. Catches `IndexerError(INDEXER_SCHEMA_CONFLICT)` and
        re-raises `ProvisioningError(INDEX_SCHEMA_DRIFT)` — the two modules' distinct
        vocabularies for the identical underlying condition. Already satisfies
        Assumption A13's idempotent create-or-update contract: two legitimate claim
        holders for the same `index_name` always resolve identical
        `dimensions`/`metric` (a deterministic function of `scenario` + the loaded
        template), so a second call always lands on `ensure_schema`'s existing
        "already compatible — idempotent no-op" branch, never its
        `INDEXER_SCHEMA_CONFLICT` branch, which is reserved for a genuine,
        independently-caused mismatch.

        Raises:
            ProvisioningError: `INDEX_SCHEMA_DRIFT` (PERMANENT) if the wrapped
                `InMemoryIndexer` already has an incompatible schema.
        """
        try:
            self._indexer.ensure_schema(dimensions, metric)
        except IndexerError as exc:
            if exc.error_code != INDEXER_SCHEMA_CONFLICT:
                raise
            raise ProvisioningError(
                index_name, exc.reason, error_code=INDEX_SCHEMA_DRIFT
            ) from exc

    def describe_index(self, index_name: str) -> IndexDescription | None:
        """See `IndexProvisionerAdapter.describe_index`."""
        schema = self._indexer.schema
        if schema is None:
            return None
        dimensions, metric = schema
        return IndexDescription(dimensions=dimensions, metric=metric)

    def delete_index(self, index_name: str) -> None:
        """See `IndexProvisionerAdapter.delete_index`.

        Best-effort test/admin utility only — resets the wrapped indexer's schema to
        `None`. Never called by `IndexProvisioner` itself (out of scope). Unlike
        `create_index` (which always goes through `ensure_schema` and never touches
        `_schema` directly), this method reaches into `InMemoryIndexer`'s private
        `_schema` attribute directly: `InMemoryIndexer` exposes no public schema-reset
        primitive (Assumption A15 adds only a read-only `schema` property), and adding
        one purely for this out-of-scope, best-effort admin path is not warranted.
        """
        self._indexer._schema = None
