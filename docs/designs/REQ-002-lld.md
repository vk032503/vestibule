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
- `ingestion/envelope/model.py` (existing, REQ-001) — `compute_doc_id` is redefined to import
  and delegate to `derive_doc_id`. Zero behavior change to the hash output; the function
  signature and all REQ-001 call sites (`build_envelope`, `_verify_doc_id`) are unchanged, since
  `_verify_doc_id` already calls `compute_doc_id` rather than inlining hash logic — it therefore
  picks up the canonical implementation transitively once `compute_doc_id` delegates, with no
  separate edit required to `_verify_doc_id`'s own code (only its docstring, to name the new
  source of truth — see §1a).

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

```python
# ingestion/envelope/model.py  (existing file — modified as part of this LLD's rollout)

from ingestion.identity.derive import derive_doc_id


def compute_doc_id(source: str, blob_path: str) -> str:
    """doc_id = sha256(source + blob_path), per Contract #2.
    Delegates to the canonical implementation in ingestion.identity.derive (REQ-002); this
    module no longer contains its own hashing logic. Zero behavior change to the hash output —
    signature and return value are unchanged, so build_envelope and the _verify_doc_id model
    validator (both REQ-001) require no further edits beyond this delegation."""
    return derive_doc_id(source, blob_path)
```

`ArrivalEnvelope._verify_doc_id`'s docstring (REQ-001 §1) is updated to note that it re-derives
via `compute_doc_id`, which is now itself a thin delegate to `derive_doc_id` — the model
validator's own code and control flow are unchanged; only the comment naming the ultimate source
of truth is updated. This closes the divergence risk: there is exactly one place
(`ingestion/identity/derive.py::derive_doc_id`) where the `doc_id` hash is actually computed.

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

**Failure paths** (all resolve to `IdentityInvalid(code="IDENTITY_INVALID", classification="PERMANENT")`)
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
  because it makes no external calls.

## 5. Error codes

| Code | Classification | Trigger condition |
|---|---|---|
| `IDENTITY_INVALID` | PERMANENT | `source` is `None`, non-`str`, or empty |
| `IDENTITY_INVALID` | PERMANENT | `blob_path` is `None`, non-`str`, or empty |
| `IDENTITY_INVALID` | PERMANENT | `doc_id` input to `derive_chunk_id`/`derive_chunk_ids_batch`/`validate_id_format` is not a 64-char lowercase hex string |
| `IDENTITY_INVALID` | PERMANENT | `chunk_index` is not an `int`, or is a `bool` |
| `IDENTITY_INVALID` | PERMANENT | `chunk_index` is a negative `int` |

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

## 8. Budget

- p95 latency added per document: < 1ms per ID derivation (single in-process sha256 hash, no
  I/O); batch derivation for a typical document (tens to low hundreds of chunks) stays well
  under 1ms total.
- Cost per document: $0 / 0 tokens — no external API calls.
