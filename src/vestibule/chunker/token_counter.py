"""TokenCounter — thin wrap of `tiktoken` (REQ-006).

`TokenCounter` is the minimal tokenizer surface every `ChunkStrategy` uses for size
accounting, so `RecursiveCharacterTextSplitter`'s `length_function` and this module's
own `token_count` bookkeeping stay consistent (house rules: "never hand-roll a
BPE/token-counting algorithm — always via adapter interfaces").

Tokenizer resource loading is lazy (LLD Assumption A9): `TOKENIZER_LOAD_FAILED`'s
declared self-transition/TRANSIENT behavior requires a `doc_id`/ledger-row context to
record against, which only `Chunker` has — a `tiktoken` encoding failure at process
startup (before any `doc_id` exists) is a deployment/composition-root concern, out of
scope here. `TiktokenCounter` therefore loads its encoding on first `count()` call and
caches it thereafter for the process lifetime, raising a placeholder-`doc_id`
`ChunkerError` that `Chunker` catches and re-raises with the real `doc_id` (see
`chunker.py`'s F2 handling).
"""

from __future__ import annotations

from typing import Protocol

import tiktoken

from vestibule.chunker.model import (
    CHUNKER_INTERNAL,
    TOKENIZER_LOAD_FAILED,
    ChunkerError,
)

_DEFAULT_ENCODING_NAME = "cl100k_base"
_TIKTOKEN_CL100K = "tiktoken_cl100k"
_DOC_ID_UNKNOWN = ""
"""Placeholder `doc_id` for errors raised below the `Chunker` layer, which has no
`doc_id` context of its own (LLD Assumption A9). `Chunker` always reconstructs the
final raised `ChunkerError` with the real `doc_id` before it reaches its own caller."""


class TokenCounter(Protocol):
    """Minimal tokenizer surface (LLD Assumption A9).

    Thin adapter over an external tokenizer library — never a hand-rolled
    BPE/token-counting algorithm (house rules).
    """

    def count(self, text: str) -> int:
        """Returns the token count of `text` under this counter's tokenizer."""
        ...


class TiktokenCounter:
    """Thin wrap of `tiktoken`. Lazily loads its encoding on first `count()` call."""

    def __init__(self, encoding_name: str = _DEFAULT_ENCODING_NAME) -> None:
        """Initializes the counter without loading any tokenizer resource yet.

        Args:
            encoding_name: The `tiktoken` encoding name to load on first use.
        """
        self._encoding_name = encoding_name
        self._encoding: tiktoken.Encoding | None = None

    def count(self, text: str) -> int:
        """Returns `text`'s token count, loading and caching the encoding if needed.

        Args:
            text: The text to tokenize.

        Returns:
            The number of tokens `text` encodes to.

        Raises:
            ChunkerError: `TOKENIZER_LOAD_FAILED` if the encoding cannot be loaded on
                first use (e.g. `tiktoken.get_encoding` raises). Cached thereafter, so
                a later call never re-attempts a load that already failed... unless a
                fresh `TiktokenCounter` instance is constructed.
        """
        if self._encoding is None:
            self._encoding = self._load_encoding()
        return len(self._encoding.encode(text))

    def _load_encoding(self) -> tiktoken.Encoding:
        """Loads this counter's `tiktoken` encoding.

        Returns:
            The loaded `tiktoken.Encoding`.

        Raises:
            ChunkerError: `TOKENIZER_LOAD_FAILED`, wrapping whatever `tiktoken` itself
                raised (declared error code, immediately re-raised — house rules).
        """
        try:
            return tiktoken.get_encoding(self._encoding_name)
        except Exception as exc:
            raise ChunkerError(
                _DOC_ID_UNKNOWN,
                f"failed to load tiktoken encoding {self._encoding_name!r}: {exc}",
                error_code=TOKENIZER_LOAD_FAILED,
            ) from exc


def build_token_counter(name: str) -> TokenCounter:
    """Factory: resolves a config `token_counter` key to a `TokenCounter` instance.

    Args:
        name: A factory key, e.g. `"tiktoken_cl100k"` (the only currently-recognized
            value, per `config/chunker.yaml`'s `token_counter` default).

    Returns:
        A `TiktokenCounter` for `"tiktoken_cl100k"`.

    Raises:
        ChunkerError: `CHUNKER_INTERNAL` for an unrecognized `name`.
    """
    if name == _TIKTOKEN_CL100K:
        return TiktokenCounter(_DEFAULT_ENCODING_NAME)
    raise ChunkerError(
        _DOC_ID_UNKNOWN,
        f"unrecognized token_counter name: {name!r}",
        error_code=CHUNKER_INTERNAL,
    )
