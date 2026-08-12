"""Unit tests for TokenCounter/TiktokenCounter/build_token_counter (REQ-006)."""

from __future__ import annotations

from typing import NoReturn

import pytest

from ingestion.chunker.model import (
    CHUNKER_INTERNAL,
    TOKENIZER_LOAD_FAILED,
    ChunkerError,
)
from ingestion.chunker.token_counter import (
    TiktokenCounter,
    build_token_counter,
)


# --- TiktokenCounter -----------------------------------------------------------------------


def test_tiktoken_counter_counts_a_known_string() -> None:
    counter = TiktokenCounter("cl100k_base")
    assert counter.count("hello world") > 0


def test_tiktoken_counter_empty_string_counts_zero() -> None:
    counter = TiktokenCounter("cl100k_base")
    assert counter.count("") == 0


def test_tiktoken_counter_loads_encoding_lazily_not_at_construction() -> None:
    counter = TiktokenCounter("this-encoding-does-not-exist")
    assert counter._encoding is None  # not loaded yet


def test_tiktoken_counter_load_failure_raises_chunker_error_tokenizer_load_failed() -> (
    None
):
    counter = TiktokenCounter("this-encoding-does-not-exist")
    with pytest.raises(ChunkerError) as exc_info:
        counter.count("hello")
    assert exc_info.value.error_code == TOKENIZER_LOAD_FAILED


def test_tiktoken_counter_caches_encoding_after_first_successful_load() -> None:
    counter = TiktokenCounter("cl100k_base")
    counter.count("first call loads the encoding")
    cached_encoding = counter._encoding
    counter.count("second call reuses it")
    assert counter._encoding is cached_encoding


def test_tiktoken_counter_load_failure_does_not_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _raise(name: str) -> NoReturn:
        calls.append(name)
        raise RuntimeError("no such encoding")

    monkeypatch.setattr("tiktoken.get_encoding", _raise)
    counter = TiktokenCounter("cl100k_base")

    with pytest.raises(ChunkerError):
        counter.count("hello")
    with pytest.raises(ChunkerError):
        counter.count("hello again")

    assert len(calls) == 2  # each failed call re-attempts the load


# --- build_token_counter ---------------------------------------------------------------


def test_build_token_counter_tiktoken_cl100k_returns_tiktoken_counter() -> None:
    counter = build_token_counter("tiktoken_cl100k")
    assert isinstance(counter, TiktokenCounter)
    assert counter._encoding_name == "cl100k_base"


def test_build_token_counter_unrecognized_name_raises_chunker_error_chunker_internal() -> (
    None
):
    with pytest.raises(ChunkerError) as exc_info:
        build_token_counter("not-a-real-counter")
    assert exc_info.value.error_code == CHUNKER_INTERNAL
