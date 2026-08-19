"""Unit tests for the Scenario data model (REQ-010)."""

from __future__ import annotations

import string
from datetime import datetime, timezone
from typing import Any, Literal

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from vestibule.chunker.chunker import Chunker, ChunkerConfig
from vestibule.chunker.token_counter import TiktokenCounter
from vestibule.embedder.adapters import azure_openai as azure_openai_adapter
from vestibule.embedder.adapters import fastembed as fastembed_adapter
from vestibule.embedder.adapters.azure_openai import AzureOpenAIEmbedder
from vestibule.embedder.adapters.base import EmbedderAdapter
from vestibule.embedder.adapters.fastembed import FastEmbedEmbedder
from vestibule.embedder.embedder import Embedder, EmbedderConfig
from vestibule.errors.registry import Severity, classify
from vestibule.indexer.adapters.in_memory import InMemoryIndexer
from vestibule.indexer.indexer import Indexer, IndexerConfig
from vestibule.ledger.store import InMemoryLedgerStore
from vestibule.scenario._validation import _NATIVE_DIMENSIONS
from vestibule.scenario.model import (
    SCENARIO_CONFLICT,
    SCENARIO_INVALID,
    SCENARIO_NOT_FOUND,
    SCENARIO_STORE_DEPENDENCY_MISSING,
    SCENARIO_STORE_READ_ONLY,
    SCENARIO_STORE_UNAVAILABLE,
    ChunkerSettings,
    EmbedderSettings,
    IndexerSettings,
    Scenario,
    ScenarioError,
)

_FIXED_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _scenario(**overrides: Any) -> Scenario:
    defaults: dict[str, Any] = {
        "scenario_id": "hr-policies-v1",
        "vertical": "hr",
        "config_version": "1",
        "index_name": "vestibule-hr-v1",
        "chunker": ChunkerSettings(
            target_chunk_tokens=512,
            overlap_tokens=64,
            min_chunk_tokens=40,
            max_chunk_tokens=800,
        ),
        "embedder": EmbedderSettings(
            adapter="azure_openai",
            model="text-embedding-3-large",
            target_dimensions=1024,
        ),
        "indexer": IndexerSettings(
            metric="cosine",
            dimensions=1024,
            hnsw_m=4,
            hnsw_ef_construction=400,
            hnsw_ef_search=500,
        ),
        "acl_source": "envelope",
        "default_trust_tier": "internal",
        "enabled": True,
        "created_at": _FIXED_TIME,
        "updated_at": _FIXED_TIME,
    }
    defaults.update(overrides)
    return Scenario(**defaults)


# --- basic construction / frozen ------------------------------------------------------------


def test_scenario_constructs_with_valid_fields() -> None:
    scenario = _scenario()
    assert scenario.scenario_id == "hr-policies-v1"
    assert scenario.vertical == "hr"


def test_scenario_index_template_id_defaults_to_none() -> None:
    scenario = _scenario()
    assert scenario.index_template_id is None


def test_scenario_accepts_explicit_index_template_id() -> None:
    scenario = _scenario(index_template_id="standard-v1")
    assert scenario.index_template_id == "standard-v1"


def test_scenario_existing_construction_call_shapes_unaffected_by_index_template_id_field() -> (
    None
):
    """REQ-011 Assumption A1 — mirrors REQ-004's
    `test_existing_constructor_signatures_unchanged` precedent: an exact pre-REQ-011
    `Scenario(...)` call shape (omitting the new `index_template_id` keyword entirely)
    continues to construct successfully, defaulting the new field to `None`."""
    scenario = Scenario(
        scenario_id="hr-policies-v1",
        vertical="hr",
        config_version="1",
        index_name="vestibule-hr-v1",
        chunker=ChunkerSettings(
            target_chunk_tokens=512,
            overlap_tokens=64,
            min_chunk_tokens=40,
            max_chunk_tokens=800,
        ),
        embedder=EmbedderSettings(
            adapter="azure_openai",
            model="text-embedding-3-large",
            target_dimensions=1024,
        ),
        indexer=IndexerSettings(
            metric="cosine",
            dimensions=1024,
            hnsw_m=4,
            hnsw_ef_construction=400,
            hnsw_ef_search=500,
        ),
        acl_source="envelope",
        default_trust_tier="internal",
        enabled=True,
        created_at=_FIXED_TIME,
        updated_at=_FIXED_TIME,
    )
    assert scenario.index_template_id is None


def test_scenario_is_frozen() -> None:
    scenario = _scenario()
    with pytest.raises(ValidationError):
        scenario.scenario_id = "other"


@pytest.mark.parametrize("field", ["scenario_id", "vertical", "default_trust_tier"])
def test_scenario_rejects_empty_required_string_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        _scenario(**{field: ""})


# --- AC5: dimension-consistency validation at construction time ------------------------------


def test_scenario_rejects_indexer_dimensions_mismatched_with_embedder_target() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _scenario(
            embedder=EmbedderSettings(
                adapter="azure_openai",
                model="text-embedding-3-large",
                target_dimensions=1024,
            ),
            indexer=IndexerSettings(
                metric="cosine",
                dimensions=1536,
                hnsw_m=4,
                hnsw_ef_construction=400,
                hnsw_ef_search=500,
            ),
        )
    assert "does not match" in str(exc_info.value)


def test_scenario_rejects_target_dimensions_exceeding_native_dimensions() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _scenario(
            embedder=EmbedderSettings(
                adapter="fastembed",
                model="nomic-ai/nomic-embed-text-v1.5",
                target_dimensions=4096,
            ),
            indexer=IndexerSettings(
                metric="cosine",
                dimensions=4096,
                hnsw_m=4,
                hnsw_ef_construction=400,
                hnsw_ef_search=500,
            ),
        )
    assert "exceeds" in str(exc_info.value)


def test_scenario_rejects_unrecognized_adapter_model_combination() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _scenario(
            embedder=EmbedderSettings(
                adapter="azure_openai",
                model="text-embedding-ada-002",
                target_dimensions=100,
            ),
        )
    assert "not a recognized" in str(exc_info.value)


def test_scenario_accepts_null_target_dimensions_matching_native_indexer_dimensions() -> (
    None
):
    scenario = _scenario(
        embedder=EmbedderSettings(
            adapter="fastembed",
            model="nomic-ai/nomic-embed-text-v1.5",
            target_dimensions=None,
        ),
        indexer=IndexerSettings(
            metric="cosine",
            dimensions=768,
            hnsw_m=4,
            hnsw_ef_construction=400,
            hnsw_ef_search=500,
        ),
    )
    assert scenario.embedder.target_dimensions is None
    assert scenario.indexer.dimensions == 768


# --- _NATIVE_DIMENSIONS registry integrity --------------------------------------------------
#
# Regression guard for the exact bug REQ-007 already hit once (see `FastEmbedEmbedder`'s
# module docstring): a hand-typed `(adapter, model)` key in this registry can silently
# drift from the adapter's real default model name (e.g. missing the `nomic-ai/`
# namespace prefix `fastembed` itself requires), and every fake-based test above sails
# through undetected because none of them construct a real adapter. These tests close
# that gap by checking the registry against each adapter module's own source of truth
# instead of trusting the table's contents.


class _FakeTextEmbedding:
    """Minimal stand-in for `fastembed.TextEmbedding` (mirrors `test_fastembed.py`'s
    own fake) — only used here to construct a real `FastEmbedEmbedder` without a real
    ONNX model load."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]


def test_native_dimensions_fastembed_key_matches_adapters_actual_default_model() -> (
    None
):
    assert (
        "fastembed",
        fastembed_adapter._DEFAULT_MODEL,
    ) in _NATIVE_DIMENSIONS


def test_native_dimensions_azure_openai_key_matches_adapters_actual_default_model() -> (
    None
):
    assert (
        "azure_openai",
        azure_openai_adapter._DEFAULT_MODEL,
    ) in _NATIVE_DIMENSIONS


def test_native_dimensions_fastembed_value_matches_a_constructed_adapters_native_dimensions() -> (
    None
):
    """Goes beyond comparing the model string: constructs a real `FastEmbedEmbedder`
    (via its fake-injection seam, so no real ONNX model load) at its default model and
    asserts the registry's native-dimensions *value* also matches the constructed
    adapter's own `native_dimensions`."""
    adapter = FastEmbedEmbedder(_text_embedding=_FakeTextEmbedding())
    key = ("fastembed", adapter.model_name)
    assert _NATIVE_DIMENSIONS[key] == adapter.native_dimensions


def test_native_dimensions_azure_openai_value_matches_a_constructed_adapters_native_dimensions() -> (
    None
):
    """Same as the fastembed variant above, but for `AzureOpenAIEmbedder`; construction
    only builds a client object and makes no network call, so this is hermetic."""
    adapter = AzureOpenAIEmbedder(
        endpoint="https://fake.example.com",
        api_key="fake-key",
        api_version="2024-02-01",
    )
    key = ("azure_openai", adapter.model_name)
    assert _NATIVE_DIMENSIONS[key] == adapter.native_dimensions


# --- chunker token bounds ---------------------------------------------------------------------


def test_scenario_rejects_target_chunk_tokens_below_min() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _scenario(
            chunker=ChunkerSettings(
                target_chunk_tokens=10,
                overlap_tokens=2,
                min_chunk_tokens=40,
                max_chunk_tokens=800,
            )
        )
    assert "chunker token bounds violated" in str(exc_info.value)


def test_scenario_rejects_target_chunk_tokens_above_max() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _scenario(
            chunker=ChunkerSettings(
                target_chunk_tokens=900,
                overlap_tokens=2,
                min_chunk_tokens=40,
                max_chunk_tokens=800,
            )
        )
    assert "chunker token bounds violated" in str(exc_info.value)


# --- index_name shape ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    ["Vestibule-HR", "a", "-leading-dash", "trailing-dash-", "has_underscore", ""],
    ids=[
        "uppercase",
        "too_short",
        "leading_dash",
        "trailing_dash",
        "underscore",
        "empty",
    ],
)
def test_scenario_rejects_invalid_index_name(bad_name: str) -> None:
    with pytest.raises(ValidationError):
        _scenario(index_name=bad_name)


def test_scenario_accepts_valid_index_name_at_minimum_length() -> None:
    scenario = _scenario(index_name="ab")
    assert scenario.index_name == "ab"


# --- converters (AC6) -------------------------------------------------------------------------


def test_to_chunker_config_produces_config_real_chunker_accepts() -> None:
    scenario = _scenario()
    config = scenario.to_chunker_config()
    assert isinstance(config, ChunkerConfig)
    assert config.target_chunk_tokens == 512
    Chunker(InMemoryLedgerStore(), config, TiktokenCounter())


def test_to_embedder_config_produces_config_real_embedder_accepts() -> None:
    scenario = _scenario()
    config = scenario.to_embedder_config()
    assert isinstance(config, EmbedderConfig)
    assert config.target_dimensions == 1024
    Embedder(InMemoryLedgerStore(), config, _FakeEmbedderAdapter())


def test_to_indexer_config_produces_config_real_indexer_accepts() -> None:
    scenario = _scenario()
    config = scenario.to_indexer_config()
    assert isinstance(config, IndexerConfig)
    assert config.dimensions == 1024
    Indexer(InMemoryLedgerStore(), config, InMemoryIndexer())


class _FakeEmbedderAdapter(EmbedderAdapter):
    """Minimal `EmbedderAdapter` for `to_embedder_config` construction tests."""

    @property
    def model_name(self) -> str:
        return "fake-model"

    @property
    def native_dimensions(self) -> int:
        return 3072

    @property
    def max_batch_size(self) -> int:
        return 16

    @property
    def max_input_tokens(self) -> int:
        return 1000

    @property
    def supports_mrl(self) -> bool:
        return True

    @property
    def document_prefix(self) -> str | None:
        return None

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] * self.native_dimensions for _ in texts]


# --- error registration (Contract #4) ----------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected_severity",
    [
        (SCENARIO_NOT_FOUND, Severity.PERMANENT),
        (SCENARIO_INVALID, Severity.PERMANENT),
        (SCENARIO_STORE_READ_ONLY, Severity.PERMANENT),
        (SCENARIO_STORE_DEPENDENCY_MISSING, Severity.PERMANENT),
        (SCENARIO_CONFLICT, Severity.TRANSIENT),
        (SCENARIO_STORE_UNAVAILABLE, Severity.TRANSIENT),
    ],
)
def test_error_code_registered_with_expected_severity(
    code: str, expected_severity: Severity
) -> None:
    assert classify(code) == expected_severity


def test_scenario_error_carries_scenario_id_reason_and_error_code() -> None:
    exc = ScenarioError("sid", "boom", error_code=SCENARIO_INVALID)
    assert exc.scenario_id == "sid"
    assert exc.reason == "boom"
    assert exc.error_code == SCENARIO_INVALID
    assert str(exc) == "sid: boom"


# --- property-based: YAML round trip (story) ----------------------------------------------------


_ADAPTER_MODEL_NATIVE_CHOICES: list[
    tuple[Literal["azure_openai", "fastembed"], str, int]
] = [
    ("azure_openai", "text-embedding-3-large", 3072),
    ("fastembed", "nomic-ai/nomic-embed-text-v1.5", 768),
]
_ADAPTER_MODEL_NATIVE = st.sampled_from(_ADAPTER_MODEL_NATIVE_CHOICES)
_ID_ALPHABET = string.ascii_lowercase + string.digits


@st.composite
def _scenarios(draw: st.DrawFn) -> Scenario:
    adapter, model, native = draw(_ADAPTER_MODEL_NATIVE)
    target = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=native)))
    effective = target if target is not None else native
    min_tokens = draw(st.integers(min_value=1, max_value=100))
    target_tokens = draw(st.integers(min_value=min_tokens, max_value=min_tokens + 500))
    max_tokens = draw(
        st.integers(min_value=target_tokens, max_value=target_tokens + 500)
    )
    overlap = draw(st.integers(min_value=0, max_value=100))
    scenario_id = draw(st.text(alphabet=_ID_ALPHABET, min_size=2, max_size=20))
    vertical = draw(st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=10))
    enabled = draw(st.booleans())
    return Scenario(
        scenario_id=scenario_id,
        vertical=vertical,
        config_version="1",
        index_name=f"{scenario_id}-idx",
        chunker=ChunkerSettings(
            target_chunk_tokens=target_tokens,
            overlap_tokens=overlap,
            min_chunk_tokens=min_tokens,
            max_chunk_tokens=max_tokens,
        ),
        embedder=EmbedderSettings(
            adapter=adapter, model=model, target_dimensions=target
        ),
        indexer=IndexerSettings(
            metric="cosine",
            dimensions=effective,
            hnsw_m=4,
            hnsw_ef_construction=400,
            hnsw_ef_search=500,
        ),
        acl_source="envelope",
        default_trust_tier="internal",
        enabled=enabled,
        created_at=_FIXED_TIME,
        updated_at=_FIXED_TIME,
    )


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(scenario=_scenarios())
def test_property_scenario_round_trips_through_yaml_unchanged(
    scenario: Scenario,
) -> None:
    dumped = yaml.safe_dump(scenario.model_dump(mode="json"))
    reloaded = Scenario(**yaml.safe_load(dumped))
    assert reloaded == scenario
