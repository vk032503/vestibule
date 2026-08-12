"""Chunker — Phase 2 pipeline component (REQ-006)."""

from vestibule.chunker.chunker import Chunker, ChunkerConfig
from vestibule.chunker.model import (
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
from vestibule.chunker.strategies.base import ChunkStrategy
from vestibule.chunker.strategies.recursive import RecursiveChunkStrategy
from vestibule.chunker.strategies.structure_aware import StructureAwareChunkStrategy
from vestibule.chunker.strategies.table_atomic import TableAtomicChunkStrategy
from vestibule.chunker.token_counter import (
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
