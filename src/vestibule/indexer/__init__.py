"""Indexer — Phase 2 pipeline component (REQ-008)."""

from vestibule.indexer.adapters.azure_ai_search import AzureAISearchIndexer
from vestibule.indexer.adapters.base import IndexerAdapter, UpsertOutcome
from vestibule.indexer.adapters.in_memory import InMemoryIndexer
from vestibule.indexer.indexer import Indexer, IndexerConfig, RetryConfig
from vestibule.indexer.model import (
    INDEXER_DEPENDENCY_MISSING,
    INDEXER_DIMENSION_MISMATCH,
    INDEXER_EMPTY_RECORDS,
    INDEXER_INTERNAL,
    INDEXER_MIXED_MODEL_BATCH,
    INDEXER_PARTIAL_BATCH_FAILURE,
    INDEXER_RATE_LIMITED,
    INDEXER_RECORD_TOO_LARGE,
    INDEXER_SCHEMA_CONFLICT,
    INDEXER_TIMEOUT,
    INDEXER_UPSTREAM_ERROR,
    IndexerError,
    IndexRecord,
    IndexResult,
)

__all__ = [
    "INDEXER_DEPENDENCY_MISSING",
    "INDEXER_DIMENSION_MISMATCH",
    "INDEXER_EMPTY_RECORDS",
    "INDEXER_INTERNAL",
    "INDEXER_MIXED_MODEL_BATCH",
    "INDEXER_PARTIAL_BATCH_FAILURE",
    "INDEXER_RATE_LIMITED",
    "INDEXER_RECORD_TOO_LARGE",
    "INDEXER_SCHEMA_CONFLICT",
    "INDEXER_TIMEOUT",
    "INDEXER_UPSTREAM_ERROR",
    "AzureAISearchIndexer",
    "Indexer",
    "IndexerAdapter",
    "IndexerConfig",
    "IndexerError",
    "IndexRecord",
    "IndexResult",
    "InMemoryIndexer",
    "RetryConfig",
    "UpsertOutcome",
]
