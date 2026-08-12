"""Real `_SearchBackend` translation layer for `AzureAISearchIndexer` (REQ-008).

Split out of `azure_ai_search.py` to keep that module's own testable
orchestration/retry logic under the house rules' module-size guidance. Everything in
this module is the "wrap the real SDK" concern only (house rules: "adapters thin") and
is constructed exclusively by `_load_real_backend`, itself called only from
`AzureAISearchIndexer.__init__` when no test `_backend` was injected. Its translation
logic cannot be exercised hermetically (no live Azure AI Search calls in CI, story: "no
live API calls in CI") — kept intentionally thin and best-effort.
"""

from __future__ import annotations

from typing import Any, Callable

from azure.core.exceptions import ResourceNotFoundError

from ingestion.indexer.adapters.azure_ai_search import _IndexDefinition, _SearchBackend
from ingestion.indexer.model import INDEXER_DEPENDENCY_MISSING, IndexerError


def _load_real_backend(endpoint: str, api_key: str) -> _SearchBackend:
    """Lazily imports `azure-search-documents` and builds the real search backend.

    Args:
        endpoint: The Azure AI Search resource endpoint URL.
        api_key: The Azure AI Search admin API key.

    Returns:
        A `_RealSearchBackend` wrapping real `SearchIndexClient`/`SearchClient`
        instances.

    Raises:
        IndexerError: `INDEXER_DEPENDENCY_MISSING` (PERMANENT) if
            `azure-search-documents` is not installed.
    """
    try:
        from azure.core.credentials import AzureKeyCredential
        from azure.search.documents import SearchClient
        from azure.search.documents.indexes import SearchIndexClient
    except ImportError as exc:
        raise IndexerError(
            "",
            "azure-search-documents is not installed; install the 'azure_search' "
            "extra (pip install vestibule[azure_search]) to use AzureAISearchIndexer",
            error_code=INDEXER_DEPENDENCY_MISSING,
        ) from exc
    credential = AzureKeyCredential(api_key)
    index_client = SearchIndexClient(endpoint, credential)

    def _search_client_factory(name: str) -> Any:
        return SearchClient(endpoint, name, credential)

    return _RealSearchBackend(index_client, _search_client_factory)


class _RealSearchBackend:
    """Production `_SearchBackend`: translates this adapter's calls to the real SDK.

    Constructed only after `azure-search-documents` has been successfully imported
    (`_load_real_backend`).
    """

    def __init__(
        self, index_client: Any, search_client_factory: Callable[[str], Any]
    ) -> None:
        """Initializes the backend.

        Args:
            index_client: A real `azure.search.documents.indexes.SearchIndexClient`.
            search_client_factory: Builds a real
                `azure.search.documents.SearchClient` for a given index name.
        """
        self._index_client = index_client
        self._search_client_factory = search_client_factory

    def get_index_schema(self, index_name: str) -> tuple[int, str] | None:
        """See `_SearchBackend.get_index_schema`."""
        try:
            index = self._index_client.get_index(index_name)
        except ResourceNotFoundError:
            return None
        vector_field = next(
            (f for f in index.fields if getattr(f, "vector_search_dimensions", None)),
            None,
        )
        if vector_field is None:
            return None
        metric = _extract_metric(index)
        return vector_field.vector_search_dimensions, metric

    def create_or_update_index(self, definition: _IndexDefinition) -> None:
        """See `_SearchBackend.create_or_update_index`."""
        self._index_client.create_or_update_index(_build_search_index(definition))

    def upload_documents(
        self, index_name: str, documents: list[dict[str, Any]]
    ) -> list[tuple[str, bool, str | None]]:
        """See `_SearchBackend.upload_documents`."""
        client = self._search_client_factory(index_name)
        results = client.merge_or_upload_documents(documents=documents)
        return [
            (r.key, r.succeeded, getattr(r, "error_message", None)) for r in results
        ]

    def delete_documents(self, index_name: str, chunk_ids: list[str]) -> None:
        """See `_SearchBackend.delete_documents`."""
        client = self._search_client_factory(index_name)
        client.delete_documents(documents=[{"chunk_id": cid} for cid in chunk_ids])

    def documents_for_doc_id(
        self, index_name: str, doc_id: str
    ) -> list[dict[str, Any]]:
        """See `_SearchBackend.documents_for_doc_id`."""
        client = self._search_client_factory(index_name)
        escaped = doc_id.replace("'", "''")
        results = client.search(
            search_text="*",
            filter=f"doc_id eq '{escaped}'",
            select=["chunk_id", "doc_version"],
        )
        return list(results)


def _extract_metric(index: Any) -> str:
    """Best-effort extraction of the configured vector search metric from an index.

    Args:
        index: A real `azure.search.documents.indexes.models.SearchIndex`.

    Returns:
        The first HNSW algorithm configuration's metric name, or `"cosine"` if the
        index carries no discoverable vector-search algorithm configuration.
    """
    vector_search = getattr(index, "vector_search", None)
    algorithms = getattr(vector_search, "algorithms", None) or []
    for algorithm in algorithms:
        parameters = getattr(algorithm, "parameters", None)
        metric = getattr(parameters, "metric", None)
        if metric is not None:
            return str(metric)
    return "cosine"


def _build_fields(definition: _IndexDefinition) -> list[Any]:
    """Builds the `SearchIndex.fields` list from this adapter's `_IndexDefinition`.

    Lazily imports `azure.search.documents.indexes.models` — only ever called (via
    `_build_search_index`) after `_load_real_backend` has already confirmed
    `azure-search-documents` is installed.

    Args:
        definition: This adapter's store-agnostic schema request.

    Returns:
        Field definitions with `chunk_id` as key, `content`/`content_vector` fields,
        and filterable `doc_id`/`doc_version`/`allowed_groups`/`trust_tier`/
        `config_version` fields (story: "Two adapters").
    """
    from azure.search.documents.indexes.models import (
        SearchField,
        SearchFieldDataType,
        SimpleField,
    )

    profile_name = f"{definition.index_name}-vector-profile"
    return [
        SimpleField(name="chunk_id", type=SearchFieldDataType.String, key=True),
        SearchField(name="content", type=SearchFieldDataType.String, searchable=True),
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=definition.dimensions,
            vector_search_profile_name=profile_name,
        ),
        SimpleField(name="doc_id", type=SearchFieldDataType.String, filterable=True),
        SimpleField(
            name="doc_version", type=SearchFieldDataType.String, filterable=True
        ),
        SimpleField(
            name="allowed_groups",
            type=SearchFieldDataType.Collection(SearchFieldDataType.String),
            filterable=True,
        ),
        SimpleField(
            name="trust_tier", type=SearchFieldDataType.String, filterable=True
        ),
        SimpleField(
            name="config_version", type=SearchFieldDataType.String, filterable=True
        ),
    ]


def _build_vector_search(definition: _IndexDefinition) -> Any:
    """Builds the `SearchIndex.vector_search` (HNSW) config from this adapter's own
    `_IndexDefinition`.

    Lazily imports `azure.search.documents.indexes.models` — only ever called (via
    `_build_search_index`) after `_load_real_backend` has already confirmed
    `azure-search-documents` is installed.

    Args:
        definition: This adapter's store-agnostic schema request.

    Returns:
        A real `azure.search.documents.indexes.models.VectorSearch` with a single HNSW
        algorithm configuration and matching vector-search profile.
    """
    from azure.search.documents.indexes.models import (
        HnswAlgorithmConfiguration,
        HnswParameters,
        VectorSearch,
        VectorSearchAlgorithmMetric,
        VectorSearchProfile,
    )

    algorithm_name = f"{definition.index_name}-hnsw"
    profile_name = f"{definition.index_name}-vector-profile"
    return VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(
                name=algorithm_name,
                parameters=HnswParameters(
                    m=definition.hnsw_m,
                    ef_construction=definition.hnsw_ef_construction,
                    ef_search=definition.hnsw_ef_search,
                    metric=VectorSearchAlgorithmMetric(definition.metric),
                ),
            )
        ],
        profiles=[
            VectorSearchProfile(
                name=profile_name, algorithm_configuration_name=algorithm_name
            )
        ],
    )


def _build_search_index(definition: _IndexDefinition) -> Any:
    """Builds a real `SearchIndex` object from this adapter's own `_IndexDefinition`.

    Composes `_build_fields` and `_build_vector_search` — only ever called after
    `_load_real_backend` has already confirmed `azure-search-documents` is installed.

    Args:
        definition: This adapter's store-agnostic schema request.

    Returns:
        A real `azure.search.documents.indexes.models.SearchIndex`, with `chunk_id` as
        key, `content`/`content_vector` fields, and filterable `doc_id`/`doc_version`/
        `allowed_groups`/`trust_tier`/`config_version` fields (story: "Two adapters").
    """
    from azure.search.documents.indexes.models import SearchIndex

    return SearchIndex(
        name=definition.index_name,
        fields=_build_fields(definition),
        vector_search=_build_vector_search(definition),
    )
