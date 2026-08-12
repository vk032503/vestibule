# Changelog

All notable changes documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Added
- REQ-007: Embedder (`Embedder`, `EmbedderConfig`, `RetryConfig`) — the third Phase 2
  component, consuming the Chunker's `list[Chunk]` output and producing
  `list[EmbeddedChunk]` (a frozen pydantic model wrapping `Chunk` with `vector`,
  `embedding_model`, `embedding_dimensions`, `truncated_from`). An `EmbedderAdapter`
  ABC (`embed_batch` plus `model_name`/`native_dimensions`/`max_batch_size`/
  `max_input_tokens`/`supports_mrl`/`document_prefix` capability properties) and two
  thin adapters: `AzureOpenAIEmbedder` (production, `text-embedding-3-large` default,
  in-adapter `tenacity` retry with exponential-backoff-with-jitter honoring a 429's
  `Retry-After` header) and `FastEmbedEmbedder` (local, credentials-free, CPU-only
  ONNX via the optional `fastembed` dependency — the new `local` extra — lazily
  imported so the base install never hard-fails without it). Matryoshka (MRL)
  dimension truncation with L2 re-normalization when `EmbedderConfig.target_dimensions`
  is set; validated against the configured adapter's `supports_mrl` at construction
  time, fail-fast (`EMBEDDER_MRL_UNSUPPORTED`). Owns exactly the
  `embedding -> indexing` exit ledger transition (`Chunker` already performs the
  `embedding` entry write); same-status `embedding -> embedding` self-transitions on
  every TRANSIENT failure (`EMBEDDER_RATE_LIMITED`, `EMBEDDER_UPSTREAM_ERROR`,
  `EMBEDDER_TIMEOUT`, `EMBEDDER_INTERNAL`); the two PERMANENT failures
  (`EMBEDDER_EMPTY_CHUNKS`, `EMBEDDER_INPUT_TOO_LONG`) terminalize the ledger row to
  `failed`, each triaged via `is_benign_concurrent_loss` (REQ-006/issue #9 pattern).
  Adds `config/embedder.yaml`, the `openai`/`tenacity` dependencies, and the
  `fastembed` optional `local` extra. One deliberate addition beyond the story's
  literal seven-code failure list: `EMBEDDER_DEPENDENCY_MISSING` (PERMANENT), covering
  `FastEmbedEmbedder`'s "raise a clear PERMANENT-classified error if [fastembed is]
  missing" requirement — flagged here since Contract #4 requires every raised error to
  be classified, and this condition would otherwise silently default to TRANSIENT.
- REQ-006: Chunker (`Chunker`, `ChunkerConfig`) — the second Phase 2 component,
  consuming the Analyzer's `list[Element]` output. Three `ChunkStrategy`
  implementations (`RecursiveChunkStrategy`, thin-wrapping
  `langchain_text_splitters.RecursiveCharacterTextSplitter`; `StructureAwareChunkStrategy`,
  flat single-active-heading `section_path` bookkeeping; `TableAtomicChunkStrategy`,
  one markdown-serialized chunk per table, never split), a `TokenCounter`
  Protocol/`TiktokenCounter` thin wrap of `tiktoken`, and the `Chunk`/`ChunkDraft`
  value objects. Owns exactly the `chunking -> embedding` exit ledger transition
  (`Analyzer` already performs the `chunking` entry write); same-status
  `chunking -> chunking` self-transitions on every TRANSIENT failure
  (`TOKENIZER_LOAD_FAILED`, `CHUNKER_INTERNAL`); the one PERMANENT failure
  (`CHUNKER_EMPTY_ELEMENTS`) terminalizes the ledger row to `failed`, itself triaged via
  `is_benign_concurrent_loss` (unlike REQ-005's `Analyzer._terminalize`, tracked
  separately as issue #9). An oversized table logs a structured `"oversized_table"`
  warning and is emitted anyway — deliberately not a registered `ErrorRegistry` code.
  Adds `config/chunker.yaml`, and the `tiktoken`/`langchain-text-splitters`
  dependencies.
- REQ-005: Document Analyzer (`Analyzer`, `AnalyzerConfig`) — the first Phase 2
  component. Type detection (`detect_type`), the `ParserAdapter`/`ParserRegistry`
  contract, and two adapters (`PyMuPDFParser` for digital PDFs, thin-wrapping
  `pymupdf`; `DocumentIntelligenceParser` for scanned PDFs, thin-wrapping Azure
  Document Intelligence's `prebuilt-layout` model). Owns the `analyzing` ledger stage:
  `pending -> analyzing -> {chunking | failed}`, with same-status `analyzing ->
  analyzing` self-transitions on every TRANSIENT failure (`PARSER_TIMEOUT`,
  `DOCINT_RATE_LIMITED`, `DOCINT_UPSTREAM_ERROR`, `PARSER_INTERNAL`) so a
  redelivered/retried document stays re-enterable; only the two PERMANENT failures
  (`ANALYZER_UNSUPPORTED_TYPE`, `ANALYZER_NO_PARSER`) terminalize the ledger row to
  `failed`. Adds `config/analyzer.yaml` and `benchmarks/README.md`.

### Changed
- `config/errors.yaml`'s `known_codes` audit block now also lists REQ-007's eight
  error codes, and the corresponding cross-module sync test now also imports
  `ingestion.embedder.model` — both are registered into the same process-wide error
  registry REQ-004 established.
- `config/errors.yaml`'s `known_codes` audit block now also lists REQ-006's three
  error codes, and the corresponding cross-module sync test now also imports
  `ingestion.chunker.model` — both are registered into the same process-wide error
  registry REQ-004 established.
- `config/errors.yaml`'s `known_codes` audit block now also lists REQ-005's six error
  codes, and the corresponding cross-module sync test now imports
  `ingestion.analyzer.model` — both are registered into the same process-wide error
  registry REQ-004 established.

### Removed
- _(none yet)_

### Fixed
- _(none yet)_

### Security
- _(none yet)_

## [0.1.0] - 2026-07-27

### Added
- REQ-001: Arrival envelope (`DocumentEnvelope`) — canonical shape for pipeline entry.
- REQ-002: Deterministic identity — `derive_doc_id`, `derive_chunk_id`, `IdentityInvalid`.
- REQ-003: State ledger (`LedgerRow`, `Status` enum, `LedgerStore` interface,
  `InMemoryLedgerStore` thread-safe implementation, `LedgerTransitionInvalid` error).
- REQ-004: Failure taxonomy — `ErrorRegistry`, `Severity` enum, `RaggedError` base class.
  Existing errors from REQ-001–003 now inherit from `RaggedError` and register their codes.