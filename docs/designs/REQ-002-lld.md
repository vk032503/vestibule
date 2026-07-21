# REQ-002 — Identity & Idempotency — LLD

**Story:** docs/stories/REQ-002.md · **Phase:** 1 · **Pipeline:** full

## Scope note — this LLD also touches ingestion/envelope/model.py (REQ-001)
REQ-001 (already merged) independently hardcodes `doc_id = sha256(source + blob_path)` inside
`ingestion/envelope/model.py::compute_doc_id`. This LLD introduces a second, canonical
implementation of the same rule in `ingestion/identity/derive.py::derive_doc_id`. Leaving both
copies live and unlinked would be internally inconsistent — if either is edited without the
other, the envelope layer and identity layer could silently diverge in what they compute as
`doc_id`, breaking upsert idempotency (Contract #2) with no test or ledger signal to catch it.

To close that gap, this LLD's rollout is explicitly scoped to touch **two** files, both covered
below (§1, §3, §4, §7):
- `ingestion/identity/derive.py` (new) — the canonical implementation.
- `ingestion/envelope/model.py` (existing, REQ-001) — two changes, both shown in §1a as minimal
  diffs against the actual current file, not a re-transcription of unrelated lines:
  1. `compute_doc_id`'s body is redefined to delegate to `derive_doc_id`. Zero behavior change
     to the hash output.
  2. `build_envelope` is updated to catch `IdentityInvalid` around its `compute_doc_id` call and
     translate it into `EnvelopeValidationError`/`ENVELOPE_INVALID`. This is required, not
     optional: `compute_doc_id(source, blob_path)` is currently evaluated as a constructor
     argument to `ArrivalEnvelope(...)`, i.e. *before* Pydantic's field validators run. Today
     that call does no input validation of its own, so bad `source`/`blob_path` silently reaches
     Pydantic and is caught there via `except ValidationError`. Once `compute_doc_id` delegates
     to `derive_doc_id` (which does validate its inputs and raises `IdentityInvalid`, not
     `pydantic.ValidationError`), that same `except ValidationError` clause would no longer
     catch it, letting `IdentityInvalid`/`IDENTITY_INVALID` escape uncaught in place of the
     documented `EnvelopeValidationError`/`ENVELOPE_INVALID`. §1a fixes this at the same time as
     the delegation, not as a follow-up.
  `_verify_doc_id` (used by `envelope_from_json`'s parse path) needs no change beyond its
  docstring — see §1a for why that path is already safe.

## 1. Interfaces

```python
# ingestion/identity/derive.py

ID_HEX_LENGTH: int = 64
_HEX_RE: re.Pattern[str]           # compiled r"^[0-9a-f]{64}$"


def derive_doc_id(source: str, blob_path: str) -> str:
    """doc_id = sha256(source + blob_path), per Contract #2. Pure, deterministic, no I/O.
    Canonical implementation of the doc_id derivation rule; ingestion.envelope.model's
    compute_doc_id (REQ-001) delegates to this function (see §1a). Raises IdentityInvalid
    (PERMANENT) if source or blob_path is None, non-str, or empty."""
    ...


def derive_chunk_id(doc_id: str, chunk_index: int) -> str:
    """chunk_id = sha256(doc_id + str(chunk_index)), per Contract #2. Pure, deterministic.
    Raises IdentityInvalid (PERMANENT) if doc_id fails validate_id_format, or chunk_index is
    not a non-negative int. Note: chunk_index must be validated with
    `isinstance(chunk_index, int) and not isinstance(chunk_index, bool)` — in Python `bool` is
    a subclass of `int`, so a naive `isinstance(chunk_index, int)` check would wrongly accept
    `True`/`False` as valid chunk indices (evaluating as 1/0). Booleans are always rejected
    regardless of value, per F4."""
    ...


def derive_chunk_ids_batch(doc_id: str, chunk_indices: Iterable[int]) -> dict[int, str]:
    """Batch helper: {chunk_index: chunk_id, ...}, one entry per input index.
    Each value is identical to calling derive_chunk_id(doc_id, i) individually.
    Fails fast: raises IdentityInvalid on the first invalid chunk_index encountered and
    returns no partial mapping (all-or-nothing)."""
    ...


def validate_id_format(value: str, *, field_name: str = "id") -> str:
    """Validates that value is a 64-character lowercase hex string (sha256 hex digest shape).
    Returns value unchanged on success. Raises IdentityInvalid (PERMANENT) otherwise.
    Used internally by derive_chunk_id to validate its doc_id input, and externally by any
    component that needs to validate a doc_id/chunk_id received from outside this module
    (e.g. off a ledger row or queue message) without re-deriving it."""
    ...


class IdentityInvalid(Exception):
    """Raised for any malformed identity input. Always PERMANENT — malformed identity inputs
    are a producer/caller bug, never a transient condition worth retrying."""
    code: str                          # always "IDENTITY_INVALID"
    classification: Literal["PERMANENT"]
    field_name: str                    # "source" | "blob_path" | "doc_id" | "chunk_index"
    reason: str                        # human-readable cause

    def __init__(self, field_name: str, reason: str) -> None:
        """code and classification are fixed by this class, not caller-supplied. Message is
        built as f"{field_name}: {reason}" and passed to Exception.__init__ for repr/logging."""
        ...
```

No adapter classes are introduced here. This module has zero I/O and zero external dependencies
beyond the standard library (`hashlib`, `re`) — per house rules, adapters wrap and never
implement algorithms; this module *is* the algorithm that adapters and store implementations
(out of scope, later phases) call into for their upsert keys.

### 1a. Cross-module change: ingestion/envelope/model.py (REQ-001, modified by this LLD)

The blocks below are minimal diffs against the actual current file content (re-verified from
disk). Only the lines shown change; every other line, docstring, import, and parameter in
`ingestion/envelope/model.py` — including `EnvelopeValidationError.__init__(self, field_errors:
list[dict[str, str]])` (no `code` constructor kwarg; `code`/`classification` are fixed class
attributes) and `build_envelope`'s existing `received_at=received_at if received_at is not None
else datetime.now(timezone.utc)` default (tz-aware, per REQ-001) — is untouched by this LLD.

**Change 1 — `compute_doc_id` delegates to the canonical implementation.** Add the import and
replace only the function body:

```python
from ingestion.identity.derive import derive_doc_id, IdentityInvalid


def compute_doc_id(source: str, blob_path: str) -> str:
    """doc_id = sha256(source + blob_path), per Contract #2. Pure, deterministic.
    Delegates to the canonical implementation in ingestion.identity.derive (REQ-002)."""
    return derive_doc_id(source, blob_path)
```

(The module's `import hashlib` becomes unused once this delegation lands, since `compute_doc_id`
was its only caller — safe to remove, but not required for correctness.)

**Change 2 — `build_envelope` translates `IdentityInvalid` into `EnvelopeValidationError`**, and
a new helper `_wrap_identity_error` is added near the existing `_wrap_validation_error`. Only the
`doc_id=compute_doc_id(...)` line moves out of the `ArrivalEnvelope(...)` call into its own
statement above the existing `try`; every other line of `build_envelope` (docstring, parameters,
`allowed_groups`/`received_at` defaults, the second `try`/`except ValidationError` block) is
unchanged:

```python
def build_envelope(
    *,
    source: str,
    blob_path: str,
    content_hash: str,
    trust_tier: TrustTier,
    allowed_groups: list[str] | None = None,
    vertical: str | None = None,
    scenario_id: str | None = None,
    received_at: datetime | None = None,
) -> ArrivalEnvelope:
    """Producer-facing factory: computes doc_id, applies default-deny, validates."""
    try:
        doc_id = compute_doc_id(source, blob_path)
    except IdentityInvalid as exc:
        raise _wrap_identity_error(exc) from exc
    try:
        return ArrivalEnvelope(
            doc_id=doc_id,
            source=source,
            blob_path=blob_path,
            vertical=vertical,
            scenario_id=scenario_id,
            allowed_groups=allowed_groups if allowed_groups is not None else [],
            trust_tier=trust_tier,
            content_hash=content_hash,
            received_at=received_at if received_at is not None else datetime.now(timezone.utc),
        )
    except ValidationError as exc:
        raise _wrap_validation_error(exc) from exc


def _wrap_identity_error(exc: IdentityInvalid) -> EnvelopeValidationError:
    """Maps an IdentityInvalid from the identity module to the module's declared PERMANENT error."""
    return EnvelopeValidationError([{"field": exc.field_name, "reason": exc.reason}])
```

`_verify_doc_id` (invoked by `envelope_from_json`'s `model_validator(mode="after")`) needs no
equivalent fix: it already runs *after* all per-field Pydantic validators have passed (`mode=
"after"`), so `source`/`blob_path` are guaranteed non-empty strings by the time it calls
`compute_doc_id(self.source, self.blob_path)` — `derive_doc_id`'s own input validation can never
trigger on that path, only its hash-mismatch check can fail, which `_verify_doc_id` already
handles by raising `ValueError` (unchanged, per REQ-001). Only `_verify_doc_id`'s docstring is
updated to name `derive_doc_id` as the ultimate source of truth — no code change.

This closes the divergence risk end-to-end: there is exactly one place
(`ingestion/identity/derive.py::derive_doc_id`) where the `doc_id` hash is actually computed, and
exactly one error code (`ENVELOPE_INVALID`) ever crosses the `ingestion.envelope.model` public
boundary, regardless of which internal path (`build_envelope` vs. `envelope_from_json`) a
malformed input takes.

## 2. Data model

This module is pure functions plus one exception type — it owns no dataclass, table, or ledger
row.

| Type | Field | Type | Notes |
|---|---|---|---|
| `IdentityInvalid` | `code` | `str` | `"IDENTITY_INVALID"` (fixed, not constructor param) |
| | `classification` | `Literal["PERMANENT"]` | fixed, not constructor param |
| | `field_name` | `str` | constructor param — which input was malformed |
| | `reason` | `str` | constructor param — human-readable cause, e.g. `"empty string"`, `"not 64 hex chars"` |

Produced values (`doc_id`, `chunk_id`) are plain `str` — 64-character lowercase hex digests —
consumed as primary/foreign keys by the State Ledger (REQ-003) and by store adapters (later
phases); no schema for those consumers is defined here.

`EnvelopeValidationError` (REQ-001, `ingestion/envelope/model.py`) is unchanged: `code` and
`classification` remain fixed class attributes (`"ENVELOPE_INVALID"`, `"PERMANENT"`), and its
constructor remains `__init__(self, field_errors: list[dict[str, str]])` — no `code` kwarg. §1a's
`_wrap_identity_error` only adds a new *caller* of this existing constructor, passing a single
`field_errors` entry built from `IdentityInvalid.field_name`/`reason`.

## 3. Sequence

**Happy path**
1. A caller (e.g. the envelope producer per REQ-001, or the ingestion coordinator per REQ-003)
   obtains `source` and `blob_path` from an already-validated `ArrivalEnvelope`.
2. Caller invokes `derive_doc_id(source, blob_path)` — either directly, or transitively via
   `ingestion.envelope.model.compute_doc_id`, which now delegates to it (§1a).
3. `derive_doc_id` validates `source` and `blob_path` are non-empty strings.
4. `derive_doc_id` computes `sha256(source + blob_path).hexdigest()` and returns the 64-char
   lowercase hex string.
5. A downstream chunking component (later phase) has a list of integer chunk indices for the
   document.
6. Caller invokes `derive_chunk_ids_batch(doc_id, chunk_indices)` (or loops
   `derive_chunk_id(doc_id, i)` individually — both are supported and must agree).
7. Each derivation validates `doc_id` via `validate_id_format` and `chunk_index` is a
   non-negative, non-bool `int`, then computes `sha256(doc_id + str(chunk_index)).hexdigest()`.
8. Store adapters (out of scope) use the resulting `doc_id`/`chunk_id` values as upsert keys
   (Contract #2) — this module has no visibility into that write.

**Failure paths** (all resolve to `IdentityInvalid(code="IDENTITY_INVALID", classification="PERMANENT")`
unless the call originated inside `build_envelope`, in which case §1a Change 2 translates it to
`EnvelopeValidationError` with fixed `code == "ENVELOPE_INVALID"` before it reaches the caller)
- F1. `source` is `None`, non-`str`, or empty (`""`) → `IdentityInvalid`, `field_name="source"`.
- F2. `blob_path` is `None`, non-`str`, or empty (`""`) → `IdentityInvalid`, `field_name="blob_path"`.
- F3. `doc_id` passed into `derive_chunk_id`/`derive_chunk_ids_batch` fails `validate_id_format`
  (wrong length, non-hex chars, uppercase, `None`, non-`str`) → `IdentityInvalid`, `field_name="doc_id"`.
- F4. `chunk_index` is not an `int`, or is a `bool` (`True`/`False` — `bool` is an `int` subclass
  in Python and must be explicitly excluded, not just relying on a bare `isinstance(x, int)`
  check) → `IdentityInvalid`, `field_name="chunk_index"`.
- F5. `chunk_index` is a negative `int` → `IdentityInvalid`, `field_name="chunk_index"`.
- F6. `derive_chunk_ids_batch` given an iterable containing any invalid index → raises on the
  first offending index; no partial `dict` is returned (all-or-nothing, matching Contract #2's
  "safely re-runnable" expectation — a caller retries the whole batch, never a half-applied one).
- F7. `validate_id_format` called directly (by an external caller, not via derive_*) on a
  malformed id string → `IdentityInvalid`, `field_name` set to the caller-supplied `field_name`
  (default `"id"`).
- F8 (cross-module, `ingestion/envelope/model.py`). `build_envelope` called with an empty/`None`
  `source` or `blob_path` → `derive_doc_id` raises `IdentityInvalid` (F1/F2 above) at the
  `compute_doc_id(source, blob_path)` call site → `build_envelope`'s `except IdentityInvalid`
  clause (§1a Change 2) catches it and raises `EnvelopeValidationError([{"field":
  "source"|"blob_path", "reason": ...}])` (fixed `code == "ENVELOPE_INVALID"`) — the caller never
  observes `IdentityInvalid`/`IDENTITY_INVALID` from this entry point.

## 4. Contract compliance

- **Arrival Envelope**: this module consumes only the already-normalized `source` and
  `blob_path` strings extracted from an `ArrivalEnvelope` (REQ-001); it never reads a raw
  source-specific document or path format, satisfying "no component reads raw source-specific
  formats past the arrival boundary."
- **Identity & Idempotency**: this module *is* Contract #2's derivation rule, implemented exactly
  once — `derive_doc_id` = `sha256(source + blob_path)`, `derive_chunk_id` = `sha256(doc_id +
  chunk_index)` — as pure, deterministic functions with no internal state. This LLD explicitly
  closes the risk of a second, divergent implementation: `ingestion/envelope/model.py::
  compute_doc_id` (REQ-001) is redefined in-scope (§1a) to delegate to `derive_doc_id` rather
  than compute its own hash, so there is exactly one hashing implementation in the codebase and
  any caller re-running the same inputs (at-least-once delivery) always gets the same IDs from
  either entry point, making every downstream write an upsert by construction.
- **State Ledger**: not applicable — this module owns no ledger row and performs no I/O; the
  `doc_id` it produces becomes the ledger's primary key once a ledger row is created by the
  ingestion coordinator (REQ-003), which is out of scope here.
- **Failure Taxonomy**: every failure path in §3 raises `IdentityInvalid` classified PERMANENT —
  malformed identity inputs indicate a caller/producer bug (never a flaky external dependency),
  so retrying without a code fix would never succeed; there is no TRANSIENT path in this module
  because it makes no external calls. §1a Change 2 additionally ensures the taxonomy stays
  correctly *scoped* at module boundaries: `IDENTITY_INVALID` never leaks out of
  `ingestion.envelope.model`'s public functions (`build_envelope`, `envelope_from_json`) — those
  continue to only ever raise the module's own documented code, `ENVELOPE_INVALID`, matching
  REQ-001's contract even though the underlying hash logic now lives elsewhere.

## 5. Error codes

| Code | Classification | Trigger condition |
|---|---|---|
| `IDENTITY_INVALID` | PERMANENT | `source` is `None`, non-`str`, or empty |
| `IDENTITY_INVALID` | PERMANENT | `blob_path` is `None`, non-`str`, or empty |
| `IDENTITY_INVALID` | PERMANENT | `doc_id` input to `derive_chunk_id`/`derive_chunk_ids_batch`/`validate_id_format` is not a 64-char lowercase hex string |
| `IDENTITY_INVALID` | PERMANENT | `chunk_index` is not an `int`, or is a `bool` |
| `IDENTITY_INVALID` | PERMANENT | `chunk_index` is a negative `int` |
| `ENVELOPE_INVALID` (cross-module, `ingestion/envelope/model.py`) | PERMANENT | `build_envelope` called with empty/`None`/non-`str` `source` or `blob_path` — `IdentityInvalid` from `compute_doc_id` is caught and translated (§1a Change 2), never surfaced as `IDENTITY_INVALID` |

No TRANSIENT codes originate in this module — pure in-process hashing has no external call to
fail transiently.

## 6. Config surface

New file `config/identity.yaml`:

```yaml
identity:
  hash_algo: sha256          # documentation/audit only — see below
  id_length: 64               # hex chars — documentation/audit only — see below
  error_codes:
    IDENTITY_INVALID: PERMANENT
```

As with REQ-001's `config/envelope.yaml`, `hash_algo` and `id_length` are listed for
documentation/audit visibility only; they are not read at runtime to change behavior. The
authoritative algorithm is hardcoded (`hashlib.sha256`) and the authoritative length check is
`_HEX_RE` in `ingestion/identity/derive.py`. This is deliberate: the derivation rule is a
safety-critical, contract-defining invariant (Contract #2) — a config edit alone must never be
able to change how `doc_id`/`chunk_id` are computed, since that would silently break
idempotency for every document already indexed under the old scheme. Changing the algorithm
requires a code change and a new LLD/PR.

`config/envelope.yaml`'s existing `error_codes: { ENVELOPE_INVALID: PERMANENT }` entry (REQ-001)
is unchanged — §1a Change 2 does not introduce a new error code, it only ensures an existing one
is emitted from a path that would otherwise have leaked a different module's code.

## 7. Test plan

- `test_derive_doc_id_deterministic` — same `(source, blob_path)` → same `doc_id`, called
  repeatedly.
- `test_derive_doc_id_differs_by_source_or_path` — changing either input changes `doc_id`.
- `test_derive_doc_id_format` — output is a 64-char lowercase hex string.
- `test_derive_chunk_id_deterministic` — same `(doc_id, chunk_index)` → same `chunk_id`.
- `test_derive_chunk_id_differs_by_index` — same `doc_id`, different `chunk_index` → different
  `chunk_id`.
- `test_derive_chunk_id_format` — output is a 64-char lowercase hex string.
- `test_derive_chunk_ids_batch_matches_individual` — for N in `[0, 1, 50]`, batch result for
  each index equals `derive_chunk_id(doc_id, i)` called individually, one-for-one.
- `test_derive_chunk_ids_batch_empty` — empty `chunk_indices` → empty dict, no error.
- `test_derive_doc_id_invalid_source_none` / `..._empty` / `..._non_str` → `IdentityInvalid`
  PERMANENT, `field_name="source"`.
- `test_derive_doc_id_invalid_blob_path_none` / `..._empty` / `..._non_str` → `IdentityInvalid`
  PERMANENT, `field_name="blob_path"`.
- `test_derive_chunk_id_invalid_doc_id_wrong_length` / `..._non_hex` / `..._uppercase` /
  `..._none` / `..._non_str` → `IdentityInvalid` PERMANENT, `field_name="doc_id"`.
- `test_derive_chunk_id_invalid_chunk_index_type` — `str`/`float`/`None` → `IdentityInvalid`
  PERMANENT, `field_name="chunk_index"`.
- `test_derive_chunk_id_invalid_chunk_index_bool` — `True`/`False` explicitly rejected despite
  `bool` being an `int` subclass → `IdentityInvalid` PERMANENT, `field_name="chunk_index"`.
- `test_derive_chunk_id_invalid_chunk_index_negative` → `IdentityInvalid` PERMANENT,
  `field_name="chunk_index"`.
- `test_derive_chunk_ids_batch_fails_fast_on_invalid_index` — one invalid index among valid ones
  → raises on the first invalid one, no partial dict returned/leaked.
- `test_validate_id_format_accepts_valid_hash` — 64-char lowercase hex string passes through
  unchanged.
- `test_validate_id_format_rejects_malformed` — wrong length, uppercase, non-hex, `None`,
  non-`str` all rejected with `IdentityInvalid` PERMANENT.
- `test_identity_invalid_constructor_sets_fixed_fields` — `IdentityInvalid(field_name=...,
  reason=...)` always yields `code == "IDENTITY_INVALID"` and `classification == "PERMANENT"`
  regardless of constructor args.
- `test_property_derive_doc_id_determinism` (hypothesis) — over ≥1,000 randomly generated
  `(source, blob_path)` string pairs, `derive_doc_id(s, p) == derive_doc_id(s, p)` always holds
  and output always matches the 64-char lowercase hex shape.
- `test_property_derive_doc_id_uniqueness` (hypothesis) — over ≥1,000 randomly generated
  distinct `(source, blob_path)` pairs, no collisions observed among produced `doc_id`s.
- `test_property_derive_chunk_id_determinism` (hypothesis) — over randomly generated
  `(doc_id, chunk_index)` pairs (doc_id drawn from valid hex strings, chunk_index ≥ 0),
  determinism holds and output shape is valid.
- `test_compute_doc_id_delegates_to_derive_doc_id` — cross-module regression guard (lives
  alongside `ingestion/envelope/model.py`'s existing REQ-001 tests): for a range of
  `(source, blob_path)` inputs, `ingestion.envelope.model.compute_doc_id(s, p) ==
  ingestion.identity.derive.derive_doc_id(s, p)`, byte-for-byte. This is the explicit test/CI
  signal the scope note above calls for — it fails loudly if the two modules are ever edited
  independently and drift apart.
- `test_build_envelope_empty_source_raises_envelope_validation_error` — cross-module regression
  guard (lives alongside `ingestion/envelope/model.py`'s existing REQ-001 tests):
  `build_envelope(source="", blob_path=..., ...)` raises `EnvelopeValidationError` with
  `code == "ENVELOPE_INVALID"` and a `field_errors` entry naming `"source"` — explicitly *not*
  `IdentityInvalid`/`"IDENTITY_INVALID"`. Guards §1a Change 2.
- `test_build_envelope_empty_blob_path_raises_envelope_validation_error` — same as above for
  `blob_path=""`, asserting `EnvelopeValidationError`/`ENVELOPE_INVALID` with a `field_errors`
  entry naming `"blob_path"`, not `IdentityInvalid`/`"IDENTITY_INVALID"`. Guards §1a Change 2.
- `test_build_envelope_default_received_at_is_tz_aware_utc` — regression guard for §1a Change 2's
  minimal-diff claim: `build_envelope(...)` called with `received_at` omitted produces an
  envelope whose `received_at.tzinfo is not None` (UTC-aware), confirming the existing
  `datetime.now(timezone.utc)` default (REQ-001) was left untouched by this LLD's edit and did
  not regress to a naive `datetime.utcnow()`.

## 8. Budget

- p95 latency added per document: < 1ms per ID derivation (single in-process sha256 hash, no
  I/O); batch derivation for a typical document (tens to low hundreds of chunks) stays well
  under 1ms total.
- Cost per document: $0 / 0 tokens — no external API calls.
