"""Shared fixtures and helpers for the Document Analyzer test suite (REQ-005).

Byte fixtures generate real bytes (real PyMuPDF-rendered PDFs, a real minimal zip for
the DOCX magic-byte case) in-memory — no fixture files are committed to the repository,
and no network access or external tooling is required (hermetic, per house rules).

`make_envelope`/`new_doc_id` are plain helper functions (not pytest fixtures), shared
across this package's test modules by direct import.
"""

from __future__ import annotations

import io
import itertools
import zipfile
from typing import cast

import pymupdf
import pytest

from ingestion.envelope.model import ArrivalEnvelope, TrustTier, build_envelope

_LOREM = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 5
_doc_counter = itertools.count()


def make_envelope(label: str) -> ArrivalEnvelope:
    """Builds a valid, fresh `ArrivalEnvelope` for test use.

    Args:
        label: A human-readable label, folded into a unique `blob_path` so repeated
            calls with the same label never collide on `doc_id`.

    Returns:
        A validated `ArrivalEnvelope` with a deterministic, collision-free `doc_id`.
    """
    return build_envelope(
        source="test-source",
        blob_path=f"{label}-{next(_doc_counter)}",
        content_hash="0" * 64,
        trust_tier=TrustTier.INTERNAL,
    )


def _pdf_with_page_text(texts: list[str], *, encrypt: bool = False) -> bytes:
    """Builds a real PDF with one page per entry in `texts`.

    Args:
        texts: Text to insert on each page; an empty string yields a blank page.
        encrypt: If `True`, saves the PDF with AES-256 encryption and a user password.

    Returns:
        The PDF's bytes.
    """
    doc = pymupdf.open()
    try:
        for text in texts:
            page = doc.new_page()
            if text:
                page.insert_text((72, 72), text, fontsize=11)
        if encrypt:
            aes_256 = pymupdf.PDF_ENCRYPT_AES_256  # type: ignore[attr-defined]
            return cast(
                bytes,
                doc.tobytes(encryption=aes_256, owner_pw="owner-pw", user_pw="user-pw"),
            )
        return cast(bytes, doc.tobytes())
    finally:
        doc.close()


def pdf_bytes_with_char_count(chars_per_page: int, *, pages: int = 3) -> bytes:
    """Builds a real PDF whose pages each contain (approximately) `chars_per_page` chars.

    Args:
        chars_per_page: Target character count per page (0 for a blank page).
        pages: Number of pages to generate.

    Returns:
        The PDF's bytes.
    """
    text = ("x" * chars_per_page) if chars_per_page > 0 else ""
    return _pdf_with_page_text([text] * pages)


@pytest.fixture
def digital_pdf_bytes() -> bytes:
    """A real 3-page PDF with substantial extractable text on every page."""
    return _pdf_with_page_text([_LOREM, _LOREM, _LOREM])


@pytest.fixture
def scanned_pdf_bytes() -> bytes:
    """A real 3-page PDF with no extractable text (stand-in for a scanned/OCR-only PDF)."""
    return _pdf_with_page_text(["", "", ""])


@pytest.fixture
def encrypted_pdf_bytes() -> bytes:
    """A real, password-protected PDF (unreadable without authentication)."""
    return _pdf_with_page_text([_LOREM], encrypt=True)


@pytest.fixture
def corrupt_pdf_bytes() -> bytes:
    """PDF magic bytes followed by structurally invalid content."""
    return b"%PDF-1.7\n%not a real xref/object structure" + b"\x00" * 64


@pytest.fixture
def empty_bytes() -> bytes:
    """A zero-byte stream."""
    return b""


@pytest.fixture
def non_pdf_bytes_disguised_as_pdf() -> bytes:
    """Plain-text content — simulates a `.pdf`-named file with non-PDF magic bytes."""
    return b"This is plain text, not a PDF, regardless of any file name it is given."


@pytest.fixture
def docx_bytes() -> bytes:
    """A minimal real zip archive (OOXML/.docx magic bytes: `PK\\x03\\x04`)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
    return buf.getvalue()


@pytest.fixture
def png_bytes() -> bytes:
    """PNG magic bytes followed by arbitrary padding (magic-byte sniffing only)."""
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
def jpeg_bytes() -> bytes:
    """JPEG magic bytes followed by arbitrary padding (magic-byte sniffing only)."""
    return b"\xff\xd8\xff\xe0" + b"\x00" * 32
