# Changelog

All notable changes documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Added
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