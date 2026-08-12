"""Embedder — Phase 2 pipeline component (REQ-007)."""

from ingestion.embedder.adapters.azure_openai import AzureOpenAIEmbedder
from ingestion.embedder.adapters.base import EmbedderAdapter
from ingestion.embedder.adapters.fastembed import FastEmbedEmbedder
from ingestion.embedder.embedder import Embedder, EmbedderConfig, RetryConfig
from ingestion.embedder.model import (
    EMBEDDER_DEPENDENCY_MISSING,
    EMBEDDER_EMPTY_CHUNKS,
    EMBEDDER_INPUT_TOO_LONG,
    EMBEDDER_INTERNAL,
    EMBEDDER_MRL_UNSUPPORTED,
    EMBEDDER_RATE_LIMITED,
    EMBEDDER_TIMEOUT,
    EMBEDDER_UPSTREAM_ERROR,
    EmbeddedChunk,
    EmbedderError,
)

__all__ = [
    "EMBEDDER_DEPENDENCY_MISSING",
    "EMBEDDER_EMPTY_CHUNKS",
    "EMBEDDER_INPUT_TOO_LONG",
    "EMBEDDER_INTERNAL",
    "EMBEDDER_MRL_UNSUPPORTED",
    "EMBEDDER_RATE_LIMITED",
    "EMBEDDER_TIMEOUT",
    "EMBEDDER_UPSTREAM_ERROR",
    "AzureOpenAIEmbedder",
    "Embedder",
    "EmbedderAdapter",
    "EmbedderConfig",
    "EmbedderError",
    "EmbeddedChunk",
    "FastEmbedEmbedder",
    "RetryConfig",
]
