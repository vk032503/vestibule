"""Chunker — Phase 2 pipeline component (REQ-006)."""

from ingestion.chunker.chunker import Chunker, ChunkerConfig
from ingestion.chunker.model import (
    CHUNKER_EMPTY_ELEMENTS,
    CHUNKER_INTERNAL,
    OVERSIZED_TABLE_LOG_EVENT,
    STRATEGY_RECURSIVE,
    STRATEGY_STRUCTURE_AWARE,
    STRATEGY_TABLE_ATOMIC,
    TOKENIZER_LOAD_FAILED,
    Chunk,
    ChunkDraft,
    ChunkerError,
)
from ingestion.chunker.strategies.base import ChunkStrategy
from ingestion.chunker.strategies.recursive import RecursiveChunkStrategy
from ingestion.chunker.strategies.structure_aware import StructureAwareChunkStrategy
from ingestion.chunker.strategies.table_atomic import TableAtomicChunkStrategy
from ingestion.chunker.token_counter import (
    TiktokenCounter,
    TokenCounter,
    build_token_counter,
)

__all__ = [
    "CHUNKER_EMPTY_ELEMENTS",
    "CHUNKER_INTERNAL",
    "OVERSIZED_TABLE_LOG_EVENT",
    "STRATEGY_RECURSIVE",
    "STRATEGY_STRUCTURE_AWARE",
    "STRATEGY_TABLE_ATOMIC",
    "TOKENIZER_LOAD_FAILED",
    "Chunk",
    "ChunkDraft",
    "Chunker",
    "ChunkerConfig",
    "ChunkerError",
    "ChunkStrategy",
    "RecursiveChunkStrategy",
    "StructureAwareChunkStrategy",
    "TableAtomicChunkStrategy",
    "TiktokenCounter",
    "TokenCounter",
    "build_token_counter",
]
