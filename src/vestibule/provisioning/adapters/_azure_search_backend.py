"""Real `_ProvisionerBackend` translation layer for `AzureAISearchProvisioner`
(REQ-011).

Split out of `azure_ai_search.py` to keep that module's own testable
orchestration/retry logic under the house rules' module-size guidance. Everything in
this module is the "wrap the real SDK" concern only (house rules: "adapters thin") and
is constructed exclusively by `_load_real_backend`, itself called only from
`AzureAISearchProvisioner.__init__` when no test `_backend` was injected. Its
translation logic cannot be exercised hermetically (no live Azure AI Search calls in
CI) — kept intentionally thin and best-effort, mirroring
`vestibule.indexer.adapters._azure_search_backend`'s precedent almost exactly,
generalized from a fixed 8-field list to `IndexTemplate.fields`.
"""

from __future__ import annotations

from typing import Any, Callable

from azure.core.exceptions import ResourceNotFoundError

from vestibule.provisioning.adapters.azure_ai_search import (
    _IndexDefinition,
    _ProvisionerBackend,
)
from vestibule.provisioning.model import (
    INDEX_PROVISIONER_DEPENDENCY_MISSING,
    ProvisioningError,
)

_EXTRA_NAME = "azure_search"


def _load_real_backend(endpoint: str, api_key: str) -> _ProvisionerBackend:
    """Lazily imports `azure-search-documents` and builds the real search backend.

    Args:
        endpoint: The Azure AI Search resource endpoint URL.
        api_key: The Azure AI Search admin API key.

    Returns:
        A `_RealSearchBackend` wrapping a real `SearchIndexClient`.

    Raises:
        ProvisioningError: `INDEX_PROVISIONER_DEPENDENCY_MISSING` (PERMANENT) if
            `azure-search-documents` is not installed.
    """
    try:
        from azure.core.credentials import AzureKeyCredential
        from azure.search.documents.indexes import SearchIndexClient
    except ImportError as exc:
        raise ProvisioningError(
            "",
            "azure-search-documents is not installed; install the 'azure_search' "
            f"extra (pip install vestibule[{_EXTRA_NAME}]) to use "
            "AzureAISearchProvisioner",
            error_code=INDEX_PROVISIONER_DEPENDENCY_MISSING,
        ) from exc
    credential = AzureKeyCredential(api_key)
    index_client = SearchIndexClient(endpoint, credential)
    return _RealSearchBackend(index_client)


class _RealSearchBackend:
    """Production `_ProvisionerBackend`: translates this adapter's calls to the real
    SDK.

    Constructed only after `azure-search-documents` has been successfully imported
    (`_load_real_backend`).
    """

    def __init__(self, index_client: Any) -> None:
        """Initializes the backend.

        Args:
            index_client: A real `azure.search.documents.indexes.SearchIndexClient`.
        """
        self._index_client = index_client

    def get_index_schema(self, index_name: str) -> tuple[int, str] | None:
        """See `_ProvisionerBackend.get_index_schema`."""
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
        return vector_field.vector_search_dimensions, _extract_metric(index)

    def create_or_update_index(self, definition: _IndexDefinition) -> None:
        """See `_ProvisionerBackend.create_or_update_index`."""
        self._index_client.create_or_update_index(_build_search_index(definition))

    def delete_index(self, index_name: str) -> None:
        """See `_ProvisionerBackend.delete_index`."""
        self._index_client.delete_index(index_name)


def _extract_metric(index: Any) -> str:
    """Best-effort extraction of the configured vector search metric from an index."""
    vector_search = getattr(index, "vector_search", None)
    algorithms = getattr(vector_search, "algorithms", None) or []
    for algorithm in algorithms:
        parameters = getattr(algorithm, "parameters", None)
        metric = getattr(parameters, "metric", None)
        if metric is not None:
            return str(metric)
    return "cosine"


def _build_fields(definition: _IndexDefinition) -> list[Any]:
    """Builds the `SearchIndex.fields` list from `definition.fields`
    (`IndexTemplate.fields`, store-agnostic).

    Lazily imports `azure.search.documents.indexes.models` — only ever called (via
    `_build_search_index`) after `_load_real_backend` has already confirmed
    `azure-search-documents` is installed.
    """
    from azure.search.documents.indexes.models import (
        SearchField,
        SearchFieldDataType,
        SimpleField,
    )

    profile_name = f"{definition.index_name}-vector-profile"
    builders: dict[str, Callable[[Any], Any]] = {
        "key": lambda f: SimpleField(
            name=f.name, type=SearchFieldDataType.String, key=True
        ),
        "text": lambda f: SearchField(
            name=f.name, type=SearchFieldDataType.String, searchable=f.searchable
        ),
        "vector": lambda f: SearchField(
            name=f.name,
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=f.searchable,
            vector_search_dimensions=definition.dimensions,
            vector_search_profile_name=profile_name,
        ),
        "filterable_string": lambda f: SimpleField(
            name=f.name, type=SearchFieldDataType.String, filterable=True
        ),
        "filterable_string_collection": lambda f: SimpleField(
            name=f.name,
            type=SearchFieldDataType.Collection(SearchFieldDataType.String),
            filterable=True,
        ),
        "filterable_datetime": lambda f: SimpleField(
            name=f.name, type=SearchFieldDataType.DateTimeOffset, filterable=True
        ),
        "retrievable_only": lambda f: SimpleField(
            name=f.name, type=SearchFieldDataType.String
        ),
    }
    return [builders[field.type](field) for field in definition.fields]


def _build_vector_search(definition: _IndexDefinition) -> Any:
    """Builds the `SearchIndex.vector_search` (HNSW) config from `definition`.

    Lazily imports `azure.search.documents.indexes.models` — only ever called (via
    `_build_search_index`) after `_load_real_backend` has already confirmed
    `azure-search-documents` is installed.
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


def _build_semantic_search(definition: _IndexDefinition) -> Any | None:
    """Builds the `SearchIndex.semantic_search` config iff
    `definition.semantic_ranker_enabled`, prioritizing the template's first `"text"`
    field as the semantic content field.

    Lazily imports `azure.search.documents.indexes.models` — only ever called (via
    `_build_search_index`) after `_load_real_backend` has already confirmed
    `azure-search-documents` is installed.

    Returns:
        A real `SemanticSearch` config, or `None` if
        `definition.semantic_ranker_enabled` is `False`.
    """
    if not definition.semantic_ranker_enabled:
        return None
    from azure.search.documents.indexes.models import (
        SemanticConfiguration,
        SemanticField,
        SemanticPrioritizedFields,
        SemanticSearch,
    )

    text_fields = [f.name for f in definition.fields if f.type == "text"]
    content_fields = [SemanticField(field_name=name) for name in text_fields]
    configuration = SemanticConfiguration(
        name=f"{definition.index_name}-semantic-config",
        prioritized_fields=SemanticPrioritizedFields(content_fields=content_fields),
    )
    return SemanticSearch(configurations=[configuration])


def _build_search_index(definition: _IndexDefinition) -> Any:
    """Builds a real `SearchIndex` object from `definition`.

    Composes `_build_fields`, `_build_vector_search`, and `_build_semantic_search` —
    only ever called after `_load_real_backend` has already confirmed
    `azure-search-documents` is installed.
    """
    from azure.search.documents.indexes.models import SearchIndex

    return SearchIndex(
        name=definition.index_name,
        fields=_build_fields(definition),
        vector_search=_build_vector_search(definition),
        semantic_search=_build_semantic_search(definition),
    )
