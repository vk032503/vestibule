"""Real `_TableBackend` translation layer for `TableStorageScenarioStore` (REQ-010).

Split out of `table_storage_store.py` to keep that module's own testable
orchestration/retry logic under the house rules' module-size guidance (mirrors
`vestibule.indexer.adapters._azure_search_backend`'s precedent for the same reason).
Everything in this module is the "wrap the real SDK" concern only (house rules:
"adapters thin") and is constructed exclusively by `_load_real_backend`, itself called
only from `TableStorageScenarioStore.__init__` when no test `_backend` was injected. Its
translation logic cannot be exercised hermetically (no live Azure calls in CI) and is
kept intentionally thin.

OData filter values are parameterized (`@pk`/`@rk` placeholders plus a `parameters`
dict), never spliced into the filter string via f-string/`str.format` interpolation
(house rules: "parameterized queries always").
"""

from __future__ import annotations

from typing import Any

from vestibule.scenario.model import SCENARIO_STORE_DEPENDENCY_MISSING, ScenarioError
from vestibule.scenario.stores.table_storage_store import (
    _TableBackend,
    _TableEntityRecord,
)

_EXTRA_NAME = "table_storage"


def _load_real_backend(connection_string: str, table_name: str) -> _TableBackend:
    """Lazily imports `azure-data-tables`, ensures the table exists, and builds the
    real backend.

    Args:
        connection_string: The Table Storage connection string.
        table_name: The table to read/write, created if it does not already exist.

    Returns:
        A `_RealTableBackend` wrapping a real `TableClient`.

    Raises:
        ScenarioError: `SCENARIO_STORE_DEPENDENCY_MISSING` (PERMANENT) if
            `azure-data-tables` is not installed.
    """
    try:
        from azure.data.tables import TableClient, TableServiceClient
    except ImportError as exc:
        raise ScenarioError(
            "",
            "azure-data-tables is not installed; install the 'table_storage' extra "
            f"(pip install vestibule[{_EXTRA_NAME}]) to use TableStorageScenarioStore",
            error_code=SCENARIO_STORE_DEPENDENCY_MISSING,
        ) from exc
    service_client = TableServiceClient.from_connection_string(connection_string)
    service_client.create_table_if_not_exists(table_name)
    table_client = TableClient.from_connection_string(
        connection_string, table_name=table_name
    )
    return _RealTableBackend(table_client)


class _RealTableBackend:
    """Production `_TableBackend`: translates this store's calls to the real SDK.

    Constructed only after `azure-data-tables` has been successfully imported
    (`_load_real_backend`).
    """

    def __init__(self, table_client: Any) -> None:
        """Initializes the backend.

        Args:
            table_client: A real `azure.data.tables.TableClient`.
        """
        self._table_client = table_client

    def get_entity(self, partition_key: str, row_key: str) -> _TableEntityRecord | None:
        """See `_TableBackend.get_entity`."""
        from azure.core.exceptions import ResourceNotFoundError

        try:
            entity = self._table_client.get_entity(
                partition_key=partition_key, row_key=row_key
            )
        except ResourceNotFoundError:
            return None
        return _to_record(entity)

    def query(
        self, *, partition_key: str | None = None, row_key: str | None = None
    ) -> list[_TableEntityRecord]:
        """See `_TableBackend.query`."""
        clauses: list[str] = []
        parameters: dict[str, Any] = {}
        if partition_key is not None:
            clauses.append("PartitionKey eq @pk")
            parameters["pk"] = partition_key
        if row_key is not None:
            clauses.append("RowKey eq @rk")
            parameters["rk"] = row_key
        if not clauses:
            entities = self._table_client.list_entities()
        else:
            entities = self._table_client.query_entities(
                query_filter=" and ".join(clauses), parameters=parameters
            )
        return [_to_record(entity) for entity in entities]

    def upsert_entity(
        self,
        partition_key: str,
        row_key: str,
        data: dict[str, Any],
        *,
        etag: str | None,
    ) -> str:
        """See `_TableBackend.upsert_entity`."""
        from azure.data.tables import UpdateMode

        entity = {"PartitionKey": partition_key, "RowKey": row_key, **data}
        if etag is None:
            result = self._table_client.create_entity(entity=entity)
        else:
            result = self._table_client.update_entity(
                entity=entity,
                mode=UpdateMode.REPLACE,
                etag=etag,
                match_condition=_if_not_modified(),
            )
        return str(result.get("etag", ""))

    def delete_entity(self, partition_key: str, row_key: str) -> None:
        """See `_TableBackend.delete_entity`."""
        self._table_client.delete_entity(partition_key=partition_key, row_key=row_key)


def _if_not_modified() -> Any:
    """Lazily imports and returns `MatchConditions.IfNotModified` (ETag precondition)."""
    from azure.core import MatchConditions

    return MatchConditions.IfNotModified


def _to_record(entity: Any) -> _TableEntityRecord:
    """Converts a real SDK `TableEntity` into this module's own `_TableEntityRecord`."""
    etag = str(entity.metadata.get("etag", "")) if hasattr(entity, "metadata") else ""
    data = {
        key: value
        for key, value in dict(entity).items()
        if key not in ("PartitionKey", "RowKey", "Timestamp", "etag")
    }
    return _TableEntityRecord(
        partition_key=str(entity["PartitionKey"]),
        row_key=str(entity["RowKey"]),
        data=data,
        etag=etag,
    )
