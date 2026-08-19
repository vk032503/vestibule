"""IndexProvisionerAdapter contract (REQ-011).

Thin store-facing port (house rules: "adapters thin" — implementations wrap an
underlying SDK/managed service, never implement provisioning algorithms themselves).
Two implementations: `AzureAISearchProvisioner`, `InMemoryProvisioner`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from vestibule.provisioning.model import IndexDescription, IndexTemplate


class IndexProvisionerAdapter(ABC):
    """Thin store-facing port every vector-store backend implements for provisioning."""

    @abstractmethod
    def index_exists(self, index_name: str) -> bool:
        """Returns whether `index_name` already exists in the underlying store."""
        raise NotImplementedError  # pragma: no cover

    @abstractmethod
    def create_index(
        self, index_name: str, template: IndexTemplate, dimensions: int, metric: str
    ) -> None:
        """Idempotent create-or-update (Assumption A13).

        Creates `index_name` if absent; if it already exists with a definition
        matching `template`/`dimensions`/`metric`, converges to the same end state as
        a no-op-equivalent success — never raises, never surfaces a 409/"already
        exists"-style conflict purely because the index was already there. This is a
        contractual requirement on every implementation, not an optional nicety: two
        legitimate callers (e.g. a still-running original claim holder and a worker
        that has since reclaimed its now-stale claim, see
        `docs/designs/REQ-011-lld.md` §3.4's Race C) may each independently call this
        method for the same `index_name` with the same resolved
        template/dimensions/metric, and both calls must converge safely, whether
        sequential or genuinely overlapping in wall-clock time.

        Args:
            index_name: The index to create or converge.
            template: The resolved `IndexTemplate` to create the index from.
            dimensions: The resolved effective vector dimension count.
            metric: The resolved similarity metric.
        """
        raise NotImplementedError  # pragma: no cover

    @abstractmethod
    def describe_index(self, index_name: str) -> IndexDescription | None:
        """Returns live schema facts for drift detection, or `None` if absent."""
        raise NotImplementedError  # pragma: no cover

    @abstractmethod
    def delete_index(self, index_name: str) -> None:
        """Deletes `index_name`.

        Never called by `IndexProvisioner` itself — used only by a future
        `mark_retired` admin flow (out of scope for this REQ).
        """
        raise NotImplementedError  # pragma: no cover
