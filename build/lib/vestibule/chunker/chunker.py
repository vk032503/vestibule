"""Chunker orchestrator (REQ-006).

`Chunker.chunk` owns exactly the `chunking` stage of Contract #3's pipeline FSM: region
partitioning, per-region strategy dispatch, the `min_chunk_tokens` merge pass,
`chunk_id`/`position` assignment, and the `chunking -> embedding` ledger transition. It
owns no splitting algorithm itself (LLD Assumption A4) — delegates entirely to
`ChunkStrategy` implementations.

Failure-path/ledger-write contract (LLD §3, §4): the one PERMANENT failure path
(`CHUNKER_EMPTY_ELEMENTS`, F1) terminalizes the ledger row to `Status.FAILED` — and,
unlike REQ-005's `Analyzer._terminalize` (a known gap tracked separately as issue #9),
this terminalize path *does* triage its own ledger write via `is_benign_concurrent_loss`
before treating it as a genuine failure (design-review MAJOR fix, LLD Assumption A5).
The two TRANSIENT failure paths (`TOKENIZER_LOAD_FAILED` F2, `CHUNKER_INTERNAL` F3)
perform a same-status `chunking -> chunking` self-transition instead — never
`Status.FAILED` — so a redelivered `chunk()` call remains re-enterable. `Chunker`
performs no *entry* transition of its own: the row is already `chunking` on entry
(written by `Analyzer`'s own `analyzing -> chunking` transition, REQ-005 §3 step 6), so
there is exactly one ledger write on the happy path (the exit, F5).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import NoReturn, Sequence, cast

from vestibule.analyzer.model import Element, ElementType
from vestibule.chunker.model import (
    CHUNKER_EMPTY_ELEMENTS,
    CHUNKER_INTERNAL,
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
from vestibule.chunker.token_counter import TokenCounter
from vestibule.envelope.model import ArrivalEnvelope
from vestibule.identity.derive import derive_chunk_id
from vestibule.ledger.store import (
    LedgerStore,
    LedgerTransitionInvalid,
    Status,
    is_benign_concurrent_loss,
)

logger = logging.getLogger(__name__)

_DEFAULT_TARGET_CHUNK_TOKENS = 512
_DEFAULT_OVERLAP_TOKENS = 64
_DEFAULT_MIN_CHUNK_TOKENS = 40
_DEFAULT_MAX_CHUNK_TOKENS = 800
_DEFAULT_TOKEN_COUNTER = "tiktoken_cl100k"


@dataclass(frozen=True)
class ChunkerConfig:
    """Documentation-authoritative-but-code-defaults tunables (LLD §6).

    Code-authoritative defaults, kept in sync with `config/chunker.yaml` via a
    dedicated test — no config-loading mechanism exists anywhere in this codebase yet
    (REQ-005 §6 Assumption A6 precedent).

    Attributes:
        target_chunk_tokens: Target size, in tokens, for a recursively-split chunk.
        overlap_tokens: Token overlap between consecutive recursively-split chunks.
        min_chunk_tokens: Threshold below which a trailing chunk is merged into its
            predecessor (LLD Assumption A8).
        max_chunk_tokens: Hard ceiling for a non-table chunk; tables may exceed this
            (logged as an advisory, never an error — LLD Assumption A3).
        token_counter: Factory key resolved via `build_token_counter`.
    """

    target_chunk_tokens: int = _DEFAULT_TARGET_CHUNK_TOKENS
    overlap_tokens: int = _DEFAULT_OVERLAP_TOKENS
    min_chunk_tokens: int = _DEFAULT_MIN_CHUNK_TOKENS
    max_chunk_tokens: int = _DEFAULT_MAX_CHUNK_TOKENS
    token_counter: str = _DEFAULT_TOKEN_COUNTER


class _RegionKind(str, Enum):
    """Closed set of region kinds the orchestrator partitions `elements` into."""

    TABLE = "table"
    NON_TABLE = "non_table"


@dataclass(frozen=True)
class _Region:
    """One contiguous, same-kind run from `_partition_regions` (LLD §3 step 3)."""

    kind: _RegionKind
    elements: list[Element]
    caption_text: str | None = None


class Chunker:
    """Orchestrates region partitioning, strategy dispatch, the min-chunk merge pass,
    chunk_id/position assignment, and the `chunking` ledger stage transitions. Owns no
    splitting algorithm itself (LLD Assumption A4)."""

    def __init__(
        self,
        ledger: LedgerStore,
        config: ChunkerConfig,
        token_counter: TokenCounter,
        *,
        recursive_strategy: ChunkStrategy | None = None,
        structure_aware_strategy: ChunkStrategy | None = None,
        table_atomic_strategy: ChunkStrategy | None = None,
    ) -> None:
        """Initializes the Chunker.

        Args:
            ledger: The `LedgerStore` this Chunker reads/writes `LedgerRow`s through.
            config: Operational tunables.
            token_counter: The `TokenCounter` passed to every strategy dispatch.
            recursive_strategy: Overrides the default `RecursiveChunkStrategy()` for
                the `recursive` slot (LLD Assumption A6, market-awareness pluggability).
            structure_aware_strategy: Overrides the default
                `StructureAwareChunkStrategy` for the `structure_aware` slot.
            table_atomic_strategy: Overrides the default `TableAtomicChunkStrategy()`
                for the `table_atomic` slot. Whatever is injected here is expected to
                accept `chunk_region`'s `caption_text`/`doc_id` keywords, exactly as
                `TableAtomicChunkStrategy` itself does.
        """
        self._ledger = ledger
        self._config = config
        self._token_counter = token_counter
        self._recursive = recursive_strategy or RecursiveChunkStrategy()
        self._structure_aware = structure_aware_strategy or StructureAwareChunkStrategy(
            RecursiveChunkStrategy()
        )
        self._table_atomic = table_atomic_strategy or TableAtomicChunkStrategy()

    def chunk(self, envelope: ArrivalEnvelope, elements: list[Element]) -> list[Chunk]:
        """Chunks `elements` and advances the ledger accordingly.

        See the LLD's §3 for the full numbered sequence, including the F1/F5
        benign-concurrent-redelivery paths.

        Args:
            envelope: The validated `ArrivalEnvelope` for this document. Only
                `envelope.doc_id` is read (Contract #1 touchpoint). A `LedgerRow` for
                `envelope.doc_id` must already be in `Status.CHUNKING` (written by
                `Analyzer`'s own `analyzing -> chunking` transition, REQ-005 §3 step 6).
            elements: The `Element`s to chunk, e.g. from `Analyzer.analyze`.

        Returns:
            The assembled `Chunk`s, in document order.

        Raises:
            ChunkerError: `CHUNKER_EMPTY_ELEMENTS` (PERMANENT, ledger row ends
                `Status.FAILED`), or `TOKENIZER_LOAD_FAILED`/`CHUNKER_INTERNAL`
                (TRANSIENT, ledger row self-transitions and stays `Status.CHUNKING`).
            LedgerTransitionInvalid: Propagated unchanged when a ledger transition
                fails for a reason `is_benign_concurrent_loss` classifies as not benign.
        """
        doc_id = envelope.doc_id
        if not elements:
            self._terminalize_empty_elements(doc_id)
        chunks = self._build_chunks(doc_id, elements)
        return self._transition_to_embedding(doc_id, chunks)

    def _terminalize_empty_elements(self, doc_id: str) -> NoReturn:
        """F1: marks the ledger row `Status.FAILED` (triaged) and raises `ChunkerError`.

        Args:
            doc_id: The document's `doc_id` (ledger key).

        Raises:
            ChunkerError: Always, `CHUNKER_EMPTY_ELEMENTS`.
            LedgerTransitionInvalid: If the ledger write fails non-benignly.
        """
        reason = "Analyzer returned zero elements; nothing to chunk"
        try:
            self._ledger.transition(
                doc_id,
                to_status=Status.FAILED,
                stage=Status.FAILED,
                error_code=CHUNKER_EMPTY_ELEMENTS,
                error_message=reason,
            )
        except LedgerTransitionInvalid as exc:
            if not is_benign_concurrent_loss(
                exc.observed_row, Status.FAILED, Status.FAILED
            ):
                raise
            # Benign: a concurrent duplicate worker already completed the whole
            # pipeline for doc_id (observed_row.status == Status.INDEXED). A terminal
            # INDEXED row must never be downgraded to FAILED, but this call's own
            # empty-elements observation is still worth surfacing to its caller.
        raise ChunkerError(doc_id, reason, error_code=CHUNKER_EMPTY_ELEMENTS)

    def _build_chunks(self, doc_id: str, elements: list[Element]) -> list[Chunk]:
        """Runs LLD §3 steps 3-8, mapping any failure to F2 or F3.

        Args:
            doc_id: The document's `doc_id` (ledger key).
            elements: The `Element`s to chunk (guaranteed non-empty by `chunk`).

        Returns:
            The assembled `Chunk`s, in document order.

        Raises:
            ChunkerError: `TOKENIZER_LOAD_FAILED` (F2) or `CHUNKER_INTERNAL` (F3).
        """
        try:
            return self._assemble_chunks(doc_id, elements)
        except ChunkerError as exc:
            if exc.error_code == TOKENIZER_LOAD_FAILED:
                self._self_transition_and_raise(
                    doc_id, TOKENIZER_LOAD_FAILED, exc.reason, cause=exc
                )
            logger.exception(
                "chunker_internal_unexpected_chunker_error",
                extra={"doc_id": doc_id, "error_code": exc.error_code},
            )
            self._self_transition_and_raise(
                doc_id, CHUNKER_INTERNAL, str(exc), cause=exc
            )
        except (
            Exception
        ) as exc:  # F3: any other unmapped exception -> CHUNKER_INTERNAL.
            logger.exception("chunker_internal", extra={"doc_id": doc_id})
            self._self_transition_and_raise(
                doc_id, CHUNKER_INTERNAL, str(exc), cause=exc
            )

    def _self_transition_and_raise(
        self, doc_id: str, error_code: str, reason: str, *, cause: BaseException
    ) -> NoReturn:
        """F2/F3: same-status `chunking -> chunking` self-transition, then raises.

        Args:
            doc_id: The document's `doc_id` (ledger key).
            error_code: `TOKENIZER_LOAD_FAILED` or `CHUNKER_INTERNAL` (both TRANSIENT).
            reason: Human-readable failure reason.
            cause: The original exception to chain via `raise ... from cause`.

        Raises:
            ChunkerError: Always.
        """
        self._ledger.transition(
            doc_id,
            to_status=Status.CHUNKING,
            stage=Status.CHUNKING,
            error_code=error_code,
            error_message=reason,
        )
        raise ChunkerError(doc_id, reason, error_code=error_code) from cause

    def _assemble_chunks(self, doc_id: str, elements: list[Element]) -> list[Chunk]:
        """LLD §3 steps 3-8: partition, dispatch, merge, concatenate, assign identity.

        Args:
            doc_id: The document's `doc_id`, threaded into `derive_chunk_id`.
            elements: The `Element`s to chunk.

        Returns:
            The assembled `Chunk`s, in document order.
        """
        has_headings = any(element.type is ElementType.HEADING for element in elements)
        all_drafts: list[ChunkDraft] = []
        for region in _partition_regions(elements):
            region_drafts = self._dispatch_region(
                region, has_headings=has_headings, doc_id=doc_id
            )
            all_drafts.extend(
                _merge_undersized_tail(region_drafts, self._config, self._token_counter)
            )
        return _assign_positions(doc_id, all_drafts)

    def _dispatch_region(
        self, region: _Region, *, has_headings: bool, doc_id: str
    ) -> list[ChunkDraft]:
        """LLD §3 step 5: dispatches one region to the appropriate `ChunkStrategy`.

        Args:
            region: The region to dispatch.
            has_headings: Whether the whole document contains any `HEADING` element.
            doc_id: The document's `doc_id`, passed through for `table_atomic`'s F4 log.

        Returns:
            The `ChunkDraft`s the dispatched strategy produced.
        """
        if region.kind is _RegionKind.TABLE:
            table_strategy = cast(TableAtomicChunkStrategy, self._table_atomic)
            return table_strategy.chunk_region(
                region.elements,
                config=self._config,
                token_counter=self._token_counter,
                caption_text=region.caption_text,
                doc_id=doc_id,
            )
        strategy = self._structure_aware if has_headings else self._recursive
        return strategy.chunk_region(
            region.elements, config=self._config, token_counter=self._token_counter
        )

    def _transition_to_embedding(self, doc_id: str, chunks: list[Chunk]) -> list[Chunk]:
        """F5: the happy-path `chunking -> embedding` exit transition.

        Args:
            doc_id: The document's `doc_id` (ledger key).
            chunks: The assembled `Chunk`s to return on success/benign redelivery.

        Returns:
            `chunks`, unchanged.

        Raises:
            LedgerTransitionInvalid: If the transition fails non-benignly.
        """
        try:
            self._ledger.transition(
                doc_id, to_status=Status.EMBEDDING, stage=Status.EMBEDDING
            )
        except LedgerTransitionInvalid as exc:
            if not is_benign_concurrent_loss(
                exc.observed_row, Status.EMBEDDING, Status.EMBEDDING
            ):
                raise
            # Benign: a concurrent duplicate worker already made this exact edge (or
            # later) happen — deterministic recomputation (Contract #2) makes returning
            # the freshly-computed chunks safe, not a correctness risk.
        return chunks


def _partition_regions(elements: Sequence[Element]) -> list[_Region]:
    """LLD §3 step 3: partitions `elements` into ordered, contiguous regions.

    Args:
        elements: The full document's `Element`s, in order.

    Returns:
        Each `TABLE` element as its own single-element `_RegionKind.TABLE` region;
        every maximal run of non-`TABLE` elements as one `_RegionKind.NON_TABLE`
        region. Document order is preserved by construction.
    """
    regions: list[_Region] = []
    buffer: list[Element] = []
    for index, element in enumerate(elements):
        if element.type is not ElementType.TABLE:
            buffer.append(element)
            continue
        if buffer:
            regions.append(_Region(kind=_RegionKind.NON_TABLE, elements=buffer))
            buffer = []
        regions.append(
            _Region(
                kind=_RegionKind.TABLE,
                elements=[element],
                caption_text=_caption_for_table(elements, index),
            )
        )
    if buffer:
        regions.append(_Region(kind=_RegionKind.NON_TABLE, elements=buffer))
    return regions


def _caption_for_table(elements: Sequence[Element], table_index: int) -> str | None:
    """LLD Assumption A2's caption-adjacency heuristic for one `TABLE` element.

    Args:
        elements: The full document's `Element`s, in order.
        table_index: The index of the `TABLE` element within `elements`.

    Returns:
        The immediately preceding element's text, if that element's type is
        `PARAGRAPH` or `IMAGE_CAPTION`; `None` otherwise (including when `table_index`
        is `0`).
    """
    if table_index == 0:
        return None
    preceding = elements[table_index - 1]
    if preceding.type in (ElementType.PARAGRAPH, ElementType.IMAGE_CAPTION):
        return preceding.text
    return None


def _merge_undersized_tail(
    drafts: list[ChunkDraft], config: ChunkerConfig, token_counter: TokenCounter
) -> list[ChunkDraft]:
    """LLD §3 step 6 / Assumption A8: merges an undersized trailing draft, repeatedly.

    Args:
        drafts: One region's own `ChunkDraft`s, as returned by its `ChunkStrategy`.
        config: Supplies `min_chunk_tokens`.
        token_counter: Used to recompute `token_count` after each merge.

    Returns:
        `drafts`, with its trailing entries merged while the last draft's
        `token_count < config.min_chunk_tokens`, more than one draft remains, and
        neither the last draft nor its predecessor is `table_atomic`-tagged.
    """
    merged = list(drafts)
    while (
        len(merged) > 1
        and merged[-1].token_count < config.min_chunk_tokens
        and merged[-1].metadata.get("strategy") != STRATEGY_TABLE_ATOMIC
        and merged[-2].metadata.get("strategy") != STRATEGY_TABLE_ATOMIC
    ):
        merged[-2:] = [_merge_two_drafts(merged[-2], merged[-1], token_counter)]
    return merged


def _merge_two_drafts(
    predecessor: ChunkDraft, last: ChunkDraft, token_counter: TokenCounter
) -> ChunkDraft:
    """Merges `last` into `predecessor`: concatenated text, unioned element types.

    Args:
        predecessor: The draft `last` is merged into.
        last: The undersized trailing draft being absorbed.
        token_counter: Used to recompute the merged draft's `token_count`.

    Returns:
        A new `ChunkDraft` keeping `predecessor`'s `metadata` (so `section_path`/`page`
        survive the merge unchanged).
    """
    merged_text = f"{predecessor.text}\n\n{last.text}"
    merged_types = list(predecessor.element_types)
    for element_type in last.element_types:
        if element_type not in merged_types:
            merged_types.append(element_type)
    return ChunkDraft(
        element_types=merged_types,
        text=merged_text,
        token_count=token_counter.count(merged_text),
        metadata=predecessor.metadata,
    )


def _assign_positions(doc_id: str, drafts: list[ChunkDraft]) -> list[Chunk]:
    """LLD §3 step 8 / Assumption A7: assigns `position`/`chunk_id`, builds `Chunk`s.

    Args:
        doc_id: The document's `doc_id`, threaded into `derive_chunk_id`.
        drafts: Every region's (merged) `ChunkDraft`s, concatenated in document order.

    Returns:
        One `Chunk` per draft, in the same order, with `position = index` and
        `chunk_id = derive_chunk_id(doc_id, position)`.
    """
    chunks: list[Chunk] = []
    for position, draft in enumerate(drafts):
        chunks.append(
            Chunk(
                chunk_id=derive_chunk_id(doc_id, position),
                doc_id=doc_id,
                position=position,
                element_types=draft.element_types,
                text=draft.text,
                token_count=draft.token_count,
                metadata=draft.metadata,
            )
        )
    return chunks
