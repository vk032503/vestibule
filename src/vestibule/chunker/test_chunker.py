"""Unit tests for the Chunker orchestrator (REQ-006)."""

from __future__ import annotations

import re
import string
from pathlib import Path
from typing import Sequence

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import vestibule.chunker.chunker as chunker_module
from vestibule.analyzer.model import Element, ElementType
from vestibule.chunker.chunker import Chunker, ChunkerConfig
from vestibule.chunker.conftest import (
    code_block,
    heading,
    image_caption,
    list_item,
    make_envelope,
    paragraph,
    table,
)
from vestibule.chunker.model import (
    CHUNKER_EMPTY_ELEMENTS,
    CHUNKER_INTERNAL,
    STRATEGY_RECURSIVE,
    STRATEGY_STRUCTURE_AWARE,
    STRATEGY_TABLE_ATOMIC,
    TOKENIZER_LOAD_FAILED,
    ChunkDraft,
    ChunkerError,
)
from vestibule.chunker.strategies.base import ChunkStrategy
from vestibule.chunker.token_counter import TiktokenCounter, TokenCounter
from vestibule.envelope.model import ArrivalEnvelope
from vestibule.identity.derive import derive_chunk_id
from vestibule.ledger.store import (
    EnvelopeSummary,
    InMemoryLedgerStore,
    LedgerTransitionInvalid,
    Status,
)


class _RaisingStrategy(ChunkStrategy):
    """A ChunkStrategy test double that always raises a fixed exception."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def chunk_region(
        self,
        elements: Sequence[Element],
        *,
        config: ChunkerConfig,
        token_counter: TokenCounter,
    ) -> list[ChunkDraft]:
        raise self._exc


class _FixedDraftStrategy(ChunkStrategy):
    """A ChunkStrategy test double that returns a fixed list of ChunkDrafts."""

    def __init__(self, drafts: list[ChunkDraft]) -> None:
        self._drafts = drafts

    def chunk_region(
        self,
        elements: Sequence[Element],
        *,
        config: ChunkerConfig,
        token_counter: TokenCounter,
    ) -> list[ChunkDraft]:
        return list(self._drafts)


class _FailingTokenCounter:
    """A TokenCounter test double that always raises ChunkerError(TOKENIZER_LOAD_FAILED)."""

    def count(self, text: str) -> int:
        raise ChunkerError("", "encoding unavailable", error_code=TOKENIZER_LOAD_FAILED)


def _build_chunker(
    *,
    config: ChunkerConfig | None = None,
    token_counter: TokenCounter | None = None,
    recursive_strategy: ChunkStrategy | None = None,
    structure_aware_strategy: ChunkStrategy | None = None,
    table_atomic_strategy: ChunkStrategy | None = None,
) -> tuple[Chunker, InMemoryLedgerStore]:
    ledger = InMemoryLedgerStore()
    chunker = Chunker(
        ledger,
        config or ChunkerConfig(),
        token_counter or TiktokenCounter("cl100k_base"),
        recursive_strategy=recursive_strategy,
        structure_aware_strategy=structure_aware_strategy,
        table_atomic_strategy=table_atomic_strategy,
    )
    return chunker, ledger


def _seed_chunking(ledger: InMemoryLedgerStore, doc_id: str) -> None:
    """Advances a freshly-created row to Status.CHUNKING — Chunker's expected entry state."""
    ledger.create(doc_id, EnvelopeSummary(config_version="v1"))
    ledger.transition(doc_id, Status.ANALYZING, Status.ANALYZING)
    ledger.transition(doc_id, Status.CHUNKING, Status.CHUNKING)


def _advance_to(ledger: InMemoryLedgerStore, doc_id: str, target: Status) -> None:
    """Advances a freshly-created row through legal forward edges up to `target`."""
    ledger.create(doc_id, EnvelopeSummary(config_version="v1"))
    for status in (
        Status.ANALYZING,
        Status.CHUNKING,
        Status.EMBEDDING,
        Status.INDEXING,
        Status.INDEXED,
    ):
        ledger.transition(doc_id, status, status)
        if status == target:
            return


# --- happy path (AC1, AC4) ----------------------------------------------------------------


def test_chunk_success_transitions_chunking_to_embedding() -> None:
    chunker, ledger = _build_chunker()
    envelope = make_envelope("happy-path")
    _seed_chunking(ledger, envelope.doc_id)

    chunks = chunker.chunk(envelope, [paragraph("hello world, a short paragraph.")])

    assert len(chunks) == 1
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.EMBEDDING


def test_chunk_every_chunk_has_a_valid_derived_chunk_id() -> None:
    chunker, ledger = _build_chunker()
    envelope = make_envelope("valid-chunk-ids")
    _seed_chunking(ledger, envelope.doc_id)

    chunks = chunker.chunk(
        envelope, [paragraph("first"), table([["a", "b"]]), paragraph("second")]
    )

    for chunk in chunks:
        assert chunk.chunk_id == derive_chunk_id(envelope.doc_id, chunk.position)
        assert chunk.doc_id == envelope.doc_id


def test_chunk_deterministic_same_elements_twice_produces_identical_chunk_ids_and_order() -> (
    None
):
    chunker, ledger = _build_chunker()
    envelope = make_envelope("deterministic")
    _seed_chunking(ledger, envelope.doc_id)
    elements = [heading("1. Scope"), paragraph("Some content."), table([["a", "b"]])]

    first = chunker.chunk(envelope, elements)
    second = chunker.chunk(envelope, elements)

    assert first == second


# --- mixed document (AC1, story) ------------------------------------------------------------


def test_mixed_document_headings_paragraphs_table_list_produces_coherent_chunk_stream() -> (
    None
):
    chunker, ledger = _build_chunker()
    envelope = make_envelope("mixed-document")
    _seed_chunking(ledger, envelope.doc_id)
    elements = [
        heading("1. Scope"),
        paragraph("Prose under Scope."),
        paragraph("More prose under Scope."),
        table([["Name", "Age"], ["Alice", "30"]]),
        heading("2. Details"),
        list_item("An item under Details."),
    ]

    chunks = chunker.chunk(envelope, elements)

    table_chunks = [
        c for c in chunks if c.metadata["strategy"] == STRATEGY_TABLE_ATOMIC
    ]
    prose_chunks = [
        c for c in chunks if c.metadata["strategy"] == STRATEGY_STRUCTURE_AWARE
    ]
    assert len(table_chunks) == 1
    assert "Alice" in table_chunks[0].text
    assert len(prose_chunks) >= 2
    for chunk in prose_chunks:
        assert chunk.token_count <= ChunkerConfig().max_chunk_tokens
    for chunk in chunks:
        assert chunk.chunk_id == derive_chunk_id(envelope.doc_id, chunk.position)


# --- Assumption A2 caption adjacency, exercised via the orchestrator's own heuristic --------


def test_chunk_table_preceded_by_image_caption_prepends_it_automatically() -> None:
    chunker, ledger = _build_chunker()
    envelope = make_envelope("table-with-image-caption")
    _seed_chunking(ledger, envelope.doc_id)
    elements = [image_caption("Figure 1: Sample data"), table([["a", "b"]])]

    chunks = chunker.chunk(envelope, elements)

    table_chunks = [
        c for c in chunks if c.metadata["strategy"] == STRATEGY_TABLE_ATOMIC
    ]
    assert len(table_chunks) == 1
    assert table_chunks[0].text.startswith("Figure 1: Sample data")


def test_chunk_table_preceded_by_heading_has_no_automatic_caption() -> None:
    chunker, ledger = _build_chunker()
    envelope = make_envelope("table-preceded-by-heading")
    _seed_chunking(ledger, envelope.doc_id)
    elements = [heading("1. Data"), table([["a", "b"]])]

    chunks = chunker.chunk(envelope, elements)

    table_chunks = [
        c for c in chunks if c.metadata["strategy"] == STRATEGY_TABLE_ATOMIC
    ]
    assert len(table_chunks) == 1
    assert not table_chunks[0].text.startswith("1. Data")


# --- F1: empty elements (AC5, story; design-review MAJOR fix) ------------------------------


def test_chunk_empty_elements_raises_chunker_empty_elements_permanent_ledger_ends_failed() -> (
    None
):
    chunker, ledger = _build_chunker()
    envelope = make_envelope("empty-elements")
    _seed_chunking(ledger, envelope.doc_id)

    with pytest.raises(ChunkerError) as exc_info:
        chunker.chunk(envelope, [])

    assert exc_info.value.error_code == CHUNKER_EMPTY_ELEMENTS
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.FAILED
    assert row.last_error_code == CHUNKER_EMPTY_ELEMENTS


def test_chunk_empty_elements_terminalize_benign_concurrent_loss_skips_ledger_write_but_still_raises() -> (
    None
):
    chunker, ledger = _build_chunker()
    envelope = make_envelope("empty-elements-benign")
    _advance_to(ledger, envelope.doc_id, Status.INDEXED)
    before = ledger.get(envelope.doc_id)

    with pytest.raises(ChunkerError) as exc_info:
        chunker.chunk(envelope, [])

    assert exc_info.value.error_code == CHUNKER_EMPTY_ELEMENTS
    after = ledger.get(envelope.doc_id)
    assert after == before
    assert after is not None
    assert after.status == Status.INDEXED


def test_chunk_empty_elements_terminalize_non_benign_reraises_ledger_transition_invalid_not_chunker_error() -> (
    None
):
    chunker, _ledger = _build_chunker()
    envelope = make_envelope("empty-elements-unknown-doc-id")
    # deliberately no ledger row created for this doc_id

    with pytest.raises(LedgerTransitionInvalid) as exc_info:
        chunker.chunk(envelope, [])

    assert exc_info.value.observed_row is None


def test_chunk_empty_elements_non_benign_already_failed_reraises_ledger_transition_invalid() -> (
    None
):
    chunker, ledger = _build_chunker()
    envelope = make_envelope("empty-elements-already-failed")
    ledger.create(envelope.doc_id, EnvelopeSummary(config_version="v1"))
    ledger.transition(
        envelope.doc_id, Status.FAILED, Status.FAILED, error_code="SOME_PRIOR_FAILURE"
    )

    with pytest.raises(LedgerTransitionInvalid) as exc_info:
        chunker.chunk(envelope, [])

    assert exc_info.value.observed_row is not None
    assert exc_info.value.observed_row.status == Status.FAILED


# --- F2: TOKENIZER_LOAD_FAILED (TRANSIENT) --------------------------------------------------


def test_chunk_tokenizer_load_failed_self_transitions_chunking_transient() -> None:
    chunker, ledger = _build_chunker(token_counter=_FailingTokenCounter())
    envelope = make_envelope("tokenizer-load-failed")
    _seed_chunking(ledger, envelope.doc_id)

    with pytest.raises(ChunkerError) as exc_info:
        chunker.chunk(envelope, [paragraph("hello world " * 20)])

    assert exc_info.value.error_code == TOKENIZER_LOAD_FAILED
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.CHUNKING
    assert row.last_error_code == TOKENIZER_LOAD_FAILED


# --- F3: CHUNKER_INTERNAL (TRANSIENT) -------------------------------------------------------


def test_chunk_unmapped_strategy_exception_wrapped_as_chunker_internal_self_transitions_chunking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    chunker, ledger = _build_chunker(
        recursive_strategy=_RaisingStrategy(RuntimeError("splitter exploded"))
    )
    envelope = make_envelope("chunker-internal")
    _seed_chunking(ledger, envelope.doc_id)

    with caplog.at_level("ERROR", logger="vestibule.chunker.chunker"):
        with pytest.raises(ChunkerError) as exc_info:
            chunker.chunk(envelope, [paragraph("hello")])

    assert exc_info.value.error_code == CHUNKER_INTERNAL
    assert "splitter exploded" in caplog.text
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.CHUNKING
    assert row.last_error_code == CHUNKER_INTERNAL


def test_chunk_unexpected_chunker_error_from_strategy_wrapped_as_chunker_internal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A strategy raising a ChunkerError whose own error_code isn't TOKENIZER_LOAD_FAILED
    (e.g. a misbehaving custom strategy) is still wrapped as CHUNKER_INTERNAL, not
    propagated with its original (unexpected-here) error_code."""
    chunker, ledger = _build_chunker(
        recursive_strategy=_RaisingStrategy(
            ChunkerError(
                "wrong-doc-id", "custom failure", error_code=CHUNKER_EMPTY_ELEMENTS
            )
        )
    )
    envelope = make_envelope("unexpected-chunker-error")
    _seed_chunking(ledger, envelope.doc_id)

    with caplog.at_level("ERROR", logger="vestibule.chunker.chunker"):
        with pytest.raises(ChunkerError) as exc_info:
            chunker.chunk(envelope, [paragraph("hello")])

    assert exc_info.value.error_code == CHUNKER_INTERNAL
    assert "custom failure" in caplog.text
    row = ledger.get(envelope.doc_id)
    assert row is not None
    assert row.status == Status.CHUNKING
    assert row.last_error_code == CHUNKER_INTERNAL


def test_chunk_transient_failure_then_redelivery_increments_attempts_without_reaching_failed() -> (
    None
):
    chunker, ledger = _build_chunker(
        recursive_strategy=_RaisingStrategy(RuntimeError("boom"))
    )
    envelope = make_envelope("transient-redelivery")
    _seed_chunking(ledger, envelope.doc_id)

    with pytest.raises(ChunkerError):
        chunker.chunk(envelope, [paragraph("hello")])
    row1 = ledger.get(envelope.doc_id)
    assert row1 is not None
    assert row1.status == Status.CHUNKING

    with pytest.raises(ChunkerError):
        chunker.chunk(envelope, [paragraph("hello")])
    row2 = ledger.get(envelope.doc_id)
    assert row2 is not None
    assert row2.status == Status.CHUNKING
    assert row2.attempts > row1.attempts


# --- F5: exit-transition benign/non-benign redelivery ---------------------------------------


def test_chunk_redelivered_after_prior_success_is_benign_noop() -> None:
    chunker, ledger = _build_chunker()
    envelope = make_envelope("redelivered-past-embedding")
    _advance_to(ledger, envelope.doc_id, Status.INDEXING)
    before = ledger.get(envelope.doc_id)

    chunks = chunker.chunk(envelope, [paragraph("hello world")])

    assert len(chunks) == 1
    after = ledger.get(envelope.doc_id)
    assert after == before


def test_chunk_ledger_transition_invalid_non_benign_propagates_unchanged() -> None:
    chunker, _ledger = _build_chunker()
    envelope = make_envelope("unknown-doc-id-exit")
    # deliberately no ledger row created for this doc_id

    with pytest.raises(LedgerTransitionInvalid) as exc_info:
        chunker.chunk(envelope, [paragraph("hello world")])

    assert exc_info.value.observed_row is None


# --- min-chunk merge pass (Assumption A8) ----------------------------------------------------


def test_min_chunk_merge_pass_absorbs_undersized_trailing_chunk() -> None:
    fake_drafts = [
        ChunkDraft(
            element_types=[ElementType.PARAGRAPH],
            text="A reasonably sized leading chunk of text. " * 10,
            token_count=100,
            metadata={"strategy": STRATEGY_RECURSIVE, "section_path": None},
        ),
        ChunkDraft(
            element_types=[ElementType.PARAGRAPH],
            text="tiny trailing remainder",
            token_count=1,
            metadata={"strategy": STRATEGY_RECURSIVE, "section_path": None},
        ),
    ]
    chunker, ledger = _build_chunker(
        config=ChunkerConfig(min_chunk_tokens=40),
        recursive_strategy=_FixedDraftStrategy(fake_drafts),
    )
    envelope = make_envelope("min-chunk-merge")
    _seed_chunking(ledger, envelope.doc_id)

    chunks = chunker.chunk(
        envelope, [paragraph("source text, ignored by the fake strategy")]
    )

    assert len(chunks) == 1
    assert "tiny trailing remainder" in chunks[0].text
    assert "A reasonably sized leading chunk" in chunks[0].text


def test_min_chunk_merge_pass_leaves_sole_undersized_chunk_unmerged() -> None:
    fake_drafts = [
        ChunkDraft(
            element_types=[ElementType.PARAGRAPH],
            text="tiny",
            token_count=1,
            metadata={"strategy": STRATEGY_RECURSIVE, "section_path": None},
        )
    ]
    chunker, ledger = _build_chunker(
        recursive_strategy=_FixedDraftStrategy(fake_drafts)
    )
    envelope = make_envelope("sole-small-chunk")
    _seed_chunking(ledger, envelope.doc_id)

    chunks = chunker.chunk(envelope, [paragraph("x")])

    assert len(chunks) == 1
    assert chunks[0].text == "tiny"


def test_merge_undersized_tail_never_merges_a_table_atomic_draft() -> None:
    counter = TiktokenCounter("cl100k_base")
    table_draft = ChunkDraft(
        element_types=[ElementType.TABLE],
        text="| a |\n| --- |\n| b |",
        token_count=1,
        metadata={"strategy": STRATEGY_TABLE_ATOMIC, "section_path": None},
    )
    prose_draft = ChunkDraft(
        element_types=[ElementType.PARAGRAPH],
        text="a normal chunk",
        token_count=100,
        metadata={"strategy": STRATEGY_RECURSIVE, "section_path": None},
    )

    result = chunker_module._merge_undersized_tail(
        [prose_draft, table_draft], ChunkerConfig(min_chunk_tokens=40), counter
    )

    assert result == [prose_draft, table_draft]  # table draft (tail) is never merged


# --- strategy pluggability (Assumption A6, market-awareness note) --------------------------


def test_chunker_swaps_in_fake_strategy_with_no_orchestrator_code_changes() -> None:
    fake_draft = ChunkDraft(
        element_types=[ElementType.PARAGRAPH],
        text="FAKE STRATEGY OUTPUT",
        token_count=3,
        metadata={"strategy": "fake", "section_path": None},
    )
    chunker, ledger = _build_chunker(
        recursive_strategy=_FixedDraftStrategy([fake_draft])
    )
    envelope = make_envelope("fake-strategy-swap")
    _seed_chunking(ledger, envelope.doc_id)

    chunks = chunker.chunk(
        envelope, [paragraph("ignored by the fake strategy"), table([["a", "b"]])]
    )

    prose_chunks = [c for c in chunks if c.metadata["strategy"] == "fake"]
    table_chunks = [
        c for c in chunks if c.metadata["strategy"] == STRATEGY_TABLE_ATOMIC
    ]
    assert len(prose_chunks) == 1
    assert prose_chunks[0].text == "FAKE STRATEGY OUTPUT"
    assert len(table_chunks) == 1  # default TableAtomicChunkStrategy still functions


# --- config/code sync (§6) ------------------------------------------------------------------


def test_config_chunker_yaml_tunables_match_chunker_config_defaults() -> None:
    config_path = Path(__file__).resolve().parents[3] / "config" / "chunker.yaml"
    text = config_path.read_text(encoding="utf-8")
    defaults = ChunkerConfig()

    def _int_value(key: str) -> int:
        match = re.search(rf"{key}:\s*(\d+)", text)
        assert match is not None
        return int(match.group(1))

    def _str_value(key: str) -> str:
        match = re.search(rf"{key}:\s*(\S+)", text)
        assert match is not None
        return match.group(1)

    assert _int_value("target_chunk_tokens") == defaults.target_chunk_tokens
    assert _int_value("overlap_tokens") == defaults.overlap_tokens
    assert _int_value("min_chunk_tokens") == defaults.min_chunk_tokens
    assert _int_value("max_chunk_tokens") == defaults.max_chunk_tokens
    assert _str_value("token_counter") == defaults.token_counter


# --- property-based (hypothesis, story) ------------------------------------------------------


_TEXT_ALPHABET = string.ascii_letters + string.digits + " "
_ELEMENT_TEXT = st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=200).map(
    lambda s: s.strip() or "x"
)


@st.composite
def _wellformed_element(draw: st.DrawFn) -> Element:
    kind = draw(st.sampled_from(["heading", "paragraph", "list", "code", "table"]))
    text = draw(_ELEMENT_TEXT)
    if kind == "heading":
        return heading(text)
    if kind == "paragraph":
        return paragraph(text)
    if kind == "list":
        return list_item(text)
    if kind == "code":
        return code_block(text)
    return table([[text, text], [text, text]])


_element_sequences = st.lists(_wellformed_element(), min_size=1, max_size=20)


def _envelope_for_hypothesis_example(counter: list[int]) -> ArrivalEnvelope:
    counter[0] += 1
    return make_envelope(f"property-{counter[0]}")


_example_counter = [0]


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(elements=_element_sequences)
def test_property_random_wellformed_element_sequence_produces_valid_chunk_stream(
    elements: list[Element],
) -> None:
    """No non-table chunk ever exceeds `max_chunk_tokens`.

    This is the one invariant `Chunker.chunk` guarantees unconditionally for *every*
    non-table chunk, regardless of document shape: `RecursiveChunkStrategy` never
    returns a piece bigger than `target_chunk_tokens` (`< max_chunk_tokens` by
    configuration convention), and the min-chunk merge pass (LLD Assumption A8) only
    ever *grows* a chunk by absorbing an undersized trailing neighbor, capped by the
    same target-sized predecessor. `TableAtomicChunkStrategy`-tagged chunks are
    deliberately exempt (LLD Assumption A3 — the story's own documented exception).

    The min_chunk_tokens side of this property ("no undersized chunk except possibly
    the document's last") deliberately is *not* asserted here for every chunk: the
    merge pass is explicitly documented (Assumption A8) as a *tail-only, per-region*
    pass, not a whole-document or per-section one — `StructureAwareChunkStrategy` can
    legitimately leave a small *leading* section (content before the first `HEADING`)
    unmerged if later sections in the same region are not themselves undersized (see
    `test_structure_aware_leading_section_before_heading_can_remain_undersized`, a
    deterministic characterization test of this exact, intentional algorithm shape).
    """
    config = ChunkerConfig()
    chunker, ledger = _build_chunker(config=config)
    envelope = _envelope_for_hypothesis_example(_example_counter)
    _seed_chunking(ledger, envelope.doc_id)

    chunks = chunker.chunk(envelope, elements)

    # chunks may legitimately be empty (e.g. an elements list of bare HEADINGs with no
    # following content contributes no section at all, LLD Assumption A1) — this
    # property only constrains whatever chunks, if any, are produced.
    for chunk in chunks:
        if chunk.metadata["strategy"] == STRATEGY_TABLE_ATOMIC:
            continue
        assert chunk.token_count <= config.max_chunk_tokens


def test_structure_aware_leading_section_before_heading_can_remain_undersized() -> None:
    """Characterizes a real, intentional consequence of Assumption A8's tail-only,
    per-region merge pass: a small leading section (content before a document's first
    HEADING) is never merged forward into a later section's chunks, because the merge
    pass only ever inspects/merges a region's own *trailing* draft, repeatedly — it
    never looks at, or fixes the size of, any interior draft. Both sections here belong
    to the same single NON_TABLE region (no TABLE element anywhere), so there is no
    region boundary to appeal to either; this is squarely inside Assumption A8's stated
    scope, not a defect."""
    chunker, ledger = _build_chunker(config=ChunkerConfig(min_chunk_tokens=40))
    envelope = make_envelope("leading-section-stays-small")
    _seed_chunking(ledger, envelope.doc_id)
    elements = [
        paragraph("tiny leading content"),
        heading("1. Scope"),
        paragraph(("A normal-sized paragraph under Scope. " * 20)),
    ]

    chunks = chunker.chunk(envelope, elements)

    assert chunks[0].metadata["section_path"] is None
    assert chunks[0].token_count < 40
    assert len(chunks) > 1  # the small leading chunk was NOT merged into what follows
