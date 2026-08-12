# REQ-006 — Chunker — LLD

**Story:** docs/stories/REQ-006.md · **Phase:** 2 · **Pipeline:** full

## Revision note

Revised after a design-review REJECT — five findings, all addressed in this revision:

1. **[BLOCKER]** `CHUNKER_OVERSIZED_TABLE` was registered in `ErrorRegistry` with a nominal
   `Severity.TRANSIENT`, even though it is never raised — a real trap (any future code attaching
   this code to a raised exception would silently inherit retry/self-transition behavior instead
   of the intended log-and-continue). **Fixed:** removed from the registry entirely; it is now a
   plain structured-log event name only. Three registered codes, not four. §1, §3 (F4), §4, §5,
   §6, §7 updated accordingly.
2. **[MAJOR]** `config/errors.yaml`'s `known_codes` block and `ingestion/errors/test_registry.py`'s
   hand-import list (`test_config_known_codes_matches_all_codes_after_importing_all_modules`) were
   unaddressed. **Fixed:** added §6's new subsection specifying both, as an explicit same-PR
   deliverable.
3. **[MAJOR]** F1 (`CHUNKER_EMPTY_ELEMENTS`'s terminalize path) performed its `chunking → failed`
   ledger write without `is_benign_concurrent_loss` triage, unlike every other ledger write in
   this design. **Fixed:** F1 now applies the same triage F5 already uses. This is a fresh design,
   not obligated to reproduce the equivalent un-triaged gap tracked separately against REQ-005's
   `Analyzer._terminalize` (issue #9). §3 (F1), §4, Assumption A5 updated accordingly.
4. **[MINOR]** Added a documentation-only note (Assumption A1) that a follow-up ticket (issue #10)
   has been filed against the Analyzer to add heading depth to `Element`, enabling a hierarchical
   `section_path` in a future REQ. The flat `section_path` design itself is unchanged.
5. **[MINOR]** Pinned `tiktoken`/`langchain-text-splitters` version constraints in §6.

## Assumptions (non-blocking, flagged per house rules — same pattern as REQ-004/REQ-005)

The story specifies the `Chunker` interface, the `Chunk` model, three strategies, and four
error codes fully enough to design against, but several details depend on upstream data
(`ingestion/analyzer/model.py`'s `Element`, REQ-005) that does not exist yet, or on a
non-negotiable house rule the story's own prose brushes up against. Following REQ-004/REQ-005
precedent, these are scoped design decisions, documented here, not open questions that block
this LLD:

- **A1 — `Element` carries no heading-level/depth signal.** Read directly from
  `ingestion/analyzer/parsers/pymupdf_parser.py` and `docint_parser.py` (REQ-005): a `HEADING`
  element's `metadata` is `{"page": int, "bbox": ...}` (PyMuPDF) or `{"role": ParagraphRole}`
  (Document Intelligence) — neither carries a numeric level/depth. The story's illustrative
  `section_path: "1. Scope > 1.2 Definitions"` implies multi-level nesting, which cannot be
  built from data that does not exist without fabricating it. This LLD's `StructureAwareChunkStrategy`
  therefore builds a **flat, single-active-heading** `section_path` — the text of the most
  recently seen `HEADING` element, replaced (not pushed/popped) on every subsequent `HEADING` —
  and reads `element.metadata.get("level")` opportunistically: if a future Analyzer revision
  starts populating it, true nesting can be added without changing this module's public API;
  until then, behavior degrades gracefully to one level. Flagged as a genuine upstream data gap,
  not an algorithm choice made here. A follow-up ticket (**issue #10**) has been filed against the
  Analyzer to add heading depth to the `Element` model, enabling a hierarchical `section_path` in
  a future REQ — this is a documentation note only; the flat design here is unchanged by it.
- **A2 — table-caption adjacency is a heuristic, not a data field.** `ElementType.IMAGE_CAPTION`
  exists in REQ-005's enum but neither shipped parser ever constructs one, and no field links a
  caption to a table. `TableAtomicChunkStrategy` treats the element immediately preceding a
  `TABLE` element in document order as its caption only if that element's `type` is `PARAGRAPH`
  or `IMAGE_CAPTION` **and** no `HEADING`/`TABLE` intervenes; otherwise no caption is prepended.
  Documented heuristic, not a guess at a field that does not exist.
- **A3 — `CHUNKER_OVERSIZED_TABLE` is not registered in `ErrorRegistry` at all.** The story lists
  it as "WARNING only" alongside three PERMANENT/TRANSIENT codes, but Contract #4's `Severity`
  enum (`ingestion/errors/registry.py`) is strictly binary — registering it under either value
  would misclassify a log-only advisory as a taxonomy member, a real trap: any future code that
  later attaches this string to a raised exception would silently inherit retry/self-transition
  behavior instead of the intended log-and-continue (design-review BLOCKER, fixed). Per review,
  `CHUNKER_OVERSIZED_TABLE` is a plain string constant used only as a structured-log event name
  (`logger.warning("oversized_table", extra={...})`, §3 F4) — never passed to `register_error`,
  never attached to any raised exception, and outside AC6's `all_codes()` requirement, which now
  applies only to the three codes this module actually registers: `CHUNKER_EMPTY_ELEMENTS`,
  `TOKENIZER_LOAD_FAILED`, `CHUNKER_INTERNAL`.
- **A4 — house rules' "never... implement chunking algorithms... in-house — always via adapter
  interfaces" governs the Recursive strategy's actual splitting logic.** `RecursiveChunkStrategy`
  is a thin wrap of `langchain_text_splitters.RecursiveCharacterTextSplitter` (new dependency;
  the story's own References section names this exact class as "reference implementation") —
  configured with `length_function=token_counter.count` so token accounting stays consistent
  with the rest of this module. `StructureAwareChunkStrategy` composes `RecursiveChunkStrategy`
  internally for its documented "falls back to recursive splitting within the section" behavior
  rather than reimplementing splitting — it owns only heading-boundary bookkeeping, no
  token-boundary algorithm of its own. `TableAtomicChunkStrategy` performs no splitting at all
  (one chunk per table by construction); its only logic is markdown serialization, analogous to
  REQ-005's own precedent of leaving "how a chunker should flatten this into text" as this
  module's job (`docint_parser.py`'s `_table_element` docstring).
- **A5 — the ledger-write pattern mirrors REQ-005's `Analyzer`, with one deliberate improvement.**
  Terminalize-to-`failed` only on the one PERMANENT path; self-transition `chunking → chunking`
  on every TRANSIENT path; triage the exit transition (`chunking → embedding`, F5) via
  `is_benign_concurrent_loss` (REQ-003) rather than inventing new idempotency logic. Unlike
  `Analyzer`, `Chunker` performs no *entry* transition — the row is already `chunking` on entry
  (written by `Analyzer`'s own `analyzing → chunking` transition, REQ-005 §3 step 6) — so there
  is exactly one ledger write on the happy path (the exit), not two. Unlike REQ-005's
  `Analyzer._terminalize` (which performs its `analyzing → failed` write without
  `is_benign_concurrent_loss` triage — a known gap tracked separately as **issue #9**), this
  module's own terminalize path (F1, design-review MAJOR fix) *does* apply the same triage every
  other ledger write here uses: this is a fresh design, not obligated to reproduce an existing gap
  elsewhere in the codebase.
- **A6 — strategy pluggability (Market-awareness note) is constructor injection, not a
  `ParserRegistry`-style type-keyed lookup.** Dispatch here is deterministic business logic keyed
  on element-type *composition* across the whole document (the story's own "Element with
  type=TABLE → table-atomic..." rules), not a single-key lookup like `DetectedType →
  ParserAdapter`. A future `SemanticChunker`/`ParentChildChunker`/`LLMBoundaryChunker` is wired in
  by constructing `Chunker` with a different `ChunkStrategy` for one of the three slots — no
  change to `Chunker.chunk`'s public signature.
- **A7 — `chunk_index` (for `derive_chunk_id`) equals the chunk's final `position`.** Assigned
  exactly once, after every region has been dispatched, interleaved back into document order, and
  the min-chunk merge pass (A8) has run — the single point of ID assignment, so `chunk()` is
  deterministic (AC4) regardless of how many internal regions/strategies contributed.
- **A8 — the `min_chunk_tokens` merge pass runs once per region**, immediately after that
  region's `ChunkStrategy.chunk_region()` call returns (not on the fully-interleaved
  whole-document list) — implemented once in the `Chunker` orchestrator, not duplicated per
  strategy (keeping strategies thin, per A4). A `table_atomic` draft is never a merge source or
  target, in either direction, preserving AC2's "exactly one chunk" invariant unconditionally.
- **A9 — tokenizer resource loading is lazy, inside `chunk()`, not at composition-root
  construction time.** `TOKENIZER_LOAD_FAILED`'s declared self-transition/TRANSIENT behavior
  requires a `doc_id`/ledger-row context to record against; a `tiktoken` encoding failure at
  process startup (before any `doc_id` exists) is a deployment/composition-root concern, out of
  scope here. `TiktokenCounter` loads its encoding on first `.count()` call and caches it
  thereafter for the process lifetime.

## 1. Interfaces

```python
# ingestion/chunker/model.py

CHUNKER_EMPTY_ELEMENTS = "CHUNKER_EMPTY_ELEMENTS"
TOKENIZER_LOAD_FAILED = "TOKENIZER_LOAD_FAILED"
CHUNKER_INTERNAL = "CHUNKER_INTERNAL"

# Structured-log event name only — NOT an ErrorRegistry code (Assumption A3, design-review
# BLOCKER fix). Never passed to register_error; never attached to any raised exception.
OVERSIZED_TABLE_LOG_EVENT = "oversized_table"

STRATEGY_RECURSIVE = "recursive"
STRATEGY_STRUCTURE_AWARE = "structure_aware"
STRATEGY_TABLE_ATOMIC = "table_atomic"


class Chunk(BaseModel):
    """Retrieval-ready unit — Contract #1's arrival boundary has no further bearing here;
    this is a pure downstream-of-Analyzer value object. Immutable (frozen)."""

    model_config = ConfigDict(frozen=True)

    chunk_id: str            # derive_chunk_id(doc_id, position) — REQ-002
    doc_id: str
    position: int             # ordinal in the document; == chunk_index (Assumption A7)
    element_types: list[ElementType]
    text: str
    token_count: int
    metadata: dict[str, Any]  # strategy, section_path (str | None), page/pages, table flags


@dataclass(frozen=True)
class ChunkDraft:
    """Pre-identity chunk content produced by a ChunkStrategy, before Chunker assigns the
    final document-order position/chunk_id (Assumption A7). Never exposed to callers of
    Chunker.chunk() — internal to this module only."""

    element_types: list[ElementType]
    text: str
    token_count: int
    metadata: dict[str, Any]


class ChunkerError(RaggedError):
    """Single exception type for every Chunker-raised failure; error_code varies per raise
    site (same shape as REQ-005's AnalyzerError, Assumption A3 there)."""

    def __init__(self, doc_id: str, reason: str, *, error_code: str) -> None:
        self.doc_id = doc_id
        self.reason = reason
        super().__init__(f"{doc_id}: {reason}", error_code=error_code)


# Registered at import time — Contract #4 (see §5 for full table). Three codes, not four
# (CHUNKER_OVERSIZED_TABLE is deliberately absent — Assumption A3).
register_error(CHUNKER_EMPTY_ELEMENTS, Severity.PERMANENT, "...")
register_error(TOKENIZER_LOAD_FAILED, Severity.TRANSIENT, "...")
register_error(CHUNKER_INTERNAL, Severity.TRANSIENT, "...")
```

```python
# ingestion/chunker/token_counter.py

class TokenCounter(Protocol):
    """Minimal tokenizer surface (Assumption A9). Thin adapter over an external
    tokenizer library — never a hand-rolled BPE/token-counting algorithm (house rules)."""
    def count(self, text: str) -> int: ...


class TiktokenCounter:
    """Thin wrap of `tiktoken`. Lazily loads its encoding on first `count()` call."""

    def __init__(self, encoding_name: str = "cl100k_base") -> None: ...
    def count(self, text: str) -> int:
        """Raises ChunkerError(TOKENIZER_LOAD_FAILED) if the encoding cannot be loaded
        on first use (e.g. `tiktoken.get_encoding` raises); cached thereafter."""
        ...


def build_token_counter(name: str) -> TokenCounter:
    """Factory: config's `token_counter: tiktoken_cl100k` -> TiktokenCounter("cl100k_base").
    Raises ChunkerError(CHUNKER_INTERNAL) for an unrecognized `name`."""
    ...
```

```python
# ingestion/chunker/strategies/base.py

class ChunkStrategy(ABC):
    """Thin strategy contract (house rules: 'adapters thin') — a strategy composes an
    external splitter/tokenizer via TokenCounter/RecursiveChunkStrategy, never hand-rolls
    a splitting algorithm of its own (Assumption A4)."""

    @abstractmethod
    def chunk_region(
        self,
        elements: Sequence[Element],
        *,
        config: ChunkerConfig,
        token_counter: TokenCounter,
    ) -> list[ChunkDraft]:
        """elements is one contiguous, same-kind run from the orchestrator's partitioning
        (§3 step 3) — never the whole document. Never raises AnalyzerError-shaped
        exceptions directly for element-count issues (Chunker owns CHUNKER_EMPTY_ELEMENTS);
        may raise ChunkerError(TOKENIZER_LOAD_FAILED) via token_counter, or let any other
        exception propagate for Chunker to wrap as CHUNKER_INTERNAL."""
        raise NotImplementedError
```

```python
# ingestion/chunker/strategies/recursive.py

class RecursiveChunkStrategy(ChunkStrategy):
    """PARAGRAPH/LIST/CODE elements. Thin wrap of
    langchain_text_splitters.RecursiveCharacterTextSplitter (Assumption A4), configured with
    length_function=token_counter.count, chunk_size=config.target_chunk_tokens,
    chunk_overlap=config.overlap_tokens. Applies the min_chunk_tokens trailing-merge pass
    itself is NOT this class's job — Chunker's orchestrator does it once per region
    (Assumption A8) so this class stays a thin, mergeless splitter wrapper."""

    def chunk_region(
        self, elements: Sequence[Element], *, config: ChunkerConfig, token_counter: TokenCounter
    ) -> list[ChunkDraft]: ...
```

```python
# ingestion/chunker/strategies/structure_aware.py

class StructureAwareChunkStrategy(ChunkStrategy):
    """Elements containing HEADING boundaries. Owns only heading-boundary bookkeeping and
    flat section_path construction (Assumption A1); delegates any oversized section's
    splitting to an injected RecursiveChunkStrategy (Assumption A4) — no token-boundary
    algorithm of its own."""

    def __init__(self, recursive: RecursiveChunkStrategy) -> None: ...

    def chunk_region(
        self, elements: Sequence[Element], *, config: ChunkerConfig, token_counter: TokenCounter
    ) -> list[ChunkDraft]: ...
```

```python
# ingestion/chunker/strategies/table_atomic.py

class TableAtomicChunkStrategy(ChunkStrategy):
    """Exactly one TABLE element per call (orchestrator invariant, §3 step 3). Serializes
    metadata["cells"] (REQ-005's docint_parser shape: row_index/column_index/content) to a
    markdown table; falls back to element.text (a plain reading-order join) if "cells" is
    absent (e.g. a future non-Document-Intelligence table source). Never splits — always
    returns exactly one ChunkDraft, regardless of token_count (AC2). When the resulting
    token_count exceeds config.max_chunk_tokens, calls logger.warning("oversized_table",
    extra={"doc_id": ..., "table_tokens": ..., "max_tokens": ...}) and continues — no
    exception, no error code (Assumption A3, design-review BLOCKER fix)."""

    def chunk_region(
        self,
        elements: Sequence[Element],
        *,
        config: ChunkerConfig,
        token_counter: TokenCounter,
        caption_text: str | None = None,
    ) -> list[ChunkDraft]:
        """elements is always [table_element]. caption_text is supplied by the orchestrator
        per the Assumption A2 adjacency heuristic, not derived here."""
        ...
```

```python
# ingestion/chunker/chunker.py

@dataclass(frozen=True)
class ChunkerConfig:
    """Documentation-authoritative-but-code-defaults tunables (REQ-005 §6 Assumption A6
    precedent — no config-loading mechanism exists anywhere in this codebase yet)."""
    target_chunk_tokens: int = 512
    overlap_tokens: int = 64
    min_chunk_tokens: int = 40
    max_chunk_tokens: int = 800
    token_counter: str = "tiktoken_cl100k"


class Chunker:
    """Orchestrates region partitioning, per-region strategy dispatch, the min-chunk merge
    pass, chunk_id/position assignment, and the `chunking` ledger stage transitions. Owns
    no splitting algorithm itself (Assumption A4)."""

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
        """Defaults to the three built-in strategies (Recursive/StructureAware/TableAtomic)
        when the corresponding kwarg is None; injectable per Assumption A6."""
        ...

    def chunk(self, envelope: ArrivalEnvelope, elements: list[Element]) -> list[Chunk]:
        """See §3 for the full numbered sequence. Reads only envelope.doc_id (Contract #1
        touchpoint) — no other envelope field is inspected."""
        ...
```

## 2. Data model

| Type | Field | Type | Notes |
|---|---|---|---|
| `Chunk` (frozen pydantic `BaseModel`) | `chunk_id` | `str` | `derive_chunk_id(doc_id, position)` (REQ-002) |
| | `doc_id` | `str` | unchanged from envelope/ledger key |
| | `position` | `int` | ordinal in document == `chunk_index` (A7) |
| | `element_types` | `list[ElementType]` | every source `Element.type` contributing to this chunk (usually one; >1 after a merge, A8) |
| | `text` | `str` | final chunk text (markdown for tables) |
| | `token_count` | `int` | via `TokenCounter.count(text)`, recomputed after any merge |
| | `metadata` | `dict[str, Any]` | `strategy: str` (A6's market-awareness field), `section_path: str \| None`, `page`/`pages`, `oversized: bool` (tables only) |
| `ChunkDraft` (frozen dataclass, internal) | `element_types` | `list[ElementType]` | pre-position/pre-id |
| | `text` | `str` | |
| | `token_count` | `int` | |
| | `metadata` | `dict[str, Any]` | same keys as `Chunk.metadata` minus `chunk_id`/`position`/`doc_id` |
| `ChunkerConfig` (frozen dataclass) | `target_chunk_tokens` | `int` | default `512` |
| | `overlap_tokens` | `int` | default `64` |
| | `min_chunk_tokens` | `int` | default `40` |
| | `max_chunk_tokens` | `int` | default `800` |
| | `token_counter` | `str` | default `"tiktoken_cl100k"` — a factory key, resolved via `build_token_counter` |
| `ChunkerError` | `doc_id` | `str` | |
| | `reason` | `str` | |
| | `error_code` | `str` | inherited from `RaggedError`; one of three registered codes |

No new ledger/table row is owned by this module — `Chunker` reads/writes existing `LedgerRow`
rows (REQ-003) via `LedgerStore`, keyed on `envelope.doc_id`, exactly as `Analyzer` does (REQ-005).

## 3. Sequence

**Happy path**
1. Caller (out of scope — a coordinator/worker) has already run `Analyzer.analyze` (REQ-005),
   which left the `LedgerRow` for `envelope.doc_id` in `Status.CHUNKING` (its own step-6
   transition), and holds the returned `list[Element]`.
2. `Chunker.chunk(envelope, elements)`: if `elements` is empty, terminalize (F1, below).
3. Partition `elements` into ordered, contiguous **regions** in a single left-to-right pass:
   each `TABLE` element is its own single-element region (`kind=TABLE`); every maximal run of
   non-`TABLE` elements between/around tables is one region (`kind=NON_TABLE`). Document order
   is preserved by construction — regions are never reordered.
4. Compute `has_headings = any(e.type is ElementType.HEADING for e in elements)` once, globally.
5. For each region, in order:
   - `kind=TABLE` → look up the immediately preceding element in the *original* element stream
     for the Assumption A2 caption heuristic; call `table_atomic_strategy.chunk_region([table],
     config=config, token_counter=token_counter, caption_text=caption_or_none)`. Always returns
     exactly one `ChunkDraft` (possibly logging an oversized-table advisory, F4 below — never
     raises for this reason).
   - `kind=NON_TABLE`, `has_headings` → `structure_aware_strategy.chunk_region(region, ...)`.
   - `kind=NON_TABLE`, not `has_headings` → `recursive_strategy.chunk_region(region, ...)`.
   Each call may raise `ChunkerError(TOKENIZER_LOAD_FAILED)` (F2) or an unmapped exception (F3).
6. Immediately after each region's strategy call returns, apply the min-chunk merge pass
   (Assumption A8) to that region's own `list[ChunkDraft]` only: while the region's last draft's
   `token_count < config.min_chunk_tokens`, `len(region_drafts) > 1`, and neither the last draft
   nor its predecessor has `metadata["strategy"] == STRATEGY_TABLE_ATOMIC`, merge the last draft
   into its predecessor (concatenate text, union `element_types`, recompute `token_count` via
   `token_counter.count`, keep the predecessor's `section_path`/`page`) and repeat.
7. Concatenate every region's (merged) `ChunkDraft`s back into one document-ordered list.
8. Assign `position = index` for `index, draft in enumerate(...)`; derive
   `chunk_id = derive_chunk_id(doc_id, position)` (REQ-002, Assumption A7); build each `Chunk`.
9. `Chunker` calls `ledger.transition(doc_id, to_status=Status.EMBEDDING,
   stage=Status.EMBEDDING)`. Succeeds; row is now `embedding`.
10. `Chunker.chunk` returns `list[Chunk]`.

**Failure paths**
- F1. `elements` is empty (Analyzer returned zero elements) → `Chunker` attempts
  `ledger.transition(doc_id, to_status=Status.FAILED, stage=Status.FAILED,
  error_code=CHUNKER_EMPTY_ELEMENTS, error_message=...)` — the same
  `is_benign_concurrent_loss` triage every other ledger write in this design uses (design-review
  MAJOR fix; see Assumption A5 for why this deliberately does *not* reproduce REQ-005
  `Analyzer._terminalize`'s un-triaged gap, tracked separately as issue #9):
  - Transition succeeds: terminalizes the row to `failed`; `Chunker` then raises
    `ChunkerError(doc_id, reason, error_code=CHUNKER_EMPTY_ELEMENTS)`. PERMANENT (story, AC5).
  - Transition raises `LedgerTransitionInvalid`: triage via
    `is_benign_concurrent_loss(exc.observed_row, Status.FAILED, Status.FAILED)`.
    - Benign (`observed_row.status == Status.INDEXED` — a concurrent duplicate worker already
      completed the whole pipeline for this `doc_id` before this call's own empty-elements
      observation was acted on): skip the ledger write (a terminal `INDEXED` row must never be
      downgraded to `FAILED`) but still raise `ChunkerError(..., error_code=CHUNKER_EMPTY_ELEMENTS)`
      — this call's own `elements` input was genuinely empty, which remains worth surfacing to
      its caller even though the document itself already succeeded elsewhere.
    - Not benign (unknown `doc_id`, or row genuinely already `Status.FAILED`): re-raise
      `LedgerTransitionInvalid` unchanged, not `ChunkerError` — mirroring F5's pattern exactly.
  This is the only failure path in this module that terminalizes the row (on its non-benign,
  successful-transition branch).
- F2. A strategy's call to `token_counter.count(...)` raises during encoding load (e.g.
  `tiktoken.get_encoding` fails) → caught at the region-dispatch call site, `Chunker` calls
  `ledger.transition(doc_id, to_status=Status.CHUNKING, stage=Status.CHUNKING,
  error_code=TOKENIZER_LOAD_FAILED, error_message=...)` — a same-status self-transition (REQ-003
  Assumption A2, mirroring REQ-005's F3-F6 pattern) — then raises
  `ChunkerError(..., error_code=TOKENIZER_LOAD_FAILED)`. TRANSIENT; row stays `chunking`,
  re-enterable on redelivery, `attempts` increments.
- F3. Any other exception from a strategy call, the merge pass, or ID assignment (not F1/F2) →
  `Chunker` logs the original exception, performs the same self-transition pattern as F2 with
  `error_code=CHUNKER_INTERNAL`, then raises `ChunkerError(..., error_code=CHUNKER_INTERNAL)`.
  TRANSIENT.
- F4. `TableAtomicChunkStrategy` computes `token_count > config.max_chunk_tokens` for a table →
  calls `logger.warning("oversized_table", extra={"doc_id": doc_id, "table_tokens": token_count,
  "max_tokens": config.max_chunk_tokens})`, sets `metadata["oversized"] = True`, and returns the
  one `ChunkDraft` anyway. No exception, no error code, no ledger write — not a Contract #4
  failure-taxonomy member at all (Assumption A3, design-review BLOCKER fix); listed here only
  because the story enumerates this condition alongside the other three.
- F5. Step 9's `ledger.transition(to_status=Status.EMBEDDING, stage=Status.EMBEDDING)` raises
  `LedgerTransitionInvalid` (a concurrent duplicate worker already advanced this `doc_id` past
  `chunking`, or the `doc_id` is unknown/already `failed`) → triage via
  `is_benign_concurrent_loss(exc.observed_row, Status.EMBEDDING, Status.EMBEDDING)` — exactly
  REQ-005's F8 pattern applied to this module's own single exit transition:
  - Benign (row already at/past `embedding`): treat as success, return the computed `list[Chunk]`
    without re-raising — deterministic recomputation (Contract #2) makes this safe, not a
    correctness risk.
  - Not benign (unknown `doc_id`, or row genuinely `failed`): re-raise `LedgerTransitionInvalid`
    unchanged — not this module's error code.

## 4. Contract compliance

- **Arrival Envelope**: `Chunker.chunk` reads only `envelope.doc_id` (for ledger keys and
  `chunk_id`/`Chunk.doc_id` derivation); no other envelope field (`vertical`, `scenario_id`,
  `allowed_groups`, etc.) is inspected — consistent with the story's own touchpoint statement.
- **Identity & Idempotency**: every `chunk_id` is `derive_chunk_id(doc_id, position)` (REQ-002),
  computed only after position assignment is final (Assumption A7) — re-chunking an identical
  `elements` input is a pure function of that input plus `ChunkerConfig` (no randomness, no wall
  clock in any strategy or the merge pass), so it produces byte-identical `Chunk`s in identical
  order (AC4), and any downstream upsert overwrites cleanly on redelivery.
- **State Ledger**: this module owns exactly the `chunking → embedding` (happy path) and
  `chunking → chunking` (self-transition retries on TRANSIENT failure, F2/F3, REQ-003 Assumption
  A2) transitions, applied only through the existing `LedgerStore.transition()` — no new
  transition table, no direct row mutation. Only F1 (PERMANENT) terminalizes the row to `failed`,
  and — unlike REQ-005's `Analyzer._terminalize` (issue #9) — F1's own write is itself triaged via
  `is_benign_concurrent_loss` before being treated as a genuine failure, exactly like F5's exit
  transition; every TRANSIENT path self-transitions, leaving the row re-enterable.
- **Failure Taxonomy**: registers all three raise-capable codes at import time via
  `register_error`, following the `_DEFAULT_REGISTRY` pattern REQ-004/REQ-005 established; every
  raised exception carries one of `CHUNKER_EMPTY_ELEMENTS`, `TOKENIZER_LOAD_FAILED`, or
  `CHUNKER_INTERNAL` — no bare unclassified exception ever escapes `chunk()`. The story's fourth
  condition, `CHUNKER_OVERSIZED_TABLE`, is deliberately *not* registered and never raised
  (Assumption A3, design-review BLOCKER fix) — it is a structured-log advisory only, kept
  strictly outside Contract #4's binary PERMANENT/TRANSIENT taxonomy rather than shoehorned into
  a misleading nominal classification.

## 5. Error codes

| Code | Classification | Trigger condition |
|---|---|---|
| `CHUNKER_EMPTY_ELEMENTS` | PERMANENT | `elements` is empty on entry to `chunk()` (F1); ledger row terminalizes to `failed` (via the F5-style `is_benign_concurrent_loss` triage described in §3) |
| `TOKENIZER_LOAD_FAILED` | TRANSIENT | `TokenCounter.count()` fails to load its underlying tokenizer resource on first use (F2); ledger row self-transitions `chunking → chunking` |
| `CHUNKER_INTERNAL` | TRANSIENT | Any exception from a strategy call, the merge pass, or ID assignment not mapped above (F3); ledger row self-transitions `chunking → chunking` |

`CHUNKER_OVERSIZED_TABLE` is intentionally absent from this table — it is not an `ErrorRegistry`
member (Assumption A3, design-review BLOCKER fix). When a table's `token_count` exceeds
`max_chunk_tokens` (F4), `TableAtomicChunkStrategy` logs `logger.warning("oversized_table",
extra={"doc_id": ..., "table_tokens": ..., "max_tokens": ...})` and emits the chunk anyway — no
exception, no error code, no ledger write.

## 6. Config surface

New file `config/chunker.yaml`:

```yaml
chunker:
  target_chunk_tokens: 512    # operational tunable — code-authoritative, see below
  overlap_tokens: 64          # operational tunable — code-authoritative, see below
  min_chunk_tokens: 40        # operational tunable — code-authoritative, see below
  max_chunk_tokens: 800       # operational tunable — code-authoritative, see below
  token_counter: tiktoken_cl100k   # factory key -> build_token_counter(); MUST match the
                                   # embedding model's tokenizer (REQ-007) once that config exists
  error_codes:
    CHUNKER_EMPTY_ELEMENTS: PERMANENT
    TOKENIZER_LOAD_FAILED: TRANSIENT
    CHUNKER_INTERNAL: TRANSIENT
    # CHUNKER_OVERSIZED_TABLE is deliberately absent — it is a structured-log advisory
    # only (Assumption A3, design-review BLOCKER fix), never a registered ErrorRegistry
    # code; see the "oversized_table" log event in ingestion/chunker/strategies/table_atomic.py.
```

`target_chunk_tokens`/`overlap_tokens`/`min_chunk_tokens`/`max_chunk_tokens`/`token_counter` are
ordinary operational tunables (REQ-005 §6 Assumption A6 precedent) — authoritative defaults live
on `ChunkerConfig`'s dataclass fields; this YAML block is kept in sync via a dedicated test (§7),
since no config-loading mechanism exists anywhere in this codebase yet. `error_codes` follows the
same documentation/audit-only convention as `config/analyzer.yaml`/`config/errors.yaml` — not read
at runtime; the registry populated via `register_error` at import time is authoritative.

**`config/errors.yaml` + sync-test integration (design-review MAJOR fix, same-PR deliverable, not
a follow-up):**

- `config/errors.yaml`'s `known_codes` aggregate-audit block must gain exactly the three codes
  this module registers:
  ```yaml
  CHUNKER_EMPTY_ELEMENTS: PERMANENT
  TOKENIZER_LOAD_FAILED: TRANSIENT
  CHUNKER_INTERNAL: TRANSIENT
  ```
  (`CHUNKER_OVERSIZED_TABLE` is not added — it is not a registered code, per Assumption A3.)
- `ingestion/errors/test_registry.py`'s
  `test_config_known_codes_matches_all_codes_after_importing_all_modules` hand-imports every
  module that registers its own codes before comparing against `config/errors.yaml`; its
  hand-import list must gain `import ingestion.chunker.model  # noqa: F401`, alongside the
  existing `ingestion.analyzer.model` / `ingestion.envelope.model` / `ingestion.identity.derive` /
  `ingestion.ledger.store` imports. Without this addition, that test's `known_codes == live_codes`
  assertion would either fail (codes registered but not documented) or pass vacuously (this
  module never imported, its codes never entering `live_codes` at all) — neither is acceptable.
- Both edits ship in the same PR as the `ingestion/chunker/` code itself, exactly as REQ-005's
  `config/analyzer.yaml` and its own registrations shipped together.

**New third-party dependencies** (flagged for `pyproject.toml`, not otherwise addressed by this
LLD), with explicit version constraints (design-review MINOR fix):

- `tiktoken>=0.8,<1.0` — `TokenCounter`'s underlying library (Assumption A9).
- `langchain-text-splitters>=0.3,<0.4` — `RecursiveChunkStrategy`'s underlying library
  (Assumption A4).

Exact pins are to be finalized against `pyproject.toml`'s existing dependency-resolution
constraints at implementation time; these ranges are the LLD's floor/ceiling recommendation, not
a claim that `pyproject.toml` has been edited yet.

## 7. Test plan

- `test_recursive_chunker_prose_only_document_respects_target_size_and_overlap` — asserts every
  non-final chunk's `token_count <= target_chunk_tokens` and consecutive chunks' text overlaps by
  approximately `overlap_tokens` (AC3, story).
- `test_recursive_chunker_min_chunk_merging_absorbs_undersized_trailing_chunk` — a document whose
  naive split would leave a final piece `< min_chunk_tokens`; asserts it is merged into its
  predecessor unless it is the document's only chunk.
- `test_structure_aware_chunker_chunks_carry_section_breadcrumb` — asserts every chunk within a
  heading's section has `metadata["section_path"]` equal to that heading's text (Assumption A1,
  flat/single-level).
- `test_structure_aware_chunker_respects_heading_boundaries` — asserts no chunk's text spans
  content from two different sections.
- `test_structure_aware_chunker_oversized_section_falls_back_to_recursive_split` — a section
  exceeding `target_chunk_tokens` is split into multiple chunks, each still tagged with that
  section's `section_path`, via the injected `RecursiveChunkStrategy` (asserted via a spy/fake).
- `test_structure_aware_chunker_no_level_metadata_degrades_to_flat_breadcrumb` — regression test
  for Assumption A1: nested-looking heading text does not produce a fabricated multi-level
  breadcrumb when `metadata.get("level")` is absent.
- `test_table_atomic_20_row_table_produces_exactly_one_chunk` (AC1/AC2, story) — asserts
  `len(chunks) == 1` and the table's full row content is present in `text`.
- `test_table_atomic_table_larger_than_max_chunk_tokens_emits_one_chunk_plus_warning_log` —
  asserts exactly one chunk, `metadata["oversized"] is True`, and a WARNING-level log record with
  event name `"oversized_table"` and `extra` fields `doc_id`/`table_tokens`/`max_tokens` — no
  exception raised, no ledger write, no error code involved (F4, design-review BLOCKER fix).
- `test_table_atomic_prepends_adjacent_caption_when_present` / `..._no_caption_when_not_adjacent`
  — Assumption A2's heuristic, both branches.
- `test_table_atomic_falls_back_to_element_text_when_cells_metadata_absent`.
- `test_mixed_document_headings_paragraphs_table_list_produces_coherent_chunk_stream` (AC1,
  story) — asserts structure-aware chunking on prose regions, exactly one chunk for the table, no
  prose chunk exceeds `max_chunk_tokens`, and every chunk has a valid `chunk_id`.
- `test_chunk_empty_elements_raises_chunker_empty_elements_permanent_ledger_ends_failed` (AC5,
  story) — asserts the ledger row's terminal `status == Status.FAILED`.
- `test_chunk_empty_elements_terminalize_benign_concurrent_loss_skips_ledger_write_but_still_raises`
  (F1, design-review MAJOR fix) — pre-seed a ledger row already at `Status.INDEXED`; invoke
  `chunk()` with empty `elements`; asserts `ChunkerError(error_code=CHUNKER_EMPTY_ELEMENTS)` is
  still raised, but the row's `status` remains `Status.INDEXED` (never downgraded to `FAILED`).
- `test_chunk_empty_elements_terminalize_non_benign_reraises_ledger_transition_invalid_not_chunker_error`
  (F1, design-review MAJOR fix) — unknown `doc_id` with empty `elements` → asserts
  `LedgerTransitionInvalid` propagates, not `ChunkerError`, mirroring F5's own non-benign test.
- `test_chunk_deterministic_same_elements_twice_produces_identical_chunk_ids_and_order` (AC4,
  story) — two `chunk()` calls on identical `elements` input produce byte-identical `Chunk` lists.
- `test_chunk_tokenizer_load_failed_self_transitions_chunking_transient` — fakes `TokenCounter`
  to raise on first `count()`; asserts ledger row stays `Status.CHUNKING` (never `FAILED`),
  `attempts` incremented, `last_error_code == TOKENIZER_LOAD_FAILED`.
- `test_chunk_unmapped_strategy_exception_wrapped_as_chunker_internal_self_transitions_chunking`
  — same self-transition assertions, `last_error_code == CHUNKER_INTERNAL`; original exception
  logged.
- `test_chunk_transient_failure_then_redelivery_increments_attempts_without_reaching_failed` —
  two consecutive `chunk()` calls hitting the same TRANSIENT code; `attempts` increases each
  time, `status` never reaches `Status.FAILED` (regression test mirroring REQ-005's own).
- `test_chunk_success_transitions_chunking_to_embedding` — ledger integration, happy path.
- `test_chunk_redelivered_after_prior_success_is_benign_noop` (F5) — pre-seed a ledger row already
  at `Status.EMBEDDING`; re-invoke `chunk()`; asserts no exception, `Chunk` list returned, row
  untouched.
- `test_chunk_ledger_transition_invalid_non_benign_propagates_unchanged` (F5) — unknown `doc_id` →
  `LedgerTransitionInvalid` propagates, not swallowed.
- `test_all_three_chunker_codes_registered_after_import` (AC6, story, as scoped by design-review
  BLOCKER fix) — `ErrorRegistry.all_codes()` contains all three (`CHUNKER_EMPTY_ELEMENTS`,
  `TOKENIZER_LOAD_FAILED`, `CHUNKER_INTERNAL`) with the declared classifications, **and**
  explicitly asserts `"CHUNKER_OVERSIZED_TABLE"` is absent from `all_codes()` — a regression test
  guarding against the misclassification trap the design review flagged.
- `test_config_chunker_yaml_tunables_match_chunker_config_defaults` — sync-via-test guard for §6.
- `test_config_errors_yaml_known_codes_includes_chunker_codes_after_import` (design-review MAJOR
  fix) — extends `ingestion/errors/test_registry.py`'s existing
  `test_config_known_codes_matches_all_codes_after_importing_all_modules` pattern: with
  `ingestion.chunker.model` added to that test's hand-import list, asserts `config/errors.yaml`'s
  `known_codes` block matches `all_codes()` exactly, including this module's three codes.
- `test_chunker_swaps_in_fake_strategy_with_no_orchestrator_code_changes` (Assumption A6,
  market-awareness note) — constructs `Chunker` with a `FakeStrategy` in one slot; asserts
  `Chunker.chunk`'s public signature/behavior for the other slots is unaffected.
- Property-based (`hypothesis`, story) — `test_property_random_wellformed_element_sequence_produces_valid_chunk_stream`:
  any random sequence of well-formed `Element`s produces a chunk stream with no oversized chunk
  except a `table_atomic`-tagged one, and no under-`min_chunk_tokens` chunk except possibly the
  document's last.

## 8. Budget

- p95 latency added per document: < 500ms for a 20-page document (story budget) — pure Python +
  `tiktoken` + `langchain_text_splitters`, no network/I/O; includes one-time `tiktoken` encoding
  load only on the process's first `chunk()` call (cached thereafter, Assumption A9).
- Cost per document: $0 — no external API calls; tokenization and splitting are both local
  library calls.
- Memory: `elements: list[Element]` is already fully materialized by the caller (REQ-005's
  `Analyzer.analyze` return type is `list[Element]`, not a generator) — this module adds no
  further whole-document string concatenation beyond per-region text joins; the largest
  additional structure retained is the final `list[Chunk]` itself.
