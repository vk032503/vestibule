# Changelog

All notable changes documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Added
- REQ-010: Scenario Config Store — per-vertical ingestion settings (`Scenario`), read at runtime,
  independent of redeployment. `YamlScenarioStore` (read-only, file-backed) and
  `TableStorageScenarioStore` (writable, Azure Table Storage-backed, optimistic concurrency via
  ETag) backends, plus a `CachedScenarioStore` TTL-cache decorator. Dimension-consistency
  validation (`embedder.target_dimensions`/`indexer.dimensions`) runs at scenario construction —
  fail-fast, before any document is ever ingested.

### Changed
- _(none yet)_

### Removed
- _(none yet)_

### Fixed
- _(none yet)_

### Security
- _(none yet)_

## [0.2.0] - 2026-08-12

### Added
- REQ-005: Document Analyzer — magic-byte type detection, PDF sub-typing (digital vs scanned),
  parser routing via `ParserRegistry`. Adapters: `PyMuPDFParser`, `DocumentIntelligenceParser`.
- REQ-006: Chunker — three strategies auto-selected per element type (recursive, structure-aware,
  table-atomic). Token counting via tiktoken.
- REQ-007: Embedder — MRL dimension truncation with L2 renormalization, task-type prefix handling,
  provenance stamping. Adapters: `AzureOpenAIEmbedder`, `FastEmbedEmbedder` (local, CPU-only).
- REQ-008: Indexer — idempotent upsert, delete-before-upsert on version change, mixed-model
  rejection, security-trimming metadata. Adapters: `AzureAISearchIndexer`, `InMemoryIndexer`
  (with brute-force cosine search).
- End-to-end pipeline: envelope → analyzer → chunker → embedder → indexer, runnable with zero
  cloud credentials via FastEmbed + InMemoryIndexer.

## [0.1.0] - 2026-07-27

### Added
- REQ-001: Arrival envelope (`DocumentEnvelope`) — canonical shape for pipeline entry.
- REQ-002: Deterministic identity — `derive_doc_id`, `derive_chunk_id`, `IdentityInvalid`.
- REQ-003: State ledger (`LedgerRow`, `Status` enum, `LedgerStore` interface,
  `InMemoryLedgerStore` thread-safe implementation, `LedgerTransitionInvalid` error).
- REQ-004: Failure taxonomy — `ErrorRegistry`, `Severity` enum, `RaggedError` base class.
  Existing errors from REQ-001–003 now inherit from `RaggedError` and register their codes.

  