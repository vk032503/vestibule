"""Scenario — data model, validation, and error-code registration (REQ-010).

`Scenario` is the frozen pydantic model this REQ persists per vertical: an admin's
declared ingestion settings for one vertical (chunker/embedder/indexer tunables, ACL
source, default trust tier), read at runtime by whichever `ScenarioStore` backend is
configured (`vestibule.scenario.stores`). This module owns exactly the model, its own
validation, and the pure `to_chunker_config`/`to_embedder_config`/`to_indexer_config`
converters — it owns no store I/O itself (see `stores/`).

Contract #3 touchpoint: this module (and the `stores/` package built on it) writes no
`LedgerRow`, ever — a `Scenario`/`ScenarioStore` is a config lookup, not a pipeline
stage.

Failure taxonomy: this module registers six error codes at import time (Contract #4) —
four PERMANENT (`SCENARIO_NOT_FOUND`, `SCENARIO_INVALID`, `SCENARIO_STORE_READ_ONLY`,
`SCENARIO_STORE_DEPENDENCY_MISSING`) and two TRANSIENT (`SCENARIO_CONFLICT`,
`SCENARIO_STORE_UNAVAILABLE`). See `stores/base.py`/`stores/yaml_store.py`/
`stores/table_storage_store.py` for how each is raised.

Validation is enforced by `Scenario`'s own `model_validator(mode="after")` — the
pydantic constructor call itself, `Scenario(...)`, is where every check below runs, so
no invalid `Scenario` instance can ever exist (mirrors `ArrivalEnvelope`'s own
`_verify_doc_id` model-level validator, REQ-001). `Scenario(...)` raises a plain
pydantic `ValidationError` (from a `ValueError` inside the validator — standard pydantic
convention), not this module's own `ScenarioError`; callers constructing a `Scenario`
from an external, untrusted source (a YAML file, a Table Storage entity) are expected to
catch that `ValidationError` and re-raise `ScenarioError(SCENARIO_INVALID)` themselves
(`stores/yaml_store.py`/`stores/table_storage_store.py` do exactly this, mirroring
`vestibule.envelope.model._wrap_validation_error`). This is what makes AC5 true: a
malformed scenario fails before it ever becomes a usable `Scenario` object, i.e. before
any document is ever in scope.

Spec-gap resolution (native embedding dimensions): validating
`EmbedderSettings.target_dimensions` against a model's native dimensionality needs a
value with no I/O, no credentials, and no optional-dependency import — but no static
"native dimensions per model" table exists anywhere else in this codebase
(`EmbedderAdapter.native_dimensions` is only available on a *constructed* adapter
instance, which would require exactly the credentials/downloads this validation must
avoid). See `_validation.py`'s own `_NATIVE_DIMENSIONS` for the small, explicit,
module-owned `(adapter, model) -> native_dimensions` registry this resolves to.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from vestibule.chunker.chunker import ChunkerConfig
from vestibule.embedder.embedder import EmbedderConfig
from vestibule.errors.registry import RaggedError, Severity, register_error
from vestibule.indexer.indexer import IndexerConfig
from vestibule.scenario._validation import (
    validate_chunker_bounds,
    validate_dimension_consistency,
    validate_index_name,
)

SCENARIO_NOT_FOUND = "SCENARIO_NOT_FOUND"
SCENARIO_INVALID = "SCENARIO_INVALID"
SCENARIO_STORE_READ_ONLY = "SCENARIO_STORE_READ_ONLY"
SCENARIO_CONFLICT = "SCENARIO_CONFLICT"
SCENARIO_STORE_UNAVAILABLE = "SCENARIO_STORE_UNAVAILABLE"
SCENARIO_STORE_DEPENDENCY_MISSING = "SCENARIO_STORE_DEPENDENCY_MISSING"


class ChunkerSettings(BaseModel):
    """Mirrors `ChunkerConfig`'s tunables (REQ-006) — see `Scenario.to_chunker_config`.

    Attributes:
        target_chunk_tokens: Target size, in tokens, for a recursively-split chunk.
        overlap_tokens: Token overlap between consecutive recursively-split chunks.
        min_chunk_tokens: Threshold below which a trailing chunk is merged into its
            predecessor.
        max_chunk_tokens: Hard ceiling for a non-table chunk.
    """

    model_config = ConfigDict(frozen=True)

    target_chunk_tokens: int
    overlap_tokens: int
    min_chunk_tokens: int
    max_chunk_tokens: int


class EmbedderSettings(BaseModel):
    """Mirrors `EmbedderConfig`'s tunables plus adapter-selection metadata (REQ-007).

    `adapter`/`model` are composition-root metadata only (which `EmbedderAdapter`
    implementation a caller, e.g. REQ-009's Vertical Loader, should construct) —
    `Scenario.to_embedder_config` does not consume them, mirroring
    `config/embedder.yaml`'s own `adapter`/`model` keys never appearing on
    `EmbedderConfig` itself.

    Attributes:
        adapter: Which `EmbedderAdapter` implementation this scenario expects.
        model: The adapter-specific model identifier, e.g. `"text-embedding-3-large"`.
        target_dimensions: Matryoshka (MRL) truncation target, or `None` for no
            truncation (the model's native dimensionality is used unmodified).
    """

    model_config = ConfigDict(frozen=True)

    adapter: Literal["azure_openai", "fastembed"]
    model: str
    target_dimensions: int | None


class IndexerSettings(BaseModel):
    """Mirrors `IndexerConfig`'s tunables plus HNSW metadata (REQ-008).

    `hnsw_m`/`hnsw_ef_construction`/`hnsw_ef_search` are `AzureAISearchIndexer`
    constructor metadata only (not `IndexerConfig` fields) — `Scenario.to_indexer_config`
    does not consume them, mirroring `AzureAISearchIndexer`'s own constructor parameters
    never appearing on `IndexerConfig` itself.

    Attributes:
        metric: The vector similarity metric, e.g. `"cosine"`.
        dimensions: The expected vector dimension count — must equal
            `EmbedderSettings.target_dimensions` (or the model's native dimensions if
            not truncating); enforced by `Scenario`'s own validation (AC5).
        hnsw_m: HNSW `m` parameter metadata.
        hnsw_ef_construction: HNSW `ef_construction` parameter metadata.
        hnsw_ef_search: HNSW `ef_search` parameter metadata.
    """

    model_config = ConfigDict(frozen=True)

    metric: str
    dimensions: int
    hnsw_m: int
    hnsw_ef_construction: int
    hnsw_ef_search: int


class Scenario(BaseModel):
    """Per-vertical ingestion settings, read at runtime (REQ-010). Frozen, self-validating.

    Attributes:
        scenario_id: Unique key, e.g. `"hr-policies-v2"`.
        vertical: The vertical this scenario applies to, e.g. `"hr"`.
        config_version: Bumped on every edit (`ScenarioStore.upsert`); stamped onto
            chunks/the ledger row so config drift is traceable (Contract #2 touchpoint —
            the Indexer already reads it from the ledger row, REQ-008).
        index_name: The vector index this scenario's documents are written to.
        chunker: Chunker tunables — see `ChunkerSettings`.
        embedder: Embedder tunables — see `EmbedderSettings`.
        indexer: Indexer tunables — see `IndexerSettings`.
        acl_source: Which field on the arrival envelope/document supplies
            `allowed_groups` for this vertical.
        default_trust_tier: The trust tier assigned when a document's own tier is
            unspecified.
        enabled: Soft-disable a scenario without deleting it (excluded from
            `ScenarioStore.get_by_vertical`/`list_all(include_disabled=False)` when
            `False`).
        created_at: UTC timestamp this scenario was first created.
        updated_at: UTC timestamp this scenario was last edited.
    """

    model_config = ConfigDict(frozen=True)

    scenario_id: str
    vertical: str
    config_version: str
    index_name: str
    chunker: ChunkerSettings
    embedder: EmbedderSettings
    indexer: IndexerSettings
    acl_source: Literal["envelope", "blob_metadata", "source_system"]
    default_trust_tier: str
    enabled: bool
    created_at: datetime
    updated_at: datetime

    @field_validator("scenario_id", "vertical", "default_trust_tier")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("must be non-empty")
        return value

    @model_validator(mode="after")
    def _validate_cross_field_invariants(self) -> "Scenario":
        """Runs every cross-field check at construction time — the AC5 fail-fast guard.

        Raises:
            ValueError: On the first failing check (`index_name` shape, chunker token
                bounds, or embedder/indexer dimension consistency); pydantic wraps this
                into a `ValidationError` for `Scenario(...)`'s caller.
        """
        validate_index_name(self.index_name)
        validate_chunker_bounds(self.chunker)
        validate_dimension_consistency(self.embedder, self.indexer)
        return self

    def to_chunker_config(self) -> ChunkerConfig:
        """Converts to the real `ChunkerConfig` the REQ-006 `Chunker` accepts unmodified.

        Returns:
            A `ChunkerConfig` built from `self.chunker`'s fields; `token_counter` is
            left at `ChunkerConfig`'s own default (not a `ChunkerSettings` field).
        """
        return ChunkerConfig(
            target_chunk_tokens=self.chunker.target_chunk_tokens,
            overlap_tokens=self.chunker.overlap_tokens,
            min_chunk_tokens=self.chunker.min_chunk_tokens,
            max_chunk_tokens=self.chunker.max_chunk_tokens,
        )

    def to_embedder_config(self) -> EmbedderConfig:
        """Converts to the real `EmbedderConfig` the REQ-007 `Embedder` accepts unmodified.

        Returns:
            An `EmbedderConfig` built from `self.embedder.target_dimensions`;
            `batch_size`/`retry` are left at `EmbedderConfig`'s own defaults (not
            `EmbedderSettings` fields — `adapter`/`model` are composition-root concerns,
            per this module's docstring).
        """
        return EmbedderConfig(target_dimensions=self.embedder.target_dimensions)

    def to_indexer_config(self) -> IndexerConfig:
        """Converts to the real `IndexerConfig` the REQ-008 `Indexer` accepts unmodified.

        Returns:
            An `IndexerConfig` built from `self.indexer.dimensions`/`.metric`;
            `batch_size`/`delete_before_upsert_on_version_change`/`retry` are left at
            `IndexerConfig`'s own defaults (not `IndexerSettings` fields — HNSW params
            are `AzureAISearchIndexer` constructor metadata, per this module's
            docstring).
        """
        return IndexerConfig(
            dimensions=self.indexer.dimensions, metric=self.indexer.metric
        )


class ScenarioError(RaggedError):
    """Single exception type for every Scenario/ScenarioStore-raised failure.

    `error_code` varies per raise site (same shape as REQ-006/007/008's
    `ChunkerError`/`EmbedderError`/`IndexerError`).

    Attributes:
        scenario_id: The `scenario_id` involved, or `""` for a failure not tied to any
            specific scenario (e.g. `SCENARIO_STORE_DEPENDENCY_MISSING`, raised at
            store construction, before any scenario is in scope).
        reason: Human-readable failure reason.
    """

    def __init__(self, scenario_id: str, reason: str, *, error_code: str) -> None:
        """Initializes the exception.

        Args:
            scenario_id: The `scenario_id` involved, or `""`.
            reason: Human-readable failure reason.
            error_code: One of this module's six registered error codes.
        """
        self.scenario_id = scenario_id
        self.reason = reason
        super().__init__(f"{scenario_id}: {reason}", error_code=error_code)


register_error(
    SCENARIO_NOT_FOUND,
    Severity.PERMANENT,
    "no scenario exists for the requested scenario_id or vertical, and no "
    "default_scenario_id resolved either (REQ-010)",
)
register_error(
    SCENARIO_INVALID,
    Severity.PERMANENT,
    "a scenario failed validation (dimension mismatch, impossible MRL truncation, "
    "invalid index_name, or invalid chunker token bounds) — raised at construction, "
    "before any document is ever processed (REQ-010)",
)
register_error(
    SCENARIO_STORE_READ_ONLY,
    Severity.PERMANENT,
    "upsert/delete was attempted against a read-only ScenarioStore backend (REQ-010)",
)
register_error(
    SCENARIO_CONFLICT,
    Severity.TRANSIENT,
    "an optimistic-concurrency ETag conflict occurred on a TableStorageScenarioStore "
    "upsert; the caller should re-read and retry (REQ-010)",
)
register_error(
    SCENARIO_STORE_UNAVAILABLE,
    Severity.TRANSIENT,
    "Table Storage returned an HTTP 429/5xx error or timed out after exhausting "
    "in-adapter retries (REQ-010)",
)
register_error(
    SCENARIO_STORE_DEPENDENCY_MISSING,
    Severity.PERMANENT,
    "TableStorageScenarioStore's required optional dependency (azure-data-tables) is "
    "not installed; never retryable — the caller must install the extra (REQ-010)",
)
