# Changelog

All notable changes documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Added
- REQ-011: Dynamic Index Provisioning — `IndexProvisioner` resolves the versioned
  `IndexTemplate` a document's index should be built from (explicit template dimensions, else
  `scenario.indexer.dimensions`) and creates the underlying index on first document for a
  vertical/scenario, rather than requiring an index to be hand-provisioned in advance. Concurrent
  first-arrivals for the same new index race safely: an atomic `register()` claim decides exactly
  one winner, and a stale claim (a worker that stalled or crashed mid-provision) is safely
  reclaimed by another worker via a compare-and-swap `reclaim()`, with `mark_ready`/`mark_failed`
  both rejecting a reclaimed, now-stale claim token (`INDEX_PROVISION_CONFLICT`) rather than
  clobbering the reclaimer's fresh entry. Drift detection compares a stored index's resolved
  template/dimensions against the scenario's current expectations and flags mismatches rather
  than silently indexing against the wrong shape. `IndexRegistry` backends: `InMemoryIndexRegistry`
  (thread-safe, single-process) and `TableStorageIndexRegistry` (Azure Table Storage, optimistic
  concurrency), plus a `CachedIndexRegistry` TTL-cache decorator to keep read-heavy lookups off
  the network on the common already-provisioned path.
- REQ-010: Scenario Config Store — per-vertical ingestion settings (`Scenario`), read at runtime,
  independent of redeployment. `YamlScenarioStore` (read-only, file-backed) and
  `TableStorageScenarioStore` (writable, Azure Table Storage-backed, optimistic concurrency via
  ETag) backends, plus a `CachedScenarioStore` TTL-cache decorator. Dimension-consistency
  validation (`embedder.target_dimensions`/`indexer.dimensions`) runs at scenario construction —
  fail-fast, before any document is ever ingested.
- REQ-009: Vertical Loader — `VerticalLoader` decides which vertical/scenario an arriving
  document belongs to (a prior human review-queue assignment, then explicit `scenario_id`/
  `vertical`, then source-mapping glob/exact rules, first-listed-wins), refusing to guess: a
  document with no signal is parked to `needs_review` for human review rather than classified,
  since a misrouted document gets the wrong ACLs. `ReviewQueue` (`list_pending`/`assign`/
  `reject`) is the minimal human-in-the-loop triage surface — `assign()` durably records the
  human's chosen `scenario_id` on the ledger row (`LedgerRow.assigned_scenario_id`), so
  redelivering the original, unchanged envelope after an assignment now resolves successfully
  instead of parking again. A stated-but-missing `scenario_id`/`vertical` is a config error
  (`SCENARIO_NOT_FOUND`), never silently downgraded to a park.

### Changed
- REQ-009: extends REQ-003's ledger transition table, additively — a new non-terminal
  `Status.NEEDS_REVIEW` value and its legal transitions (`pending -> needs_review`,
  `needs_review -> pending`, `needs_review -> failed`); `indexed -> needs_review` remains
  illegal (AC8). Also extends `LedgerRow` with a new `assigned_scenario_id` field (set once by
  `ReviewQueue.assign()`, never cleared afterward — a completely inert value once a row has
  advanced past `PENDING`) and threads a matching optional `assigned_scenario_id` keyword
  through `LedgerStore.transition()` — every existing caller (Analyzer, Chunker, Embedder,
  Indexer) is unaffected, since omitting the new keyword leaves the field unchanged.
  `is_benign_concurrent_loss` gained two additional special cases (mirroring the design-review
  Round 2 fix for `Status.FAILED`) so it continues to never raise for the new status — see
  `docs/designs/REQ-003-lld.md`'s Revision note, third entry.
- REQ-009: `VerticalLoader.__init__`'s `review_registry` argument is now required
  (keyword-only, no default) instead of optional — an unshared `ReviewRegistry` between a
  `VerticalLoader`/`ReviewQueue` pair previously degraded `ReviewQueue.list_pending()` silently
  (blank `source`/`blob_path`/`suggested_verticals` for every parked item, nothing raised or
  logged); making it required makes that mistake structurally impossible.

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

  