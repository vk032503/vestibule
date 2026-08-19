"""Unit tests for the provisioning data model (REQ-011)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vestibule.errors.registry import Severity, classify
from vestibule.provisioning.model import (
    INDEX_AUTO_CREATE_DISABLED,
    INDEX_PROVISION_CONFLICT,
    INDEX_PROVISION_FAILED,
    INDEX_PROVISION_TIMEOUT,
    INDEX_PROVISIONER_DEPENDENCY_MISSING,
    INDEX_PROVISIONER_UNAVAILABLE,
    INDEX_REGISTRY_UNAVAILABLE,
    INDEX_RETIRED,
    INDEX_SCHEMA_DRIFT,
    INDEX_TEMPLATE_INVALID,
    INDEX_TEMPLATE_NOT_FOUND,
    FieldSpec,
    HnswSettings,
    IndexTemplate,
    ProvisioningError,
)

_HNSW = HnswSettings(m=4, ef_construction=400, ef_search=500)


def _fields(**overrides: list[FieldSpec]) -> list[FieldSpec]:
    defaults = [
        FieldSpec(name="chunk_id", type="key"),
        FieldSpec(name="content_vector", type="vector", searchable=True),
    ]
    return overrides.get("fields", defaults)


def _template(**overrides: object) -> IndexTemplate:
    defaults: dict[str, object] = {
        "template_id": "standard-v1",
        "template_version": "1",
        "dimensions": None,
        "metric": "cosine",
        "hnsw": _HNSW,
        "fields": _fields(),
        "semantic_ranker_enabled": False,
        "hybrid_enabled": False,
    }
    defaults.update(overrides)
    return IndexTemplate(**defaults)  # type: ignore[arg-type]


# --- IndexTemplate construction / validation -------------------------------------------------


def test_index_template_constructs_with_valid_fields() -> None:
    template = _template()
    assert template.template_id == "standard-v1"
    assert template.dimensions is None


def test_index_template_is_frozen() -> None:
    template = _template()
    with pytest.raises(ValidationError):
        template.template_id = "other"


@pytest.mark.parametrize(
    "bad_version",
    ["v1", "1.0", "", "one", "-1x"],
    ids=["v-prefix", "float", "empty", "word", "mixed"],
)
def test_index_template_rejects_non_integer_template_version(bad_version: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _template(template_version=bad_version)
    assert "non-negative integer" in str(exc_info.value)


def test_index_template_rejects_negative_template_version() -> None:
    with pytest.raises(ValidationError):
        _template(template_version="-1")


def test_index_template_rejects_missing_key_field() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _template(fields=[FieldSpec(name="content_vector", type="vector")])
    assert "exactly one field with type='key'" in str(exc_info.value)


def test_index_template_rejects_multiple_key_fields() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _template(
            fields=[
                FieldSpec(name="chunk_id", type="key"),
                FieldSpec(name="doc_id", type="key"),
                FieldSpec(name="content_vector", type="vector"),
            ]
        )
    assert "exactly one field with type='key'" in str(exc_info.value)


def test_index_template_rejects_no_vector_field() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _template(fields=[FieldSpec(name="chunk_id", type="key")])
    assert "at least one field with type='vector'" in str(exc_info.value)


def test_index_template_rejects_duplicate_field_names() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _template(
            fields=[
                FieldSpec(name="chunk_id", type="key"),
                FieldSpec(name="chunk_id", type="vector"),
            ]
        )
    assert "unique" in str(exc_info.value)


# --- ProvisioningError -----------------------------------------------------------------------


def test_provisioning_error_carries_index_name_reason_and_error_code() -> None:
    exc = ProvisioningError("idx-1", "boom", error_code=INDEX_SCHEMA_DRIFT)
    assert exc.index_name == "idx-1"
    assert exc.reason == "boom"
    assert exc.error_code == INDEX_SCHEMA_DRIFT
    assert str(exc) == "idx-1: boom"


# --- error registration (Contract #4, AC11) -------------------------------------------------


@pytest.mark.parametrize(
    "code,expected_severity",
    [
        (INDEX_TEMPLATE_NOT_FOUND, Severity.PERMANENT),
        (INDEX_TEMPLATE_INVALID, Severity.PERMANENT),
        (INDEX_SCHEMA_DRIFT, Severity.PERMANENT),
        (INDEX_PROVISION_FAILED, Severity.PERMANENT),
        (INDEX_RETIRED, Severity.PERMANENT),
        (INDEX_AUTO_CREATE_DISABLED, Severity.PERMANENT),
        (INDEX_PROVISIONER_DEPENDENCY_MISSING, Severity.PERMANENT),
        (INDEX_PROVISION_TIMEOUT, Severity.TRANSIENT),
        (INDEX_PROVISION_CONFLICT, Severity.TRANSIENT),
        (INDEX_REGISTRY_UNAVAILABLE, Severity.TRANSIENT),
        (INDEX_PROVISIONER_UNAVAILABLE, Severity.TRANSIENT),
    ],
)
def test_error_code_registered_with_expected_severity(
    code: str, expected_severity: Severity
) -> None:
    assert classify(code) == expected_severity


def test_all_eleven_provisioning_codes_registered_after_import() -> None:
    codes = {
        INDEX_TEMPLATE_NOT_FOUND,
        INDEX_TEMPLATE_INVALID,
        INDEX_SCHEMA_DRIFT,
        INDEX_PROVISION_FAILED,
        INDEX_RETIRED,
        INDEX_AUTO_CREATE_DISABLED,
        INDEX_PROVISION_TIMEOUT,
        INDEX_PROVISION_CONFLICT,
        INDEX_REGISTRY_UNAVAILABLE,
        INDEX_PROVISIONER_UNAVAILABLE,
        INDEX_PROVISIONER_DEPENDENCY_MISSING,
    }
    assert len(codes) == 11
    from vestibule.errors.registry import all_codes

    live_codes = {entry.code for entry in all_codes()}
    assert codes <= live_codes
