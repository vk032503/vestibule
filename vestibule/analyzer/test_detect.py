"""Unit tests for detect_type (REQ-005)."""

from __future__ import annotations

import io

import pytest

from vestibule.analyzer.conftest import make_envelope, pdf_bytes_with_char_count
from vestibule.analyzer.detect import detect_type
from vestibule.analyzer.model import BytesReader, DetectedType

_PROBE_PAGES = 3
_MIN_CHARS_PER_PAGE = 50

_envelope = make_envelope("detect-type")


def _detect(
    data: bytes,
    *,
    probe_pages: int = _PROBE_PAGES,
    min_chars: int = _MIN_CHARS_PER_PAGE,
) -> DetectedType:
    reader: BytesReader = io.BytesIO(data)
    return detect_type(
        _envelope,
        reader,
        scanned_pdf_probe_pages=probe_pages,
        scanned_pdf_min_chars_per_page=min_chars,
    )


def test_detect_type_digital_pdf_fixture(digital_pdf_bytes: bytes) -> None:
    assert _detect(digital_pdf_bytes) == DetectedType.DIGITAL_PDF


def test_detect_type_scanned_pdf_fixture(scanned_pdf_bytes: bytes) -> None:
    assert _detect(scanned_pdf_bytes) == DetectedType.SCANNED_PDF


def test_detect_type_docx_fixture(docx_bytes: bytes) -> None:
    assert _detect(docx_bytes) == DetectedType.DOCX


@pytest.mark.parametrize("fixture_name", ["png_bytes", "jpeg_bytes"])
def test_detect_type_image_fixture(
    fixture_name: str, request: pytest.FixtureRequest
) -> None:
    data: bytes = request.getfixturevalue(fixture_name)
    assert _detect(data) == DetectedType.IMAGE


def test_detect_type_never_uses_file_extension(
    non_pdf_bytes_disguised_as_pdf: bytes,
) -> None:
    """A ".pdf"-named file with non-PDF magic bytes is UNSUPPORTED, not DIGITAL_PDF.

    BytesReader carries no filename/extension at all, so this also demonstrates
    detect_type is structurally incapable of consulting one (AC3).
    """
    assert _detect(non_pdf_bytes_disguised_as_pdf) == DetectedType.UNSUPPORTED


def test_detect_type_encrypted_pdf_returns_unsupported(
    encrypted_pdf_bytes: bytes,
) -> None:
    assert _detect(encrypted_pdf_bytes) == DetectedType.UNSUPPORTED


def test_detect_type_corrupt_pdf_returns_unsupported(corrupt_pdf_bytes: bytes) -> None:
    assert _detect(corrupt_pdf_bytes) == DetectedType.UNSUPPORTED


def test_detect_type_empty_file_returns_unsupported(empty_bytes: bytes) -> None:
    assert _detect(empty_bytes) == DetectedType.UNSUPPORTED


def test_detect_type_pdf_below_char_threshold_classified_scanned() -> None:
    data = pdf_bytes_with_char_count(10)
    assert _detect(data, min_chars=50) == DetectedType.SCANNED_PDF


def test_detect_type_pdf_above_char_threshold_classified_digital() -> None:
    data = pdf_bytes_with_char_count(500)
    assert _detect(data, min_chars=50) == DetectedType.DIGITAL_PDF


@pytest.mark.parametrize(
    "fixture_name",
    [
        "digital_pdf_bytes",
        "scanned_pdf_bytes",
        "encrypted_pdf_bytes",
        "corrupt_pdf_bytes",
        "empty_bytes",
        "non_pdf_bytes_disguised_as_pdf",
        "docx_bytes",
        "png_bytes",
        "jpeg_bytes",
    ],
)
def test_detect_type_never_raises(
    fixture_name: str, request: pytest.FixtureRequest
) -> None:
    data: bytes = request.getfixturevalue(fixture_name)
    result = _detect(data)
    assert isinstance(result, DetectedType)
