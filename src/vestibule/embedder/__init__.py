"""Embedder — Phase 2 pipeline component (REQ-007)."""

from vestibule.embedder.adapters.azure_openai import AzureOpenAIEmbedder
from vestibule.embedder.adapters.base import EmbedderAdapter
from vestibule.embedder.adapters.fastembed import FastEmbedEmbedder
from vestibule.embedder.embedder import Embedder, EmbedderConfig, RetryConfig
from vestibule.embedder.model import (
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
