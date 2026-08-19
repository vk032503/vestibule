"""Unit tests for AzureAISearchProvisioner (REQ-011).

No live API calls — every test injects a recorded-response fake `_backend` (satisfying
`_ProvisionerBackend`'s duck-typed contract), mirroring
`vestibule.indexer.adapters.test_azure_ai_search`'s convention.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from azure.core.exceptions import HttpResponseError
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vestibule.provisioning.adapters.azure_ai_search import (
    AzureAISearchProvisioner,
    _IndexDefinition,
)
from vestibule.provisioning.conftest import build_index_template
from vestibule.provisioning.model import (
    INDEX_PROVISIONER_DEPENDENCY_MISSING,
    INDEX_PROVISIONER_UNAVAILABLE,
    FieldSpec,
    FieldType,
    ProvisioningError,
)


class _FakeResponse:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.reason = "error"
        self.headers = headers or {}


def _http_error(
    status_code: int, headers: dict[str, str] | None = None
) -> HttpResponseError:
    fake_response = cast(Any, _FakeResponse(status_code, headers))
    return HttpResponseError(response=fake_response)


class _FakeBackend:
    """Recorded-response fake standing in for `_RealSearchBackend`."""

    def __init__(
        self,
        *,
        schema: tuple[int, str] | None = None,
        error_sequence: list[Exception | None] | None = None,
    ) -> None:
        self.schema = schema
        self._error_sequence = list(error_sequence or [])
        self.create_calls: list[_IndexDefinition] = []
        self.delete_calls: list[str] = []
        self.attempt_count = 0

    def _maybe_raise(self) -> None:
        self.attempt_count += 1
        if self._error_sequence:
            error = self._error_sequence.pop(0)
            if error is not None:
                raise error

    def get_index_schema(self, index_name: str) -> tuple[int, str] | None:
        self._maybe_raise()
        return self.schema

    def create_or_update_index(self, definition: _IndexDefinition) -> None:
        self._maybe_raise()
        self.create_calls.append(definition)
        self.schema = (definition.dimensions, definition.metric)

    def delete_index(self, index_name: str) -> None:
        self._maybe_raise()
        self.delete_calls.append(index_name)
        self.schema = None


def _provisioner(backend: _FakeBackend, **kwargs: Any) -> AzureAISearchProvisioner:
    return AzureAISearchProvisioner(
        endpoint="https://fake.example.com",
        api_key="fake-key",
        _sleep=lambda _seconds: None,
        backoff_base_seconds=0.01,
        backoff_max_seconds=0.02,
        _backend=backend,
        **kwargs,
    )


# --- dependency missing ------------------------------------------------------------------------


def test_azure_ai_search_provisioner_dependency_missing_raises_permanent() -> None:
    with pytest.raises(ProvisioningError) as exc_info:
        AzureAISearchProvisioner(
            endpoint="https://fake.example.com", api_key="fake-key"
        )
    assert exc_info.value.error_code == INDEX_PROVISIONER_DEPENDENCY_MISSING


# --- index_exists / describe_index ---------------------------------------------------------


def test_index_exists_true_when_schema_present() -> None:
    provisioner = _provisioner(_FakeBackend(schema=(1024, "cosine")))
    assert provisioner.index_exists("idx-1") is True


def test_index_exists_false_when_schema_absent() -> None:
    provisioner = _provisioner(_FakeBackend(schema=None))
    assert provisioner.index_exists("idx-1") is False


def test_describe_index_returns_none_when_absent() -> None:
    provisioner = _provisioner(_FakeBackend(schema=None))
    assert provisioner.describe_index("idx-1") is None


def test_describe_index_returns_description_when_present() -> None:
    provisioner = _provisioner(_FakeBackend(schema=(1024, "cosine")))
    description = provisioner.describe_index("idx-1")
    assert description is not None
    assert description.dimensions == 1024
    assert description.metric == "cosine"


# --- create_index translates the template ----------------------------------------------------


def test_azure_ai_search_provisioner_create_index_translates_template_to_search_index() -> (
    None
):
    backend = _FakeBackend()
    provisioner = _provisioner(backend)
    template = build_index_template()

    provisioner.create_index("idx-1", template, 1024, "cosine")

    assert len(backend.create_calls) == 1
    definition = backend.create_calls[0]
    assert definition.index_name == "idx-1"
    assert definition.dimensions == 1024
    assert definition.metric == "cosine"
    assert definition.hnsw_m == template.hnsw.m
    assert definition.hnsw_ef_construction == template.hnsw.ef_construction
    assert definition.hnsw_ef_search == template.hnsw.ef_search
    assert list(definition.fields) == template.fields
    assert definition.semantic_ranker_enabled == template.semantic_ranker_enabled


def test_azure_ai_search_provisioner_create_index_is_idempotent_second_call_with_identical_definition_succeeds() -> (
    None
):
    """Assumption A13."""
    backend = _FakeBackend()
    provisioner = _provisioner(backend)
    template = build_index_template()

    provisioner.create_index("idx-1", template, 1024, "cosine")
    provisioner.create_index("idx-1", template, 1024, "cosine")

    assert len(backend.create_calls) == 2


# --- delete_index ------------------------------------------------------------------------------


def test_delete_index_delegates_to_backend() -> None:
    backend = _FakeBackend()
    provisioner = _provisioner(backend)
    provisioner.delete_index("idx-1")
    assert backend.delete_calls == ["idx-1"]


# --- retry / timeout / rate limited -> INDEX_PROVISIONER_UNAVAILABLE -------------------------


def test_create_index_rate_limited_exhausted_raises_index_provisioner_unavailable() -> (
    None
):
    backend = _FakeBackend(
        error_sequence=[_http_error(429), _http_error(429), _http_error(429)]
    )
    provisioner = _provisioner(backend, max_attempts=3)
    with pytest.raises(ProvisioningError) as exc_info:
        provisioner.create_index("idx-1", build_index_template(), 1024, "cosine")
    assert exc_info.value.error_code == INDEX_PROVISIONER_UNAVAILABLE


def test_describe_index_upstream_5xx_exhausted_raises_index_provisioner_unavailable() -> (
    None
):
    backend = _FakeBackend(
        error_sequence=[_http_error(503), _http_error(503), _http_error(503)]
    )
    provisioner = _provisioner(backend, max_attempts=3)
    with pytest.raises(ProvisioningError) as exc_info:
        provisioner.describe_index("idx-1")
    assert exc_info.value.error_code == INDEX_PROVISIONER_UNAVAILABLE


def test_create_index_retries_honor_retry_after_header() -> None:
    backend = _FakeBackend(
        error_sequence=[_http_error(429, headers={"retry-after": "0"})]
    )
    provisioner = _provisioner(backend, max_attempts=3)
    provisioner.create_index("idx-1", build_index_template(), 1024, "cosine")
    assert len(backend.create_calls) == 1


def test_create_index_unmapped_http_error_propagates_unchanged() -> None:
    backend = _FakeBackend(error_sequence=[_http_error(400)])
    provisioner = _provisioner(backend, max_attempts=3)
    with pytest.raises(HttpResponseError):
        provisioner.create_index("idx-1", build_index_template(), 1024, "cosine")


# --- property-based -----------------------------------------------------------------------------

_FIELD_TYPE_CHOICES: list[FieldType] = [
    "text",
    "filterable_string",
    "filterable_string_collection",
    "filterable_datetime",
    "retrievable_only",
]
_FIELD_TYPES: st.SearchStrategy[FieldType] = st.sampled_from(_FIELD_TYPE_CHOICES)


@st.composite
def _templates(draw: st.DrawFn) -> Any:
    extra_field_count = draw(st.integers(min_value=0, max_value=4))
    extra_fields = [
        FieldSpec(name=f"extra_{i}", type=draw(_FIELD_TYPES))
        for i in range(extra_field_count)
    ]
    return build_index_template(
        template_version=str(draw(st.integers(min_value=0, max_value=99))),
        semantic_ranker_enabled=draw(st.booleans()),
        hybrid_enabled=draw(st.booleans()),
        fields=[
            FieldSpec(name="chunk_id", type="key"),
            FieldSpec(name="content_vector", type="vector", searchable=True),
            *extra_fields,
        ],
    )


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(template=_templates(), dimensions=st.integers(min_value=1, max_value=4096))
def test_property_any_valid_template_and_scenario_produces_well_formed_index_definition(
    template: Any, dimensions: int
) -> None:
    backend = _FakeBackend()
    provisioner = _provisioner(backend)
    provisioner.create_index("idx-1", template, dimensions, "cosine")
    definition = backend.create_calls[-1]
    assert definition.dimensions == dimensions
    assert definition.fields == tuple(template.fields)
