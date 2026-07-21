# REQ-001 — Arrival Envelope — LLD

**Story:** docs/stories/REQ-001.md · **Phase:** 1 · **Pipeline:** full

## Assumptions (non-blocking, flagged per house rules)
The story specifies the envelope's field list and validation behavior fully enough to design
against, but leaves two details unstated. These are treated as scoped design decisions, not
open questions, because they do not change the shape of the interfaces or the contract mapping:

- `trust_tier` enum membership is not enumerated in the story. This LLD defines a fixed 4-value
  Python `Enum` (`public`, `internal`, `confidential`, `restricted`) as a placeholder value set.
  This enum is the sole authority for accepted values — it is **not** driven by
  `config/envelope.yaml` at runtime; the YAML `trust_tiers` key is documentation/audit-only (see
  §6). Extending the accepted set requires a code change to the enum, not a config change. This
  keeps a validation-only, contract-defining module free of config-load complexity.
- `doc_id` is a required field on the wire envelope (per Contract #1's shape), but the *producer*
  of an envelope should not have to compute the hash by hand. This LLD provides both a factory
  (`build_envelope`) that computes `doc_id` automatically, and a model-level validator that
  re-derives and cross-checks `doc_id` once all fields are populated, whenever an envelope is
  parsed from an untrusted source (e.g. off the queue), rejecting any mismatch as PERMANENT. This
  treats `doc_id` as derived-and-verified rather than producer-trusted, closing a tamper/bug gap
  the story doesn't explicitly call out.

## 1. Interfaces

```python
# ingestion/envelope/model.py

class TrustTier(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class ArrivalEnvelope(BaseModel):
    """Canonical arrival envelope — Contract #1. Strict schema, immutable."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_id: str                       # sha256 hex digest, verified against source+blob_path
    source: str                       # non-empty, e.g. "confluence", "s3"
    blob_path: str                    # non-empty, source-relative path/key
    vertical: str | None = None
    scenario_id: str | None = None
    allowed_groups: list[str] = Field(default_factory=list)   # default-deny: [] means no access
    trust_tier: TrustTier
    content_hash: str                 # sha256 hex digest of blob content, 64 lowercase hex chars
    received_at: datetime             # UTC, ISO-8601 on the wire

    @field_validator("content_hash")
    @classmethod
    def _content_hash_is_sha256_hex(cls, v: str) -> str: ...

    @field_validator("source", "blob_path")
    @classmethod
    def _non_empty(cls, v: str) -> str: ...

    @model_validator(mode="after")
    def _verify_doc_id(self) -> "ArrivalEnvelope":
        """Runs once all fields (incl. source/blob_path) are populated and individually valid.
        Re-derives doc_id via compute_doc_id(source, blob_path) and rejects on mismatch.
        Field-level validators cannot do this cross-check reliably in Pydantic v2, because
        `info.data` only contains fields declared/validated earlier than the field currently
        being validated — doc_id is declared first, so source/blob_path would not yet be
        available to a field_validator on doc_id."""
        ...


def compute_doc_id(source: str, blob_path: str) -> str:
    """doc_id = sha256(source + blob_path), per Contract #2. Pure, deterministic."""
    ...


def build_envelope(
    *,
    source: str,
    blob_path: str,
    content_hash: str,
    trust_tier: TrustTier,
    allowed_groups: list[str] | None = None,
    vertical: str | None = None,
    scenario_id: str | None = None,
    received_at: datetime | None = None,   # defaults to utcnow() if omitted
) -> ArrivalEnvelope:
    """Producer-facing factory: computes doc_id, applies default-deny, validates."""
    ...


def envelope_to_json(envelope: ArrivalEnvelope) -> str:
    """Lossless serialization for queue message body."""
    ...


def envelope_from_json(raw: str | bytes) -> ArrivalEnvelope:
    """Deserialize + re-validate (including doc_id cross-check). Raises EnvelopeValidationError."""
    ...


class EnvelopeValidationError(Exception):
    code: str                          # always "ENVELOPE_INVALID"
    classification: Literal["PERMANENT"]
    field_errors: list[dict[str, str]] # [{"field": ..., "reason": ...}, ...]
```

No adapter classes are introduced here — source adapters that *produce* envelopes are explicitly
out of scope (Phase 2). This module is consumed by those adapters and by any component that reads
envelopes off the queue.

## 2. Data model

| Type | Field | Type | Notes |
|---|---|---|---|
| `ArrivalEnvelope` | `doc_id` | `str` | 64 lowercase hex chars, `= sha256(source+blob_path)`, verified not trusted (via model-level validator, not field-level — see §1) |
| | `source` | `str` | non-empty |
| | `blob_path` | `str` | non-empty |
| | `vertical` | `str \| None` | optional |
| | `scenario_id` | `str \| None` | optional |
| | `allowed_groups` | `list[str]` | default `[]` → default-deny |
| | `trust_tier` | `TrustTier` (enum) | `public \| internal \| confidential \| restricted` — fixed enum, not config-driven |
| | `content_hash` | `str` | 64 lowercase hex chars |
| | `received_at` | `datetime` | UTC, tz-aware |
| `EnvelopeValidationError` | `code` | `str` | `"ENVELOPE_INVALID"` |
| | `classification` | `Literal["PERMANENT"]` | fixed |
| | `field_errors` | `list[dict[str,str]]` | one entry per failing field |

No table/ledger rows are owned by this module (see Contract compliance §3).

## 3. Sequence

**Happy path**
1. Producer adapter (out of scope) calls `build_envelope(source=..., blob_path=..., content_hash=..., trust_tier=..., ...)`.
2. `build_envelope` computes `doc_id = compute_doc_id(source, blob_path)`.
3. Pydantic constructs `ArrivalEnvelope`; strict-schema, enum, and per-field validators run first, then the `_verify_doc_id` model-level validator runs (mode="after", once all fields are populated) and cross-checks `doc_id` against `compute_doc_id(source, blob_path)`; `allowed_groups` defaults to `[]` if omitted.
4. `envelope_to_json(envelope)` serializes to the queue message body.
5. Downstream consumer calls `envelope_from_json(raw)` on dequeue; the same field- and model-level validation (including the post-validation `doc_id` cross-check) reruns against the payload.
6. Consumer receives a valid `ArrivalEnvelope`; `doc_id` is used as the ledger/idempotency key by downstream components (out of scope here).

**Failure paths** (all terminate in `EnvelopeValidationError(code="ENVELOPE_INVALID", classification="PERMANENT")`)
- F1. Required field missing (`source`, `blob_path`, `trust_tier`, `content_hash`, `received_at`) → pydantic `ValidationError` → wrapped.
- F2. Unknown/extra field present → strict-schema (`extra="forbid"`) violation → wrapped, message names the offending field.
- F3. `trust_tier` value not a member of `TrustTier` → enum validation failure → wrapped.
- F4. `content_hash` not 64 lowercase hex chars → field validator failure → wrapped.
- F5. `doc_id` present but ≠ `sha256(source + blob_path)` → all per-field validators pass, then the `_verify_doc_id` model-level validator (mode="after") fails → wrapped (tamper/producer-bug guard).
- F6. `allowed_groups` present but not a list of strings (e.g. `null`, string, dict) → type validation failure → wrapped. (Explicit `null` is rejected, not treated as default; only *omission* defaults to `[]`.)
- F7. `received_at` not a parseable ISO-8601 datetime → wrapped.
- F8. `envelope_from_json` given a payload that is not valid JSON → `json.JSONDecodeError` caught and wrapped as the same `ENVELOPE_INVALID` PERMANENT error (no partial/garbage envelope is ever constructed).

## 4. Contract compliance

- **Arrival Envelope**: this module *is* the canonical definition — `ArrivalEnvelope` is the single normalized shape every document must enter through; strict schema (`extra="forbid"`) guarantees no source-specific fields leak past this boundary.
- **Identity & Idempotency**: `doc_id = sha256(source + blob_path)` is computed by `compute_doc_id` and enforced (not merely trusted) via the `_verify_doc_id` model-level validator (`mode="after"`) on every parse, once `source`/`blob_path` are available; `chunk_id` derivation is out of scope (owned by the chunking component, Phase-later).
- **State Ledger**: not applicable here — this module owns no ledger row or stage transition; a ledger row is created by the ingestion coordinator only after an envelope validates successfully (that coordinator is out of scope for REQ-001).
- **Failure Taxonomy**: every failure path in §3 resolves to exactly one code, `ENVELOPE_INVALID`, classified PERMANENT (never retried, always acked); this satisfies the taxonomy's "every error is classified" rule with zero unclassified/default-TRANSIENT cases in this module.

## 5. Error codes

| Code | Classification | Trigger condition |
|---|---|---|
| `ENVELOPE_INVALID` | PERMANENT | Missing required field (`source`, `blob_path`, `trust_tier`, `content_hash`, `received_at`) |
| `ENVELOPE_INVALID` | PERMANENT | Unknown/extra field present (strict-schema violation) |
| `ENVELOPE_INVALID` | PERMANENT | `trust_tier` not a valid `TrustTier` enum member |
| `ENVELOPE_INVALID` | PERMANENT | `content_hash` not 64 lowercase hex characters |
| `ENVELOPE_INVALID` | PERMANENT | `doc_id` does not equal `sha256(source + blob_path)` (caught by the `_verify_doc_id` model-level validator) |
| `ENVELOPE_INVALID` | PERMANENT | `allowed_groups` present but not a `list[str]` (e.g. `null`, scalar) |
| `ENVELOPE_INVALID` | PERMANENT | `received_at` not a parseable ISO-8601 datetime |
| `ENVELOPE_INVALID` | PERMANENT | Malformed (non-JSON) payload passed to `envelope_from_json` |

No TRANSIENT codes originate in this module — pure in-process validation has no external call to
fail transiently.

## 6. Config surface

New file `config/envelope.yaml`:

```yaml
envelope:
  trust_tiers: [public, internal, confidential, restricted]   # documentation/audit only — see below
  content_hash_algo: sha256                                   # documentation/audit only — see below
  content_hash_length: 64          # hex chars — documentation/audit only — see below
  strict_schema: true              # reject unknown fields; must stay true, not overridable to false
  default_allowed_groups: []       # default-deny; must stay empty, not overridable
  error_codes:
    ENVELOPE_INVALID: PERMANENT
```

`trust_tiers`, `content_hash_algo`, `content_hash_length`, `strict_schema`, and
`default_allowed_groups` are all listed for documentation/audit visibility only; none of them are
read at runtime to change behavior. The authoritative definitions live in code: `TrustTier` (fixed
`Enum`), the `_content_hash_is_sha256_hex` field validator (hardcoded sha256/64-hex-char check),
`extra="forbid"` on the model, and the `Field(default_factory=list)` default on `allowed_groups`.
This is a deliberate choice for this module: it is the canonical, contract-defining schema for
Contract #1, and its accepted-value sets and formats are safety-critical invariants, not operator
tunables — a config change alone must never be able to loosen what counts as a valid envelope.
Changing any of these requires a code change (and a new LLD/PR), which is intentional. If a
concrete future need arises to make `trust_tiers` genuinely config-driven (e.g. multi-tenant
tier sets), that is a separate, explicitly-scoped design change, not an implicit side effect of
editing this YAML file.

## 7. Test plan

- `test_compute_doc_id_deterministic` — same `(source, blob_path)` → same `doc_id`, always.
- `test_compute_doc_id_differs_by_source_or_path` — changing either input changes `doc_id`.
- `test_build_envelope_valid_minimal` — only required fields → valid envelope, `allowed_groups == []`.
- `test_build_envelope_valid_full` — all optional fields populated → valid envelope.
- `test_missing_required_field_source` / `..._blob_path` / `..._trust_tier` / `..._content_hash` / `..._received_at` → `ENVELOPE_INVALID` PERMANENT, each field named in `field_errors`.
- `test_unknown_extra_field_rejected` — extra key in input dict/JSON → `ENVELOPE_INVALID` PERMANENT with clear message naming the field.
- `test_invalid_trust_tier_value` — string not in enum → `ENVELOPE_INVALID` PERMANENT.
- `test_invalid_content_hash_format` — wrong length / non-hex chars → `ENVELOPE_INVALID` PERMANENT.
- `test_doc_id_mismatch_rejected` — hand-crafted JSON with `doc_id` not matching `sha256(source+blob_path)`, all other fields individually valid → rejected by the `_verify_doc_id` model-level (`mode="after"`) validator → `ENVELOPE_INVALID` PERMANENT.
- `test_doc_id_verified_only_after_source_and_blob_path_valid` — regression guard: a payload with both an invalid `source` (e.g. empty) and a mismatched `doc_id` reports the `source` field error, not a spurious/undefined doc_id check, confirming the model-level validator runs after field-level validation succeeds.
- `test_allowed_groups_omitted_defaults_to_deny` — omitted field → `[]`, not org-wide.
- `test_allowed_groups_explicit_null_rejected` — `allowed_groups: null` → `ENVELOPE_INVALID` PERMANENT (not silently defaulted).
- `test_allowed_groups_wrong_type_rejected` — string/dict instead of list → `ENVELOPE_INVALID` PERMANENT.
- `test_received_at_invalid_format_rejected` — non-ISO-8601 string → `ENVELOPE_INVALID` PERMANENT.
- `test_malformed_json_rejected` — `envelope_from_json("{not json")` → `ENVELOPE_INVALID` PERMANENT.
- `test_round_trip_lossless` — `envelope_from_json(envelope_to_json(e)) == e` for a fully-populated envelope.
- `test_round_trip_lossless_minimal` — same, with only required fields.
- `test_envelope_is_immutable` — mutating a field on a constructed `ArrivalEnvelope` raises.

## 8. Budget

- p95 latency added per document: < 5ms (pure in-process pydantic validation, no I/O).
- Cost per document: $0 / 0 tokens — no external API calls.
