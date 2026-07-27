# REQ-004 — Failure Taxonomy — LLD

**Story:** docs/stories/REQ-004.md · **Phase:** 1 · **Pipeline:** full

## Assumptions (non-blocking, flagged per house rules)
The story specifies `Severity`, `ErrorCode`, `ErrorRegistry`, the three free functions, and
`RaggedError` fully enough to design against, but leaves several details unstated. Following the
precedent set by REQ-001/002/003, these are treated as scoped design decisions, documented here,
not open questions that block the LLD:

- **A1 — `error_code` (story) vs. `code` (REQ-001/002/003, already merged).** The story names
  `RaggedError`'s carried attribute `error_code: str`; the three already-implemented exception
  classes (`EnvelopeValidationError`, `IdentityInvalid`, `LedgerTransitionInvalid`) each already
  expose a fixed class attribute named `code` (not `error_code`), and the migration note requires
  this to be a **non-breaking** change ("the concrete classes stay, they just gain a base class").
  This LLD does not rename `code` on any existing class. Instead, `RaggedError.__init__(message,
  *, error_code)` sets `self.error_code = error_code`, and each retrofitted subclass's
  constructor is updated to call `super().__init__(message, error_code=self.code)` — so every
  instance ends up with **both** `.code` (existing, unchanged, class-level) and `.error_code`
  (new, story-mandated, instance-level, always equal to `.code`) populated. AC5 ("existing errors
  ... carry their error_code") is satisfied literally without touching any existing public
  attribute name.
- **A2 — `ErrorRegistry` is instantiable, not the module's only registry.** The story's three free
  functions (`register_error`, `classify`, `all_codes`) imply one process-wide catalog that every
  module registers into at import time. This LLD backs those free functions with a private
  module-level singleton (`_DEFAULT_REGISTRY`), but keeps `ErrorRegistry` itself a plain
  instantiable class so unit tests of registry *behavior* (duplicate rejection, thread safety,
  unknown-code fallback, AC2/AC3/AC7) can construct isolated `ErrorRegistry()` instances rather
  than mutating global process state shared with every other test in the suite (see §7).
- **A3 — malformed `register_error()` inputs (not just duplicates) are rejected, not silently
  accepted.** The story's AC2 only specifies duplicate-code rejection, but a registry that
  silently accepted an empty `code`, a non-`Severity` `severity`, or an empty `description` would
  corrupt `all_codes()` for every consumer (house rules: "type hints everywhere," and the pattern
  established by every prior module of validating every input at its boundary — REQ-001's
  `_non_empty` field validators, REQ-002's `_validate_non_empty_str`). This LLD rejects all such
  inputs (F2–F4 below) with the same exception type used for duplicates, differentiated only by
  `.reason` — mirroring REQ-003's `LedgerTransitionInvalid` precedent of one code, several
  distinguishable triggers, rather than minting a new code per input-shape problem.
- **A4 — no `is_benign_concurrent_loss()`-style triage exists for this module, by design.** The
  task context flags REQ-003's two design-review rounds over PERMANENT-vs-concurrency semantics
  as territory to get right the first time here. This LLD deliberately does **not** add an
  equivalent helper, because the underlying situation is not analogous:
  - REQ-003's ledger races are *document-scoped, redelivery-driven*: under Contract #2's
    at-least-once assumption, two workers can legitimately race to advance the *same `doc_id`*
    through the *same* legal edge, and the loser's failure is an artifact of redelivery, not a
    bug — hence a triage helper was necessary to avoid spuriously failing a document that is
    actually progressing correctly.
  - This module's registrations are *process-scoped, import-time, declare-once* operations. Each
    error code is registered by exactly one module's top-level code, which Python's import
    machinery guarantees executes **at most once per process** (modules are cached in
    `sys.modules`; re-importing an already-imported module does not re-run its top-level
    statements). There is no "redelivery" of a module import, and no legitimate scenario in which
    two call sites are both *correctly* racing to register the identical code string — a
    collision is always either an accidental double-registration bug within one module, or two
    different modules colliding on the same code string by mistake. Both are code bugs to be
    fixed, not benign concurrent progress. See §3 F7 for the explicit reasoning this LLD applies
    at the one raise site where this question could otherwise recur. **This at-most-once
    guarantee holds under standard Python `import` semantics only:** `importlib.reload()` of a
    module containing `register_error()` calls at module scope is **NOT supported** and may raise
    a spurious `ErrorCodeRegistrationInvalid` (duplicate-registration) on an otherwise benign
    reload, since `reload()` re-executes the module's top-level code without clearing any prior
    registrations already recorded against `_DEFAULT_REGISTRY`.
  - Consequently, there is also no analog of REQ-003's Assumption A2 ("same-status retry" as a
    legal self-transition): a module calling `register_error` twice for its own code is always a
    bug (e.g. a copy-paste error), never a legitimate retry, for the same reason above.
- **A5 — PERMANENT-on-duplicate-registration is justified primarily by the story's AC2, not by a
  claim that Contract #2 doesn't apply here.** The story is explicit: "Duplicate registration of
  the same code raises `RaggedError` classified PERMANENT (registry is immutable after first
  registration)" (AC2) — this LLD implements that acceptance criterion directly, as the primary
  justification for `ErrorCodeRegistrationInvalid`/PERMANENT on F1 (§3, §5). CLAUDE.md's Contract
  #2 ("every operation must be safely re-runnable... at-least-once delivery is assumed
  everywhere") is left exactly as CLAUDE.md states it, at its full stated scope — this LLD does
  not reinterpret or narrow that language to argue it excludes registration calls. Both hold
  without conflict: AC2 mandates immutable-after-first-registration behavior for this module
  regardless of how Contract #2's general re-runnability language is read, and in practice no
  tension arises because, under standard Python import semantics (A4), a genuine "re-run" of the
  same `register_error()` call never actually happens during normal operation.
- **A6 — `RaggedError.severity` convenience property.** Beyond the story's literal ask, this LLD
  adds a read-only `severity` property on `RaggedError` that calls `classify(self.error_code)` —
  directly serving the story's own framing of Contract #4 as "the decision layer for what do we do
  when something goes wrong," letting any catching coordinator ask `exc.severity` without a
  separate import. It changes no existing signature and adds no new public state (computed, not
  stored). **`.severity` always resolves against the module-level `_DEFAULT_REGISTRY`
  specifically — never against whatever `ErrorRegistry` instance (if any) actually raised the
  exception.** Concretely: a test that constructs an isolated `ErrorRegistry()` instance (per
  Assumption A2) and raises a `RaggedError` tied to that local registry will still see
  `.severity` resolve against the *global* `_DEFAULT_REGISTRY`, not the local one — which may
  disagree with, or not even contain, the code in question. Callers who need severity from a
  local/isolated registry must call `registry.classify(code)` directly on that instance instead
  of relying on `.severity`.
- **A7 — drift risk between existing classes' hardcoded `classification` attribute and the
  registry's `Severity`.** `EnvelopeValidationError.classification`, `IdentityInvalid.classification`,
  and `LedgerTransitionInvalid.classification` remain untouched, hardcoded `Literal["PERMANENT"]`
  class attributes (A1 — non-breaking). This creates two independent sources of truth for the same
  fact (the hardcoded literal, and `classify(cls.code)` against the registry) that could silently
  drift if either is edited without the other. Rather than unify them (which would touch existing
  public attributes, contradicting the migration note), this LLD closes the gap with a dedicated
  regression test (§7, `test_existing_classification_attr_matches_registry_severity`) — the same
  "add a test, not a code change" strategy REQ-002 used to guard `compute_doc_id`/`derive_doc_id`
  drift.

## 1. Interfaces

```python
# ingestion/errors/registry.py

class Severity(str, Enum):
    """PERMANENT | TRANSIENT — Contract #4's binary classification. Fixed, code-defined;
    the sole authority for accepted values (same pattern as Status (REQ-003), TrustTier
    (REQ-001))."""
    PERMANENT = "PERMANENT"
    TRANSIENT = "TRANSIENT"


@dataclass(frozen=True)
class ErrorCode:
    """One registered entry — code, its fixed severity, and a human-readable description,
    for observability/docs (AC6)."""
    code: str
    severity: Severity
    description: str


class RaggedError(Exception):
    """Base exception for every framework error (story: 'all framework errors inherit
    from it'). Carries error_code: str — see Assumption A1 for how this coexists with
    existing subclasses' pre-existing `code` class attribute."""

    def __init__(self, message: str, *, error_code: str) -> None:
        """error_code is always caller-supplied by the concrete subclass's own
        constructor (typically `self.code`, its own fixed class attribute) — RaggedError
        itself defines no fixed code, since it is a base class shared by every module."""
        self.error_code = error_code
        super().__init__(message)

    @property
    def severity(self) -> Severity:
        """Convenience accessor — always classify(self.error_code) against the
        module-level _DEFAULT_REGISTRY specifically, never against any local/isolated
        ErrorRegistry instance that may have raised this exception (Assumption A6). A
        RaggedError raised against a code registered only in a local ErrorRegistry()
        will see this property resolve via _DEFAULT_REGISTRY, not that local instance —
        callers needing local-registry severity must call registry.classify(code)
        directly on that instance instead. Never raises; unknown codes resolve to
        Severity.TRANSIENT with a logged warning, per classify()'s own contract."""
        return classify(self.error_code)


class ErrorCodeRegistrationInvalid(RaggedError):
    """Raised by ErrorRegistry.register() for any invalid registration call: a code
    already registered (AC2's 'duplicate registration'), or a malformed code/severity/
    description (Assumption A3). Always PERMANENT — every trigger is a caller/module bug
    at import time, never a transient condition (Assumption A4/A5)."""

    code: str = "ERROR_CODE_REGISTRATION_INVALID"
    classification: Literal["PERMANENT"] = "PERMANENT"

    def __init__(
        self,
        attempted_code: str,
        reason: str,
        *,
        existing: ErrorCode | None = None,
    ) -> None:
        """existing: the already-registered ErrorCode for attempted_code, populated only
        for the duplicate-registration trigger (F1); None for malformed-input triggers
        (F2-F4), where nothing was previously registered under that (invalid) code."""
        self.attempted_code = attempted_code
        self.reason = reason
        self.existing = existing
        super().__init__(f"{attempted_code}: {reason}", error_code=self.code)


class ErrorRegistry:
    """Thread-safe registry mapping error code -> ErrorCode. Immutable per entry once
    registered (AC2) — no update/remove method exists; the only mutation is register().
    Backing store: dict[str, ErrorCode] (insertion-ordered, per Python 3.7+ dict
    semantics) guarded by one threading.Lock held for the full duration of each public
    method (coarse-grained, correct-under-concurrency — same pattern as REQ-003's
    InMemoryLedgerStore; negligible contention expected, since registration only happens
    at import time, not per-document)."""

    def __init__(self) -> None:
        self._codes: dict[str, ErrorCode] = {}
        self._lock = threading.Lock()

    def register(self, code: str, severity: Severity, description: str) -> ErrorCode:
        """Registers a new error code. Checks, in order: (1) code is already present in
        this registry (AC2) — if so, raises ErrorCodeRegistrationInvalid(code,
        "duplicate registration", existing=<prior ErrorCode>) immediately, before any
        other input is inspected; (2) code is a non-empty str (Assumption A3); (3)
        severity is a Severity member; (4) description is a non-empty str. Each of
        checks 2-4 raises ErrorCodeRegistrationInvalid (PERMANENT) with existing=None on
        failure. This order is mandated, not incidental: a call supplying both an
        already-registered code AND an invalid severity/description raises the
        duplicate-registration error (check 1) — checks 2-4 never run against a code
        that is already present. Thread-safe: the presence check and the write happen
        atomically under self._lock, so two concurrent calls for the same code can never
        both succeed (AC7, §3 F7). Returns the newly-created ErrorCode on success."""
        ...

    def classify(self, code: str) -> Severity:
        """Looks up code's severity. Returns Severity.TRANSIENT and logs a warning
        (module-level logger, WARNING level, message includes the unknown code) if code
        is not registered (AC3) — never raises, for any str input including malformed
        shapes. This is the literal implementation of CLAUDE.md's 'unclassified errors
        default to TRANSIENT.'"""
        ...

    def all_codes(self) -> list[ErrorCode]:
        """Snapshot list of every registered ErrorCode, in registration order (AC6).
        Returns a new list on every call (no aliasing of internal state); empty registry
        returns []."""
        ...


_DEFAULT_REGISTRY: ErrorRegistry = ErrorRegistry()
"""Process-wide singleton every framework module registers its own codes into at import
time (story: 'a registry every module registers its error codes into at import time').
Unit tests exercising ErrorRegistry's own behavior MUST construct a fresh ErrorRegistry()
instance instead (Assumption A2) — see §7."""


def register_error(code: str, severity: Severity, description: str) -> ErrorCode:
    """Registers code into the process-wide default registry. See
    ErrorRegistry.register."""
    return _DEFAULT_REGISTRY.register(code, severity, description)


def classify(code: str) -> Severity:
    """Classifies code via the process-wide default registry. See
    ErrorRegistry.classify."""
    return _DEFAULT_REGISTRY.classify(code)


def all_codes() -> list[ErrorCode]:
    """Snapshot of every code registered in the process-wide default registry. See
    ErrorRegistry.all_codes."""
    return _DEFAULT_REGISTRY.all_codes()


# Bootstrap — this module registers its own error code into the same default registry
# it exposes, at import time, following the exact pattern every consumer module (§1a)
# uses for its own code. Not recursive: this is the first-ever registration call in a
# fresh process, so the registry is empty and this call always succeeds.
register_error(
    ErrorCodeRegistrationInvalid.code,
    Severity.PERMANENT,
    "an error code was registered with an invalid code/severity/description, or was "
    "already registered (registry is immutable after first registration)",
)
```

No adapter classes are introduced. `ErrorRegistry` is the whole persistence surface — an
in-process dict, no I/O, no external backend (matches the story's explicit zero-cost budget).

### 1a. Cross-module changes: retrofitting REQ-001/002/003 exception classes

Per the story's Migration note, this REQ also makes minimal, non-breaking edits to three already-
merged files. No public method/constructor signature changes on any of the three exception
classes; each gets exactly three edits: (1) new import, (2) base class, (3) `super().__init__`
call gains `error_code=self.code`, plus one new module-level `register_error(...)` call placed
immediately after the class definition.

**`ingestion/identity/derive.py`** (current content per §1 of this LLD's source review, lines
1–98):
```python
from ingestion.errors.registry import RaggedError, Severity, register_error


class IdentityInvalid(RaggedError):          # was: class IdentityInvalid(Exception):
    code: str = "IDENTITY_INVALID"
    classification: Literal["PERMANENT"] = "PERMANENT"

    def __init__(self, field_name: str, reason: str) -> None:
        self.field_name = field_name
        self.reason = reason
        super().__init__(f"{field_name}: {reason}", error_code=self.code)   # was: super().__init__(f"{field_name}: {reason}")


register_error(
    IdentityInvalid.code,
    Severity.PERMANENT,
    "malformed identity input to doc_id/chunk_id derivation (REQ-002)",
)
```

**`ingestion/envelope/model.py`**:
```python
from ingestion.errors.registry import RaggedError, Severity, register_error


class EnvelopeValidationError(RaggedError):   # was: class EnvelopeValidationError(Exception):
    code: str = "ENVELOPE_INVALID"
    classification: Literal["PERMANENT"] = "PERMANENT"

    def __init__(self, field_errors: list[dict[str, str]]) -> None:
        self.field_errors = field_errors
        summary = "; ".join(f"{e['field']}: {e['reason']}" for e in field_errors)
        super().__init__(f"{self.code}: {summary or 'invalid envelope'}", error_code=self.code)
        # was: super().__init__(f"{self.code}: {summary or 'invalid envelope'}")


register_error(
    EnvelopeValidationError.code,
    Severity.PERMANENT,
    "arrival envelope failed schema or doc_id cross-check validation (REQ-001)",
)
```

**`ingestion/ledger/store.py`**:
```python
from ingestion.errors.registry import RaggedError, Severity, register_error


class LedgerTransitionInvalid(RaggedError):   # was: class LedgerTransitionInvalid(Exception):
    code: str = "LEDGER_TRANSITION_INVALID"
    classification: Literal["PERMANENT"] = "PERMANENT"

    def __init__(self, doc_id, reason, *, observed_row, attempted_to_status, attempted_stage) -> None:
        self.doc_id = doc_id
        self.reason = reason
        self.observed_row = observed_row
        self.attempted_to_status = attempted_to_status
        self.attempted_stage = attempted_stage
        super().__init__(f"{doc_id}: {reason}", error_code=self.code)   # was: super().__init__(f"{doc_id}: {reason}")


register_error(
    LedgerTransitionInvalid.code,
    Severity.PERMANENT,
    "illegal or malformed ledger state transition, incl. benign concurrent races "
    "requiring caller-side is_benign_concurrent_loss() triage (REQ-003)",
)
```

Import-order note: `ingestion.envelope.model` already imports `ingestion.identity.derive`
(REQ-002), and `ingestion.ledger.store` already imports `ingestion.identity.derive` (for
`validate_id_format`, REQ-003) — so however a caller first imports any one of the three, Python's
import machinery resolves the full dependency graph and every module's top-level `register_error`
call runs exactly once, before any code that could observe `all_codes()`. No new import cycle is
introduced: `ingestion/errors/registry.py` imports nothing from `ingestion.envelope`,
`ingestion.identity`, or `ingestion.ledger`.

## 2. Data model

| Type | Field | Type | Notes |
|---|---|---|---|
| `Severity` (enum, str) | — | — | `PERMANENT \| TRANSIENT` |
| `ErrorCode` (frozen dataclass) | `code` | `str` | registry key, non-empty |
| | `severity` | `Severity` | fixed at registration, never updated |
| | `description` | `str` | non-empty, human-readable |
| `RaggedError` | `error_code` | `str` | set via `__init__(message, *, error_code)`; canonical attribute per story AC5 |
| | `severity` (property) | `Severity` | computed via `classify(self.error_code)` against `_DEFAULT_REGISTRY` specifically, Assumption A6 |
| `ErrorCodeRegistrationInvalid` | `code` | `str` | `"ERROR_CODE_REGISTRATION_INVALID"` (fixed class attr) |
| | `classification` | `Literal["PERMANENT"]` | fixed |
| | `attempted_code` | `str` | the code the caller tried to register |
| | `reason` | `str` | which check failed — `"duplicate"`, `"empty code"`, `"invalid severity"`, `"empty description"`, etc. |
| | `existing` | `ErrorCode \| None` | the pre-existing entry, populated only for the duplicate trigger (F1) |
| `ErrorRegistry` | `_codes` | `dict[str, ErrorCode]` | private, insertion-ordered |
| | `_lock` | `threading.Lock` | private |

No table/ledger rows are owned by this module. `IdentityInvalid`, `EnvelopeValidationError`, and
`LedgerTransitionInvalid` (REQ-001/002/003) gain no new public fields — see §1a; only their base
class and one internal `super().__init__()` call site change.

## 3. Sequence

**Happy path**
1. Process starts. Some entry point imports `ingestion.envelope.model` (or `ingestion.identity
   .derive` or `ingestion.ledger.store` — order doesn't matter, see §1a's import-order note).
2. Python's import machinery resolves the dependency graph: `ingestion.errors.registry` is
   imported first (transitively, by whichever of the three modules is imported first); its
   top-level code runs once, constructing `_DEFAULT_REGISTRY` and self-registering
   `ERROR_CODE_REGISTRATION_INVALID` (PERMANENT).
3. `ingestion.identity.derive` finishes importing; its top-level `register_error("IDENTITY_INVALID",
   Severity.PERMANENT, ...)` call runs once, adding that entry.
4. `ingestion.envelope.model` finishes importing (it depends on `ingestion.identity.derive`,
   already cached); its top-level `register_error("ENVELOPE_INVALID", ...)` call runs once.
5. `ingestion.ledger.store` finishes importing; its top-level `register_error(
   "LEDGER_TRANSITION_INVALID", ...)` call runs once.
6. `all_codes()` now returns all four registered `ErrorCode` entries, in the order above.
7. Some downstream call, e.g. `ingestion.identity.derive.derive_doc_id("", "path")`, raises
   `IdentityInvalid(field_name="source", reason="empty string")`. `isinstance(exc, RaggedError)`
   is `True`; `exc.code == exc.error_code == "IDENTITY_INVALID"`; `exc.severity` (property, A6)
   evaluates `classify("IDENTITY_INVALID")` → `Severity.PERMANENT`.
8. A catching coordinator (out of scope — retry execution and poison-queue integration are
   explicitly out of scope for this REQ) inspects `exc.severity` (or calls `classify(exc.error_code)`
   directly) and decides: PERMANENT → mark the document `failed` via `LedgerStore.transition(...)`
   (REQ-003) and ack; TRANSIENT → re-raise for `tenacity` (or equivalent) to retry with backoff.

**Failure paths**
- F1. `register_error(code, ...)` called with a `code` already present in the target registry →
  `ErrorCodeRegistrationInvalid(code, "duplicate registration", existing=<the prior ErrorCode>)`,
  PERMANENT (AC2). The registry is left completely unchanged — the original entry's
  `severity`/`description` are never overwritten.
- F2. `register_error` called with `code` that is `None`, non-`str`, or empty (`""`) →
  `ErrorCodeRegistrationInvalid(code, "invalid code")`, PERMANENT, `existing=None`.
- F3. `register_error` called with `severity` that is not a `Severity` member →
  `ErrorCodeRegistrationInvalid(code, "invalid severity")`, PERMANENT, `existing=None`.
- F4. `register_error` called with `description` that is `None`, non-`str`, or empty → same
  pattern, `reason="invalid description"`.

  Check order for F1–F4 is mandated (§1's `register` docstring), not incidental: the
  duplicate-code check (F1) always runs first, before code/severity/description shape validation
  (F2–F4). A single call that both names an already-registered `code` and supplies an invalid
  `severity`/`description` raises F1's duplicate-registration error — never F2–F4. Checks F2–F4
  only ever run against a `code` that is not already present in the registry.
- F5. `classify(code)` called with a `code` never registered (any `str`, including malformed
  shapes) → returns `Severity.TRANSIENT`; logs one `WARNING`-level message naming the unknown
  `code` via the module logger (`logging.getLogger(__name__)`); never raises (AC3 — this is the
  literal implementation of CLAUDE.md's "unclassified errors default to TRANSIENT").
- F6. Concurrent `register_error()` calls for **different** codes, from different threads, against
  the same `ErrorRegistry` (e.g. parallel test collection, or a multi-threaded import in an
  unusual deployment) → serialized by the registry's internal lock; both succeed; neither entry is
  lost or corrupted (AC7).
- F7. Concurrent `register_error()` calls for the **identical** code, from different threads,
  against the same `ErrorRegistry` → serialized by the lock: exactly one caller's presence-check-
  then-write executes first and succeeds; every other concurrent (or later) caller for that same
  code observes it as already present and raises `ErrorCodeRegistrationInvalid` (F1), PERMANENT.
  **Unlike REQ-003's F7, this is never triaged as benign** — see Assumption A4 for the full
  reasoning: a registry entry is a process-lifetime, declare-once fact with no "redelivery"
  concept, so a collision on the same code string always indicates a real bug (duplicate
  registration inside one module, or two modules colliding on the same code string), never
  legitimate concurrent progress on the same logical unit of work. No `is_benign_*` helper is
  provided or needed for this raise site; every raise here is uniformly PERMANENT with no further
  caller-side triage step, unlike `LedgerTransitionInvalid`.
- F8. `classify()`/`all_codes()` called on an `ErrorRegistry` instance concurrently with an
  in-flight `register()` call for an unrelated code → serialized by the same lock; a reader either
  observes the state fully before or fully after the writer's single atomic dict insert — never a
  torn read (no code list observed with the new entry's key present but its value not yet a
  fully-constructed `ErrorCode`).

## 4. Contract compliance

- **Arrival Envelope**: not applicable to this module's own logic — it never reads envelope fields
  or raw source-specific formats. It does classify the `ENVELOPE_INVALID` code that
  `ingestion.envelope.model` (REQ-001) registers into it, satisfying the story's explicit
  touchpoint ("`ENVELOPE_INVALID` registered as PERMANENT") without this module depending on
  `ingestion.envelope.model` at all — the dependency runs the other direction (§1a).
- **Identity & Idempotency**: not applicable to per-document `doc_id`/`chunk_id` derivation — this
  module owns no document identity and performs no document-scoped write. Its own `register_error`
  calls are process-lifetime, declare-once operations, not document upserts; Contract #2's "every
  operation must be safely re-runnable" governs document-scoped writes, and does not require (or
  even sensibly apply to) idempotent re-registration of the same code string — see Assumption A5.
  Rejecting a genuine duplicate registration is therefore consistent with, not a violation of,
  Contract #2's scope.
- **State Ledger**: not applicable — this module owns no ledger row. `classify()`'s return value is
  the input a coordinator (out of scope) uses to decide whether to call `LedgerStore.transition(...,
  to_status=FAILED, ...)` (REQ-003) or requeue for retry; this module makes no such call itself.
- **Failure Taxonomy: this module *is* Contract #4.** `Severity`/`ErrorCode`/`ErrorRegistry` plus
  `register_error`/`classify`/`all_codes` are its canonical implementation; `classify()`'s
  unknown-code fallback to `Severity.TRANSIENT` with a logged warning is the literal
  implementation of CLAUDE.md's "Unclassified errors default to TRANSIENT." `RaggedError` is the
  common base every framework exception now inherits from (§1a retrofits REQ-001/002/003's three
  existing exception classes, non-breaking per Assumption A1), so `isinstance(exc, RaggedError)`
  and `exc.error_code` are reliable across the whole framework going forward. `all_codes()` becomes
  the single source of truth enumerating every error code declared anywhere in the framework, for
  observability/docs (AC6). This module deliberately does not implement retry execution or
  poison-queue integration (explicitly out of scope per the story) — it owns classification only.

## 5. Error codes

| Code | Classification | Trigger condition |
|---|---|---|
| `ERROR_CODE_REGISTRATION_INVALID` | PERMANENT | `register_error()` called with a `code` already registered (F1) |
| `ERROR_CODE_REGISTRATION_INVALID` | PERMANENT | `register_error()` called with empty/non-`str` `code` (F2) |
| `ERROR_CODE_REGISTRATION_INVALID` | PERMANENT | `register_error()` called with a `severity` not a `Severity` member (F3) |
| `ERROR_CODE_REGISTRATION_INVALID` | PERMANENT | `register_error()` called with empty/non-`str` `description` (F4) |
| `ENVELOPE_INVALID` (cross-module, `ingestion/envelope/model.py`, REQ-001) | PERMANENT | registered by this module at import time; trigger conditions unchanged from REQ-001 |
| `IDENTITY_INVALID` (cross-module, `ingestion/identity/derive.py`, REQ-002) | PERMANENT | registered by this module at import time; trigger conditions unchanged from REQ-002 |
| `LEDGER_TRANSITION_INVALID` (cross-module, `ingestion/ledger/store.py`, REQ-003) | PERMANENT | registered by this module at import time; trigger conditions unchanged from REQ-003 |
| *(any code never registered)* | **TRANSIENT (default)** | `classify(code)` called with an unregistered code (F5) — logged warning, per Contract #4's literal "unclassified errors default to TRANSIENT" |

No TRANSIENT code is ever *registered* by this module or any of REQ-001/002/003 — `TRANSIENT` only
ever arises here as `classify()`'s default fallback for a code nobody has declared, matching the
story's explicit scope (no per-code retry policy, no TRANSIENT codes owned by Phase-1 modules yet).

## 6. Config surface

New file `config/errors.yaml`:

```yaml
errors:
  error_codes:
    ERROR_CODE_REGISTRATION_INVALID: PERMANENT
  unknown_code_default_severity: TRANSIENT   # documentation only — see below
  known_codes:                                # aggregate audit view — documentation only, see below
    ERROR_CODE_REGISTRATION_INVALID: PERMANENT
    ENVELOPE_INVALID: PERMANENT
    IDENTITY_INVALID: PERMANENT
    LEDGER_TRANSITION_INVALID: PERMANENT
```

Both `unknown_code_default_severity` and `known_codes` are listed for documentation/audit
visibility only, following the exact precedent set by `config/envelope.yaml`'s `trust_tiers` and
`config/ledger.yaml`'s `legal_transitions`: neither is read at runtime to change behavior. The
authoritative default-severity behavior is hardcoded in `ErrorRegistry.classify` (§1); the
authoritative code catalog is whatever has actually been registered into `_DEFAULT_REGISTRY` at
runtime, retrievable via `all_codes()`. This is a genuine safety-invariant carve-out for the same
reason as REQ-001/002/003's equivalents: a config edit alone must never be able to change which
severity an unregistered code defaults to, or fabricate/omit an entry in the audit list without a
corresponding code change. `config/errors.yaml`'s `known_codes` block is kept from silently
drifting against the real registry by a dedicated test (§7,
`test_config_known_codes_matches_all_codes_after_importing_all_modules`) — the same
sync-via-test strategy `config/ledger.yaml`'s `default_list_limit` established in REQ-003, applied
here to a full-catalog comparison rather than a single scalar.

`config/envelope.yaml`, `config/identity.yaml`, and `config/ledger.yaml`'s existing `error_codes:`
blocks (each declaring their own single code, per house rules' "every module declares its error
codes in config") are unchanged by this REQ — `config/errors.yaml` is a new, additional aggregate
view, not a replacement for the per-module declarations.

## 7. Test plan

Registry core (`ingestion/errors/test_registry.py`, each test constructs its own fresh
`ErrorRegistry()` instance per Assumption A2, never mutating `_DEFAULT_REGISTRY`):

- `test_register_error_success_returns_error_code` — returns an `ErrorCode` with the given
  `code`/`severity`/`description`.
- `test_register_error_then_classify_returns_registered_severity` (AC1, AC4).
- `test_register_error_duplicate_raises_error_code_registration_invalid_permanent` (AC2) —
  `.existing` on the caught exception equals the originally-registered `ErrorCode`.
- `test_register_error_duplicate_does_not_overwrite_existing_entry` — after a rejected duplicate,
  `classify(code)`/`all_codes()` still reflect the *original* registration's severity/description.
- `test_register_error_rejects_empty_code` / `..._none_code` / `..._non_str_code` (F2).
- `test_register_error_rejects_invalid_severity_type` — e.g. `"PERMANENT"` (plain `str`, not a
  `Severity` member) rejected (F3).
- `test_register_error_rejects_empty_description` / `..._none_description` (F4).
- `test_register_error_duplicate_check_runs_before_shape_validation` — a single `register()` call
  supplying both an already-registered `code` and an invalid `severity` (or empty `description`)
  raises `ErrorCodeRegistrationInvalid` with `reason="duplicate registration"` and a populated
  `.existing`, never the shape-validation `reason` — confirms the mandated check order (§1, §3).
- `test_classify_unknown_code_returns_transient` (AC3).
- `test_classify_unknown_code_logs_warning_naming_code` (AC3) — `caplog` asserts a `WARNING`-level
  record whose message contains the unknown code string.
- `test_classify_never_raises_for_any_input` — including empty string, non-hex garbage, very long
  strings.
- `test_all_codes_empty_registry_returns_empty_list`.
- `test_all_codes_returns_every_registered_code_with_severity_and_description` (AC6).
- `test_all_codes_preserves_registration_order`.
- `test_all_codes_returns_a_fresh_list_each_call` — mutating the returned list does not affect the
  registry's internal state.
- `test_ragged_error_carries_error_code` (AC5) — construct a minimal `RaggedError` subclass,
  assert `.error_code` is set correctly and `isinstance(exc, Exception)`.
- `test_ragged_error_severity_property_delegates_to_default_registry` — `exc.severity` for a code
  registered in `_DEFAULT_REGISTRY` matches a direct `classify(exc.error_code)` call (Assumption
  A6).
- `test_ragged_error_severity_property_ignores_local_registry` — construct an isolated
  `ErrorRegistry()`, register a code into it only, raise a `RaggedError` carrying that code →
  `.severity` does **not** reflect the local registry's entry; it resolves via `_DEFAULT_REGISTRY`
  (`Severity.TRANSIENT` with a logged warning if that code was never registered globally) —
  regression guard for Assumption A6.
- `test_error_code_dataclass_is_frozen` — mutating a field on a constructed `ErrorCode` raises.
- `test_thread_safety_concurrent_registrations_distinct_codes_all_succeed` (AC7, F6) — N threads
  each registering a distinct code into one `ErrorRegistry` → `all_codes()` afterward contains
  exactly N entries, none lost or corrupted.
- `test_thread_safety_concurrent_registration_same_code_exactly_one_wins` (AC7, §3 F7) — N threads
  racing to register the identical code → exactly one thread's `register()` call returns normally;
  the other N-1 raise `ErrorCodeRegistrationInvalid` PERMANENT; the registry ends with exactly one
  entry for that code, matching the winner's `severity`/`description`.
- `test_thread_safety_reads_never_observe_torn_state` (F8) — a reader thread calling
  `classify()`/`all_codes()` concurrently with a writer thread never observes a code present with
  a partially-constructed `ErrorCode` (property/hypothesis-style repeated-trial test).

Migration/cross-module regression tests (added to the existing test files alongside REQ-001/002/003,
per the established pattern of REQ-002's cross-module regression tests):

- `ingestion/identity/test_derive.py`: `test_identity_invalid_is_ragged_error` —
  `isinstance(IdentityInvalid("x", "y"), RaggedError)`; `.error_code == .code == "IDENTITY_INVALID"`.
- `ingestion/identity/test_derive.py`: `test_identity_invalid_registered_in_default_registry` —
  `ingestion.errors.registry.classify("IDENTITY_INVALID") == Severity.PERMANENT` after import.
- `ingestion/envelope/test_model.py`: `test_envelope_validation_error_is_ragged_error` — same
  pattern for `EnvelopeValidationError`/`"ENVELOPE_INVALID"`.
- `ingestion/envelope/test_model.py`: `test_envelope_invalid_registered_in_default_registry`.
- `ingestion/ledger/test_store.py`: `test_ledger_transition_invalid_is_ragged_error` — same pattern
  for `LedgerTransitionInvalid`/`"LEDGER_TRANSITION_INVALID"`.
- `ingestion/ledger/test_store.py`: `test_ledger_transition_invalid_registered_in_default_registry`.
- `test_existing_constructor_signatures_unchanged` (one per module, or parametrized) — regression
  guard: `EnvelopeValidationError(field_errors)`, `IdentityInvalid(field_name, reason)`,
  `LedgerTransitionInvalid(doc_id, reason, observed_row=..., attempted_to_status=...,
  attempted_stage=...)` all still construct successfully with the exact call shapes used by
  REQ-001/002/003's own existing tests — confirms the migration note's "non-breaking" claim.
- `test_existing_classification_attr_matches_registry_severity` (Assumption A7) — parametrized over
  the three retrofitted classes: `classify(cls.code).value == cls.classification` for each,
  guarding against the two parallel sources of truth (hardcoded `classification` literal vs.
  registry `Severity`) silently drifting apart.
- `test_config_known_codes_matches_all_codes_after_importing_all_modules` — after importing
  `ingestion.envelope.model`, `ingestion.identity.derive`, and `ingestion.ledger.store`, parses
  `config/errors.yaml`'s `errors.known_codes` and asserts it is exactly the set of
  `{code: severity.value}` pairs returned by `all_codes()` — keeps the audit config from drifting
  (§6).
- `test_error_code_registration_invalid_registered_in_default_registry` — `classify(
  "ERROR_CODE_REGISTRATION_INVALID") == Severity.PERMANENT` holds immediately on importing
  `ingestion.errors.registry` alone, with no other module imported (bootstrap self-registration,
  §1).

## 8. Budget

- p95 latency added per document: effectively **0ms** on the hot path — every `register_error()`
  call happens exactly once per module, at process/import time, never per document. `classify()`
  (a dict lookup under a short-held lock) is < 1ms per call (matches the story's explicit budget),
  and is only invoked when an exception is actually raised and caught (the error path, not the
  per-document happy path) — most documents never trigger a `classify()` call at all.
- Cost per document: **$0 / 0 tokens** — pure in-memory, in-process registry; no external API, no
  network call, no I/O of any kind.
