# REQ-003 — State Ledger — LLD

**Story:** docs/stories/REQ-003.md · **Phase:** 1 · **Pipeline:** full

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
  overwritten if `error_code`/`error_message` are supplied on a self-transition retry (A2), so an
  in-progress stage's last transient failure remains visible via `get()` even though the row
  hasn't reached `failed`.
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
    PERMANENT — an illegal transition indicates a caller/coordinator bug (wrong stage
    order, missing error_code, stale doc_id), never a transient condition."""
    code: str = "LEDGER_TRANSITION_INVALID"
    classification: Literal["PERMANENT"] = "PERMANENT"

    def __init__(self, doc_id: str, reason: str) -> None:
        """code and classification are fixed by this class, not caller-supplied."""
        self.doc_id = doc_id
        self.reason = reason
        super().__init__(f"{doc_id}: {reason}")


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
    No I/O, no mutation. Raises LedgerTransitionInvalid; returns None on success.
    Checks, in order: current is not None (F2); stage == to_status (F1); current.status
    is not terminal (F4); to_status is in _LEGAL_TRANSITIONS[current.status] or
    to_status == current.status (A2) (F3); if to_status == Status.FAILED, error_code is
    not None (F5)."""
    ...


class LedgerStore(ABC):
    """Persistence port for Contract #3. Concrete backends implement all four methods,
    each calling validate_transition() before any write."""

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
        """Calls validate_transition() first, then writes and returns the new row.
        Raises LedgerTransitionInvalid (PERMANENT, §3/§5) or, cross-module,
        IdentityInvalid (PERMANENT, §5) if doc_id is malformed."""
        ...

    @abstractmethod
    def get(self, doc_id: str) -> LedgerRow | None:
        """Returns None for an unknown doc_id; never raises (AC8). Does not validate
        doc_id shape — see Assumption A6."""
        ...

    @abstractmethod
    def list_by_status(self, status: Status, limit: int = 100) -> list[LedgerRow]:
        """Rows with the given status, ordered by updated_at descending, capped at
        limit (AC7). limit <= 0 returns an empty list; never raises."""
        ...


class InMemoryLedgerStore(LedgerStore):
    """Thread-safe, fully-functional LedgerStore for tests and local dev. Backed by a
    single dict[str, LedgerRow] guarded by one threading.RLock held for the full
    duration of each public method (coarse-grained; correct-under-concurrency, not
    throughput-optimized — acceptable given the in-memory, test/dev-only scope and the
    <5ms budget, §8)."""

    def __init__(self) -> None: ...

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
rather than reimplementing the FSM). This module depends only on
`ingestion.identity.derive.validate_id_format` (REQ-002, for `doc_id` shape checks per A6) and the
standard library (`threading`, `dataclasses`, `enum`) — it does not import
`ingestion.envelope.model` (REQ-001), per Assumption A3.

## 2. Data model

| Type | Field | Type | Notes |
|---|---|---|---|
| `Status` (enum) | — | — | `pending \| analyzing \| chunking \| embedding \| indexing \| indexed \| failed` |
| `LedgerRow` | `doc_id` | `str` | PK, 64-char lowercase hex, validated via `validate_id_format` (REQ-002) |
| | `status` | `Status` | current FSM state |
| | `stage` | `Status` | mirror of `status` — see Assumption A1 |
| | `attempts` | `int` | `>= 0`; semantics per Assumption A4 |
| | `last_error_code` | `str \| None` | cleared/overwritten per Assumption A5 |
| | `last_error_message` | `str \| None` | cleared/overwritten per Assumption A5 |
| | `config_version` | `str` | from `EnvelopeSummary`; set once at `create()`, immutable |
| | `ingested_at` | `datetime` | UTC; set once at `create()` only (AC1, AC5) |
| | `updated_at` | `datetime` | UTC; set on every write (AC5) |
| `EnvelopeSummary` | `config_version` | `str` | only field persisted into `LedgerRow` — Assumption A3 |
| `LedgerTransitionInvalid` | `code` | `str` | `"LEDGER_TRANSITION_INVALID"` (fixed) |
| | `classification` | `Literal["PERMANENT"]` | fixed |
| | `doc_id` | `str` | constructor param |
| | `reason` | `str` | constructor param, human-readable |

`InMemoryLedgerStore`'s internal storage (`dict[str, LedgerRow]` + `threading.RLock`) is a private
implementation detail, not part of the public data model.

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

**Failure paths** (all resolve to `LedgerTransitionInvalid(code="LEDGER_TRANSITION_INVALID",
classification="PERMANENT")` unless noted)
- F1. `transition()` called with `stage != to_status` → rejected (A1's mirrored-pair invariant).
- F2. `transition()` called for a `doc_id` with no prior `create()` row → rejected ("unknown
  doc_id"); no row is created as a side effect.
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
  serialized by the store's internal `RLock`; the losing caller observes the winner's
  already-applied status and its own `to_status` is then evaluated as an ordinary F3/F4 legality
  check against that new current state — never a torn/partial write (AC6).
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
  same-status retry (A2) or is rejected as a side-effect-free PERMANENT error — never silently
  corrupting state under re-delivery.
- **State Ledger: this module *is* Contract #3.** Exactly one `LedgerRow` per `doc_id`; the exact
  `pending → analyzing → chunking → embedding → indexing → indexed | failed` FSM from CLAUDE.md
  and the story's References table is enforced by `validate_transition` (not left to callers, per
  the story's explicit requirement); every transition records `stage`, `attempts`, `error_code`
  (via `last_error_code`), and `updated_at`, with no silent state (F1–F5 reject every
  unrecorded/ambiguous transition attempt rather than allowing a no-op write).
- **Failure Taxonomy**: every failure in §3 resolves to `LedgerTransitionInvalid`, classified
  PERMANENT — an illegal transition, stage/status mismatch, or missing `error_code` on `failed` is
  a caller/coordinator bug, never worth blind retry; `config/ledger.yaml` declares this code per
  house rules. The one cross-module exception (F6, `IdentityInvalid`/`IDENTITY_INVALID`) is
  already classified PERMANENT by REQ-002, so no unclassified/default-TRANSIENT case exists in
  this module. `InMemoryLedgerStore` makes no external call, so no TRANSIENT code originates here
  (matches the story's explicit "in-memory implementation... has no external cost").

## 5. Error codes

| Code | Classification | Trigger condition |
|---|---|---|
| `LEDGER_TRANSITION_INVALID` | PERMANENT | `stage != to_status` passed to `transition()` (F1) |
| `LEDGER_TRANSITION_INVALID` | PERMANENT | `transition()` called for a `doc_id` with no existing row (F2) |
| `LEDGER_TRANSITION_INVALID` | PERMANENT | `to_status` not legal from the row's current status and not a same-status retry (F3) |
| `LEDGER_TRANSITION_INVALID` | PERMANENT | `transition()` called on a row already `INDEXED` or `FAILED` (F4) |
| `LEDGER_TRANSITION_INVALID` | PERMANENT | `to_status == FAILED` with `error_code` omitted (F5) |
| `IDENTITY_INVALID` (cross-module, `ingestion/identity/derive.py`) | PERMANENT | `doc_id` passed to `create()`/`transition()` fails `validate_id_format` (F6) |

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
  default_list_limit: 100     # documentation/audit only — see below
  legal_transitions:          # documentation/audit only — see below
    pending: [analyzing, failed]
    analyzing: [chunking, failed]
    chunking: [embedding, failed]
    embedding: [indexing, failed]
    indexing: [indexed, failed]
    indexed: []
    failed: []
```

As with `config/envelope.yaml` and `config/identity.yaml`, `default_list_limit` and
`legal_transitions` are listed for documentation/audit visibility only; neither is read at runtime
to change behavior. The authoritative default lives in `list_by_status`'s `limit: int = 100`
parameter (part of the documented public interface, §1); the authoritative FSM lives in
`_LEGAL_TRANSITIONS` and `validate_transition` in `ingestion/ledger/store.py`. This is deliberate,
matching REQ-001/REQ-002's precedent: the transition table is Contract #3's safety-critical
invariant — a config edit alone must never be able to loosen which stage transitions are
considered legal, since that would silently permit corrupt/out-of-order pipeline state. Changing
the FSM requires a code change and a new LLD/PR.

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
- `test_transition_unknown_doc_id_rejected` — no prior `create()` → PERMANENT (F2).
- `test_attempts_increment_on_same_stage_retry` — self-transition increments `attempts` by 1 each
  call (AC4, A2).
- `test_attempts_reset_on_advance_to_new_stage` — forward legal transition resets `attempts` to 1
  (A4).
- `test_attempts_frozen_at_failed_value` — `attempts` on the row after transitioning to `FAILED`
  equals the value it held immediately before (not incremented, not reset).
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
  another ≤ 5ms.
- Cost per document: $0 / 0 tokens — `InMemoryLedgerStore` is pure in-process dict access, no
  external API or network call.
