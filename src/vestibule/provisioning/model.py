"""IndexTemplate/FieldSpec/HnswSettings/IndexRegistryEntry data model, error codes, and
`ProvisioningError` (REQ-011).

Mirrors `vestibule.scenario.model`'s shape: model + exception + `register_error` calls,
no store I/O (see `templates.py`/`registry.py`/`provisioner.py` for that).

Failure taxonomy: this module registers all eleven error codes at import time
(Contract #4) — seven PERMANENT (`INDEX_TEMPLATE_NOT_FOUND`, `INDEX_TEMPLATE_INVALID`,
`INDEX_SCHEMA_DRIFT`, `INDEX_PROVISION_FAILED`, `INDEX_RETIRED`,
`INDEX_AUTO_CREATE_DISABLED`, `INDEX_PROVISIONER_DEPENDENCY_MISSING`) and four TRANSIENT
(`INDEX_PROVISION_TIMEOUT`, `INDEX_PROVISION_CONFLICT`, `INDEX_REGISTRY_UNAVAILABLE`,
`INDEX_PROVISIONER_UNAVAILABLE`). See `docs/designs/REQ-011-lld.md` §5 for the full
trigger-condition table.

Spec-gap resolution (Assumption A7 — no catch-all `*_INTERNAL`-style code): unlike every
other module in this codebase, the story's eleven codes provide no dedicated internal/
catch-all code. Any exception from `IndexProvisionerAdapter.create_index`/
`describe_index`/`index_exists` not already mapped to
`INDEX_PROVISIONER_DEPENDENCY_MISSING` (import-time-only) is wrapped by
`IndexProvisioner` (see `provisioner.py`) as `INDEX_PROVISIONER_UNAVAILABLE`
(TRANSIENT) — the closest declared code, consistent with Contract #4's "unclassified
errors default to TRANSIENT."
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from vestibule.errors.registry import RaggedError, Severity, register_error

INDEX_TEMPLATE_NOT_FOUND = "INDEX_TEMPLATE_NOT_FOUND"
INDEX_TEMPLATE_INVALID = "INDEX_TEMPLATE_INVALID"
INDEX_SCHEMA_DRIFT = "INDEX_SCHEMA_DRIFT"
INDEX_PROVISION_FAILED = "INDEX_PROVISION_FAILED"
INDEX_RETIRED = "INDEX_RETIRED"
INDEX_AUTO_CREATE_DISABLED = "INDEX_AUTO_CREATE_DISABLED"
INDEX_PROVISION_TIMEOUT = "INDEX_PROVISION_TIMEOUT"
INDEX_PROVISION_CONFLICT = "INDEX_PROVISION_CONFLICT"
INDEX_REGISTRY_UNAVAILABLE = "INDEX_REGISTRY_UNAVAILABLE"
INDEX_PROVISIONER_UNAVAILABLE = "INDEX_PROVISIONER_UNAVAILABLE"
INDEX_PROVISIONER_DEPENDENCY_MISSING = "INDEX_PROVISIONER_DEPENDENCY_MISSING"

FieldType = Literal[
    "key",
    "text",
    "vector",
    "filterable_string",
    "filterable_string_collection",
    "filterable_datetime",
    "retrievable_only",
]


class FieldSpec(BaseModel):
    """One store-agnostic schema field.

    Attributes:
        name: The field's name.
        type: The field's store-agnostic type.
        searchable: Whether the field participates in full-text search.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    type: FieldType
    searchable: bool = False


class HnswSettings(BaseModel):
    """HNSW vector-index tuning.

    Attributes:
        m: Number of bi-directional links per node.
        ef_construction: Size of the dynamic candidate list at index-build time.
        ef_search: Size of the dynamic candidate list at query time.
    """

    model_config = ConfigDict(frozen=True)

    m: int
    ef_construction: int
    ef_search: int


class IndexTemplate(BaseModel):
    """Versioned, store-agnostic index schema (REQ-011).

    Frozen, self-validating — loaded from `config/index_templates/*.yaml` by
    `IndexTemplateStore` (`templates.py`).

    Attributes:
        template_id: e.g. `"standard-v1"` — the config filename's stem, cross-checked
            against the file's own declared `template_id` at load time (mirrors
            `YamlScenarioStore`'s fail-fast-on-load pattern).
        template_version: Non-negative-integer string (Assumption A6) — bumped when the
            schema changes; drives `INDEX_SCHEMA_DRIFT`'s staleness ordering.
        dimensions: Explicit override, or `None` to inherit
            `scenario.indexer.dimensions` (Assumption A2).
        metric: `"cosine" | "dotProduct" | "euclidean"`.
        hnsw: HNSW tuning (Assumption A11: this REQ's sole source of truth for
            creation-time HNSW parameters — `Scenario.indexer.hnsw_*` is not read here).
        fields: Store-agnostic schema, validated for exactly one `"key"` field and at
            least one `"vector"` field (`model_validator`, below).
        semantic_ranker_enabled: Whether `AzureAISearchProvisioner` configures a
            semantic-search config on the index (schema-level).
        hybrid_enabled: Recorded for provenance/future query-side use; no schema
            effect (BM25-searchable `"text"` fields are already hybrid-eligible).
    """

    model_config = ConfigDict(frozen=True)

    template_id: str
    template_version: str
    dimensions: int | None
    metric: Literal["cosine", "dotProduct", "euclidean"]
    hnsw: HnswSettings
    fields: list[FieldSpec]
    semantic_ranker_enabled: bool
    hybrid_enabled: bool

    @field_validator("template_version")
    @classmethod
    def _version_is_integer_string(cls, value: str) -> str:
        """Validates `template_version` parses as a non-negative integer (Assumption A6).

        Raises:
            ValueError: If `value` does not parse as `int(...) >= 0`.
        """
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(
                f"template_version={value!r} must parse as a non-negative integer "
                "(Assumption A6)"
            ) from exc
        if parsed < 0:
            raise ValueError(f"template_version={value!r} must be non-negative")
        return value

    @model_validator(mode="after")
    def _validate_field_shape(self) -> "IndexTemplate":
        """Validates `fields`'s overall shape.

        Raises:
            ValueError: Unless exactly one `FieldSpec` has `type == "key"`, at least
                one has `type == "vector"`, and every `name` is unique —
                `INDEX_TEMPLATE_INVALID`'s "invalid field spec" trigger.
        """
        key_fields = [f for f in self.fields if f.type == "key"]
        vector_fields = [f for f in self.fields if f.type == "vector"]
        names = [f.name for f in self.fields]
        if len(key_fields) != 1:
            raise ValueError(
                "IndexTemplate must declare exactly one field with type='key', "
                f"found {len(key_fields)}"
            )
        if not vector_fields:
            raise ValueError(
                "IndexTemplate must declare at least one field with type='vector'"
            )
        if len(names) != len(set(names)):
            raise ValueError("IndexTemplate field names must be unique")
        return self


class IndexRegistryEntry(BaseModel):
    """One row per provisioned (or in-flight) index (REQ-011).

    Frozen.

    Attributes:
        index_name: Key.
        vertical: Owning vertical.
        scenario_id: The scenario that triggered this index's (last) provisioning.
        template_id: Resolved template id at (last) provisioning/reclaim time.
        template_version: Resolved template version — drift-checked (Assumption A6).
        dimensions: Resolved effective dimensions (Assumption A2).
        metric: Resolved metric.
        embedding_model: `scenario.embedder.model`.
        status: `provisioning | ready | failed | retired`.
        created_at: Set once, when this `index_name`'s row is first inserted
            (`register()`'s winning insert); preserved across reclaim (Assumption A3).
        last_verified_at: `None` until the first `mark_ready`/`touch_verified`; drives
            `verification_interval_seconds` caching.
        document_count: `None` — no writer in this REQ populates it (out of scope).
        claim_token: Opaque per-claim token (Assumption A3/A4) — non-`None` only while
            `status == "provisioning"`; `None` otherwise. Cross-process-safe: an
            unguessable value (`uuid4().hex`), never interpreted, only compared for
            equality by whichever backend's CAS primitive is checking it.
        claimed_at: When the *current* claim was made or last reclaimed (Assumption
            A3) — distinct from `created_at`; drives `provisioning_stale_after_seconds`.
        last_error_message: `mark_failed`'s `reason`, for `INDEX_PROVISION_FAILED`'s
            "requires human intervention" to be actionable (Assumption A3).
        resolved_template: The exact `IndexTemplate` resolved at the moment this claim
            (original or reclaimed) was written — non-`None` only while
            `status == "provisioning"`; cleared at `mark_ready`/`mark_failed` exactly
            like `claim_token`/`claimed_at` (Assumption A14).
    """

    model_config = ConfigDict(frozen=True)

    index_name: str
    vertical: str
    scenario_id: str
    template_id: str
    template_version: str
    dimensions: int
    metric: str
    embedding_model: str
    status: Literal["provisioning", "ready", "failed", "retired"]
    created_at: datetime
    last_verified_at: datetime | None
    document_count: int | None
    claim_token: str | None
    claimed_at: datetime | None
    last_error_message: str | None
    resolved_template: IndexTemplate | None


class IndexDescription(BaseModel):
    """Live-index facts an `IndexProvisionerAdapter` can report, for drift detection.

    Frozen. Deliberately minimal — no `template_version` (no store tracks this REQ's
    own metadata); `template_version` drift is checked against the registry entry's own
    recorded value instead.

    Attributes:
        dimensions: The live index's vector dimension count.
        metric: The live index's configured similarity metric.
    """

    model_config = ConfigDict(frozen=True)

    dimensions: int
    metric: str


class ProvisioningError(RaggedError):
    """Single exception type for every provisioning-module-raised failure.

    `error_code` varies per raise site (same shape as `ScenarioError`/`ChunkerError`/
    `EmbedderError`/`IndexerError`).

    Attributes:
        index_name: The `index_name` involved, or `""` for a failure not tied to any
            specific index (e.g. a template-store load failure before any index is in
            scope).
        reason: Human-readable failure reason.
    """

    def __init__(self, index_name: str, reason: str, *, error_code: str) -> None:
        """Initializes the exception.

        Args:
            index_name: The `index_name` involved, or `""`.
            reason: Human-readable failure reason.
            error_code: One of this module's eleven registered error codes.
        """
        self.index_name = index_name
        self.reason = reason
        super().__init__(f"{index_name}: {reason}", error_code=error_code)


register_error(
    INDEX_TEMPLATE_NOT_FOUND,
    Severity.PERMANENT,
    "no IndexTemplate is loaded for the resolved template_id (REQ-011)",
)
register_error(
    INDEX_TEMPLATE_INVALID,
    Severity.PERMANENT,
    "a template file failed validation at load time, or its resolved dimensions "
    "disagree with scenario.indexer.dimensions at provisioning time (REQ-011)",
)
register_error(
    INDEX_SCHEMA_DRIFT,
    Severity.PERMANENT,
    "a live index's dimensions/metric/template_version are incompatible with the "
    "resolved template; never auto-migrated, requires human intervention (REQ-011)",
)
register_error(
    INDEX_PROVISION_FAILED,
    Severity.PERMANENT,
    "the registry entry for this index_name is terminally failed; requires human "
    "intervention, never retried automatically (REQ-011)",
)
register_error(
    INDEX_RETIRED,
    Severity.PERMANENT,
    "the registry entry for this index_name has been retired (REQ-011)",
)
register_error(
    INDEX_AUTO_CREATE_DISABLED,
    Severity.PERMANENT,
    "no index exists for this index_name and config.auto_create is False (REQ-011)",
)
register_error(
    INDEX_PROVISION_TIMEOUT,
    Severity.TRANSIENT,
    "a waiting worker exceeded wait_for_provisioning_timeout_seconds without "
    "observing the claim reach ready (REQ-011)",
)
register_error(
    INDEX_PROVISION_CONFLICT,
    Severity.TRANSIENT,
    "an IndexRegistry CAS write (reclaim/mark_ready/mark_failed) lost a race against "
    "a concurrent writer; the caller should re-read and retry (REQ-011)",
)
register_error(
    INDEX_REGISTRY_UNAVAILABLE,
    Severity.TRANSIENT,
    "TableStorageIndexRegistry returned an HTTP 429/5xx error or timed out after "
    "exhausting in-adapter retries (REQ-011)",
)
register_error(
    INDEX_PROVISIONER_UNAVAILABLE,
    Severity.TRANSIENT,
    "an IndexProvisionerAdapter call exhausted retries against an HTTP 429/5xx/"
    "timeout, or raised an exception not otherwise mapped (Assumption A7) (REQ-011)",
)
register_error(
    INDEX_PROVISIONER_DEPENDENCY_MISSING,
    Severity.PERMANENT,
    "a required optional dependency (azure-search-documents/azure-data-tables) is "
    "not installed; never retryable — the caller must install the extra (REQ-011)",
)
