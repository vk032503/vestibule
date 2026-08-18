"""Unit tests for `vestibule.vertical.model` (REQ-009)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vestibule.errors.registry import Severity, classify
from vestibule.vertical.model import (
    REVIEW_ITEM_NOT_FOUND,
    VERTICAL_MAPPING_INVALID,
    VERTICAL_REJECTED,
    VERTICAL_UNRESOLVED,
    ResolutionSource,
    VerticalError,
    VerticalResolution,
)

# --- VerticalResolution ------------------------------------------------------------------


def test_vertical_resolution_is_frozen() -> None:
    resolution = VerticalResolution(
        vertical="hr",
        scenario_id="hr-1",
        source=ResolutionSource.ENVELOPE_VERTICAL,
        confidence="explicit",
        reason="envelope declared vertical",
    )
    with pytest.raises(ValidationError):
        resolution.vertical = "legal"


def test_vertical_resolution_rejects_empty_reason() -> None:
    with pytest.raises(ValidationError):
        VerticalResolution(
            vertical=None,
            scenario_id=None,
            source=ResolutionSource.UNRESOLVED,
            confidence="unresolved",
            reason="",
        )


def test_vertical_resolution_allows_none_vertical_and_scenario_id_when_unresolved() -> (
    None
):
    resolution = VerticalResolution(
        vertical=None,
        scenario_id=None,
        source=ResolutionSource.UNRESOLVED,
        confidence="unresolved",
        reason="no signal matched",
    )
    assert resolution.vertical is None
    assert resolution.scenario_id is None


# --- VerticalError -------------------------------------------------------------------------


def test_vertical_error_carries_error_code_and_doc_id() -> None:
    exc = VerticalError("doc-1", "no signal", error_code=VERTICAL_UNRESOLVED)
    assert exc.error_code == VERTICAL_UNRESOLVED
    assert exc.doc_id == "doc-1"
    assert exc.reason == "no signal"


def test_vertical_error_allows_empty_doc_id_for_construction_time_failures() -> None:
    exc = VerticalError("", "bad mapping", error_code=VERTICAL_MAPPING_INVALID)
    assert exc.doc_id == ""


# --- error registration (Contract #4) -------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        VERTICAL_UNRESOLVED,
        VERTICAL_MAPPING_INVALID,
        VERTICAL_REJECTED,
        REVIEW_ITEM_NOT_FOUND,
    ],
)
def test_all_four_codes_registered_permanent(code: str) -> None:
    assert classify(code) == Severity.PERMANENT
