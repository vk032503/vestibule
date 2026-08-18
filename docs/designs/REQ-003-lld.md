# REQ-003 — State Ledger — LLD

**Story:** docs/stories/REQ-003.md · **Phase:** 1 · **Pipeline:** full

## Revision note
Revised twice after design-review REJECTs, both rounds confined to the concurrency/PERMANENT
semantics fix for `LedgerTransitionInvalid` — **no `LedgerStore` method signature has changed in
either round** (`create`/`transition`/`get`/`list_by_status` remain exactly as given by the story).
A third, post-merge revision (below) additively extends this LLD's own transition table under a
later REQ; it is a live-reference pointer, not a re-opening of the frozen historical record §1-§8
below.

**Round 1** — three findings:
1. **[MAJOR]** A losing caller in a concurrent `transition()` race (§3 F7) raised
   `LedgerTransitionInvalid` with no way to distinguish "benign lost race under at-least-once
   delivery" from "real corruption," which — read literally against Contract #4's "PERMANENT means
   mark failed, never retry" — would spuriously fail a document that is actually progressing
   correctly via the winner. Fixed by enriching the exception with the observed current row and
   the caller's own attempted request (§1), adding a pure `is_benign_concurrent_loss()` triage
   helper (§1), and adding explicit caller guidance in §3/§4.
2. **[MINOR]** `config/ledger.yaml`'s `default_list_limit` was bundled into the same "config edit
   must never change behavior" carve-out as `legal_transitions`. Un-bundled in §6, with a
   sync-check test added in §7. Confirmed resolved on re-review; unchanged in Round 2.
3. **[MINOR]** Added an explicit one-line acknowledgment (§4, §8) that `attempts` has no
   ledger-enforced retry cap. Confirmed resolved on re-review; unchanged in Round 2.

**Round 2** — one narrow gap inside Round 1's fix for finding #1:
`is_benign_concurrent_loss`'s reachability check indexed `attempted_to_status` into
`_FORWARD_ORDER`, but `_FORWARD_ORDER` deliberately excludes `Status.FAILED` — and
`attempted_to_status` can legitimately *be* `Status.FAILED` (a losing caller racing to mark a
document `FAILED` while a concurrent winner simultaneously advances it forward, e.g. to
`INDEXED`, is a reachable, legal scenario, not a contrived one). As specified, this crashed the
triage helper itself (`tuple.index()` raises `ValueError` for a value not present) on exactly the
input it exists to handle safely, and — because that crash occurred before the helper could return
`True` — the surrounding F7 decision tree would have driven a document that actually succeeded
toward an erroneous `failed`-mark attempt. Fixed by giving `is_benign_concurrent_loss` an explicit,
order-dependent special case for `attempted_to_status == Status.FAILED` that never reaches the
`_FORWARD_ORDER` lookup for that value (§1), updating §3 F7's caller-guidance steps to match, and
adding regression tests, including one that exhaustively exercises every `(attempted_to_status,
observed_row.status)` pair to assert the helper never raises (§7).

**Round 3 (this revision, REQ-009)** — a post-merge, additive extension of this LLD's own
transition table, not a design-review REJECT: REQ-009 (Vertical Loader) needed a way to park a
document for human review when its vertical/scenario cannot be confidently determined, without
guessing at ACL-relevant routing. This added a new `Status.NEEDS_REVIEW` value and three new
`_LEGAL_TRANSITIONS` edges (`pending -> needs_review`, `needs_review -> pending`,
`needs_review -> failed`) directly in `src/vestibule/ledger/store.py` — deliberately in-place,
not a new module, since Contract #3's ledger has exactly one FSM. `NEEDS_REVIEW` is *not* added to
`_TERMINAL_STATUSES`; that is the point of a review queue that can return documents to the
pipeline. This re-opened the exact class of gap Round 2 fixed: `Status.NEEDS_REVIEW`, like
`Status.FAILED`, is absent from `_FORWARD_ORDER` (it branches only off `PENDING`, so it has no
well-defined position on the single linear pipeline path either), and — as confirmed by this
repo's own pre-existing `test_is_benign_concurrent_loss_never_raises` hypothesis test, which
samples every `Status` member automatically via `tuple(Status)` — adding `NEEDS_REVIEW` to the
enum without also updating `is_benign_concurrent_loss` would have reintroduced the identical
`tuple.index() -> ValueError` crash Round 2 fixed, this time on both `attempted_to_status ==
Status.NEEDS_REVIEW` and `observed_row.status == Status.NEEDS_REVIEW`. Fixed with the same
technique as Round 2: two more order-dependent special cases in `is_benign_concurrent_loss`,
resolved before either side ever reaches `_FORWARD_ORDER.index()`. See `docs/stories/REQ-009.md`
for the full vertical-routing rationale (why ambiguity parks but a stated-but-missing
vertical/scenario is a config error instead, and why `VERTICAL_UNRESOLVED` is classified PERMANENT
even though the parked document is not dead) — that rationale is REQ-009's own, not restated here.

## Assumptions (non-blocking, flagged per house rules)
The story specifies the four `LedgerStore` methods, the `Status` values, and the legal-transition
table fully enough to design against, but leaves several details underspecified. None of these
change the shape of the four given public method signatures, so — following the precedent set by
REQ-001 (`trust_tier` enum membership, `doc_id`-as-derived-not-trusted) — they are treated as
scoped design decisions, documented here, not open questions that block the LLD:

- **A1 — `status` vs. `stage`.** The story lists `LedgerRow`'s fields as `status (enum), stage
  (enum)` but defines only one enum, `Status`, and CLAUDE.md's own Contract #3 text calls the
  recorded value "stage" while the story's acceptance criteria and `transition(doc_id, to_status,
  stage, ...)` signature call it "status" / `to_status`. No test in the acceptance criteria
  exercises `status` and `stage` diverging. This LLD treats them as a **mirrored pair of the same
  `Status` value** — `stage` must equal `to_status` on every `transition()` call (mismatch is
  rejected, see F1) — reconciling both vocabularies (story's "status", CLAUDE.md's "stage")
  without inventing a second, independent state dimension the story never defines.
- **A2 — "retry within the same stage" (AC4) requires same-status self-transitions.** The literal
  transition table in the story has no self-loops (e.g. no `analyzing → analyzing`), yet AC4
  requires `attempts` to increment "on every retry within the same stage," and `transition()` is
  the only mutation method given (no separate `retry()`/`bump_attempts()` method). This LLD treats
  a same-status call (`to_status == current.status`, current non-terminal) as a legal, distinct
  case validated alongside the table: it is how a redelivered message re-enters a stage after a
  transient failure without yet advancing. Terminal rows (`indexed`, `failed`) accept no
  self-transition either, consistent with "terminal, no transitions out" in the story's table.
- **A3 — `EnvelopeSummary` shape.** `create(doc_id, envelope_summary)` is given, but `LedgerRow`
  persists exactly one field sourced from it: `config_version`. This LLD defines
  `EnvelopeSummary` as a minimal frozen dataclass holding only `config_version: str`, rather than
  importing the full `ArrivalEnvelope` (REQ-001) into this module — keeping the ledger
  single-responsibility and decoupled from the envelope schema, per house rules.
- **A4 — `attempts` semantics.** Not fully specified beyond AC4. This LLD defines: `0` at
  `create()` (nothing attempted yet, still `pending`); reset to `1` on every legal forward advance
  into a new non-terminal status (first attempt at that stage); incremented by 1 on each same-
  status self-transition (A2) at that stage; **frozen** at its current value when transitioning to
  `failed` (failure is the terminal outcome of the attempts already made, not itself a retry).
- **A5 — `last_error_code`/`last_error_message` on non-`failed` transitions.** Cleared to `None`
  on a clean forward advance with no `error_code` supplied (the new stage starts error-free);
  overwritten if `error_code`/`error_message` are supplied on a self-transition retry (A2). If a
  self-transition retry (A2) supplies no `error_code` (i.e. `error_code=None`), the row's prior
  `last_error_code`/`last_error_message` are preserved unchanged, not cleared — so an in-progress
  stage's last transient failure remains visible via `get()` across a bare retry, even though the
  row hasn't reached `failed`.
- **A6 — `doc_id` format is validated in `create()`/`transition()`, not `get()`.** This module
  reuses `ingestion.identity.derive.validate_id_format` (REQ-002) rather than re-implementing a
  hex-shape check, so `create()`/`transition()` reject a malformed `doc_id` as `IdentityInvalid`
  (cross-module, PERMANENT — see §5). `get()` deliberately skips this check so AC8 ("`get` for an
  unknown doc_id returns `None`, does not raise") holds literally for any string input, not just
  well-formed-but-absent ids.

## 1. Interfaces

```python
# ingestion/ledger/store.py

class Status(str, Enum):
    """Pipeline lifecycle states — Contract #3's FSM. Fixed, code-defined enum; sole
    authority for legal values and legal transitions (_LEGAL_TRANSITIONS below). Used for
    both LedgerRow.status and LedgerRow.stage — see Assumption A1."""
    PENDING = "pending"
    ANALYZING = "analyzing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    INDEXED = "indexed"
    FAILED = "failed"


_TERMINAL_STATUSES: frozenset[Status] = frozenset({Status.INDEXED, Status.FAILED})

_LEGAL_TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.PENDING:   frozenset({Status.ANALYZING, Status.FAILED}),
    Status.ANALYZING: frozenset({Status.CHUNKING, Status.FAILED}),
    Status.CHUNKING:  frozenset({Status.EMBEDDING, Status.FAILED}),
    Status.EMBEDDING: frozenset({Status.INDEXING, Status.FAILED}),
    Status.INDEXING:  frozenset({Status.INDEXED, Status.FAILED}),
    Status.INDEXED:   frozenset(),
    Status.FAILED:    frozenset(),
}
"""Authoritative legal-forward-transition table — an exact transcription of the story's
References table. Same-status self-transitions (retry-within-stage, Assumption A2) are
legal for every non-terminal status and are validated separately in validate_transition,
not folded into this dict, so this table stays a literal match to the story's spec."""

_FORWARD_ORDER: tuple[Status, ...] = (
    Status.PENDING, Status.ANALYZING, Status.CHUNKING,
    Status.EMBEDDING, Status.INDEXING, Status.INDEXED,
)
"""The single linear non-FAILED path through _LEGAL_TRANSITIONS, precomputed for
is_benign_concurrent_loss's reachability check (§1, below) — this FSM has exactly one
forward branch (every status's only non-FAILED successor is the next entry here), so
"reachable via zero or more further legal forward edges" reduces to a list-index
comparison rather than a graph search.

Status.FAILED is deliberately absent: it is legal from every non-terminal status (see
_LEGAL_TRANSITIONS), not part of this single linear path, so it has no well-defined
position here. Any code indexing into this tuple MUST special-case both
`attempted_to_status == Status.FAILED` and `observed_row.status == Status.FAILED` before
calling .index() on either — is_benign_concurrent_loss (below) does exactly this, having
been fixed in a design-review round to no longer call .index(Status.FAILED)."""


class LedgerRow(BaseModel):
    """One row per document — Contract #3. Immutable snapshot; store methods return a
    new LedgerRow on every write, never mutate a returned instance in place."""
    model_config = ConfigDict(frozen=True)

    doc_id: str                    # 64-char lowercase hex; validated via
                                    # ingestion.identity.derive.validate_id_format (REQ-002)
    status: Status                 # current FSM state — drives legal-transition checks
    stage: Status                  # mirror of status; must equal status — see Assumption A1
    attempts: int                  # attempts made at the current (status, stage); >= 0, see A4
    last_error_code: str | None = None
    last_error_message: str | None = None
    config_version: str            # opaque; from EnvelopeSummary; set once at create(), immutable
    ingested_at: datetime          # UTC; set once at create(), never overwritten (AC1, AC5)
    updated_at: datetime           # UTC; set on every create()/transition() write (AC5)


@dataclass(frozen=True)
class EnvelopeSummary:
    """Minimal context create() needs beyond doc_id itself — see Assumption A3."""
    config_version: str


class LedgerTransitionInvalid(Exception):
    """Raised for any illegal state transition or malformed transition request. Always
    classified PERMANENT at the exception-class level (code + classification below are
    fixed, never caller-supplied) — an illegal transition indicates a caller/coordinator
    bug (wrong stage order, stale doc_id, missing error_code) in the general case.

    IMPORTANT — concurrency (§3 F7, §4): under Contract #2's at-least-once-delivery
    assumption, this same exception is also raised for the *losing* caller of a benign,
    expected concurrent transition() race — that is not evidence of corruption. Rather
    than requiring a catching caller to issue a second get(doc_id) (races a second time,
    and reads a possibly-already-changed-again row — a TOCTOU gap), this exception carries
    the row observed at raise time plus the caller's own original request, so
    is_benign_concurrent_loss() (below) can triage deterministically from the exception
    alone. See §3/§4 for the required caller-side handling before treating this exception
    as a document failure.
    """
    code: str = "LEDGER_TRANSITION_INVALID"
    classification: Literal["PERMANENT"] = "PERMANENT"

    def __init__(
        self,
        doc_id: str,
        reason: str,
        *,
        observed_row: LedgerRow | None,
        attempted_to_status: Status,
        attempted_stage: Status,
    ) -> None:
        """code and classification are fixed by this class, not caller-supplied.
        observed_row: the current row read (under the store's lock, so it reflects
        whatever write — if any — a concurrent winner already applied) at the moment this
        exception is raised; None only for F2 (no row exists for doc_id at all — nothing to
        observe). attempted_to_status/attempted_stage: the caller's own original request,
        always present regardless of observed_row, so is_benign_concurrent_loss() never
        needs anything beyond this exception's own attributes."""
        self.doc_id = doc_id
        self.reason = reason
        self.observed_row = observed_row
        self.attempted_to_status = attempted_to_status
        self.attempted_stage = attempted_stage
        super().__init__(f"{doc_id}: {reason}")


def is_benign_concurrent_loss(
    observed_row: LedgerRow | None,
    attempted_to_status: Status,
    attempted_stage: Status,
) -> bool:
    """Pure, backend-independent triage helper for a caller that has caught
    LedgerTransitionInvalid (§3 F7, §4). Intended call pattern:
    `is_benign_concurrent_loss(exc.observed_row, exc.attempted_to_status, exc.attempted_stage)`
    — no extra get(doc_id) needed, no TOCTOU gap. Never raises, for any input — see the
    ordered checks below, and the Round 2 regression test in §7.

    Evaluated in this exact order (each step either returns or falls through to the next):
      1. `attempted_stage != attempted_to_status` → False (malformed request, A1).
      2. `observed_row is None` → False (F2, unknown doc_id — nothing to observe).
      3. `observed_row.status != observed_row.stage` → False (observed row itself
         inconsistent; should be unreachable in practice given store invariants —
         defensive only).
      4. `attempted_to_status == Status.FAILED` — checked and fully resolved HERE, before
         any `_FORWARD_ORDER.index()` call, because `Status.FAILED` is deliberately absent
         from `_FORWARD_ORDER` (§1) and is a legal target from every non-terminal status —
         so a losing caller racing to mark `FAILED` while a concurrent winner
         simultaneously advances the document forward (e.g. to `INDEXED`) is a reachable,
         legitimate scenario, not a contrived edge case (this is the exact bug fixed in
         design-review Round 2 — see the Revision note):
           - `observed_row.status == Status.INDEXED` → **True**. The document already
             reached the other terminal state via the winner; forcing this caller's
             `FAILED` mark through would be both impossible (F4 rejects writes against an
             already-terminal row) and semantically wrong, since the document actually
             succeeded.
           - Any other `observed_row.status` here (including `Status.FAILED` itself, and
             including every non-terminal/mid-pipeline status) → **False**. An observed
             `Status.FAILED` is a distinct, separately-handled outcome — see §3 F7 step 2
             — deliberately NOT reported as "benign" by this helper. An observed
             non-terminal/mid-pipeline status means this caller's own reason for
             attempting `FAILED` does not correspond to any legitimate concurrent-progress
             story, so it is a genuine conflict, treated as a real bug per §3 F7 step 3.
      5. (Reached only when `attempted_to_status != Status.FAILED`, i.e. it is one of the
         five non-`FAILED` values, all always present in `_FORWARD_ORDER`.)
         `observed_row.status == Status.FAILED` → False. A concurrent real failure — a
         distinct case, see §3 F7 step 2, deliberately NOT reported as benign here either.
      6. Otherwise (both `attempted_to_status` and `observed_row.status` are now guaranteed
         members of `_FORWARD_ORDER` — step 4 has already returned for
         `attempted_to_status == Status.FAILED`; step 5 has already returned for
         `observed_row.status == Status.FAILED` — so the following can never raise
         `ValueError`): **True** iff `_FORWARD_ORDER.index(observed_row.status) >=
         _FORWARD_ORDER.index(attempted_to_status)` — i.e. a concurrent winner already
         applied this exact edge, or has since advanced the document even further along
         the same single linear path (late redelivery). Otherwise **False**.

    In every False case, the caller must NOT treat this return value alone as "real
    PERMANENT failure" — this helper is step 1 of §3 F7's full three-step decision tree;
    step 2 separately (and unconditionally) handles `observed_row.status == Status.FAILED`
    regardless of what the caller itself attempted; only step 3, the remainder, is a
    genuine bug."""
    ...


def validate_transition(
    current: LedgerRow | None,
    to_status: Status,
    stage: Status,
    error_code: str | None,
) -> None:
    """Pure, backend-independent legality check shared by every LedgerStore
    implementation (in-memory now; Azure Table Storage / Cosmos DB in Phase 2, out of
    scope) so the legal-transition table is enforced exactly once, per house rules'
    "adapters thin" principle — future backends call this rather than reimplementing it.
    No I/O, no mutation. Checks, in order: current is not None (F2); stage == to_status
    (F1); current.status is not terminal (F4); to_status is in
    _LEGAL_TRANSITIONS[current.status] or to_status == current.status (A2) (F3); if
    to_status == Status.FAILED, error_code is not None (F5). On any failure, raises
    LedgerTransitionInvalid(doc_id, reason, observed_row=current, attempted_to_status=
    to_status, attempted_stage=stage) — current is threaded straight through as
    observed_row so every raise site is triage-ready via is_benign_concurrent_loss()
    without extra I/O. Returns None on success."""
    ...


class LedgerStore(ABC):
    """Persistence port for Contract #3. Concrete backends implement all four methods,
    each calling validate_transition() before any write, passing the current in-store row
    (read under whatever locking the backend uses) as validate_transition's `current`."""

    @abstractmethod
    def create(self, doc_id: str, envelope_summary: EnvelopeSummary) -> LedgerRow:
        """Idempotent (AC1): an existing row for doc_id is returned unchanged — every
        field, including ingested_at, is untouched by a repeat call, regardless of what
        envelope_summary the repeat call carries. Does not call validate_transition()."""
        ...

    @abstractmethod
    def transition(
        self,
        doc_id: str,
        to_status: Status,
        stage: Status,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> LedgerRow:
        """Calls validate_transition() first, then writes and returns the new row. Raises
        LedgerTransitionInvalid (PERMANENT, §3/§5 — see is_benign_concurrent_loss() and
        §4 for required caller triage before treating this as a document failure) or,
        cross-module, IdentityInvalid (PERMANENT, §5) if doc_id is malformed."""
        ...

    @abstractmethod
    def get(self, doc_id: str) -> LedgerRow | None:
        """Returns None for an unknown doc_id; never raises (AC8). Does not validate
        doc_id shape — see Assumption A6."""
        ...

    @abstractmethod
    def list_by_status(self, status: Status, limit: int = 100) -> list[LedgerRow]:
        """Rows with the given status, ordered by updated_at descending, capped at
        limit (AC7). limit <= 0 returns an empty list; never raises. limit's literal
        default (100) is the documented, sync-checked counterpart of config/ledger.yaml's
        default_list_limit — see §6."""
        ...


class InMemoryLedgerStore(LedgerStore):
    """Thread-safe, fully-functional LedgerStore for tests and local dev. Backed by a
    single dict[str, LedgerRow] guarded by one threading.RLock held for the full
    duration of each public method (coarse-grained; correct-under-concurrency, not
    throughput-optimized — acceptable given the in-memory, test/dev-only scope and the
    <5ms budget, §8)."""

    def __init__(self, *, default_list_limit: int = 100) -> None:
        """default_list_limit is accepted for forward compatibility with a future
        composition root that reads config/ledger.yaml and wires it in explicitly (§6);
        it does not change list_by_status's own `limit: int = 100` default parameter,
        which remains the story's given, always-effective default for direct callers."""
        ...

    def create(self, doc_id: str, envelope_summary: EnvelopeSummary) -> LedgerRow: ...

    def transition(
        self,
        doc_id: str,
        to_status: Status,
        stage: Status,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> LedgerRow: ...

    def get(self, doc_id: str) -> LedgerRow | None: ...

    def list_by_status(self, status: Status, limit: int = 100) -> list[LedgerRow]: ...
```

No adapter classes are introduced. `LedgerStore` is the persistence port itself (Phase-2 Azure
Table/Cosmos adapters, out of scope, will be the thin wrappers, each reusing `validate_transition`
and `is_benign_concurrent_loss` rather than reimplementing the FSM or the race-triage logic). This
module depends only on `ingestion.identity.derive.validate_id_format` (REQ-002, for `doc_id` shape
checks per A6) and the standard library (`threading`, `dataclasses`, `enum`) — it does not import
`ingestion.envelope.model` (REQ-001), per Assumption A3.

## 2. Data model

| Type | Field | Type | Notes |
|---|---|---|---|
| `Status` (enum) | — | — | `pending \| analyzing \| chunking \| embedding \| indexing \| indexed \| failed` |
| `LedgerRow` | `doc_id` | `str` | PK, 64-char lowercase hex, validated via `validate_id_format` (REQ-002) |
| | `status` | `Status` | current FSM state |
| | `stage` | `Status` | mirror of `status` — see Assumption A1 |
| | `attempts` | `int` | `>= 0`; semantics per Assumption A4; **no ledger-enforced upper cap** — see §4 |
| | `last_error_code` | `str \| None` | cleared/overwritten per Assumption A5 |
| | `last_error_message` | `str \| None` | cleared/overwritten per Assumption A5 |
| | `config_version` | `str` | from `EnvelopeSummary`; set once at `create()`, immutable |
| | `ingested_at` | `datetime` | UTC; set once at `create()` only (AC1, AC5) |
| | `updated_at` | `datetime` | UTC; set on every write (AC5) |
| `EnvelopeSummary` | `config_version` | `str` | only field persisted into `LedgerRow` — Assumption A3 |
| `LedgerTransitionInvalid` | `code` | `str` | `"LEDGER_TRANSITION_INVALID"` (fixed) |
| | `classification` | `Literal["PERMANENT"]` | fixed at the exception-class level — see §1/§4 for caller-side triage before treating as a document failure |
| | `doc_id` | `str` | constructor param |
| | `reason` | `str` | constructor param, human-readable |
| | `observed_row` | `LedgerRow \| None` | current row at raise time; `None` only for F2 |
| | `attempted_to_status` | `Status` | caller's own original `to_status` |
| | `attempted_stage` | `Status` | caller's own original `stage` |

`InMemoryLedgerStore`'s internal storage (`dict[str, LedgerRow]` + `threading.RLock`) is a private
implementation detail, not part of the public data model. `_FORWARD_ORDER` (§1) is likewise a
private module constant, not part of the public data model.

## 3. Sequence

**Happy path**
1. Arrival adapter (out of scope, REQ-001/REQ-002) validates an `ArrivalEnvelope` and derives
   `doc_id`.
2. Coordinator calls `store.create(doc_id, EnvelopeSummary(config_version=...))`. No row exists
   yet → a new row is written: `status = stage = PENDING`, `attempts = 0`,
   `ingested_at = updated_at = now()`.
3. The same `create()` call is redelivered (at-least-once). The row already exists → returned
   unchanged; `ingested_at` is not overwritten (AC1).
4. Analyzer stage begins: coordinator calls `transition(doc_id, to_status=ANALYZING,
   stage=ANALYZING)`. `validate_transition` confirms `PENDING → ANALYZING` is in
   `_LEGAL_TRANSITIONS[PENDING]` and `stage == to_status`; new row: `status = stage = ANALYZING`,
   `attempts = 1` (A4), `last_error_code/message = None` (A5), `updated_at` refreshed.
5. The analyzer hits a transient error before it can hand off; the queue message redelivers.
   Coordinator re-calls `transition(doc_id, to_status=ANALYZING, stage=ANALYZING)` — a same-status
   self-transition (A2). `validate_transition` permits it (`ANALYZING` is non-terminal);
   `attempts` increments to `2`; `error_code`/`error_message`, if supplied, are recorded (A5).
6. Analyzer succeeds: `transition(doc_id, to_status=CHUNKING, stage=CHUNKING)` — legal forward
   edge; `attempts` resets to `1`; error fields clear.
7. Steps analogous to 4–6 repeat through `EMBEDDING`, `INDEXING`.
8. `transition(doc_id, to_status=INDEXED, stage=INDEXED)` — legal (`INDEXING → INDEXED`); row is
   now terminal; no further `transition()` call on this `doc_id` succeeds.
9. Any component calls `get(doc_id)` at any point to inspect current state; an operator/monitor
   calls `list_by_status(Status.INDEXED, limit=...)` to page through recently indexed documents,
   most-recently-updated first (AC7).

**Failure paths** (all raise `LedgerTransitionInvalid(code="LEDGER_TRANSITION_INVALID",
classification="PERMANENT")` unless noted — see F7 for the required caller-side triage before
any of these is treated as a document failure)
- F1. `transition()` called with `stage != to_status` → rejected (A1's mirrored-pair invariant).
- F2. `transition()` called for a `doc_id` with no prior `create()` row → rejected ("unknown
  doc_id"); no row is created as a side effect; `observed_row=None`.
- F3. `to_status` is not in `_LEGAL_TRANSITIONS[current.status]` and `to_status != current.status`
  → rejected ("illegal transition from X to Y").
- F4. `transition()` called on a row already `INDEXED` or `FAILED` → rejected ("no transitions out
  of terminal status"), including an attempted same-status self-transition on a terminal row —
  terminal rows accept no self-transition either (A2).
- F5. `transition()` to `FAILED` with `error_code=None` → rejected ("error_code required for
  failed transition", AC3).
- F6. `create()` or `transition()` called with a `doc_id` that fails
  `ingestion.identity.derive.validate_id_format` (wrong length, non-hex, uppercase, `None`,
  non-`str`) → `IdentityInvalid` (REQ-002, `code="IDENTITY_INVALID"`, PERMANENT) propagates
  un-wrapped — cross-module, see §4/§5.
- F7. Concurrent `transition()` calls race for the same `doc_id` on `InMemoryLedgerStore` →
  serialized by the store's internal `RLock`; never a torn/partial write (AC6). The losing
  caller's request is then evaluated as an ordinary F1/F3/F4/F5 legality check against the
  winner's already-applied state; if illegal, `LedgerTransitionInvalid` is raised with
  `observed_row` = the winner's now-current row and `attempted_to_status`/`attempted_stage` = the
  loser's own original request (§1). Per Contract #2's at-least-once-delivery assumption, this is
  an **expected, often-benign** outcome, not automatically a real failure — a coordinator MUST NOT
  treat every `LedgerTransitionInvalid` raised here as "mark document failed" without first
  triaging it:
  1. Call `is_benign_concurrent_loss(exc.observed_row, exc.attempted_to_status,
     exc.attempted_stage)`. If **True** — the winner already applied the identical edge this
     caller wanted, has since advanced the document even further along the same linear path
     (late redelivery), OR — the `attempted_to_status == Status.FAILED` special case, §1, fixed in
     design-review Round 2 — the document already reached `INDEXED` via a concurrent winner while
     this caller was racing to mark it `FAILED` — the coordinator acks the triggering message and
     takes no further action; the document is progressing, or has already progressed, correctly,
     and no ledger write was lost.
  2. Else, if `exc.observed_row is not None and exc.observed_row.status == Status.FAILED` — the
     document has already been terminally failed via a different concurrent path. This applies
     **regardless of whether this caller was itself attempting `FAILED` or something else** — a
     losing caller that was itself racing to independently mark `FAILED` while a different
     concurrent path already did so is exactly as benign an outcome as any other case here (see
     §1 step 5, which deliberately does not report it as "True" from
     `is_benign_concurrent_loss`, precisely so it is caught here, uniformly, instead). The
     coordinator acks (re-processing a terminal `doc_id` can never succeed regardless of who
     caused it) but does not conflate this with case 1: a real failure occurred and remains
     visible via `exc.observed_row.last_error_code`.
  3. Otherwise — `is_benign_concurrent_loss` returned False and the observed state is not
     `FAILED` — the exception reflects a genuine caller/coordinator bug: wrong stage order, stale
     `doc_id`, stage/status mismatch, or — the scenario that motivated `is_benign_concurrent_loss`'s
     `Status.FAILED` special case, §1 — a caller racing to mark `FAILED` while the observed row is
     still non-terminal/mid-pipeline, an unresolved conflict rather than legitimate concurrent
     progress. Handled per Contract #4's literal PERMANENT semantics: mark the document `failed`
     (via a fresh `transition(..., to_status=FAILED, error_code="LEDGER_TRANSITION_INVALID", ...)`
     call, itself subject to the same F1–F7 handling), ack, never retry.
- F8. `list_by_status()` called with `limit <= 0` → returns `[]`; does not raise (boundary
  condition, not a taxonomy error).

## 4. Contract compliance

- **Arrival Envelope**: `create()` touches only `doc_id` and a minimal `EnvelopeSummary`
  (`config_version`, Assumption A3) — no raw envelope or source-specific field ever crosses into
  this module, satisfying "no component reads raw source-specific formats past the arrival
  boundary"; `ingestion.envelope.model` is never imported here.
- **Identity & Idempotency**: `doc_id` shape is validated by reusing (not reimplementing)
  `ingestion.identity.derive.validate_id_format` (REQ-002) in `create()`/`transition()` (A6);
  `create()` is idempotent per AC1 — a repeat call under at-least-once delivery returns the
  existing row unchanged; every `transition()` write is a full-row replace keyed on `doc_id`, and
  F1–F5's validation ensures a redelivered/repeated call either succeeds identically as a
  same-status retry (A2) or is rejected as a side-effect-free error subject to F7's triage (below)
  — never silently corrupting state under re-delivery, and never spuriously punishing a document
  for the redelivery Contract #2 itself declares is normal.
- **State Ledger: this module *is* Contract #3.** Exactly one `LedgerRow` per `doc_id`; the exact
  `pending → analyzing → chunking → embedding → indexing → indexed | failed` FSM from CLAUDE.md
  and the story's References table is enforced by `validate_transition` (not left to callers, per
  the story's explicit requirement); every transition records `stage`, `attempts`, `error_code`
  (via `last_error_code`), and `updated_at`, with no silent state — F1–F7 reject every
  unrecorded/ambiguous/racing transition attempt rather than allowing a silent no-op or a torn
  write, and F7 specifically is recorded and surfaced to the caller (via `observed_row`) rather
  than swallowed, even in the benign case.
- **Failure Taxonomy**: every failure in §3 raises `LedgerTransitionInvalid`, classified PERMANENT
  at the exception-class level — that classification itself is never weakened or made conditional,
  and "never blindly retry an illegal transition" always holds. What §3 F7 refines is a separate
  question the story/CLAUDE.md do not explicitly address: whether raising this exception should,
  as a side effect, cause the coordinator to mark the *document* `failed`. Under Contract #2's
  explicit at-least-once assumption, a losing caller in a legitimate race is not evidence of a
  caller/coordinator bug, so marking the document failed in that case would be a framework-induced
  false failure, not a taxonomy-correct outcome. This module therefore enriches
  `LedgerTransitionInvalid` with `observed_row`/`attempted_to_status`/`attempted_stage` (§1) and
  provides `is_benign_concurrent_loss()` — including its explicit, crash-free
  `attempted_to_status == Status.FAILED` handling, fixed in design-review Round 2 — so a catching
  coordinator can make the three-way call (benign progress / already-failed-elsewhere / genuine
  bug — F7) deterministically, from the exception alone, without a second racy `get(doc_id)`.
  `config/ledger.yaml` declares `LEDGER_TRANSITION_INVALID: PERMANENT` per house rules; this
  triage is coordinator-facing guidance layered on top of that fixed classification, not a new
  classification and not a new error code. The one cross-module exception (F6,
  `IdentityInvalid`/`IDENTITY_INVALID`) is already classified PERMANENT by REQ-002, so no
  unclassified/default-TRANSIENT case exists in this module. `InMemoryLedgerStore` makes no
  external call, so no TRANSIENT code originates here (matches the story's explicit "in-memory
  implementation... has no external cost"). Separately: the `attempts` counter (A4) has **no
  ledger-enforced cap** on same-status self-transition retries — enforcing Contract #4's "poison
  after max dequeue" is intentionally deferred to the coordinator/queue layer, which owns
  delivery-count tracking, not this module; this module only *records* how many attempts occurred
  (via `attempts`), it does not decide when to stop retrying.

## 5. Error codes

| Code | Classification | Trigger condition |
|---|---|---|
| `LEDGER_TRANSITION_INVALID` | PERMANENT | `stage != to_status` passed to `transition()` (F1) |
| `LEDGER_TRANSITION_INVALID` | PERMANENT | `transition()` called for a `doc_id` with no existing row (F2) |
| `LEDGER_TRANSITION_INVALID` | PERMANENT | `to_status` not legal from the row's current status and not a same-status retry (F3) |
| `LEDGER_TRANSITION_INVALID` | PERMANENT | `transition()` called on a row already `INDEXED` or `FAILED` (F4) |
| `LEDGER_TRANSITION_INVALID` | PERMANENT | `to_status == FAILED` with `error_code` omitted (F5) |
| `IDENTITY_INVALID` (cross-module, `ingestion/identity/derive.py`) | PERMANENT | `doc_id` passed to `create()`/`transition()` fails `validate_id_format` (F6) |

`F7` (concurrent loss) is **not** a distinct trigger/code — a losing caller's request surfaces as
one of F1/F3/F4/F5 above, evaluated against the winner's already-applied state. The classification
of `LEDGER_TRANSITION_INVALID` itself does not change under F7; what changes is that the raising
module now hands the catching coordinator everything (`observed_row`,
`attempted_to_status`/`attempted_stage`) needed to run `is_benign_concurrent_loss()` (§1) and
decide whether this particular raise should also mark the *document* `failed`, per the three-way
triage in §3 F7 / §4, before applying Contract #4's literal "PERMANENT → mark failed" action. This
includes the `attempted_to_status == Status.FAILED` sub-case fixed in design-review Round 2 (§1).

No TRANSIENT codes originate in this module. Future backend adapters (Azure Table Storage /
Cosmos DB, Phase 2, out of scope here) will need to declare their own TRANSIENT codes for
network/throttling failures at that time, per house rules ("every module declares its error codes
in config").

## 6. Config surface

New file `config/ledger.yaml`:

```yaml
ledger:
  error_codes:
    LEDGER_TRANSITION_INVALID: PERMANENT
  default_list_limit: 100     # operational tunable — kept in sync with the code default, see below
  legal_transitions:          # documentation/audit only — safety invariant, code-authoritative, see below
    pending: [analyzing, failed]
    analyzing: [chunking, failed]
    chunking: [embedding, failed]
    embedding: [indexing, failed]
    indexing: [indexed, failed]
    indexed: []
    failed: []
```

These two keys are **not** the same kind of "config-only" carve-out, and this LLD deliberately
does not bundle them under one justification:

- **`legal_transitions`** matches `config/envelope.yaml`/`config/identity.yaml`'s established
  precedent exactly: it is listed for documentation/audit visibility only and is never read at
  runtime to change behavior. The authoritative FSM lives in `_LEGAL_TRANSITIONS` and
  `validate_transition` in `ingestion/ledger/store.py`. This is a genuine safety-invariant
  exception to "all tunables from config": the transition table is Contract #3's safety-critical
  invariant — a config edit alone must never be able to loosen which stage transitions are
  legal, since that would silently permit corrupt/out-of-order pipeline state. Changing the FSM
  requires a code change and a new LLD/PR.
- **`default_list_limit` is a plain operational tunable with no idempotency or safety
  implication** — pagination page size has no bearing on Contract #2/#3 correctness — so it does
  **not** share `legal_transitions`'s justification. In this LLD it is expressed as the literal
  default (`limit: int = 100`) on `LedgerStore.list_by_status` / on
  `InMemoryLedgerStore.__init__`'s `default_list_limit` parameter (§1), matching the story's given
  signature exactly rather than changing it. `config/ledger.yaml`'s `default_list_limit` is the
  documented source of truth for that literal, and the two are required to be kept in sync by a
  dedicated test (`test_config_default_list_limit_matches_code_default`, §7) rather than by a
  runtime config-load, because **no config-loading mechanism exists anywhere in this codebase
  yet** — REQ-001/REQ-002's YAMLs are equally documentation-only today (see `config/envelope.yaml`,
  `config/identity.yaml`), and introducing the first one is out of scope for this LLD. Unlike
  `legal_transitions`, nothing about Contract #3 blocks a future revision from having
  `InMemoryLedgerStore`'s composition root read `config/ledger.yaml` and pass
  `default_list_limit` in directly (the constructor parameter already exists for exactly this,
  §1) — this is a scoping simplification, not a safety exception.

## 7. Test plan

- `test_create_new_doc_returns_pending_row` — fresh `doc_id` → `status == stage == PENDING`,
  `attempts == 0`, `ingested_at == updated_at`.
- `test_create_idempotent_returns_same_row_same_ingested_at` — two `create()` calls, same
  `doc_id` → identical row returned both times, `ingested_at` unchanged (AC1).
- `test_create_idempotent_ignores_new_envelope_summary_on_repeat` — second `create()` call with a
  different `config_version` does not overwrite the first row's `config_version`.
- `test_transition_each_legal_edge_in_table` — parametrized over every `(from, to)` pair in
  `_LEGAL_TRANSITIONS` → succeeds, row's `status`/`stage` updated to `to`.
- `test_transition_illegal_edge_rejected` — parametrized over every `(from, to)` pair *not* in the
  table and `!= from` → `LedgerTransitionInvalid` PERMANENT (F3).
- `test_transition_same_status_retry_legal_for_non_terminal` — `to_status == current.status` for
  each of `pending/analyzing/chunking/embedding/indexing` → succeeds (A2).
- `test_transition_same_status_retry_rejected_for_terminal` — `to_status == current.status` for
  `indexed` and for `failed` → `LedgerTransitionInvalid` PERMANENT (F4).
- `test_transition_from_terminal_indexed_rejected` — any `to_status` from an `INDEXED` row →
  `LedgerTransitionInvalid` PERMANENT (F4).
- `test_transition_from_terminal_failed_rejected` — any `to_status` from a `FAILED` row →
  `LedgerTransitionInvalid` PERMANENT (F4).
- `test_transition_to_failed_without_error_code_rejected` — `error_code=None` → PERMANENT (AC3, F5).
- `test_transition_to_failed_with_error_code_succeeds_and_stores_it` — row's `last_error_code`/
  `last_error_message` set as given.
- `test_transition_stage_mismatch_rejected` — `stage != to_status` → PERMANENT (F1).
- `test_transition_unknown_doc_id_rejected` — no prior `create()` → PERMANENT (F2),
  `exc.observed_row is None`.
- `test_attempts_increment_on_same_stage_retry` — self-transition increments `attempts` by 1 each
  call (AC4, A2).
- `test_attempts_reset_on_advance_to_new_stage` — forward legal transition resets `attempts` to 1
  (A4).
- `test_attempts_frozen_at_failed_value` — `attempts` on the row after transitioning to `FAILED`
  equals the value it held immediately before (not incremented, not reset).
- `test_attempts_has_no_enforced_upper_cap` — many consecutive same-status retries all succeed and
  keep incrementing `attempts`; the store never itself raises/blocks based on `attempts` magnitude
  (§4 — poison threshold is out of scope for this module).
- `test_updated_at_set_on_every_transition_ingested_at_only_on_create` — `ingested_at` constant
  across all transitions; `updated_at` changes on each (AC5).
- `test_last_error_cleared_on_clean_forward_advance` — a row with a stale `last_error_code` from a
  prior retry (A2) has it cleared to `None` after a clean forward `transition()` with no
  `error_code` (A5).
- `test_get_unknown_doc_id_returns_none_no_raise` — including a malformed-shape string (AC8, A6).
- `test_get_known_doc_id_returns_row`.
- `test_list_by_status_orders_by_updated_at_desc` (AC7).
- `test_list_by_status_respects_limit` (AC7).
- `test_list_by_status_filters_correct_status_only` — rows of other statuses excluded.
- `test_list_by_status_empty_when_no_matches` — including `limit <= 0` → `[]`, no raise (F8).
- `test_create_rejects_malformed_doc_id` — delegates to `validate_id_format`; wrong length,
  non-hex, uppercase, `None`, non-`str` → `IdentityInvalid` PERMANENT (F6, A6).
- `test_transition_rejects_malformed_doc_id` — same, via `transition()`.
- `test_thread_safety_concurrent_transitions_consistent_final_state` — N threads racing legal
  transitions on the same `doc_id` → exactly one sequence of writes is observed; final row is a
  reachable, internally-consistent state (`status == stage`, `attempts` matches the applied
  sequence); no lost updates or torn writes (AC6, F7).
- `test_thread_safety_concurrent_create_same_doc_id` — N threads calling `create()` concurrently
  for the same new `doc_id` → exactly one row created, all callers receive rows with identical
  `ingested_at`.
- `test_concurrent_loss_exception_carries_observed_row_and_attempted_request` — two threads race
  the identical `transition(doc_id, to_status=X, stage=X)`; the losing thread's caught
  `LedgerTransitionInvalid` has `observed_row.status == X` (the winner's applied state) and
  `attempted_to_status == attempted_stage == X` (F7).
- `test_is_benign_concurrent_loss_true_when_observed_equals_attempted` — `observed_row.status ==
  attempted_to_status`, both non-`FAILED` → `True`.
- `test_is_benign_concurrent_loss_true_when_observed_further_along_same_path` —
  `observed_row.status` later in `_FORWARD_ORDER` than `attempted_to_status` (both non-`FAILED`)
  → `True`.
- `test_is_benign_concurrent_loss_false_when_observed_row_is_none` — F2 case → `False`.
- `test_is_benign_concurrent_loss_false_when_observed_earlier_or_off_path` — `observed_row.status`
  earlier than `attempted_to_status` (both non-`FAILED`), or `stage != to_status` in the original
  request → `False`.
- `test_is_benign_concurrent_loss_false_when_attempted_non_failed_and_observed_failed` —
  `attempted_to_status` is any non-`FAILED` status, `observed_row.status == Status.FAILED` →
  `False` (step 5), paired with the F7-step-2 caller guidance that treats this as
  already-failed-elsewhere rather than a real bug.
- `test_is_benign_concurrent_loss_true_when_attempted_failed_and_observed_indexed` — **(Round 2,
  regression for the fixed bug)** `attempted_to_status = attempted_stage = Status.FAILED`,
  `observed_row.status == Status.INDEXED` → returns `True` without raising `ValueError` (§1
  step 4).
- `test_is_benign_concurrent_loss_false_when_attempted_failed_and_observed_mid_pipeline` —
  **(Round 2)** `attempted_to_status = attempted_stage = Status.FAILED`, `observed_row.status` one
  of `ANALYZING`/`CHUNKING`/`EMBEDDING`/`INDEXING` (parametrized) → returns `False` without
  raising (§1 step 4 — genuine-conflict branch, handled as a real bug per F7 step 3).
- `test_is_benign_concurrent_loss_false_when_attempted_failed_and_observed_failed` — **(Round 2)**
  `attempted_to_status = attempted_stage = Status.FAILED`, `observed_row.status == Status.FAILED`
  → returns `False` without raising (§1 step 4), paired with F7 step 2 independently catching this
  exact case (regardless of what the caller attempted) and treating it as already-resolved, not a
  bug.
- `test_is_benign_concurrent_loss_never_raises` — **(Round 2, the primary regression test for the
  fixed crash)** parametrized/hypothesis-driven over every `(attempted_to_status,
  observed_row.status)` pair across all seven `Status` values, plus `observed_row=None` and
  `attempted_stage != attempted_to_status`, asserting the helper always returns a `bool` and never
  raises any exception, `ValueError` in particular.
- `test_config_default_list_limit_matches_code_default` — parses `config/ledger.yaml`'s
  `ledger.default_list_limit` and asserts it equals `inspect.signature(LedgerStore.list_by_status)
  .parameters["limit"].default` and `InMemoryLedgerStore().__init__`'s own default — keeps the
  two in sync per §6.
- `test_property_legal_transition_sequences_end_consistent` (hypothesis) — random walks through
  the legal graph (forward edges + same-status retries) starting from `pending`, of varying
  length, always leave the row in a state matching the last-applied edge, with `attempts`/
  `status`/`stage` mutually consistent per A1/A4 (property test required by the story).
- `test_property_illegal_edges_never_silently_applied` (hypothesis) — for randomly sampled
  `(current_status, to_status)` pairs outside the legal set (and `!=`), `transition()` always
  raises and the row is provably unmutated (compared before/after).

## 8. Budget

- p95 latency added per document: < 5ms per `transition()` call (in-memory, matches the story's
  explicit budget); a full document lifecycle (`create` + 5 forward transitions:
  analyzing/chunking/embedding/indexing/indexed) is ≤ 6 ledger writes, so < 30ms p95 total ledger
  overhead per successfully-indexed document, excluding any same-status retries (A2), each adding
  another ≤ 5ms. `is_benign_concurrent_loss()` is an in-process, allocation-free comparison
  (§1) — negligible (<< 1ms) added latency on the caught-exception path, not on the happy path.
- Cost per document: $0 / 0 tokens — `InMemoryLedgerStore` is pure in-process dict access, no
  external API or network call.
- No cap is enforced by this module on retry attempts (§4); poison-queue thresholds and any
  latency/cost impact of unbounded retries are a coordinator/queue-layer budget concern, out of
  scope for this module's own budget.
