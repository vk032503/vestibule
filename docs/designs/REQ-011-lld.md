# REQ-011 — Dynamic Index Provisioning — LLD

**Story:** docs/stories/REQ-011.md · **Phase:** 3 · **Pipeline:** full (story → LLD →
design-review → developer → code-review)

## Assumptions (non-blocking, flagged per house rules — same pattern as REQ-004/REQ-005/REQ-006)

The story specifies `IndexTemplate`/`IndexRegistry`/`IndexProvisioner`/the adapter interface/
config/11 error codes fully enough to design against, but several details are genuinely
underspecified — most of all the exact conditional-write semantics behind "a second conditional
write" for reclaim, and how a caller of `register()` is meant to know whether it won or lost a
race given the story's own `register(entry) -> IndexRegistryEntry` signature carries no boolean.
Per house rules, these are scoped design decisions, documented here, not open questions that
block this LLD:

- **A1 — `Scenario` (REQ-010, already merged) has no `index_template_id` field.** The story says
  "`scenario.index_template_id` selects the template; falls back to config's
  `default_template_id`," but `vestibule/scenario/model.py`'s `Scenario` (read directly, REQ-010)
  declares no such field. This LLD requires a small, additive, non-breaking cross-module edit to
  `vestibule/scenario/model.py` (mirroring REQ-004 §1a's "minimal edits to an already-merged
  file" precedent): add `index_template_id: str | None = None` to `Scenario`. Default `None`
  means "use `config/index_provisioning.yaml`'s `default_template_id`" — every existing
  `Scenario(...)` call site (fixtures, `YamlScenarioStore`/`TableStorageScenarioStore` tests)
  continues to construct successfully unchanged, since pydantic fields with defaults never break
  existing keyword-argument construction. Same-PR follow-on edits, mirroring REQ-004 §1a's
  scope: `vestibule/scenario/stores/_entity_codec.py`'s `scenario_to_entity_data`/
  `entity_to_scenario` must round-trip this new property (absent-on-read defaults to `None`, so
  pre-existing Table Storage entities written before this REQ deserialize cleanly); a
  regression test confirming existing `Scenario(...)` construction call shapes are unaffected
  (mirrors REQ-004's `test_existing_constructor_signatures_unchanged`). This additive,
  `None`-defaulted field also does not break `vestibule/scenario/test_model.py`'s existing
  property-based round-trip test, `test_property_scenario_round_trips_through_yaml_unchanged`
  (`@given(scenario=_scenarios())`) — read directly, confirmed load-bearing per REQ-010's own
  precedent of treating this test as load-bearing for any `Scenario` model change: the
  `_scenarios()` Hypothesis strategy does not yet know about `index_template_id`, so every
  generated `Scenario` in that test still constructs with the field defaulting to `None` on both
  the dump and the reload side, and the dump/reload round trip stays byte-for-byte equal exactly
  as it does today — the test requires no update and continues to pass unmodified.
- **A2 — dimension-resolution tiers 2 and 3 collapse into one: `scenario.indexer.dimensions`.**
  The story's stated order is "template's explicit `dimensions` → scenario's embedder
  `target_dimensions` → adapter's native dimensions." `Scenario`'s own
  `model_validator(mode="after")` (REQ-010, `validate_dimension_consistency`,
  `vestibule/scenario/_validation.py`) already guarantees, at `Scenario` construction time, that
  `indexer.dimensions == effective(embedder)` where `effective` is exactly "`target_dimensions`
  if set, else the model's native dimensionality" — the same two tiers the story's own second and
  third bullets describe. Re-deriving native dimensions independently here would require reaching
  into `vestibule.scenario._validation._NATIVE_DIMENSIONS`, a **module-private** table (that
  module's own docstring: "these functions are not part of this package's public API") — doing so
  would couple this REQ to REQ-010's private internals and risks the two derivations silently
  drifting apart. This LLD instead resolves dimensions as `template.dimensions if template.dimensions
  is not None else scenario.indexer.dimensions` — one public, already-validated field, not two
  private ones. `INDEX_TEMPLATE_INVALID`'s "dimensions mismatch" trigger becomes precisely: the
  template names an **explicit** `dimensions` value that disagrees with `scenario.indexer.dimensions`.
  This is also the exact mechanism that answers the story's "re-enforces REQ-010's own
  dimension-consistency check at a second boundary" framing: REQ-010's own validator closes the
  window between an admin typing a bad `Scenario` and that `Scenario` ever being stored; this
  check closes a *different* window — between a `Scenario` being validly stored (with one
  `index_template_id`) and provisioning time, during which `scenario.index_template_id` may have
  been edited (`TableStorageScenarioStore.upsert`, REQ-010) to point at a *different* template
  whose own explicit `dimensions` was never checked against this scenario's embedder at edit
  time (nothing in REQ-010 validates that; `Scenario`'s validator only knows about
  `IndexerSettings.dimensions`, never about a template file's own `dimensions` field, since
  `IndexTemplate` did not exist until this REQ). Both checks are real and independently necessary.
- **A3 — `IndexRegistryEntry` gains three fields beyond the story's literal list:
  `claim_token: str | None`, `claimed_at: datetime | None`, `last_error_message: str | None`.**
  The story's own concurrency section requires (a) a way for a racing caller to tell "did my
  `register()` call win or lose," (b) a timestamp driving `provisioning_stale_after_seconds`
  distinct from `created_at` (needed so a *reclaim* can reset "how long has this specific claim
  been outstanding" without falsifying "when did this index_name's provisioning story begin"),
  and (c) somewhere to put `mark_failed`'s `reason` so `INDEX_PROVISION_FAILED`'s "requires human
  intervention" is actionable rather than just a status flag. None of these are enumerated in the
  story's field list; all three are additive, non-breaking, `None` in every non-`provisioning`/
  non-`failed` state.
- **A4 — `IndexRegistry` gains two methods beyond the story's literal list:
  `reclaim(index_name, observed, new_entry) -> IndexRegistryEntry` and
  `touch_verified(index_name) -> IndexRegistryEntry`.** The story states reclaim happens "via a
  second conditional write" without specifying its signature; `reclaim` is this LLD's concrete
  answer — see §3.4 for its exact compare-and-swap semantics on both backends. `touch_verified`
  is the minimal primitive the story's own "verification is cached per `last_verified_at`" caching
  behavior needs: a lightweight, non-CAS timestamp bump on an already-`ready` entry, kept separate
  from `mark_ready` (which is CAS-protected against a live `provisioning` claim, §3.4) since
  touching an already-`ready` entry's verification clock has no claim to protect.
- **A5 — `mark_ready`/`mark_failed` gain an `expected_claim_token: str | None` keyword.** This is
  the LLD's answer to the task's third race — "the original (crashed-looking-but-actually-just-
  slow) worker finishing normally right as reclamation happens." Both finalization writes are
  themselves CAS-protected against the caller's own `claim_token`, not just plain "flip the
  status" calls — see §3.4, step 4b, for why this is required, not optional. `expected_claim_token
  = None` skips the check (reserved for a future out-of-scope admin/manual-registration path;
  `IndexProvisioner` itself always passes its own non-`None` token).
- **A6 — `template_version` is a non-negative-integer string (`"1"`, `"2"`, ...), enforced by
  `IndexTemplate`'s own field validator.** The story's drift check requires ordering ("same or
  newer template_version" is compatible; older is drift), but nothing guarantees an arbitrary
  string sorts correctly (`"v10" < "v2"` lexicographically). Rather than adding a versioning
  library dependency or inventing a bespoke parser, this LLD adopts the same "pick the simplest
  workable convention, document it, test it" resolution REQ-010's `_bump_config_version` used for
  an analogous ambiguity: `template_version` must parse as `int(...)`; `IndexTemplate` rejects a
  non-integer value at load time (`INDEX_TEMPLATE_INVALID`), so the ordering comparison
  (`int(entry.template_version) >= int(resolved_template.template_version)`) is always well-defined.
- **A7 — no catch-all `*_INTERNAL`-style code exists among the story's 11 codes**, unlike every
  other module in this codebase (`CHUNKER_INTERNAL`, `EMBEDDER_INTERNAL`, `INDEXER_INTERNAL`).
  Any exception from `IndexProvisionerAdapter.create_index`/`describe_index`/`index_exists` not
  already mapped to `INDEX_PROVISIONER_DEPENDENCY_MISSING` (import-time-only) is wrapped as
  `INDEX_PROVISIONER_UNAVAILABLE` (TRANSIENT) — the closest declared code, and consistent with
  Contract #4's "unclassified errors default to TRANSIENT." Flagged because every other REQ in
  this codebase has a dedicated catch-all; this one deliberately reuses an existing code instead
  of the story minting a 12th.
- **A8 — a new config key, `wait_poll_interval_seconds` (default `2.0`), is added to
  `config/index_provisioning.yaml`.** The story specifies `wait_for_provisioning_timeout_seconds`
  (the overall bound) but not how often a waiting worker re-polls the registry within that bound.
  Fixed-interval polling (no exponential backoff — the wait is already bounded and short-lived,
  unlike an external-API retry loop) is this LLD's minimal choice, analogous to how REQ-010 added
  a documented, tested tunable for an unstated implementation detail rather than hardcoding it.
- **A9 — `TableStorageIndexRegistry` does not import REQ-010's `_TableBackend`/
  `_RealTableBackend`/`_TableEntityRecord`.** Those types are `vestibule.scenario.stores`-private
  (undocumented as reusable, entity-codec tied to `Scenario` specifically) — reusing them directly
  would create a cross-package coupling REQ-010 was never designed to support. Per the task's
  framing, this LLD **reuses the pattern, not the code**: `provisioning/stores/_azure_table_backend.py`
  is a structurally identical (`get_entity`/`query`/`upsert_entity`/`delete_entity`,
  `etag=None` → `create_entity`, `etag=<value>` → `update_entity(..., match_condition=IfNotModified)`)
  but independent implementation, keyed on `PartitionKey=vertical`, `RowKey=index_name`. A
  follow-up (non-blocking, filed as a documentation note here, not a ticket number since none is
  assigned yet) could extract a shared `_TableBackend` protocol + `_RealTableBackend` into a
  common `vestibule.storage` module for both REQ-010 and this REQ to depend on — genuinely
  worthwhile, but a cross-cutting refactor of already-merged, already-tested code is out of scope
  for this REQ's own LLD.
- **A11 — composition-root integration with REQ-008's `Indexer`/`AzureAISearchIndexer.ensure_schema`
  is explicitly out of scope for this LLD, and is flagged, not silently resolved.** `Indexer.index()`
  (already merged, `vestibule/indexer/indexer.py`) unconditionally calls
  `adapter.ensure_schema(config.dimensions, config.metric)` at the top of every call — a second,
  independent index-creation/compatibility-check code path that will now coexist with
  `IndexProvisioner.ensure()`. Whether a future integration PR removes/no-ops that call once
  `IndexProvisioner.ensure()` runs ahead of it in the pipeline, or leaves both running (redundant
  but each individually idempotent, per REQ-008's own `ensure_schema` design), is a
  composition-root wiring decision the story's own In/Out-of-scope sections do not mention and
  this LLD does not decide. Similarly, `AzureAISearchIndexer`'s own `hnsw_m`/`hnsw_ef_construction`/
  `hnsw_ef_search` constructor args (sourced from `Scenario.indexer.hnsw_*`, REQ-010) become
  vestigial for index **creation** once this REQ owns that — `AzureAISearchProvisioner` reads HNSW
  parameters exclusively from the resolved `IndexTemplate.hnsw`, never from `Scenario.indexer`.
  If an operator sets `Scenario.indexer.hnsw_m` to a value different from the template's `hnsw.m`,
  the index is created with the template's value and the scenario's value now has no effect on
  creation (it was never read by anything else either — `AzureAISearchIndexer.upsert()` never
  reads `self._hnsw_m`). Flagged for the design reviewer as a real, if latent, duplication between
  REQ-010's `IndexerSettings.hnsw_*` and this REQ's `IndexTemplate.hnsw`, not fixed here.
- **A12 — REQ-008's `_record_to_document` payload carries fields (`position`, `section_path`,
  `element_types`, `strategy`, `embedding_model`, `embedding_dimensions`, `indexed_at`) that
  `_build_fields`'s schema never declares.** Discovered while extracting AC9's regression-guard
  schema from `azure_ai_search.py`/`_azure_search_backend.py` (read directly, per task
  instructions). Standard-v1's `fields` list in this LLD reproduces `_build_fields`'s **8 declared
  fields exactly** (that is AC9's literal bar — "equivalent to REQ-008's hardcoded schema"), not
  the superset of fields the write path happens to also send. This looks like a latent REQ-008 gap
  (Azure AI Search generally rejects/drops undeclared document properties), but fixing it is out of
  scope here — flagged for a follow-up issue against REQ-008, not silently "fixed" by inflating
  standard-v1 beyond what AC9 asks this LLD to reproduce.
- **A13 — `IndexProvisionerAdapter.create_index` is contractually an idempotent create-or-update,
  not a create-only call (design-review BLOCKER resolution — see §3.4's new Race C, §3.3 step 7's
  corrected reasoning, and §1's revised adapter docstrings).** The design-reviewer's trace is
  correct: §3.3 step 7's original claim that "the registry claim's exclusivity guarantees at most
  one `create_index` call" does not hold once §3.4's stale-claim reclamation is in play.
  Concretely — `AzureAISearchProvisioner`'s own retry envelope (§1: `max_attempts=5`,
  `backoff_base_seconds=2.0` doubling to `backoff_max_seconds=60.0`, `timeout_seconds=30.0` per
  attempt, mirroring `AzureAISearchIndexer`'s identical, already-merged pattern — read directly,
  `vestibule/indexer/adapters/azure_ai_search.py`) can legitimately run up to roughly
  5×30s + 4×60s ≈ 390s under sustained 429s/5xx — longer than `provisioning_stale_after_seconds`'s
  default 300s — while the original claim holder makes no registry write at all mid-`create_index`
  (registry writes happen only at claim time and at `mark_ready`/`mark_failed`, never mid-call). A
  waiting worker can therefore legitimately reclaim a claim whose holder is alive and still
  working, then itself call `create_index` — two calls originating from two legitimate, sequential
  claim holders (never racing the *registry* concurrently, but potentially overlapping in
  wall-clock time against the *store*), not from an out-of-band third party. Three options were
  considered to close this gap:
  - **(a) Heartbeat** — the working worker periodically refreshes its claim's `claimed_at` during
    `create_index`, so a live worker never looks stale to a waiter. Rejected: this adds a new
    registry write path with its own new failure mode (what does a heartbeat write failure, or a
    heartbeat racing a concurrent `reclaim`'s read, actually mean for the worker — abort, or press
    on with a possibly-already-reclaimed claim?) — and it can only be driven from *inside*
    `create_index`'s own blocking retry loop, the thing that is slow. Either the adapter itself
    would need to call back into the registry mid-retry (violating house rules' "adapters thin:
    wrap, never implement algorithms" — the adapter would now own orchestration-layer state it has
    no business owning), or `IndexProvisioner` would need to run `create_index` on a background
    thread and heartbeat on a timer from the caller's thread — introducing real new concurrency
    (thread lifecycle, cancellation, exception propagation) into a currently single-threaded
    synchronous call path solely to guard against this one race.
  - **(b) Timing bound** — raise `provisioning_stale_after_seconds` above the maximum possible
    `create_index` retry envelope (e.g. to 450s) and enforce that relationship with a
    config-load-time validation check. Rejected: the two values do not live on the same config
    surface — `provisioning_stale_after_seconds` is `index_provisioning.yaml`'s own tunable, while
    the retry envelope is `AzureAISearchProvisioner`'s own constructor defaults (§1), not
    config-driven at all today, and a future second adapter implementation could carry its own,
    different envelope. A "genuinely enforced" validation check would need to read across
    independently-owned config/constructor surfaces and stay correct as either evolves — exactly
    the drift risk the task itself warns about, and not resolved merely by asserting it once at
    load time if nothing forces the two values to be edited together in the future. It also
    weakens the *far more common* case this config exists for — a worker that is genuinely dead —
    by widening the crash-detection window to 450s+ for every claim, not just the rare
    slow-retry one: a worse trade against AC4's own "reclaimable" requirement.
  - **(c) Idempotent-by-contract (CHOSEN)** — require `create_index` to be a create-or-update that
    converges to the same end state no matter how many times it is legitimately called with the
    same resolved template/dimensions/metric. This sidesteps the timing question entirely and,
    checked against this codebase's own precedent rather than assumed, turns out to be the
    smallest actual lift, not a bigger one: `_azure_search_backend._RealSearchBackend
    .create_or_update_index` (read directly) already wraps the real SDK's
    `SearchIndexClient.create_or_update_index` — an inherently idempotent create-or-update call,
    not a create-only one — and REQ-008's `AzureAISearchIndexer.ensure_schema` already relies on
    exactly this idempotency for its own "already compatible — idempotent no-op" branch.
    `InMemoryProvisioner.create_index` (§1) already delegates to `InMemoryIndexer.ensure_schema`,
    the identical REQ-008 idempotent primitive. Both adapters this REQ ships therefore already
    satisfy a create-or-update contract in practice; A13 simply makes that the *stated, required*
    contract rather than an unstated accident, closing the gap §3.3 step 7 exposed without
    inventing any new mechanism, config key, or error code.
- **A14 — Race C's convergence safety additionally requires that both `create_index` calls
  resolve an *identical* `IndexTemplate` object, not merely identical `dimensions`/`metric`
  (design-review MAJOR resolution, round 2) — closed by stamping the resolved template into the
  claim itself.** `IndexTemplateStore` (§1) eager-loads every `config/index_templates/*.yaml`
  file once at construction and never refreshes; Race C's own scenario (§3.4 step 7) involves two
  potentially separate processes/workers, each holding its own independently-constructed
  `IndexTemplateStore` instance. If a template file is edited/redeployed at any point during the
  window Race C spans — up to roughly `create_index`'s own ~390s retry envelope (Assumption A13)
  plus `provisioning_stale_after_seconds`'s default 300s before a reclaim even triggers — the
  original claim holder and a reclaiming worker could otherwise resolve genuinely different
  `template_version`/`hnsw`/`fields` content for the identical `template_id`, so the two
  `create_index` calls would not converge, and whichever call's write loses the interleaving could
  have its own `describe_index` compatibility check (§3.2 steps 7–8) observe the other worker's
  write and spuriously raise `INDEX_SCHEMA_DRIFT`/`mark_failed` — a false human-intervention-
  required failure caused entirely by an ordinary template deployment landing at an unlucky moment.
  Rejected the alternative (documenting this as an accepted operational constraint — "do not
  redeploy a template file while a claim referencing it may be in flight"): that would impose a
  real, easy-to-violate operational discipline on every template deploy indefinitely, for a gap
  this LLD can instead close outright with a small, bounded, additive mechanism — consistent with
  this LLD's own precedent at A13 of adding a real mechanism rather than documenting the original
  BLOCKER as an accepted risk. Concretely: `IndexRegistryEntry` gains
  `resolved_template: IndexTemplate | None` (§1, §2) — the exact `IndexTemplate` object resolved
  by `_resolve_template` at the moment the *original* claim is inserted (§3.2 no-entry sub-case
  step 3), non-`None` only while `status == "provisioning"`, cleared at `mark_ready`/`mark_failed`
  exactly like `claim_token`/`claimed_at` (Assumption A3's established lifecycle pattern — §1's
  `mark_ready`/`mark_failed` docstrings updated accordingly). A reclaiming worker (§3.4 step 1) no
  longer calls `_resolve_template` against its own, possibly-stale `IndexTemplateStore` to build
  `new_candidate` — it copies `entry.resolved_template` (and the already-recorded
  `template_id`/`template_version`/`dimensions`/`metric`, themselves derived from that same
  resolution) verbatim from the stale entry it observed. `_claim_and_create` (§3.2 steps 5–10),
  the shared sub-flow entered by both the original winner and any successful reclaimer, is
  correspondingly simplified to a single-argument-beyond-`scenario` signature —
  `_claim_and_create(scenario, won_entry) -> IndexRegistryEntry` — reading the template/dimensions/
  metric it creates the index with directly off `won_entry.resolved_template`/`.dimensions`/
  `.metric`, never via a separately threaded parameter, so both callers share one unambiguous
  source of truth (this also resolves a pre-existing, cosmetic inconsistency between §3.2 step 5's
  four-argument call and §1's own provisioner.py inline comment, which already documented the
  two-argument shape). `TableStorageIndexRegistry` persists `resolved_template` as a
  JSON-serialized string entity property (the same nested-model-as-JSON approach its own entity
  codec already needs for `hnsw`/`fields`), deserialized back into an `IndexTemplate` on read.
  This does not change `ensure()`'s ready-and-verification-elapsed path (§3.2 verification-elapsed
  sub-case), which always re-resolves the live template afresh on every call by design — that is
  what lets an intentional template upgrade be picked up by an already-`ready` index at its next
  verification; the gap this closes is scoped precisely to the in-flight provisioning-claim
  window, where two calls both purporting to create/converge on the same index for the same claim
  must agree, not to the live-template-evolution behavior everywhere else in this module, which
  remains untouched and intentional.
- **A15 — `InMemoryIndexer` (REQ-008, already merged) gains a read-only `schema` property
  exposing its existing private `_schema: tuple[int, str] | None` attribute (design-review MINOR
  resolution, round 4 — promoted to a formal Assumption, matching A1's precedent for cross-module
  edits to already-merged code).** `InMemoryProvisioner.index_exists` (§1) needs to observe
  whether the wrapped `InMemoryIndexer` instance already has a schema set, without duplicating
  `InMemoryIndexer`'s own schema-state bookkeeping (house rules: DRY, adapters thin) — the same
  class of small, additive, non-breaking cross-module edit to already-merged code as Assumption
  A1's `Scenario.index_template_id` addition. This LLD requires adding
  `@property def schema(self) -> tuple[int, str] | None: return self._schema` to
  `vestibule/indexer/adapters/in_memory.py`'s `InMemoryIndexer`: purely additive (a new read-only
  accessor, no existing method signature or behavior changes), non-breaking (every existing call
  site of `InMemoryIndexer` — `ensure_schema`/`upsert`/`search`/its own tests — is unaffected,
  since nothing about `_schema`'s own read/write behavior inside `ensure_schema` changes), and
  does not weaken encapsulation in any way that matters here: the property is read-only (no
  setter), so `InMemoryProvisioner` can observe but never mutate `InMemoryIndexer`'s schema state
  directly — `InMemoryProvisioner.create_index` (§1) itself never touches `_schema` directly
  either, always going through `ensure_schema`.
- **A16 — `CachedIndexRegistry`, a TTL-cache decorator over `IndexRegistry.get`/
  `list_by_vertical` only, is added to close a design-review MAJOR (round 4): `ensure()`'s fast
  path (§3.1) calling `registry.get()` unconditionally on every document is a live network round
  trip against `TableStorageIndexRegistry` with no cache in front of it, unlike this codebase's
  own `ScenarioStore` (REQ-010), whose analogous per-document hot-path read is already solved by
  `CachedScenarioStore` (`vestibule/scenario/stores/cached_store.py`, read directly).**
  `CachedIndexRegistry` (`vestibule/provisioning/stores/cached_registry.py`, §1) mirrors
  `CachedScenarioStore`'s actual shape exactly — same `threading.RLock`-guarded
  `dict[str, tuple[T, float]]` cache keyed by lookup key, same `time.monotonic`-based TTL expiry
  via an injectable `_now` seam, same whole-cache-clear `invalidate(key)` scope decision (a
  per-`index_name` reverse index into `list_by_vertical`'s own cached rows would need
  `index_name -> vertical` bookkeeping with its own edge cases, for a store expected to hold, in
  practice, one row per provisioned index — small by nature, the same reasoning
  `CachedScenarioStore`'s own docstring gives for scenarios) — applied to a different port.
  - **Load-bearing constraint — the claim path is never cached.** `get(index_name)`/
    `list_by_vertical(vertical)` are cacheable: read-mostly, and staleness within a bounded TTL is
    an acceptable, ordinary tradeoff for them (exactly `CachedScenarioStore.get`/
    `get_by_vertical`/`list_all`'s own tradeoff). `register()`/`reclaim()` — the two CAS
    primitives §3.3/§3.4's entire concurrency argument, and Assumptions A13/A14, are built on —
    are different in kind, not degree: `CachedIndexRegistry.register`/`.reclaim` always delegate
    straight to the wrapped registry's real backing store, every call, unconditionally, and never
    populate or consult the cache on the way in. This is a correctness requirement, not an
    implementation nicety: a cached "no entry exists" (or cached stale-`provisioning`) `get()`
    answer has no bearing on what `register()`/`reclaim()` themselves ever see, because those two
    methods never read through the cache at all — only through `wrapped.register`/
    `wrapped.reclaim` directly, which is what actually resolves the race, exactly as it does today
    with no cache present. A future maintainer must not "simplify" this by routing either through
    the cache's own read path — doing so would let two callers each observe a cached,
    no-longer-true "absent"/"stale" answer and each believe they are the first to claim, defeating
    the exclusivity guarantee `register`'s atomic insert / `reclaim`'s ETag-CAS exists to provide.
  - **Invalidation on every write, including ones that bypass the cache on the read side.**
    `CachedIndexRegistry` wraps every mutating `IndexRegistry` method — `register`, `reclaim`,
    `mark_ready`, `mark_failed`, `mark_retired`, and `touch_verified` — delegating each straight
    through to `wrapped` (uncached, as above) and then invalidating the affected `index_name` on
    this decorator's own cache after that call returns. Concretely: `register(entry)` invalidates
    `entry.index_name` after delegating, regardless of whether this call's own return value
    indicates a win or a loss — a loss still means *some* entry (the winner's) now exists where
    the cache may have previously held a stale `None`/absent answer, so invalidating is required
    in both outcomes, not just the winning one. `reclaim`/`mark_ready`/`mark_failed`/
    `mark_retired`/`touch_verified` invalidate `index_name` after a successful delegate call; a
    raised `INDEX_PROVISION_CONFLICT` means nothing changed on the wrapped store, so the exception
    propagates before the invalidate call runs (nothing to invalidate for). This is the precise
    answer to "how is invalidation wired for a call that bypasses the cache on the read side but
    must still affect it on the write side": `CachedIndexRegistry` itself — not `wrapped` — is
    responsible for calling `self.invalidate()` after each of its own mutating methods returns,
    exactly mirroring `CachedScenarioStore.upsert`/`.delete`'s own
    `self.invalidate(result.scenario_id)`-after-delegating pattern, just applied to six mutating
    methods instead of two.
  - **`touch_verified` also invalidates — closing a `last_verified_at` staleness gap
    `verification_interval_seconds` would otherwise hit.** `CachedIndexRegistry`'s own TTL and
    `config.verification_interval_seconds` (§1's `ProvisioningConfig`) are two separate,
    independently-purposed caches and must not be conflated: the former governs how fresh a
    `registry.get()`/`list_by_vertical()` read is (avoiding a registry round trip per document);
    the latter governs how often a `ready` entry's live compatibility is re-verified against the
    underlying search service via `describe_index` (avoiding a strictly more expensive live-API
    call per document, §3.2's verification-elapsed sub-case). Worked through: if `touch_verified`'s
    write were *not* invalidated, a `CachedIndexRegistry`-served `get()` immediately afterward,
    within the cache's own TTL window, would still return the pre-write cached entry — whose
    `last_verified_at` is now older than what a fresh registry read would show. This cannot cause
    a verification to be *skipped* when it should run (the stale `last_verified_at` only makes
    `_clock() - entry.last_verified_at` look *larger*, i.e. more, not less, elapsed than reality —
    §3.2's `>= verification_interval_seconds` check errs toward re-verifying, never toward
    silently trusting a compatibility check that never happened); it *would*, however, cause a
    spurious extra `describe_index` call on every subsequent `ensure()` within the stale cache
    window, defeating part of the whole point of `verification_interval_seconds`'s own caching.
    This LLD closes that gap by having `touch_verified`'s write also invalidate `index_name`
    (beyond this MAJOR finding's own literal minimum of `mark_ready`/`mark_failed`/`mark_retired`/
    a successful claim) — so a `get()` immediately after `touch_verified` always reflects the
    just-bumped `last_verified_at`, and no redundant `describe_index` cost is paid. **Conclusion:**
    no correctness issue either way (verification is never silently skipped), but invalidating
    `touch_verified` too is required to actually deliver the caching's own cost-avoidance goal,
    not merely to avoid a bug.
  - **Interaction with `_wait_for_ready`'s own polling loop (§3.4 step 6).** `_wait_for_ready`
    re-`registry.get()`s every `config.wait_poll_interval_seconds` (default `2.0`, Assumption A8)
    while bounded by `config.wait_for_provisioning_timeout_seconds` (default `60.0`). Because this
    is the *same* `get()` method the fast path caches, a `CachedIndexRegistry` TTL comparable to
    or larger than `wait_for_provisioning_timeout_seconds` would risk a waiting worker never
    observing a genuine `provisioning -> ready` transition the live store already reflects,
    producing a spurious `INDEX_PROVISION_TIMEOUT` on a provision that actually succeeded quickly.
    This is the deciding factor (over simply matching `CachedScenarioStore`'s own `60.0` default)
    behind this LLD's chosen default of `10.0` seconds — see §6.
  - **Composition-root wiring** (out of scope for this LLD's own decision to make, flagged the
    same way A11 flags REQ-008 integration): the composition root wraps `TableStorageIndexRegistry`
    in `CachedIndexRegistry` before constructing `IndexProvisioner` (`registry_cache.enabled`,
    §6); `InMemoryIndexRegistry` (dev/test) is already zero-network-cost and is not wrapped by
    default, though nothing in `IndexRegistry`'s port prevents it — §7's cache-behavior tests run
    `CachedIndexRegistry` over a `_CountingRegistry`-shaped test double, mirroring
    `test_cached_store.py`'s own `_CountingStore` convention (REQ-010, read directly), not over a
    live `TableStorageIndexRegistry`.

## 1. Interfaces

```python
# vestibule/provisioning/model.py
"""IndexTemplate/FieldSpec/HnswSettings/IndexRegistryEntry data model, ProvisioningError,
and the 11 registered error codes (REQ-011). Mirrors vestibule.scenario.model's shape:
model + exception + register_error() calls, no store I/O.
"""

INDEX_TEMPLATE_NOT_FOUND = "INDEX_TEMPLATE_NOT_FOUND"
INDEX_TEMPLATE_INVALID = "INDEX_TEMPLATE_INVALID"
INDEX_SCHEMA_DRIFT = "INDEX_SCHEMA_DRIFT"
INDEX_PROVISION_FAILED = "INDEX_PROVISION_FAILED"
INDEX_RETIRED = "INDEX_RETIRED"
INDEX_AUTO_CREATE_DISABLED = "INDEX_AUTO_CREATE_DISABLED"
INDEX_PROVISION_TIMEOUT = "INDEX_PROVISION_TIMEOUT"
INDEX_PROVISION_CONFLICT = "INDEX_PROVISION_CONFLICT"
INDEX_REGISTRY_UNAVAILABLE = "INDEX_REGISTRY_UNAVAILABLE"
INDEX_PROVISIONER_UNAVAILABLE = "INDEX_PROVISIONER_UNAVAILABLE"
INDEX_PROVISIONER_DEPENDENCY_MISSING = "INDEX_PROVISIONER_DEPENDENCY_MISSING"

FieldType = Literal[
    "key", "text", "vector", "filterable_string",
    "filterable_string_collection", "filterable_datetime", "retrievable_only",
]


class FieldSpec(BaseModel):
    """One store-agnostic schema field. Frozen."""
    model_config = ConfigDict(frozen=True)

    name: str
    type: FieldType
    searchable: bool = False


class HnswSettings(BaseModel):
    """HNSW vector-index tuning. Frozen."""
    model_config = ConfigDict(frozen=True)

    m: int
    ef_construction: int
    ef_search: int


class IndexTemplate(BaseModel):
    """Versioned, store-agnostic index schema (REQ-011). Frozen, self-validating —
    loaded from config/index_templates/*.yaml by IndexTemplateStore (templates.py).

    Attributes:
        template_id: e.g. "standard-v1" — the config filename's stem, cross-checked
            against the file's own declared template_id at load time (Assumption,
            mirrors YamlScenarioStore's fail-fast-on-load pattern).
        template_version: Non-negative-integer string (Assumption A6) — bumped when
            the schema changes; drives INDEX_SCHEMA_DRIFT's staleness ordering.
        dimensions: Explicit override, or None to inherit scenario.indexer.dimensions
            (Assumption A2).
        metric: "cosine" | "dotProduct" | "euclidean".
        hnsw: HNSW tuning (Assumption A11: this REQ's sole source of truth for
            creation-time HNSW parameters — Scenario.indexer.hnsw_* is not read here).
        fields: Store-agnostic schema, validated for exactly one "key" field and at
            least one "vector" field (model_validator, below).
        semantic_ranker_enabled: Whether AzureAISearchProvisioner configures a
            SemanticSearch/SemanticConfiguration on the index (schema-level).
        hybrid_enabled: Recorded for provenance/future query-side use; no schema
            effect (BM25-searchable "text" fields are already hybrid-eligible) —
            Assumption, documented here since it could otherwise look like a no-op bug.
    """
    model_config = ConfigDict(frozen=True)

    template_id: str
    template_version: str
    dimensions: int | None
    metric: Literal["cosine", "dotProduct", "euclidean"]
    hnsw: HnswSettings
    fields: list[FieldSpec]
    semantic_ranker_enabled: bool
    hybrid_enabled: bool

    @field_validator("template_version")
    @classmethod
    def _version_is_integer_string(cls, value: str) -> str:
        """Raises ValueError unless value parses as a non-negative int (Assumption A6)."""
        ...

    @model_validator(mode="after")
    def _validate_field_shape(self) -> "IndexTemplate":
        """Raises ValueError unless exactly one FieldSpec has type == 'key', at least
        one has type == 'vector', and every `name` is unique — INDEX_TEMPLATE_INVALID's
        'invalid field spec' trigger, mirroring Scenario's own model_validator
        fail-fast-at-construction pattern (REQ-010).
        """
        ...


class IndexRegistryEntry(BaseModel):
    """One row per provisioned (or in-flight) index (REQ-011). Frozen.

    Attributes:
        index_name: Key.
        vertical: Owning vertical.
        scenario_id: The scenario that triggered this index's (last) provisioning.
        template_id: Resolved template id at (last) provisioning/reclaim time.
        template_version: Resolved template version — drift-checked (Assumption A6).
        dimensions: Resolved effective dimensions (Assumption A2).
        metric: Resolved metric.
        embedding_model: scenario.embedder.model, for the registry's "what exists,
            holding which model" answer (story's market-awareness note).
        status: provisioning | ready | failed | retired.
        created_at: Set once, when this index_name's row is first inserted
            (register()'s winning insert); preserved across reclaim (Assumption A3).
        last_verified_at: None until the first mark_ready/touch_verified; drives
            verification_interval_seconds caching.
        document_count: None — no writer in this REQ populates it (out of scope).
        claim_token: Opaque per-claim token (Assumption A3/A4) — non-None only while
            status == "provisioning"; None otherwise. Cross-process-safe: an
            unguessable value (uuid4 hex), never interpreted, only compared for
            equality by whichever backend's CAS primitive is checking it.
        claimed_at: When the *current* claim was made or last reclaimed (Assumption
            A3) — distinct from created_at; drives provisioning_stale_after_seconds.
        last_error_message: mark_failed's reason, for INDEX_PROVISION_FAILED's
            "requires human intervention" to be actionable (Assumption A3).
        resolved_template: The exact IndexTemplate resolved by _resolve_template at the
            moment THIS claim (original or reclaimed) was written — non-None only
            while status == "provisioning"; cleared at mark_ready/mark_failed exactly
            like claim_token/claimed_at (Assumption A14). A reclaiming worker copies
            this field verbatim from the stale entry it observed rather than
            independently re-resolving it, guaranteeing every create_index call made
            across a single claim's lifetime is called with a byte-identical template
            regardless of what config/index_templates/*.yaml looks like on disk at the
            moment any individual call runs (closes the design-review MAJOR on Race
            C's convergence argument, round 2 — see §3.4).
    """
    model_config = ConfigDict(frozen=True)

    index_name: str
    vertical: str
    scenario_id: str
    template_id: str
    template_version: str
    dimensions: int
    metric: str
    embedding_model: str
    status: Literal["provisioning", "ready", "failed", "retired"]
    created_at: datetime
    last_verified_at: datetime | None
    document_count: int | None
    claim_token: str | None
    claimed_at: datetime | None
    last_error_message: str | None
    resolved_template: IndexTemplate | None


class IndexDescription(BaseModel):
    """Live-index facts an IndexProvisionerAdapter can report, for drift detection.
    Frozen. Deliberately minimal — no template_version (no store tracks this REQ's
    own metadata); template_version drift is checked against the registry entry's
    own recorded value instead (see §3.1)."""
    model_config = ConfigDict(frozen=True)

    dimensions: int
    metric: str


class ProvisioningError(RaggedError):
    """Single exception type for every provisioning-module-raised failure.
    error_code varies per raise site (same shape as ScenarioError/ChunkerError/...).
    """

    def __init__(self, index_name: str, reason: str, *, error_code: str) -> None:
        self.index_name = index_name
        self.reason = reason
        super().__init__(f"{index_name}: {reason}", error_code=error_code)


# Registered at import time (Contract #4) — all 11 codes, see §5 for the full table.
register_error(INDEX_TEMPLATE_NOT_FOUND, Severity.PERMANENT, "...")
register_error(INDEX_TEMPLATE_INVALID, Severity.PERMANENT, "...")
register_error(INDEX_SCHEMA_DRIFT, Severity.PERMANENT, "...")
register_error(INDEX_PROVISION_FAILED, Severity.PERMANENT, "...")
register_error(INDEX_RETIRED, Severity.PERMANENT, "...")
register_error(INDEX_AUTO_CREATE_DISABLED, Severity.PERMANENT, "...")
register_error(INDEX_PROVISION_TIMEOUT, Severity.TRANSIENT, "...")
register_error(INDEX_PROVISION_CONFLICT, Severity.TRANSIENT, "...")
register_error(INDEX_REGISTRY_UNAVAILABLE, Severity.TRANSIENT, "...")
register_error(INDEX_PROVISIONER_UNAVAILABLE, Severity.TRANSIENT, "...")
register_error(INDEX_PROVISIONER_DEPENDENCY_MISSING, Severity.PERMANENT, "...")
```

```python
# vestibule/provisioning/templates.py
"""IndexTemplateStore — loads/validates config/index_templates/*.yaml (REQ-011).
Mirrors YamlScenarioStore's eager-load-at-construction, fail-fast pattern exactly
(vestibule/scenario/stores/yaml_store.py)."""


class IndexTemplateStore:
    """Read-only, in-memory catalog of every IndexTemplate under `directory`. Never
    refreshes after construction — a template file edited/redeployed after this
    object is built is not observed by this instance; this is precisely why a
    reclaiming worker no longer re-resolves a template through its own
    IndexTemplateStore mid-claim, and instead reuses the exact IndexTemplate snapshot
    stamped into the claim at original-registration time (Assumption A14)."""

    def __init__(self, directory: str | Path) -> None:
        """Loads and validates every *.yaml file under `directory` at construction.

        Raises:
            ProvisioningError: INDEX_TEMPLATE_INVALID (PERMANENT) if any file fails to
                parse as YAML, is not a mapping, fails IndexTemplate validation, or its
                filename stem does not match its own declared template_id — fail fast,
                before any document is ever in scope (mirrors YamlScenarioStore AC5).
        """
        ...

    def get(self, template_id: str) -> IndexTemplate | None:
        """Returns the loaded template, or None if template_id is unknown."""
        ...

    def get_or_raise(self, template_id: str) -> IndexTemplate:
        """Returns the loaded template.

        Raises:
            ProvisioningError: INDEX_TEMPLATE_NOT_FOUND (PERMANENT) if template_id is
                unknown.
        """
        ...
```

```python
# vestibule/provisioning/registry.py
"""IndexRegistry port + InMemoryIndexRegistry (REQ-011). See §3.3/§3.4 for the full
concurrency reasoning behind register()/reclaim()/mark_ready()/mark_failed()'s exact
semantics — this is the load-bearing module of this REQ.
"""


class IndexRegistry(ABC):
    """Persistence port every backend (InMemoryIndexRegistry, TableStorageIndexRegistry,
    CachedIndexRegistry) implements. Every mutating method is a single, backend-defined
    atomic operation — never a read call followed by a separate write call from the
    caller's side."""

    @abstractmethod
    def get(self, index_name: str) -> IndexRegistryEntry | None:
        """Returns the current entry, or None if unknown. Never raises for a normal
        miss; raises ProvisioningError(INDEX_REGISTRY_UNAVAILABLE) only on a genuine
        backend failure (TableStorageIndexRegistry only, after exhausting retries)."""
        ...

    @abstractmethod
    def list_by_vertical(self, vertical: str) -> list[IndexRegistryEntry]:
        """Every entry for `vertical`, order unspecified."""
        ...

    @abstractmethod
    def register(self, entry: IndexRegistryEntry) -> IndexRegistryEntry:
        """Atomic conditional insert: insert-if-absent, else return the entry already
        stored, unconditionally — never validates `entry`'s fields against what is
        already stored, and never raises purely because an entry already exists (the
        story's 'the losing worker does not error'). See §3.3 for the full two-worker
        walkthrough and exactly what 'atomic' means per backend.

        Returns:
            `entry` itself (by value, backend-echoed) if this call performed the
            insert; the ALREADY-STORED entry otherwise. Callers distinguish "did I
            win" via `returned.claim_token == entry.claim_token` — never via identity
            comparison (unsafe across a serialized backend) or wall-clock comparison
            (unsafe under clock skew).

        Raises:
            ProvisioningError: INDEX_REGISTRY_UNAVAILABLE (TRANSIENT) on a genuine
                backend failure (TableStorageIndexRegistry only). Never raises for a
                create-race — see §3.3.
        """
        ...

    @abstractmethod
    def reclaim(
        self, index_name: str, observed: IndexRegistryEntry, new_entry: IndexRegistryEntry
    ) -> IndexRegistryEntry:
        """The story's 'second conditional write.' Succeeds ONLY if the entry
        currently stored for index_name is still exactly `observed` (Assumption A4;
        see §3.4 for the precise per-backend equality/ETag semantics) — replaces it
        with `new_entry` atomically. Never partially applies.

        Returns:
            `new_entry`, on success.

        Raises:
            ProvisioningError: INDEX_PROVISION_CONFLICT (TRANSIENT) if the stored
                entry has changed since `observed` was read (someone else reclaimed
                first, or the original claim owner finished normally) —
                INDEX_REGISTRY_UNAVAILABLE (TRANSIENT) on a genuine backend failure.
        """
        ...

    @abstractmethod
    def touch_verified(self, index_name: str) -> IndexRegistryEntry:
        """Bumps last_verified_at to now on an already-'ready' entry. Not CAS-protected
        (Assumption A4 — no claim to protect; two concurrent touches are both harmless).

        Raises:
            ProvisioningError: INDEX_REGISTRY_UNAVAILABLE (TRANSIENT) on backend
                failure. Never raises for a status other than 'ready' — callers only
                ever invoke this after already confirming status == 'ready'.
        """
        ...

    @abstractmethod
    def mark_ready(
        self, index_name: str, *, expected_claim_token: str | None
    ) -> IndexRegistryEntry:
        """Flips status -> 'ready', stamps last_verified_at = now, clears
        claim_token/claimed_at/resolved_template (Assumption A14). CAS-protected
        against expected_claim_token when non-None (Assumption A5; see §3.4 step 4b
        for why).

        Raises:
            ProvisioningError: INDEX_PROVISION_CONFLICT (TRANSIENT) if
                expected_claim_token is non-None and does not match the currently
                stored entry's claim_token (someone else already finalized this
                claim — a reclaimer beat this caller to it). INDEX_REGISTRY_UNAVAILABLE
                (TRANSIENT) on backend failure.
        """
        ...

    @abstractmethod
    def mark_failed(
        self, index_name: str, reason: str, *, expected_claim_token: str | None
    ) -> IndexRegistryEntry:
        """Flips status -> 'failed', records last_error_message = reason, clears
        claim_token/claimed_at/resolved_template (Assumption A14). Same CAS semantics
        as mark_ready. Terminal: nothing in this module ever transitions a 'failed'
        entry onward automatically (story: 'requires human intervention'; Out of
        scope: no un-stick mechanism exists)."""
        ...

    @abstractmethod
    def mark_retired(self, index_name: str) -> IndexRegistryEntry:
        """Flips status -> 'retired'. Never called by IndexProvisioner itself (Out of
        scope: 'nothing calls them automatically') — an explicit admin operation, no
        CAS needed since it is not racing an in-flight claim by construction."""
        ...


class InMemoryIndexRegistry(IndexRegistry):
    """Thread-safe, single-process IndexRegistry for tests and local dev. Backed by a
    `dict[str, IndexRegistryEntry]` guarded by one `threading.RLock` held for the full
    duration of each public method — the exact InMemoryLedgerStore pattern (REQ-003).

    Cross-process note (Assumption, Contract #2 touchpoint): this backend proves
    nothing about cross-process safety — it IS one process. Its job is to correctly
    *simulate* the same claim/conflict/reclaim state machine TableStorageIndexRegistry
    enforces for real, deterministically, so §7's concurrency unit tests can exercise
    real `threading` races against it. See §3.3/§3.4 for exactly what 'atomic' means
    here: the RLock, not the GIL, is the guarantee — see the inline reasoning there.
    """

    def __init__(self) -> None: ...

    # get/list_by_vertical/register/reclaim/touch_verified/mark_ready/mark_failed/
    # mark_retired — see §3.3/§3.4 for the precise critical-section contents of
    # register()/reclaim()/mark_ready()/mark_failed(); every other method is a
    # single dict read or write under self._lock, no CAS needed.
```

```python
# vestibule/provisioning/stores/table_storage_registry.py
"""TableStorageIndexRegistry — Azure Table Storage-backed IndexRegistry (REQ-011).
Production path; the only backend that provides genuine cross-process guarantees
(Assumption, Contract #2 touchpoint). Mirrors TableStorageScenarioStore's shape and
retry/timeout machinery (_with_retry/_call_with_timeout, tenacity, azure-data-tables
lazy import -> INDEX_PROVISIONER_DEPENDENCY_MISSING) — reusing the PATTERN established
there, not its private types (Assumption A9).

Partition key = vertical, row key = index_name (mirrors REQ-010's vertical/scenario_id
choice, substituting index_name for scenario_id since IndexRegistryEntry is keyed by
index_name, not scenario_id).
"""


class TableStorageIndexRegistry(IndexRegistry):
    def __init__(
        self,
        connection_string: str,
        table_name: str,
        *,
        timeout_seconds: float = 30.0,
        max_attempts: int = 5,
        backoff_base_seconds: float = 2.0,
        backoff_max_seconds: float = 60.0,
        _sleep: Callable[[float], None] = time.sleep,
        _backend: "_TableBackend | None" = None,
    ) -> None:
        """See TableStorageScenarioStore.__init__ for the identical parameter shape.

        Raises:
            ProvisioningError: INDEX_PROVISIONER_DEPENDENCY_MISSING (PERMANENT) if
                azure-data-tables is not installed and _backend was not injected —
                this REQ's one dependency-missing code covers both the registry
                backend and the search adapter backend (Assumption; the story's 11
                codes provide only one DEPENDENCY_MISSING code, unlike REQ-010, which
                has a store-specific one separate from REQ-008's indexer-specific one
                — this REQ deliberately shares a single code across both of this
                module's own optional-dependency boundaries, since both name the same
                underlying azure-data-tables/azure-search-documents install story).
        """
        ...

    # get/list_by_vertical -> query(partition_key=vertical) / query(row_key=index_name)
    # register(entry) -> backend.upsert_entity(vertical, index_name, data, etag=None);
    #   a 409 (ResourceExistsError) is caught HERE specifically (not treated as a
    #   generic conflict) and falls back to backend.get_entity(...) — see §3.3.
    # reclaim/mark_ready/mark_failed -> fresh backend.get_entity() for its live etag,
    #   then backend.upsert_entity(..., etag=<that live etag>) (update branch,
    #   match_condition=IfNotModified) — a 409/412 maps to INDEX_PROVISION_CONFLICT,
    #   never silently retried (blindly retrying an ETag conflict can never resolve
    #   it, exactly TableStorageScenarioStore's own established reasoning). See §3.4.
```

```python
# vestibule/provisioning/stores/cached_registry.py
"""CachedIndexRegistry — TTL-cache decorator over any IndexRegistry backend (REQ-011,
Assumption A16, design-review MAJOR resolution, round 4). Mirrors CachedScenarioStore
(REQ-010, vestibule/scenario/stores/cached_store.py, read directly) shape and conventions
exactly — same threading.RLock-guarded dict[str, tuple[T, float]] cache, same
time.monotonic-based TTL via an injectable _now seam, same whole-cache-clear
invalidate(key) scope decision — applied to a different port.

LOAD-BEARING CONSTRAINT: get()/list_by_vertical() are the only cached reads. register()/
reclaim() ALWAYS delegate straight to the wrapped registry's real backing store, every
call, and never read from or write into the cache themselves — only the invalidation
they trigger on their own mutating write touches the cache. Caching any part of the
claim path would silently break the exclusivity/CAS guarantees Assumptions A13/A14 and
§3.3/§3.4 depend on: a cached "no entry exists" (or cached stale) read could let two
callers each believe they are first. Do not "simplify" this by routing register()/
reclaim() through the cache — see Assumption A16 for the full reasoning, including why
touch_verified also invalidates and how this cache's TTL interacts with
_wait_for_ready's own polling loop (§3.4 step 6).
"""

_DEFAULT_TTL_SECONDS = 10.0  # Assumption A16 — deliberately well below both
                              # wait_poll_interval_seconds' own timeout bound
                              # (wait_for_provisioning_timeout_seconds, default 60.0) and
                              # CachedScenarioStore's 60.0 default; see Assumption A16.


class CachedIndexRegistry(IndexRegistry):
    """TTL-cache decorator wrapping any other IndexRegistry backend. get()/
    list_by_vertical() are served from cache within ttl_seconds; every mutating method
    (register/reclaim/mark_ready/mark_failed/mark_retired/touch_verified) always
    delegates straight to `wrapped`, uncached, then invalidates the affected index_name
    on this decorator's own cache (Assumption A16 — load-bearing, see module docstring)."""

    def __init__(
        self,
        wrapped: IndexRegistry,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        _now: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initializes an empty cache over `wrapped`. Mirrors
        CachedScenarioStore.__init__'s identical parameter shape (REQ-010)."""
        ...

    def get(self, index_name: str) -> IndexRegistryEntry | None:
        """Served from cache within `ttl_seconds` (mirrors CachedScenarioStore.get)."""
        ...

    def list_by_vertical(self, vertical: str) -> list[IndexRegistryEntry]:
        """Served from cache within `ttl_seconds` (mirrors CachedScenarioStore.list_all)."""
        ...

    def register(self, entry: IndexRegistryEntry) -> IndexRegistryEntry:
        """NEVER served from or written into the cache (Assumption A16) — always
        delegates straight to `wrapped.register(entry)`. Invalidates `entry.index_name`
        after delegating, regardless of whether the return value indicates this call
        won or lost (§3.3) — either outcome means the live store's answer for this
        `index_name` may now differ from whatever was previously cached."""
        ...

    def reclaim(
        self, index_name: str, observed: IndexRegistryEntry, new_entry: IndexRegistryEntry
    ) -> IndexRegistryEntry:
        """NEVER served from or written into the cache (Assumption A16) — always
        delegates straight to `wrapped.reclaim(...)`. Invalidates `index_name` after a
        successful delegate call; a raised INDEX_PROVISION_CONFLICT means nothing
        changed on the wrapped store, so no invalidation runs on that path (the
        exception propagates before reaching the invalidate call)."""
        ...

    def touch_verified(self, index_name: str) -> IndexRegistryEntry:
        """Delegates straight to `wrapped.touch_verified`, then invalidates
        `index_name` (Assumption A16 — required so a `get()` immediately afterward
        reflects the just-bumped `last_verified_at`, avoiding a spurious redundant
        `describe_index` call; see Assumption A16 for the full worked-through
        reasoning)."""
        ...

    def mark_ready(
        self, index_name: str, *, expected_claim_token: str | None
    ) -> IndexRegistryEntry:
        """Delegates straight to `wrapped.mark_ready`, then invalidates `index_name`
        on success. A raised INDEX_PROVISION_CONFLICT (§3.4 step 4b) propagates before
        the invalidate call — nothing changed on the wrapped store to invalidate for."""
        ...

    def mark_failed(
        self, index_name: str, reason: str, *, expected_claim_token: str | None
    ) -> IndexRegistryEntry:
        """Delegates straight to `wrapped.mark_failed`, then invalidates `index_name`
        on success. Same conflict-propagates-before-invalidate behavior as mark_ready."""
        ...

    def mark_retired(self, index_name: str) -> IndexRegistryEntry:
        """Delegates straight to `wrapped.mark_retired`, then invalidates `index_name`."""
        ...

    def invalidate(self, index_name: str) -> None:
        """Forces the next `get`/`list_by_vertical` read of anything to re-query
        `wrapped`. Mirrors CachedScenarioStore.invalidate's whole-cache-clear-
        regardless-of-key decision (REQ-010) — same reasoning: a per-`index_name`
        reverse index into `list_by_vertical`'s own cached rows would need
        `index_name -> vertical` bookkeeping with its own edge cases, for a store
        expected to hold, in practice, one row per provisioned index."""
        ...
```

```python
# vestibule/provisioning/adapters/base.py

class IndexProvisionerAdapter(ABC):
    """Thin store-facing port (house rules: 'adapters thin'). Two implementations:
    AzureAISearchProvisioner, InMemoryProvisioner."""

    @abstractmethod
    def index_exists(self, index_name: str) -> bool: ...

    @abstractmethod
    def create_index(
        self, index_name: str, template: IndexTemplate, dimensions: int, metric: str
    ) -> None:
        """Idempotent create-or-update (Assumption A13): creates `index_name` if
        absent; if it already exists with a definition matching
        `template`/`dimensions`/`metric`, converges to the same end state as a
        no-op-equivalent success — never raises, never surfaces a 409/"already
        exists"-style conflict purely because the index was already there. This is a
        contractual requirement on every implementation, not an optional nicety:
        `IndexProvisioner`'s own orchestration calls `index_exists()` first purely to
        skip a redundant round trip when cheap to do so (§3.2 step 5), but
        correctness never depends on that check running, or on this method being
        called at most once for a given `index_name`. Two legitimate callers — e.g. a
        still-running original claim holder and a worker that has since reclaimed its
        now-stale claim, §3.4's Race C — may each independently call this method for
        the same `index_name` with the same resolved template/dimensions/metric, and
        both calls must converge safely, whether sequential or genuinely overlapping
        in wall-clock time.
        """
        ...

    @abstractmethod
    def describe_index(self, index_name: str) -> IndexDescription | None: ...

    @abstractmethod
    def delete_index(self, index_name: str) -> None:
        """Never called by IndexProvisioner itself (Out of scope: used only by a
        future mark_retired admin flow)."""
        ...
```

```python
# vestibule/provisioning/adapters/azure_ai_search.py
"""AzureAISearchProvisioner — thin IndexProvisionerAdapter wrapping Azure AI Search
(REQ-011). Mirrors AzureAISearchIndexer's shape exactly (REQ-008): duck-typed
_ProvisionerBackend seam, lazy azure-search-documents import, tenacity retry +
_call_with_timeout, Retry-After-aware backoff."""


class AzureAISearchProvisioner(IndexProvisionerAdapter):
    def __init__(
        self,
        endpoint: str,
        api_key: str,
        *,
        timeout_seconds: float = 30.0,
        max_attempts: int = 5,
        backoff_base_seconds: float = 2.0,
        backoff_max_seconds: float = 60.0,
        _sleep: Callable[[float], None] = time.sleep,
        _backend: "_ProvisionerBackend | None" = None,
    ) -> None:
        """Raises ProvisioningError(INDEX_PROVISIONER_DEPENDENCY_MISSING) (PERMANENT)
        if azure-search-documents is not installed and _backend was not injected."""
        ...

    def index_exists(self, index_name: str) -> bool: ...

    def create_index(
        self, index_name: str, template: IndexTemplate, dimensions: int, metric: str
    ) -> None:
        """Translates `template.fields`/`.hnsw`/`.semantic_ranker_enabled` into a real
        SearchIndex (mirrors _azure_search_backend._build_fields/_build_vector_search,
        generalized from a fixed 8-field list to template.fields; adds a SemanticSearch
        config iff semantic_ranker_enabled). hybrid_enabled has no schema effect
        (Assumption, IndexTemplate docstring). Satisfies Assumption A13's idempotent
        create-or-update contract by construction, not by any new logic this REQ adds:
        the underlying backend call is `_ProvisionerBackend.create_or_update_index`,
        which — exactly mirroring `_azure_search_backend._RealSearchBackend
        .create_or_update_index` (REQ-008, read directly) — dispatches to the real
        SDK's `SearchIndexClient.create_or_update_index`, an inherently idempotent
        create-or-update primitive, never a create-only one. Calling this method twice
        with an identical `template`/`dimensions`/`metric` for the same `index_name`
        is safe and converges to the same index definition either way; no separate
        create-vs-update branching is implemented here."""
        ...

    def describe_index(self, index_name: str) -> IndexDescription | None: ...
    def delete_index(self, index_name: str) -> None: ...

    # _with_retry/_call_with_timeout/_RetryAfterAwareWait — identical in shape to
    # AzureAISearchIndexer's own (azure_ai_search.py); HTTP 429 -> retry,
    # 5xx -> retry, exhausted -> ProvisioningError(INDEX_PROVISIONER_UNAVAILABLE)
    # (TRANSIENT); any other exception propagates for IndexProvisioner to wrap per
    # Assumption A7.
```

```python
# vestibule/provisioning/adapters/in_memory.py
"""InMemoryProvisioner — local, credentials-free IndexProvisionerAdapter (REQ-011).
Story: 'backed by the same dict the InMemoryIndexer uses, so the local end-to-end flow
provisions and then writes.' Concretely: wraps a live InMemoryIndexer instance and
delegates to its own already-idempotent ensure_schema() (REQ-008) — no duplicated
schema-state logic (house rules: DRY, adapters thin)."""


class InMemoryProvisioner(IndexProvisionerAdapter):
    def __init__(self, indexer: "InMemoryIndexer") -> None:
        """`indexer` is the same InMemoryIndexer instance the local composition root
        wires as the write-path IndexerAdapter — this is what makes 'provision, then
        write' observable in one process without any adapter-to-adapter coupling
        beyond this constructor injection."""
        ...

    def index_exists(self, index_name: str) -> bool:
        """See InMemoryIndexer's new `schema` read-only property (Assumption A15 —
        small, additive, non-breaking cross-module edit to
        vestibule/indexer/adapters/in_memory.py: exposes the existing private
        `_schema` attribute read-only, replacing no existing behavior) —
        `schema is not None`."""
        ...

    def create_index(
        self, index_name: str, template: IndexTemplate, dimensions: int, metric: str
    ) -> None:
        """Delegates to indexer.ensure_schema(dimensions, metric) (REQ-008) — reuses
        its existing set-if-none/conflict-if-mismatched idempotency rather than
        reimplementing it. Catches IndexerError(INDEXER_SCHEMA_CONFLICT) and re-raises
        ProvisioningError(INDEX_SCHEMA_DRIFT) — the two modules' distinct vocabularies
        for the identical underlying condition. Already satisfies Assumption A13's
        idempotent create-or-update contract: two legitimate claim holders for the
        same `index_name` always resolve identical `dimensions`/`metric` (§3.3 step 2
        — a deterministic function of `scenario` + the loaded template), so a second
        call always lands on `ensure_schema`'s existing 'already compatible —
        idempotent no-op' branch (REQ-008 AC8), never its `INDEXER_SCHEMA_CONFLICT`
        branch, which is reserved for a genuine, independently-caused mismatch."""
        ...

    def describe_index(self, index_name: str) -> IndexDescription | None: ...
    def delete_index(self, index_name: str) -> None:
        """Best-effort test utility only — resets the wrapped indexer's schema to
        None. Never called by IndexProvisioner itself (Out of scope)."""
        ...
```

```python
# vestibule/provisioning/provisioner.py
"""IndexProvisioner — the single public entry point (REQ-011). Orchestrates template
resolution, the atomic-claim/reclaim state machine (§3.3/§3.4), drift verification,
and index creation. Owns no I/O algorithm itself — every I/O call is delegated to
IndexRegistry/IndexProvisionerAdapter (house rules: 'adapters thin', 'never implement
... in-house')."""


@dataclass(frozen=True)
class ProvisioningConfig:
    """Documentation-authoritative-but-code-defaults tunables (REQ-006 §6 Assumption
    A6 precedent — no config-loading mechanism for *tunables* exists in this codebase
    yet; config/index_provisioning.yaml is kept in sync via a dedicated test, §6)."""
    default_template_id: str = "standard-v1"
    provisioning_stale_after_seconds: float = 300.0
    verification_interval_seconds: float = 300.0
    wait_for_provisioning_timeout_seconds: float = 60.0
    wait_poll_interval_seconds: float = 2.0  # Assumption A8
    auto_create: bool = True


class IndexProvisioner:
    def __init__(
        self,
        registry: IndexRegistry,
        adapter: IndexProvisionerAdapter,
        templates: IndexTemplateStore,
        config: ProvisioningConfig,
        *,
        _clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        _sleep: Callable[[float], None] = time.sleep,
        _new_claim_token: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        """`registry` may be a bare `TableStorageIndexRegistry`/`InMemoryIndexRegistry`
        or a `CachedIndexRegistry`-wrapped one (Assumption A16, composition-root
        decision, §6's `registry_cache.enabled`) — this constructor's signature is
        unchanged either way, since `CachedIndexRegistry` implements the same
        `IndexRegistry` port. _clock/_sleep/_new_claim_token are test-injection
        seams — the exact pattern TableStorageScenarioStore's `_sleep` establishes,
        extended here so §7's deterministic concurrency tests can control both time
        and identity."""
        ...

    def ensure(self, scenario: Scenario) -> IndexRegistryEntry:
        """The single public entry point. See §3 for the full numbered sequence.
        Reads scenario.index_name/.vertical/.scenario_id/.index_template_id (Assumption
        A1)/.embedder.model/.indexer.dimensions (Assumption A2) — no ArrivalEnvelope
        field (this module's Contract #1 touchpoint: 'not read directly').
        """
        ...

    # _resolve_template(scenario) -> tuple[IndexTemplate, int, str]  (template,
    #   resolved dimensions, resolved metric) — §3.1; raises INDEX_TEMPLATE_NOT_FOUND/
    #   INDEX_TEMPLATE_INVALID.
    # _check_compatibility(entry_or_None, description, resolved_template, resolved_dims,
    #   resolved_metric) -> None — raises INDEX_SCHEMA_DRIFT; §3.1/§3.2.
    # _claim_and_create(scenario, won_entry) -> IndexRegistryEntry — the shared sub-flow used
    #   by both the original winner (§3.3) and a successful reclaimer (§3.4) — §3.2 step 5.
    #   Reads the template/dimensions/metric it creates with directly off
    #   won_entry.resolved_template/.dimensions/.metric (Assumption A14) — no separately
    #   threaded template parameter.
    # _wait_for_ready(index_name, entry, scenario, resolved_*) -> IndexRegistryEntry —
    #   the bounded poll/reclaim loop — §3.4.
```

## 2. Data model

| Type | Field | Type | Notes |
|---|---|---|---|
| `FieldSpec` (frozen pydantic) | `name` | `str` | |
| | `type` | `FieldType` | `key\|text\|vector\|filterable_string\|filterable_string_collection\|filterable_datetime\|retrievable_only` |
| | `searchable` | `bool` | default `False` |
| `HnswSettings` (frozen pydantic) | `m` | `int` | |
| | `ef_construction` | `int` | |
| | `ef_search` | `int` | |
| `IndexTemplate` (frozen pydantic) | `template_id` | `str` | e.g. `"standard-v1"` |
| | `template_version` | `str` | non-negative-integer string (Assumption A6) |
| | `dimensions` | `int \| None` | `None` = inherit `scenario.indexer.dimensions` (A2) |
| | `metric` | `Literal["cosine","dotProduct","euclidean"]` | |
| | `hnsw` | `HnswSettings` | sole HNSW source at creation time (A11) |
| | `fields` | `list[FieldSpec]` | validated: exactly one `key`, >=1 `vector`, unique names |
| | `semantic_ranker_enabled` | `bool` | |
| | `hybrid_enabled` | `bool` | no schema effect (docstring) |
| `IndexRegistryEntry` (frozen pydantic) | `index_name` | `str` | key |
| | `vertical` | `str` | |
| | `scenario_id` | `str` | |
| | `template_id` | `str` | |
| | `template_version` | `str` | |
| | `dimensions` | `int` | resolved, not raw template value |
| | `metric` | `str` | |
| | `embedding_model` | `str` | |
| | `status` | `Literal["provisioning","ready","failed","retired"]` | |
| | `created_at` | `datetime` | set once, preserved across reclaim |
| | `last_verified_at` | `datetime \| None` | |
| | `document_count` | `int \| None` | always `None` in this REQ (out of scope) |
| | `claim_token` | `str \| None` | Assumption A3 — non-`None` iff `status=="provisioning"` |
| | `claimed_at` | `datetime \| None` | Assumption A3 — drives staleness |
| | `last_error_message` | `str \| None` | Assumption A3 — `mark_failed`'s reason |
| | `resolved_template` | `IndexTemplate \| None` | Assumption A14 — exact `IndexTemplate` resolved at claim time; non-`None` iff `status=="provisioning"`; a reclaiming worker copies this verbatim instead of re-resolving from `IndexTemplateStore` |
| `IndexDescription` (frozen pydantic) | `dimensions` | `int` | live-store fact |
| | `metric` | `str` | live-store fact |
| `ProvisioningError` | `index_name` | `str` | |
| | `reason` | `str` | |
| | `error_code` | `str` | inherited from `RaggedError`; one of 11 codes |
| `ProvisioningConfig` (frozen dataclass) | `default_template_id` | `str` | default `"standard-v1"` |
| | `provisioning_stale_after_seconds` | `float` | default `300.0` |
| | `verification_interval_seconds` | `float` | default `300.0` |
| | `wait_for_provisioning_timeout_seconds` | `float` | default `60.0` |
| | `wait_poll_interval_seconds` | `float` | default `2.0` (Assumption A8) |
| | `auto_create` | `bool` | default `True` |

Cross-module additive field (Assumption A1): `vestibule/scenario/model.py`'s `Scenario` gains
`index_template_id: str | None = None`.

Cross-module additive property (Assumption A15): `vestibule/indexer/adapters/in_memory.py`'s
`InMemoryIndexer` gains a read-only `schema: tuple[int, str] | None` property over its existing
private `_schema` attribute — no new field, no serialized/persisted state.

No `LedgerRow` is owned or written by this module — Contract #3 touchpoint (story): "The registry
is a separate store from the ledger by design — indexes outlive documents." `IndexRegistryEntry`
is this module's own, entirely separate lifecycle table.

`CachedIndexRegistry` (Assumption A16) introduces no new persisted field or table — its cache
(`dict[str, tuple[IndexRegistryEntry | None, float]]` for `get`, `dict[str, tuple[list[IndexRegistryEntry], float]]`
for `list_by_vertical`) is process-local, in-memory only, and never itself durable state; every
persisted `IndexRegistryEntry` field remains exactly as listed above regardless of whether a
`CachedIndexRegistry` sits in front of the backend that stores it.

## 3. Sequence

### 3.1 Fast path — registry says ready, verification cached

1. `IndexProvisioner.ensure(scenario)` calls `registry.get(scenario.index_name)` — in production,
   `registry` is a `CachedIndexRegistry`-wrapped `TableStorageIndexRegistry` (Assumption A16), so
   this call is a cache hit on every document except once per `registry_cache.ttl_seconds` window
   per `index_name` (§6).
2. `entry.status == "ready"` and `_clock() - entry.last_verified_at < config.verification_interval_seconds`
   → return `entry` immediately. **No template resolution, no `describe_index` call.** See §8 for
   the realistic, cache-state- and backend-dependent latency this step now costs — a single
   blanket `< 5ms` figure is no longer accurate once a live, uncached registry read is possible on
   this path (Assumption A16, design-review MAJOR resolution, round 4).

### 3.2 Cold path — registry says ready, verification interval elapsed (or entry absent, single worker)

**Verification-elapsed sub-case** (`entry.status == "ready"`, interval elapsed):

1. `_resolve_template(scenario)`: `template_id = scenario.index_template_id or config.default_template_id`;
   `template = templates.get_or_raise(template_id)` (raises `INDEX_TEMPLATE_NOT_FOUND` if absent);
   `resolved_dimensions = template.dimensions if template.dimensions is not None else scenario.indexer.dimensions`
   (Assumption A2); if `template.dimensions is not None and template.dimensions != scenario.indexer.dimensions`
   → raise `INDEX_TEMPLATE_INVALID` (Assumption A2's "second boundary" check) before any further
   work; `resolved_metric = template.metric`.
2. `description = adapter.describe_index(scenario.index_name)`. `description is None` (index
   vanished out-of-band) is treated identically to a dimensions/metric mismatch, below.
3. Compatibility check: `description is None or description.dimensions != resolved_dimensions
   or description.metric != resolved_metric or int(entry.template_version) < int(template.template_version)`
   → raise `INDEX_SCHEMA_DRIFT` (PERMANENT). Entry status is **not** changed on drift — this is a
   live-index/config mismatch, not a provisioning-claim failure (§4, Failure Taxonomy touchpoint);
   it fails loudly on every subsequent `ensure()` call until a human resolves it (edits the
   scenario, bumps the template, or migrates the index — none of which this module does).
4. Compatible → `registry.touch_verified(scenario.index_name)`; return the refreshed entry.

**No-entry sub-case** (`entry is None`) — the single-worker happy path; §3.3 covers the
two-or-more-worker race over the identical starting condition:

1. `config.auto_create` is `False` → raise `INDEX_AUTO_CREATE_DISABLED` (PERMANENT) immediately —
   before any resolution or claim attempt (AC7).
2. `_resolve_template(scenario)` — as above (steps 1–3 there apply verbatim; can raise
   `INDEX_TEMPLATE_NOT_FOUND`/`INDEX_TEMPLATE_INVALID`).
3. Build `candidate = IndexRegistryEntry(index_name=scenario.index_name, vertical=scenario.vertical,
   scenario_id=scenario.scenario_id, template_id=template.template_id,
   template_version=template.template_version, dimensions=resolved_dimensions, metric=resolved_metric,
   embedding_model=scenario.embedder.model, status="provisioning", created_at=_clock(),
   last_verified_at=None, document_count=None, claim_token=_new_claim_token(),
   claimed_at=_clock(), last_error_message=None, resolved_template=template)` — `resolved_template`
   stamps this exact `IndexTemplate` object into the claim (Assumption A14), so any later reclaimer
   never needs to re-resolve it independently.
4. `won_entry = registry.register(candidate)`.
5. `won_entry.claim_token == candidate.claim_token` → this worker won; call
   `_claim_and_create(scenario, won_entry)` (below — reads `won_entry.resolved_template`/
   `.dimensions`/`.metric` directly, Assumption A14). Otherwise, another worker already holds (or
   has finished) this claim — dispatch on `won_entry.status` using the same branch logic as steps
   4/5/6 of the story (ready → return; provisioning → `_wait_for_ready`, §3.4; failed →
   `INDEX_PROVISION_FAILED`; retired → `INDEX_RETIRED`). See §3.3 for the full two-worker
   walkthrough of this exact branch.

**`_claim_and_create(scenario, won_entry)` — the shared "I hold the claim, make it real"
sub-flow** (used by the original winner here, and by a successful reclaimer in §3.4). Reads the
template/dimensions/metric it creates the index with directly off
`won_entry.resolved_template`/`.dimensions`/`.metric` — never a separately threaded parameter
(Assumption A14) — so both callers below share one unambiguous source of truth. More than one
legitimate claim holder for the same `index_name` can reach this sub-flow across a single claim's
lifetime once staleness and reclamation are in play (§3.4's Race C); safety in that case rests on
two things together: `create_index`'s idempotent create-or-update contract (Assumption A13), and
both calls sharing byte-identical `resolved_template`/`dimensions`/`metric` input (Assumption
A14) — never on an assumption that this sub-flow is entered by only one caller total:

5. `already_exists = adapter.index_exists(index_name)`.
6. `already_exists` is `False` → `adapter.create_index(index_name, won_entry.resolved_template,
   won_entry.dimensions, won_entry.metric)`. Any exception here → **F5** (below): `registry.mark_failed(index_name,
   str(exc), expected_claim_token=won_entry.claim_token)`, then re-raise the *original* mapped
   error (`INDEX_PROVISIONER_UNAVAILABLE`/`INDEX_PROVISIONER_DEPENDENCY_MISSING`/Assumption A7's
   fallback) — **not** `INDEX_PROVISION_FAILED` (that code is reserved for a *subsequent* caller
   observing the now-`failed` entry, exactly AC5's wording: "the entry is marked failed and **a
   subsequent** `ensure()` raises `INDEX_PROVISION_FAILED`").
7. `description = adapter.describe_index(index_name)` (always re-fetched, whether just-created or
   found pre-existing — this is the story's "already exists... treated as success" rule, and it is
   the **same** rule regardless of *who* created it: another registry-claiming worker cannot have
   reached this point concurrently (the registry claim was exclusive, §3.3), so `already_exists ==
   True` here can only mean an out-of-band actor — a third process never touching this registry at
   all — created it first; this module has no way to distinguish that from any other
   "already exists" cause, and does not need to: `index_exists()`/`describe_index()` are agnostic
   to the creator's identity, so no separate code path exists for the out-of-band case). Compare
   `description` against `(won_entry.dimensions, won_entry.metric)` only (no `template_version` check
   here — a brand-new claim has no prior registry `template_version` to compare against, and an
   out-of-band index carries none of this module's own metadata to compare either).
8. Incompatible (or `description is None`) → **F6**: `registry.mark_failed(index_name, reason,
   expected_claim_token=won_entry.claim_token)`, then raise `INDEX_SCHEMA_DRIFT` directly to this
   call's own caller (same "original call gets the specific code" rule as F5) — this is what
   prevents a drift discovered mid-claim from leaving the registry entry stuck `provisioning`
   until `provisioning_stale_after_seconds` elapses and re-discovering the identical drift on
   every subsequent reclaim (a non-healing loop); marking it `failed` immediately makes it visible
   and stops the loop.
9. Compatible → `registry.mark_ready(index_name, expected_claim_token=won_entry.claim_token)`.
   `mark_ready`'s own CAS can itself raise `INDEX_PROVISION_CONFLICT` — see §3.4 step 4b for why,
   and for how this call site absorbs it (never propagates it to `ensure()`'s own caller): catch
   it, re-`registry.get(index_name)`, and return whatever is now stored via the same
   ready/failed/provisioning/retired dispatch used everywhere else in this module — a reclaimer
   already finished this exact index_name first, and this worker's own (now-redundant, but
   harmless per step 5–8's idempotent `index_exists`/`describe_index` calls) work is simply
   discarded in favor of the authoritative current state.
10. Return the `ready` entry.

### 3.3 Concurrency — two workers race for one brand-new index (the core requirement)

Preconditions: two workers, W1 and W2, each processing a document for the same brand-new
`scenario.index_name`; neither has ever seen a registry entry for it.

1. W1 calls `registry.get(index_name)` → `None`. W2 calls `registry.get(index_name)` → `None`.
   Both proceed to §3.2's no-entry sub-case.
2. Both independently resolve the identical template (deterministic function of `scenario` +
   `config/index_templates/*.yaml`, no I/O-dependent branching) — `resolved_dimensions`/
   `resolved_metric`/`template_id`/`template_version` are identical between W1 and W2. Only
   `claim_token` (a fresh `uuid4().hex` per call) and `created_at`/`claimed_at` (wall-clock, may
   differ by microseconds) differ between W1's and W2's `candidate` entries.
3. W1 calls `registry.register(candidate_W1)`. W2 calls `registry.register(candidate_W2)`. **These
   two calls may be truly concurrent** (different processes/threads); exactly one of them performs
   the insert.

   - **`InMemoryIndexRegistry.register`** — what "atomic" means here concretely: the entire
     "is `index_name` absent? if so, insert" sequence executes inside one `with self._lock:` block
     — a single, uninterruptible critical section. This is **not** "safe because of the GIL":
     the GIL only guarantees a single Python *bytecode operation* is atomic (e.g. one `dict.get`
     call); it does **not** make a *check-then-write* sequence spanning two statements atomic —
     without the lock, W1 could check-and-find-absent, then be preempted before its own write,
     let W2 also check-and-find-absent, and both would insert, each believing it won. The
     `threading.RLock`, held for the full method call (mirroring `InMemoryLedgerStore.transition`'s
     established `with self._lock: current = ...; validate(...); self._rows[...] = ...` shape,
     REQ-003), is what makes the check-then-write one indivisible unit: whichever of W1/W2 acquires
     the lock first executes its entire check-and-insert before the other's call can even begin
     its own check. The loser's check therefore always observes the winner's already-inserted
     entry — there is no interleaving under which both observe absence.
   - **`TableStorageIndexRegistry.register`** — what "atomic" means here concretely: calls
     `backend.upsert_entity(vertical, index_name, data, etag=None)`, which (per
     `_azure_table_backend.py`'s already-established pattern, REQ-010, reused verbatim per
     Assumption A9) dispatches to the real SDK's `table_client.create_entity(entity=entity)` — an
     unconditional-create call that Azure Table Storage itself guarantees fails with HTTP 409
     (`ResourceExistsError`) if an entity with that exact `(PartitionKey, RowKey)` already exists,
     server-side, across every process/region talking to that table. This is the genuine
     cross-process atomicity primitive (Assumption, §4 Identity & Idempotency touchpoint) — no
     client-side lock could ever provide this guarantee across two separate OS processes; only the
     storage service's own transactional insert can. `TableStorageIndexRegistry.register` catches
     this specific 409 (not the generic `_is_retryable`/`_map_status` machinery used for genuine
     429/5xx conflicts elsewhere) and falls back to `backend.get_entity(vertical, index_name)`,
     returning the now-existing entity — this is what makes `register()`'s contract "never raises
     purely due to a create-race" true on this backend too.
4. Say W1 wins: `registry.register` returns `candidate_W1` itself (with `candidate_W1.claim_token`
   intact) to W1, and returns the **stored** `candidate_W1` (not `candidate_W2`) to W2, since
   `candidate_W2`'s insert never happened.
5. W1 checks `won_entry.claim_token == candidate_W1.claim_token` → `True` → W1 proceeds into
   `_claim_and_create` (§3.2 steps 5–10): calls `adapter.index_exists(index_name)` (→ `False`,
   nothing has created it yet), calls `adapter.create_index(...)` — **exactly one `create_index`
   invocation total in this two-worker race**, by construction: only the registry's atomic-insert
   winner ever reaches this call, and there is exactly one winner. (This "exactly one" guarantee is
   scoped to a single, non-stale claim episode — see step 7 below and §3.4's Race C for what
   changes once reclamation is in play.) W1 marks the entry `ready` and returns it.
6. W2 checks `won_entry.claim_token == candidate_W2.claim_token` → `False` (it is `candidate_W1`'s
   token) → W2 does **not** error, does **not** call `create_index` — it dispatches on
   `won_entry.status`. If W2's check happens before W1 finishes (`won_entry.status == "provisioning"`),
   W2 enters `_wait_for_ready` (§3.4) using `won_entry` as its starting point; if W2's check happens
   after W1 already finished (`won_entry.status == "ready"` — W2's own `registry.get`-equivalent
   read via `register`'s return value already reflects this if W1's `mark_ready` landed first),
   W2 returns immediately with W1's entry. **Both W1 and W2 return an equivalent `ready`
   `IndexRegistryEntry` for the same `index_name`** (AC3's "both callers receive an equivalent
   ready entry") — not byte-identical (W2's may have a later `last_verified_at` if it arrived via
   the wait loop's own re-`get`), but equal on every field that matters for correctness
   (`dimensions`/`metric`/`template_id`/`template_version`/`status`).
7. **Third, out-of-band actor**: if some other process — one that never calls `IndexRegistry` at
   all (e.g. a human using the Azure portal, or an unrelated tool) — creates the index in the
   store *before either W1 or W2 reaches step 5's `index_exists` check, this is handled by the
   **same** rule as step 5, not a distinct case: whichever of W1/W2 wins the registry claim still
   calls `adapter.index_exists(index_name)` as its very first act inside `_claim_and_create`,
   finds `True`, skips `create_index` entirely, and proceeds straight to the `describe_index`
   compatibility check (§3.2 step 7) — indistinguishable, by design, from "another registry-
   claiming worker already created it" (which, per step 5's reasoning, can never actually happen
   *concurrently* within this same two-worker, non-stale race) or "I myself created it a moment
   ago." The registry claim's exclusivity guarantees that, *within a single, non-stale claim
   episode* (no reclaim has occurred, exactly this subsection's scope), *at most one*
   `create_index` call originates from `IndexProvisioner` itself — that is what step 5 above
   establishes. It does **not**, on its own, guarantee at most one call across a claim's *entire*
   lifetime once §3.4's stale-claim reclamation enters the picture: a legitimately-still-running
   original claim holder and a worker that has since reclaimed its now-stale claim can each
   independently reach `_claim_and_create` for the same `index_name` (§3.4's Race C, added per
   design review) — safety there comes from Assumption A13's idempotent create-or-update contract
   plus Assumption A14's guarantee that both calls resolve the same `IndexTemplate` object, not
   from call-count exclusivity. Separately, and unconditionally regardless of reclaim: this
   step's rule says nothing about, and does not need to say anything about, whether some entirely
   separate system created the index by other means — `index_exists()` is a uniform guard against
   *redundant* work, not the source of correctness (Assumptions A13/A14 are).

### 3.4 Concurrency — stale-claim reclamation (the two nested races)

Precondition: W1 won the claim (§3.3 step 4/5) and either crashed or is merely slow; W3 (a third
worker, or W2 continuing its own `_wait_for_ready` loop from §3.3 step 6) observes
`entry.status == "provisioning"` with `_clock() - entry.claimed_at >= config.provisioning_stale_after_seconds`.

1. W3 builds `new_candidate` — a fresh `IndexRegistryEntry` with a new `claim_token`, `claimed_at
   = _clock()`, `created_at` **preserved from `entry.created_at`** (this `index_name`'s
   provisioning story began when the *original* claim was first inserted, not when it was
   reclaimed), and every resolved field — `template_id`/`template_version`/`dimensions`/`metric`/
   `resolved_template`/`embedding_model`/`vertical`/`scenario_id` — copied **verbatim from
   `entry`**, never independently re-resolved (Assumption A14, design-review MAJOR resolution,
   round 2). W3 does **not** call `_resolve_template` here: `IndexTemplateStore` eager-loads its
   template files once at construction and never refreshes (§1), so if a template file was
   edited/redeployed at any point since the original claim was made, W3's own, independently-
   constructed `IndexTemplateStore` could otherwise resolve genuinely different content for the
   same `template_id` than the original claim holder did — copying `entry.resolved_template`
   verbatim instead closes that gap outright, guaranteeing byte-identical `create_index` input
   regardless of what happened to the template files in the meantime. A reclaiming worker fully
   takes over the original claim owner's role, including the create-index step that follows, using
   exactly the schema the original claim was made against.
2. W3 calls `registry.reclaim(index_name, observed=entry, new_entry=new_candidate)`.

   - **`InMemoryIndexRegistry.reclaim`** — "atomic" means: inside one `with self._lock:` block,
     compare the *currently stored* entry for `index_name` to `observed` by full field equality
     (frozen pydantic `BaseModel.__eq__`, comparing every field including `claim_token` —
     equivalent to an ETag comparison, since every mutation on this backend also changes
     `claim_token`/`claimed_at`/`status`/`last_error_message` at least one of which will differ if
     *anything* changed); if equal, replace the stored entry with `new_entry` and return it — all
     within the same lock acquisition, so there is no window in which a second thread's
     `mark_ready`/`reclaim`/`register` call could interleave between the compare and the write.
     If unequal, raise `INDEX_PROVISION_CONFLICT` without writing anything.
   - **`TableStorageIndexRegistry.reclaim`** — "atomic" means: a fresh
     `backend.get_entity(vertical, index_name)` read obtains the entity's *current* real ETag;
     this LLD does **not** rely on a client-side "is it still equal to `observed`" pre-check as
     the source of correctness (that check, if present at all, is a cheap early-exit only — a
     genuine TOCTOU window exists between any client-side read and a client-side write, exactly
     as it does for `TableStorageScenarioStore.upsert`); correctness comes entirely from issuing
     `backend.upsert_entity(vertical, index_name, new_data, etag=<that just-read ETag>)`, which
     dispatches to `table_client.update_entity(..., match_condition=IfNotModified)` — Azure Table
     Storage itself guarantees this write only lands if the entity's ETag, server-side, at the
     instant of the write, still equals the ETag just read. Anything that changed the entity in
     between (a competing reclaim, or the original owner's own `mark_ready`/`mark_failed`) changes
     its ETag, and the conditional write fails with 409/412, mapped to `INDEX_PROVISION_CONFLICT` —
     never silently retried (an ETag conflict can never resolve by retrying the identical write).
3. **Race A — a fourth worker (or W2) reclaims first.** W3's `reclaim` call's compare (in-memory)
   or conditional write (Table Storage) observes a *different* current entry than `observed` (a
   different `claim_token`/ETag, because the other reclaimer's write already landed) →
   `INDEX_PROVISION_CONFLICT`. W3 does not treat this as an error to surface: it re-`registry.get`s,
   and re-enters `_wait_for_ready`'s loop from the top using the freshly observed entry (which is
   now some other worker's fresh `provisioning` claim, not yet stale) — exactly the same as losing
   the original claim race in §3.3 step 6.
4. **Race B — W1 was not actually dead, and finishes normally right as W3 reclaims.** Two
   sub-orderings:
   - **4a. W1's `mark_ready`/`mark_failed` call lands *before* W3's `reclaim` write.** Whether
     in-memory (both under the same lock, strictly ordered) or Table Storage (whichever
     conditional write's ETag-read happens to observe the other's already-completed write), W3's
     `reclaim` now sees a stored entry unequal to `observed` (`status` is no longer
     `"provisioning"`, or its `claim_token`/ETag has changed) → `INDEX_PROVISION_CONFLICT`, exactly
     Race A's handling — W3 loops back, observes W1's `ready` (or `failed`) result, and returns/
     raises accordingly. **The reclaim never clobbers the legitimately-just-finished provision** —
     this is the precise guarantee the task calls out, and it falls directly out of `reclaim`'s
     CAS semantics: a reclaim can only ever succeed against the *exact* stale snapshot it observed,
     never against anything that has since changed, regardless of *why* it changed.
   - **4b. W3's `reclaim` write lands first, and *then* W1 (unaware it has been reclaimed) calls
     its own `mark_ready`/`mark_failed`.** This is exactly why `mark_ready`/`mark_failed` are
     themselves CAS-protected via `expected_claim_token` (Assumption A5), not plain "flip the
     status" writes: W1 calls `registry.mark_ready(index_name, expected_claim_token=<W1's own,
     now-superseded claim_token>)`. Both backends reject this the same way `reclaim` rejects a
     stale `observed`: in-memory, under the lock, the stored entry's `claim_token` no longer
     equals W1's — reject with `INDEX_PROVISION_CONFLICT` before writing anything; Table Storage,
     the conditional `update_entity` call's ETag precondition fails for the identical underlying
     reason (W3's reclaim write already changed the entity, hence its ETag) — reject with
     `INDEX_PROVISION_CONFLICT`. W1's own `_claim_and_create` call site (§3.2 step 9's own
     documented catch) absorbs this exactly as described there: it does not propagate the
     conflict to whatever caller originally invoked W1's `ensure()` — it re-`get`s and returns
     the (by now, W3's) authoritative current state instead. **W1's own now-redundant
     `create_index`/`describe_index` calls, made before this rejection, are harmless** — no state
     was ever written to the registry based on them (only `mark_ready`/`mark_failed` write to the
     registry, and those are exactly the calls being rejected here); at worst, W1 performed a
     wasted (but idempotent-per-adapter-contract, Assumption A13) `create_index`/`describe_index`
     round-trip against the underlying store.
5. Whichever worker's `reclaim` call succeeds becomes the new claim owner and proceeds into
   `_claim_and_create` (§3.2 steps 5–10) exactly as the original winner would have.
6. `_wait_for_ready`'s overall loop (bounding every worker that is *waiting*, not reclaiming) is
   bounded by `config.wait_for_provisioning_timeout_seconds`; on each iteration that does not
   observe staleness (or does observe it but loses the reclaim race), it sleeps
   `config.wait_poll_interval_seconds` (Assumption A8) and re-`registry.get`s. Exceeding the
   overall deadline → `INDEX_PROVISION_TIMEOUT` (TRANSIENT) — the caller's own retry/redelivery
   (Contract #2) will observe a fresh registry state on its next attempt, very likely `ready` by
   then. This re-`get()` call is the *same* `IndexRegistry.get` method §3.1's fast path caches —
   if `registry` is `CachedIndexRegistry`-wrapped (Assumption A16), a TTL too close to
   `wait_for_provisioning_timeout_seconds` would risk this loop never observing a genuine
   transition the live store already reflects; §6's chosen `registry_cache.ttl_seconds` default
   (`10`, well below both `wait_poll_interval_seconds`'s cadence and this loop's own `60`-second
   bound) keeps that risk negligible — see Assumption A16.
7. **Race C — W1 is not dead, merely slow inside its own `create_index` retry envelope, and both
   W1 and the winning reclaimer independently call `create_index` (the design-review BLOCKER on
   this section, resolved by Assumption A13).** `AzureAISearchProvisioner`'s own retry envelope
   (§1: up to 5 attempts, `backoff_base_seconds=2.0` doubling to `backoff_max_seconds=60.0`,
   `timeout_seconds=30.0` per attempt — worst case on the order of 5×30s + 4×60s ≈ 390s under
   sustained 429s/5xx) can legitimately exceed `provisioning_stale_after_seconds`'s default 300s
   while W1 has made no registry write since its original claim (registry writes only happen at
   claim time and at `mark_ready`/`mark_failed`, never mid-`create_index`, §1). A waiting worker
   (W3) observes `claimed_at` older than the staleness threshold, reclaims successfully via step
   2's CAS (the stored entry still equals what W3 observed — W1 has genuinely not touched the
   registry), and proceeds into its own `_claim_and_create` (§3.2 steps 5–10), which may call
   `adapter.create_index(...)` a second time while W1's own original call is still in flight. This
   is **not** prevented by anything in steps 1–6 above — W1 is a legitimate claim holder for the
   entire duration, not an out-of-band third party, so this is a case §3.3 step 7's exclusivity
   reasoning does not cover. Safety here comes from two things together: Assumption A13's
   idempotent create-or-update contract, and Assumption A14's guarantee that both calls resolve
   the identical `resolved_template` object (not merely matching `dimensions`/`metric` scalars) —
   W3 copies `entry.resolved_template` verbatim rather than independently re-resolving it (step 1
   above; this is the design-review MAJOR resolution, round 2 — without it, W3's own,
   independently-constructed `IndexTemplateStore` could have loaded different template-file
   content than the original claim holder did, if the file was edited/redeployed somewhere in this
   race's own multi-hundred-second window). Both calls resolve identical
   `template`/`resolved_dimensions`/`resolved_metric` by construction, so both `create_index`
   calls — whichever lands first, whichever lands last, even genuinely concurrently — converge to
   the same target index definition; neither raises, and no interleaving of the two calls can
   leave the store in an inconsistent state. Whichever of W1's or W3's subsequent `mark_ready` call
   wins the CAS race (steps 4a/4b above, unchanged by this addition) is the entry that becomes
   authoritative; the loser's own `create_index` call was real store I/O but never wrote any
   registry state and is otherwise inert. This is the scenario §7's
   `test_ensure_slow_but_alive_worker_survives_stale_reclaim_both_create_index_calls_converge`
   exercises directly.

### 3.5 Cross-process idempotency — the process-local/cross-process boundary, made explicit

Per the task's explicit ask, every place this design's correctness depends on something
process-local versus something the backing store genuinely enforces across processes:

- **Genuinely cross-process (`TableStorageIndexRegistry` only):** `register()`'s winner-takes-all
  guarantee (Azure's own atomic `create_entity`, HTTP 409 on conflict, server-side, across every
  process/region) and `reclaim()`/`mark_ready()`/`mark_failed()`'s CAS guarantee (Azure's own
  ETag-conditional `update_entity`, HTTP 409/412 on a changed entity, server-side). These are the
  *only* two primitives in this whole design that provide a real cross-process guarantee — every
  other piece of correctness reasoning in §3.3/§3.4 is built entirely on top of these two.
  `CachedIndexRegistry` (Assumption A16) never intermediates either of them — `register`/`reclaim`
  always pass straight through to these two primitives, uncached.
- **Process-local only (`InMemoryIndexRegistry`/`InMemoryProvisioner`):** the `threading.RLock`
  held for the duration of `register`/`reclaim`/`mark_ready`/`mark_failed` provides mutual
  exclusion **only among threads of one Python process sharing one `InMemoryIndexRegistry`
  instance**. Two separate OS processes, each holding their own `InMemoryIndexRegistry()`, would
  each maintain an independent, unsynchronized dict — both could "win" a claim, both would call
  `create_index` against whatever `InMemoryProvisioner`/`InMemoryIndexer` instance *they* happen
  to hold. This is not a bug to fix; it is this backend's documented, in-story scope ("dev/test
  only"). Its job, restated precisely: correctly *simulate*, deterministically, within one
  process, the exact same claim/conflict/reclaim state-machine outcomes
  `TableStorageIndexRegistry` enforces for real — so that §7's unit tests can exercise genuine
  `threading`-based races (real OS thread scheduling, not a hand-scripted call order) against a
  backend that requires no live Azure credentials, and get the same pass/fail verdicts a
  production race against Table Storage would produce. It proves the *state machine* is correct;
  it does not, and cannot, prove cross-process safety — only `TableStorageIndexRegistry`'s own
  tests (against a recorded-response fake exercising the 409/ETag-conflict paths, no live Azure
  calls in CI, mirroring REQ-010's own precedent) do that.
- **Also process-local, and worth naming explicitly:** `_clock`/`_new_claim_token`'s default
  implementations (`datetime.now`/`uuid4().hex`) are ordinary Python calls with no cross-process
  coordination of their own — their *safety* here comes entirely from the fact that they are only
  ever compared for equality (`claim_token`) or used as a threshold input to a comparison already
  protected by one of the two genuinely cross-process primitives above (`claimed_at` staleness is
  read by whichever worker is polling, but the *decision* to reclaim is only ever finalized by
  `reclaim()`'s own CAS — a wall-clock skew between workers can, at worst, cause a slightly early
  or slightly late reclaim *attempt*, never an incorrect reclaim *outcome*). `CachedIndexRegistry`'s
  own TTL clock (`_now`, defaulting to `time.monotonic`) is likewise process-local and purely an
  implementation detail of how fresh a cached `get()`/`list_by_vertical()` read is within one
  process — it has no bearing on, and is never consulted by, either genuinely cross-process
  primitive above (Assumption A16).

## 4. Contract compliance

- **Arrival Envelope**: not read directly (story's own touchpoint) — `IndexProvisioner.ensure`
  takes a `Scenario` (REQ-010's own resolution of the envelope, REQ-009, upstream of this module).
- **Identity & Idempotency**: `ensure()` is idempotent across repeated calls for the same
  `scenario.index_name` — a `ready` entry's fast path (§3.1) is a pure read with no side effect,
  and the cold/claim paths (§3.2–§3.4) guarantee that at most one `IndexRegistryEntry` for a given
  `index_name` is ever finalized `ready` (the registry's CAS primitives — `register`'s
  atomic-insert and `reclaim`/`mark_ready`/`mark_failed`'s ETag-conditional writes — admit exactly
  one winner per finalization race, §3.3/§3.4), and that any `create_index` call, however many
  legitimate claim holders end up making one across a claim's lifetime under stale-claim
  reclamation (§3.4's Race C), converges safely because `create_index` is contractually an
  idempotent create-or-update (Assumption A13) called with an identical resolved template
  (Assumption A14) rather than relying on call-count exclusivity. `CachedIndexRegistry`
  (Assumption A16) never weakens this: its claim-path methods (`register`/`reclaim`) are never
  served from or written into the cache, so the exact same CAS primitives resolve every race
  regardless of whether a cache sits in front of `get`/`list_by_vertical`.
  This is the story's explicitly-called-out hardest test of this contract to date: unlike every
  prior REQ's idempotency (process-local, single-worker retry semantics), this module's own
  correctness argument is built entirely on two genuinely cross-process primitives (§3.5) —
  `TableStorageIndexRegistry`'s atomic `create_entity` and ETag-conditional `update_entity` —
  composed with two adapter/claim-level idempotency contracts (A13/A14), not on any in-process
  lock, cache, or call-ordering assumption. `doc_id`/`chunk_id` derivation (REQ-002) is untouched
  by this module — it operates one level above document identity, on `index_name` identity
  instead.
- **State Ledger**: owns no `LedgerRow` transition, ever (story's own touchpoint) —
  `IndexRegistryEntry`/`IndexRegistry` is a deliberately separate lifecycle store: "indexes
  outlive documents." No call in this module's entire design ever reads or writes
  `vestibule.ledger.store`.
- **Failure Taxonomy**: registers all 11 codes at import time via `register_error`, following the
  established `_DEFAULT_REGISTRY` pattern. The story's own deliberate split is honored precisely:
  every code requiring human intervention — `INDEX_TEMPLATE_NOT_FOUND`, `INDEX_TEMPLATE_INVALID`,
  `INDEX_SCHEMA_DRIFT`, `INDEX_PROVISION_FAILED`, `INDEX_RETIRED`, `INDEX_AUTO_CREATE_DISABLED`,
  `INDEX_PROVISIONER_DEPENDENCY_MISSING` — is PERMANENT; everything resolvable by waiting or
  retrying — `INDEX_PROVISION_TIMEOUT`, `INDEX_PROVISION_CONFLICT`, `INDEX_REGISTRY_UNAVAILABLE`,
  `INDEX_PROVISIONER_UNAVAILABLE` — is TRANSIENT. One deliberate departure from every other
  module's TRANSIENT-self-transition pattern: a `create_index`/post-creation-compatibility
  failure marks the registry entry `failed` (terminal, human-intervention-required) *regardless*
  of whether the underlying triggering exception was itself TRANSIENT-classified (§3.2 steps 6/8)
  — automatic re-provisioning after a crashed/failed create is deliberately not attempted, unlike
  per-document TRANSIENT self-transitions elsewhere in this codebase, because retrying index
  *creation* automatically risks duplicate/partial infrastructure operations and thundering-herd
  load against the search service, not merely re-processing one document.

## 5. Error codes

| Code | Classification | Trigger condition |
|---|---|---|
| `INDEX_TEMPLATE_NOT_FOUND` | PERMANENT | `_resolve_template`'s `templates.get_or_raise(template_id)` finds no loaded template for the resolved `template_id` (§3.2) |
| `INDEX_TEMPLATE_INVALID` | PERMANENT | (a) `IndexTemplateStore` load-time: a template file fails `IndexTemplate` validation (bad field spec, non-integer `template_version`, filename/`template_id` mismatch); (b) `_resolve_template`: `template.dimensions` is explicit and disagrees with `scenario.indexer.dimensions` (Assumption A2, §3.2 step 1) |
| `INDEX_SCHEMA_DRIFT` | PERMANENT | (a) `ensure()`'s ready-and-stale-verification path finds live `dimensions`/`metric` incompatible, or `template_version` older than the resolved template's (§3.2, verification-elapsed sub-case step 3); (b) `_claim_and_create`'s post-creation/already-exists compatibility check finds live `dimensions`/`metric` incompatible (§3.2 step 8) |
| `INDEX_PROVISION_FAILED` | PERMANENT | `ensure()` finds `entry.status == "failed"` (a *subsequent* call after some earlier call's `mark_failed`, §3.2 step 6/8, F5/F6) |
| `INDEX_RETIRED` | PERMANENT | `ensure()` finds `entry.status == "retired"` |
| `INDEX_AUTO_CREATE_DISABLED` | PERMANENT | `entry is None` and `config.auto_create is False` (§3.2 no-entry sub-case step 1) — checked before any template resolution or claim attempt |
| `INDEX_PROVISION_TIMEOUT` | TRANSIENT | `_wait_for_ready`'s loop exceeds `config.wait_for_provisioning_timeout_seconds` without observing `ready` (§3.4 step 6) |
| `INDEX_PROVISION_CONFLICT` | TRANSIENT | `IndexRegistry.reclaim`/`mark_ready`/`mark_failed`'s CAS check fails (in-memory: stored entry != observed/expected `claim_token`; Table Storage: ETag-conditional write returns 409/412) — §3.4 steps 2–4; absorbed internally (never re-raised to `ensure()`'s own caller) at every call site in this module except a genuine `reclaim` loss during `_wait_for_ready`, which loops rather than raising |
| `INDEX_REGISTRY_UNAVAILABLE` | TRANSIENT | Any `TableStorageIndexRegistry` call exhausts retries against an HTTP 429/5xx or timeout (mirrors `SCENARIO_STORE_UNAVAILABLE`'s `_with_retry` pattern, Assumption A9) |
| `INDEX_PROVISIONER_UNAVAILABLE` | TRANSIENT | An `IndexProvisionerAdapter` call (`index_exists`/`create_index`/`describe_index`) exhausts retries against an HTTP 429/5xx or timeout, or raises any exception not otherwise mapped (Assumption A7's fallback) |
| `INDEX_PROVISIONER_DEPENDENCY_MISSING` | PERMANENT | `AzureAISearchProvisioner`/`TableStorageIndexRegistry` construction: `azure-search-documents`/`azure-data-tables` not installed and no test backend injected (one shared code across both boundaries, Assumption in §1's `TableStorageIndexRegistry` docstring) |

## 6. Config surface

New file `config/index_provisioning.yaml`:

```yaml
index_provisioning:
  registry_backend: in_memory        # in_memory | table_storage — selects which
                                      # IndexRegistry backend the composition root
                                      # constructs; not an IndexRegistry field

  default_template_id: standard-v1   # operational tunable — code-authoritative, see below
  template_directory: config/index_templates

  provisioning_stale_after_seconds: 300    # operational tunable — code-authoritative
  verification_interval_seconds: 300       # operational tunable — code-authoritative
  wait_for_provisioning_timeout_seconds: 60 # operational tunable — code-authoritative
  wait_poll_interval_seconds: 2             # operational tunable — code-authoritative
                                             # (Assumption A8 — not in the story's own
                                             # config list)
  auto_create: true                  # operational tunable — code-authoritative

  table_storage:
    table_name: VestibuleIndexRegistry
    connection_string_env_var: AZURE_TABLES_CONNECTION_STRING  # read from env at
                                                                 # process start, never here
    extra: table_storage             # pip install vestibule[table_storage]

  registry_cache:
    enabled: true                    # whether the composition root wraps the selected
                                      # registry backend in a CachedIndexRegistry
                                      # (Assumption A16) before constructing
                                      # IndexProvisioner; not an IndexRegistry field
    ttl_seconds: 10                  # operational tunable — code-authoritative, see below

  azure_ai_search:
    endpoint_env_var: AZURE_SEARCH_ENDPOINT     # reuses REQ-008's own indexer.yaml
                                                 # azure_ai_search.endpoint_env_var verbatim —
                                                 # see note below
    api_key_env_var: AZURE_SEARCH_API_KEY       # reuses REQ-008's own indexer.yaml
                                                 # azure_ai_search.api_key_env_var verbatim —
                                                 # see note below
    extra: azure_search               # pip install vestibule[azure_search] — already declared,
                                       # REQ-008; no new extra introduced by this REQ

  error_codes:
    INDEX_TEMPLATE_NOT_FOUND: PERMANENT
    INDEX_TEMPLATE_INVALID: PERMANENT
    INDEX_SCHEMA_DRIFT: PERMANENT
    INDEX_PROVISION_FAILED: PERMANENT
    INDEX_RETIRED: PERMANENT
    INDEX_AUTO_CREATE_DISABLED: PERMANENT
    INDEX_PROVISION_TIMEOUT: TRANSIENT
    INDEX_PROVISION_CONFLICT: TRANSIENT
    INDEX_REGISTRY_UNAVAILABLE: TRANSIENT
    INDEX_PROVISIONER_UNAVAILABLE: TRANSIENT
    INDEX_PROVISIONER_DEPENDENCY_MISSING: PERMANENT

# NOTE: every *_seconds/auto_create/default_template_id/template_directory tunable above is an
# ordinary operational tunable (REQ-006 §6 Assumption A6 precedent) — authoritative defaults live
# on ProvisioningConfig's own dataclass fields; kept in sync via a dedicated test
# (test_config_index_provisioning_yaml_tunables_match_provisioning_config_defaults), since no
# config-loading mechanism for *tunables* exists anywhere in this codebase yet.
# registry_backend/table_storage.* select and configure which IndexRegistry backend the
# composition root constructs (out of scope here) — mirrors config/scenario_store.yaml's
# backend/table_storage.* block exactly.
# registry_cache.ttl_seconds is a separate operational tunable (no bearing on Contract #2/#3
# correctness, Assumption A16) — its authoritative default lives on CachedIndexRegistry's own
# constructor default (vestibule/provisioning/stores/cached_registry.py), kept in sync via its
# own dedicated test (test_config_index_provisioning_yaml_registry_cache_tunables_match_cached_index_registry_defaults,
# mirroring config/scenario_store.yaml's test_config_scenario_store_yaml_tunables_match_code_defaults
# precedent, REQ-010). Deliberately set to 10 seconds, well below CachedScenarioStore's own 60-second
# default (config/scenario_store.yaml) — see Assumption A16 for why: _wait_for_ready's own polling
# loop (§3.4 step 6) shares this module's registry.get() call with the hot ensure() fast path, and a
# TTL too close to wait_for_provisioning_timeout_seconds (default 60) risks a waiting worker never
# observing a genuine provisioning -> ready transition the live store already reflects.
# registry_cache.enabled selects whether the composition root wraps the selected registry backend
# in a CachedIndexRegistry (out of scope here) — mirrors config/scenario_store.yaml's own
# cache.enabled block exactly.
# azure_ai_search.endpoint_env_var/api_key_env_var name the environment variables the
# composition root must read to construct AzureAISearchProvisioner (§1) — deliberately the exact
# same env vars config/indexer.yaml's own azure_ai_search.endpoint_env_var/api_key_env_var already
# name for REQ-008's AzureAISearchIndexer, not a new pair: AzureAISearchProvisioner and
# AzureAISearchIndexer talk to the same Azure AI Search service, so reusing REQ-008's existing
# credentials is the simpler, obvious choice over minting a second, redundant env-var pair for an
# identical secret (house rules: secrets from env only; the actual endpoint URL and API key are
# never written to this file or any code either way).
# error_codes follows the same documentation/audit-only convention as config/errors.yaml's
# known_codes: not read at runtime; the registry populated via register_error at import time
# (vestibule/provisioning/model.py) is authoritative.
```

New directory `config/index_templates/`, one file `standard-v1.yaml` shipped by default:

```yaml
template_id: standard-v1
template_version: "1"
dimensions: null              # inherit scenario.indexer.dimensions (Assumption A2)
metric: cosine                 # matches indexer.py's own _DEFAULT_METRIC (REQ-008)
hnsw:
  m: 4                         # matches AzureAISearchIndexer's own constructor defaults (REQ-008)
  ef_construction: 400
  ef_search: 500
semantic_ranker_enabled: false  # REQ-008's ensure_schema never configured this — AC9 parity
hybrid_enabled: false           # REQ-008's ensure_schema never configured this — AC9 parity
fields:
  - name: chunk_id
    type: key
  - name: content
    type: text
    searchable: true
  - name: content_vector
    type: vector
    searchable: true
  - name: doc_id
    type: filterable_string
  - name: doc_version
    type: filterable_string
  - name: allowed_groups
    type: filterable_string_collection
  - name: trust_tier
    type: filterable_string
  - name: config_version
    type: filterable_string
```

`fields` reproduces `_azure_search_backend._build_fields`'s exact 8-field schema (Assumption A12
— deliberately *not* the superset of fields `_record_to_document` also sends; AC9's regression
guard is checked against this exact list).

**`config/errors.yaml` + sync-test integration (same-PR deliverable, per REQ-006's established
precedent):**

- `config/errors.yaml`'s `known_codes` block gains exactly the 11 codes from §5.
- `vestibule/errors/test_registry.py`'s
  `test_config_known_codes_matches_all_codes_after_importing_all_modules` hand-import list gains
  `import vestibule.provisioning.model  # noqa: F401` (the story's own explicit ask). Without it,
  that test's `known_codes == live_codes` assertion would pass vacuously (this module's codes
  never entering `live_codes`) — not acceptable, per REQ-006's own documented reasoning for the
  identical gap.

**Cross-module config edits (Assumption A1):** `config/scenarios/*.yaml` fixture files may
optionally gain `index_template_id: <template_id>`; its absence is valid (`None`, falls back to
`config/index_provisioning.yaml`'s `default_template_id`).

**New third-party dependency reuse (no new dependency)**: `AzureAISearchProvisioner`/
`TableStorageIndexRegistry` reuse the already-declared `azure-search-documents`/`azure-data-tables`
optional extras (REQ-008/REQ-010) — no new `pyproject.toml` extra is introduced by this REQ.

## 7. Test plan

**Template/model:**
- `test_index_template_standard_v1_loads_and_validates`.
- `test_index_template_rejects_non_integer_template_version` (Assumption A6).
- `test_index_template_rejects_missing_key_field` / `..._multiple_key_fields` /
  `..._no_vector_field` / `..._duplicate_field_names`.
- `test_index_template_store_fails_fast_on_malformed_yaml_file` (mirrors
  `YamlScenarioStore`'s AC5 precedent).
- `test_standard_v1_field_list_matches_azure_ai_search_ensure_schema_exactly` (**AC9, the
  regression guard**) — asserts the loaded `standard-v1` `IndexTemplate.fields` list, translated
  through `AzureAISearchProvisioner`'s `_build_fields`-equivalent, produces the same
  key/searchable/filterable/vector-dimension shape `_azure_search_backend._build_fields` produces
  today, field-for-field.

**Registry — shared abstract contract (parametrized over both backends, mirrors
`vestibule/scenario/stores/test_base.py`'s precedent):**
- `test_register_on_absent_index_name_inserts_and_returns_matching_claim_token`.
- `test_register_on_existing_index_name_returns_stored_entry_unconditionally_no_validation`
  (register() never validates settings-equality, per §3.2's documented interpretation).
- `test_get_returns_none_for_unknown_index_name`.
- `test_mark_ready_then_mark_failed_are_mutually_exclusive_terminal_states`.
- `test_mark_retired_is_never_called_by_anything_in_this_module` (a static/documentation-style
  guard — grep-based or a `unittest.mock` spy asserting zero calls across the full `ensure()`
  integration test suite, confirming Out of scope holds in practice, not just in prose).

**Registry — concurrency (the core test, run against `InMemoryIndexRegistry` with real
`threading.Thread`s, per §3.5's "correctly simulate... via real threading races" mandate — never
a hand-scripted call order):**
- `test_two_threads_racing_register_for_same_new_index_name_exactly_one_wins` (**AC3's registry
  half**) — N=20 threads, one `index_name`, asserts exactly one thread's returned `claim_token`
  matches its own submitted candidate's, the other N-1 all observe the winner's `claim_token`.
- `test_register_never_raises_on_a_create_race` — same setup, asserts zero exceptions across all
  N threads (the story's "the losing worker does not error").
- `test_reclaim_succeeds_only_against_the_exact_observed_stale_entry` — seed a stale `provisioning`
  entry; one thread reclaims successfully; a second, concurrent `reclaim` call against the *same*
  originally-observed snapshot fails with `INDEX_PROVISION_CONFLICT` (**Race A**, §3.4 step 3).
- `test_reclaim_fails_if_original_owner_finished_first` — seed a `provisioning` entry; call
  `mark_ready` (simulating W1 finishing normally) *then* attempt `reclaim` against the
  pre-`mark_ready` snapshot; asserts `INDEX_PROVISION_CONFLICT`, and the entry remains `ready`,
  never reverted to a fresh `provisioning` claim (**Race B, sub-ordering 4a**).
- `test_mark_ready_fails_if_reclaimed_first` — seed a `provisioning` entry with `claim_token=T1`;
  successfully `reclaim` it (now `claim_token=T2`); attempt `mark_ready(index_name,
  expected_claim_token=T1)`; asserts `INDEX_PROVISION_CONFLICT`, and the entry's `claim_token`
  remains `T2` (never clobbered) (**Race B, sub-ordering 4b — the task's third named race**).
- `test_stale_provisioning_entry_is_reclaimable_fresh_one_is_not` (**story's explicit test**) —
  parametrized on `claimed_at` age relative to `provisioning_stale_after_seconds`.
- `test_ten_threads_racing_reclaim_of_one_stale_entry_exactly_one_wins` — same shape as the
  register race test, applied to `reclaim`.

**`CachedIndexRegistry` (Assumption A16, design-review MAJOR resolution, round 4) — run against a
`_CountingRegistry`-shaped test double, mirroring `test_cached_store.py`'s own `_CountingStore`
convention (REQ-010, read directly) and `test_cached_store.py`'s `_FakeClock` for TTL control:**
- `test_get_serves_from_cache_within_ttl_wrapped_get_called_once` (mirrors
  `test_get_serves_from_cache_within_ttl_wrapped_get_called_once`, REQ-010).
- `test_list_by_vertical_serves_from_cache_within_ttl` (mirrors
  `test_get_by_vertical_serves_from_cache_within_ttl`, REQ-010).
- `test_get_re_reads_wrapped_registry_after_ttl_elapses` (mirrors
  `test_get_re_reads_wrapped_store_after_ttl_elapses`, REQ-010, `ttl_seconds=1`).
- `test_get_still_cached_just_under_ttl` (mirrors REQ-010's identical test).
- `test_register_never_served_from_or_populates_cache_even_when_cache_seeded_with_stale_wrong_answer`
  (**the claim-path exclusion — Assumption A16's load-bearing constraint**) — prime the cache with
  a `get(index_name)` call returning `None` (absent) while the wrapped registry is later seeded,
  out from under the cache, with an existing entry for the same `index_name`; call `register()`
  with a fresh candidate; asserts the wrapped registry's real `register()` is invoked and its real
  "already exists" answer (not the stale cached `None`) determines the outcome — the call does not
  win, and no second entry is inserted.
- `test_reclaim_never_served_from_or_populates_cache_even_when_cache_seeded_with_stale_wrong_answer`
  — analogous, for `reclaim()`: prime the cache with a stale `get()` snapshot of an entry that has
  since changed on the wrapped registry (a mark_ready that ran directly against `wrapped`,
  bypassing the cache); attempt `reclaim()` against the stale, cache-seeded `observed` value;
  asserts `INDEX_PROVISION_CONFLICT` is raised (the wrapped registry's real, current entry — not
  the cached stale one — is what `reclaim()`'s CAS actually checks against), proving `reclaim()`
  never trusts the cache.
- `test_register_invalidates_cache_so_subsequent_get_reflects_the_new_claim` — `get()` a miss
  (cached `None`); `register()` a winning claim; assert an immediate subsequent `get()` (within
  the TTL window) returns the new entry, not the stale cached `None`.
- `test_mark_ready_invalidates_cache_so_subsequent_get_reflects_ready_status` — cache a
  `provisioning` entry via `get()`; call `mark_ready`; assert an immediate subsequent `get()`
  (within TTL) returns `status == "ready"`, not the stale cached `provisioning` snapshot.
- `test_mark_failed_invalidates_cache` / `test_mark_retired_invalidates_cache` — analogous.
- `test_touch_verified_invalidates_cache_so_subsequent_get_reflects_bumped_last_verified_at`
  (**Assumption A16's `last_verified_at`/drift-detection interaction, worked through and
  closed**) — cache a `ready` entry via `get()`; call `touch_verified`; assert an immediate
  subsequent `get()` (within TTL) reflects the newly-bumped `last_verified_at`, not the
  pre-`touch_verified` cached value.
- `test_config_index_provisioning_yaml_registry_cache_tunables_match_cached_index_registry_defaults`
  (mirrors `test_config_scenario_store_yaml_tunables_match_code_defaults`, REQ-010, read directly)
  — asserts `config/index_provisioning.yaml`'s `registry_cache.ttl_seconds`/`.enabled` match
  `CachedIndexRegistry.__init__`'s own `ttl_seconds` default and `True`, respectively.

**`IndexProvisioner.ensure()` — orchestration (`InMemoryIndexRegistry` + `InMemoryProvisioner`
+ real `threading` where noted):**
- `test_ensure_unknown_index_creates_registers_marks_ready_returns_entry` (story, **AC1**) —
  asserts `entry.dimensions == scenario.embedder.target_dimensions` (or native, if untruncated).
- `test_ensure_ready_index_returns_immediately_without_calling_create_index` (story, **AC2**) —
  spy-asserts zero `create_index` calls.
- `test_ensure_two_concurrent_calls_for_same_new_index_exactly_one_create_index_call` (**AC3, the
  story's own "core test"**) — two real threads calling `ensure()` on the same fresh
  `IndexProvisioner`/registry/adapter; spy-asserts exactly one `create_index` invocation; both
  threads' return values compare equal on `dimensions`/`metric`/`template_id`/`template_version`/
  `status`. Scope note: this test exercises the *original, non-stale* claim race only (§3.3) — it
  does not involve reclamation, so "exactly one `create_index` call" is the correct, unqualified
  assertion here; see the next test for the stale-reclaim completeness case.
- `test_ensure_slow_but_alive_worker_survives_stale_reclaim_both_create_index_calls_converge`
  (**AC3 completeness — the design-review BLOCKER on §3.3 step 7 / §3.4 Race C**) — a claim
  holder (W1) is given a fake adapter whose `create_index` blocks on a `threading.Event` (standing
  in for a legitimately long retry envelope) while an injected `_clock` seam advances W1's own
  `claimed_at` past `provisioning_stale_after_seconds`; a second worker (W3) observes staleness,
  successfully `reclaim`s (asserted: W3's CAS succeeds because W1 has made no registry write since
  its claim), and independently calls `create_index` on the same fake adapter while W1's own call
  is still blocked; the test then releases W1's blocked call, letting it complete and attempt its
  own `mark_ready(expected_claim_token=<W1's original token>)`. Asserts: (a) the fake adapter
  observes **two** `create_index` invocations (the test explicitly forces and confirms the
  "two legitimate calls" scenario the BLOCKER identified, rather than asserting call-count
  exclusivity); (b) neither `create_index` call raises; (c) both calls' recorded definitions
  (dimensions/metric) match the template, i.e. they converge; (d) exactly one of W1's or W3's
  `mark_ready` calls succeeds (the other is rejected with `INDEX_PROVISION_CONFLICT` per §3.4 step
  4b, absorbed internally, never surfaced to either worker's own `ensure()` caller); (e) the final
  registry entry is `ready` with `dimensions`/`metric`/`template_id`/`template_version` matching
  what both W1 and W3 independently resolved; (f) neither W1's nor W3's `ensure()` call itself
  raises an exception due purely to the redundant `create_index` call (Assumption A13).
- `test_ensure_reclaim_uses_original_claims_resolved_template_not_a_freshly_loaded_one_even_when_they_differ`
  (**Assumption A14 — the design-review MAJOR resolution, round 2**) — seed a stale
  `provisioning` entry whose `resolved_template` is a specific `IndexTemplate` object (e.g.
  `hnsw.ef_construction=400`); construct the reclaiming `IndexProvisioner` with a *different*
  `IndexTemplateStore` instance whose `config/index_templates/standard-v1.yaml`-equivalent fixture
  has since been edited to a different value (e.g. `hnsw.ef_construction=999`) for the same
  `template_id`/`template_version` — simulating an eager-loaded, never-refreshed `IndexTemplateStore`
  that read different file content than the original claimant's store did. After a successful
  `reclaim`, asserts: (a) `create_index` is invoked with the *original* entry's `resolved_template`
  (`ef_construction=400`), never the reclaiming worker's own freshly-loaded template store's content
  (`ef_construction=999`); (b) the finalized `ready` entry's `resolved_template`-derived fields match
  the original claim's resolution, not the reclaiming worker's local `IndexTemplateStore`; (c) no
  `_resolve_template` call is made by the reclaim path at all (spy-asserted zero calls against the
  reclaiming worker's `IndexTemplateStore.get_or_raise`) — proving reuse, not coincidental
  agreement.
- `test_ensure_stale_provisioning_reclaimed_by_waiter_fresh_one_times_out` (story, **AC4**) —
  fresh entry: waiter observes `provisioning`, never reclaims, eventually raises
  `INDEX_PROVISION_TIMEOUT` (TRANSIENT) once `wait_for_provisioning_timeout_seconds` elapses
  (injected `_sleep`/`_clock` seams, no real wall-clock wait).
- `test_ensure_create_index_failure_after_claim_marks_failed_subsequent_ensure_raises_provision_failed`
  (story, **AC5**) — first call raises the adapter's own mapped code (e.g.
  `INDEX_PROVISIONER_UNAVAILABLE`); a second, independent `ensure()` call raises
  `INDEX_PROVISION_FAILED` (PERMANENT), never re-attempting `create_index`.
- `test_ensure_index_already_exists_with_compatible_schema_after_won_claim_is_success` (story) —
  `InMemoryProvisioner`'s wrapped `InMemoryIndexer` pre-seeded with a matching schema before
  `ensure()` is called; asserts zero `create_index` calls, entry ends `ready`.
- `test_ensure_index_already_exists_with_incompatible_schema_after_won_claim_raises_drift` — same
  setup, mismatched dimensions; asserts `INDEX_SCHEMA_DRIFT` and the entry ends `failed` (§3.2
  step 8), not stuck `provisioning`.
- `test_ensure_out_of_band_third_party_created_index_before_either_worker_claims_still_one_create_index_call`
  (§3.3 step 7) — pre-seed the adapter's backing store directly (bypassing the registry
  entirely) before either of two racing `ensure()` calls; asserts zero `create_index` calls from
  either worker, both return `ready`.
- `test_ensure_live_index_dimension_mismatch_raises_schema_drift` (story, **AC6**) — a `ready`
  entry whose `verification_interval_seconds` has elapsed, live `describe_index` reporting
  different `dimensions`.
- `test_ensure_drift_verification_skipped_within_interval_performed_after` (story) — asserts zero
  `describe_index` calls inside the interval, exactly one just after it elapses.
- `test_ensure_auto_create_false_missing_index_raises_auto_create_disabled` (story, **AC7**) —
  asserts zero template resolution/registry-claim side effects (checked before any of that work).
- `test_ensure_template_dimensions_inconsistent_with_embedder_settings_raises_template_invalid_before_creation`
  (story, **AC8**) — asserts zero `registry.register`/`create_index` calls.
- `test_ensure_register_idempotent_identical_settings_twice_returns_same_entry` (story) —
  sequential (non-concurrent) double-call.
- `test_ensure_registry_etag_conflict_raises_provision_conflict` (story) — direct
  `TableStorageIndexRegistry` unit test (recorded-response fake, no live Azure calls) rather than
  through `ensure()`, since `ensure()` itself absorbs this conflict internally per §5's table;
  this test targets the registry method directly to confirm the code is actually raised
  somewhere observable, not swallowed everywhere.
- `test_ensure_retired_index_raises_index_retired`.
- `test_ensure_dimensions_derived_from_scenario_indexer_dimensions_when_template_dimensions_null`
  (Assumption A2).
- `test_ensure_template_explicit_dimensions_conflicting_with_scenario_raises_template_invalid`
  (Assumption A2's "second boundary" — a scenario whose `index_template_id` was edited, via a
  fake `TableStorageScenarioStore`-shaped double, to point at a template with an explicit,
  conflicting `dimensions`, after the scenario's own REQ-010 construction-time validation already
  passed against its *original* template).

**Adapters:**
- `test_azure_ai_search_provisioner_create_index_translates_template_to_search_index` — recorded-
  response fake, no live calls in CI (story's explicit requirement).
- `test_azure_ai_search_provisioner_create_index_is_idempotent_second_call_with_identical_definition_succeeds`
  (**Assumption A13**) — calls `create_index` twice against the recorded-response fake with an
  identical `template`/`dimensions`/`metric` for the same `index_name`; asserts neither call
  raises and the fake's underlying `create_or_update_index`-equivalent call is invoked both times
  without the fake ever surfacing a create-only "already exists" error.
- `test_azure_ai_search_provisioner_dependency_missing_raises_permanent`.
- `test_in_memory_provisioner_shares_state_with_wrapped_in_memory_indexer` — `create_index` via
  the provisioner, then `upsert`/`search` via the same `InMemoryIndexer` instance succeed,
  proving the "provision, then write" local flow (feeds AC10).
- `test_in_memory_provisioner_create_index_is_idempotent_second_call_with_identical_definition_succeeds`
  (**Assumption A13**) — calls `create_index` twice with an identical `dimensions`/`metric` for
  the same `index_name`; asserts neither call raises (exercises `InMemoryIndexer.ensure_schema`'s
  existing "already compatible — idempotent no-op" branch, REQ-008 AC8, through the provisioner
  adapter).

**Property-based (`hypothesis`, story):**
- `test_property_any_valid_template_and_scenario_produces_well_formed_index_definition` — random
  valid `IndexTemplate` + `Scenario` pairs never raise inside `AzureAISearchProvisioner`'s
  template-translation logic (using the recorded-response fake backend).

**End-to-end (story, AC10):**
- `test_e2e_new_vertical_provisions_indexes_and_retrieves_locally` — extends
  `vestibule/indexer/test_e2e_pipeline.py`'s existing local quickstart: a brand-new
  `scenario.index_name` with no pre-existing registry entry; `IndexProvisioner.ensure()` runs
  ahead of indexing; a document is chunked/embedded/indexed into the newly-provisioned
  `InMemoryIndexer`; `InMemoryIndexer.search()` retrieves it — zero cloud credentials.

**Config/error-code sync:**
- `test_config_index_provisioning_yaml_tunables_match_provisioning_config_defaults`.
- `test_config_index_provisioning_yaml_registry_cache_tunables_match_cached_index_registry_defaults`
  (Assumption A16 — listed above under `CachedIndexRegistry`, repeated here for the config-sync
  grouping's own completeness).
- `test_config_errors_yaml_known_codes_includes_provisioning_codes_after_import` (mirrors
  REQ-006's identical test, extended with `vestibule.provisioning.model`'s 11 codes).
- `test_all_eleven_provisioning_codes_registered_after_import` (**AC11**).

**Cross-module regression (Assumption A1):**
- `test_scenario_existing_construction_call_shapes_unaffected_by_index_template_id_field` (mirrors
  REQ-004's `test_existing_constructor_signatures_unchanged` precedent).
- `test_scenario_table_storage_round_trip_preserves_index_template_id_and_defaults_none_when_absent`.

**Cross-module regression (Assumption A15):**
- `test_in_memory_indexer_schema_property_reflects_private_schema_attribute_read_only` — asserts
  `InMemoryIndexer().schema is None` before any `ensure_schema` call, and equals the internally
  set `(dimensions, metric)` tuple after one; asserts the property has no setter (`AttributeError`
  on assignment).

## 8. Budget

- p95 latency added per document (Assumption A16 — distinguished per the design-review MAJOR
  resolution, round 4; a single blanket figure is no longer accurate once a live, uncached
  registry read is possible on the hot path):
  - **(a) Cached fast path** — `CachedIndexRegistry` cache hit on `registry.get()` (§3.1) and
    `verification_interval_seconds` also not yet elapsed: `< 1ms`. One in-memory dict lookup plus
    one datetime comparison; no I/O at all. This is the truly cheap, steady-state case — the
    overwhelming majority of documents once an index is `ready`.
  - **(b) Uncached registry read, drift verification still within its own interval** — the
    registry cache entry for this `index_name` has expired (§6's `registry_cache.ttl_seconds`,
    default `10`) but `verification_interval_seconds` (default `300`) has not: `~15-40ms`. One
    live `TableStorageIndexRegistry.get()` round trip (a single-entity point read against Azure
    Table Storage), no `describe_index` call.
  - **(c) Uncached registry read AND drift verification due** — both the registry cache and the
    verification interval have elapsed for this `index_name`: `~100-300ms`. Adds one live
    `describe_index` call against the configured search service (§3.2's verification-elapsed
    sub-case) on top of (b)'s registry round trip — a materially more expensive operation than a
    Table Storage point read, consistent with why `verification_interval_seconds`'s own default
    (`300s`) is an order of magnitude longer than `registry_cache.ttl_seconds`'s (`10s`,
    Assumption A16).
  - **(d) First provision for a brand-new `index_name`** — the full claim/create/mark-ready path
    (§3.2 no-entry sub-case): `< 2s` typical (one `register()` insert, one `create_index` call,
    one `describe_index` call, one `mark_ready` call — each a single round trip, dominated by
    `create_index`'s own latency against the search service), `< 60s` worst case while waiting on
    another worker (`wait_for_provisioning_timeout_seconds`, §3.4), after which
    `INDEX_PROVISION_TIMEOUT` (TRANSIENT) lets the caller's own redelivery retry against what is,
    by then, very likely an already-`ready` index. This case is identical to the pre-A16 budget —
    provisioning itself was never on the cached read path; only (a)–(c) above change.
- Cost per document: effectively zero after the first document for a given `index_name` — one
  registry read per subsequent document (cached per Assumption A16 the overwhelming majority of
  the time, §3.1/(a) above), no embedding/LLM API calls anywhere in this module. The first
  document for a brand-new `index_name` costs exactly one `create_index`/`describe_index` round
  trip against the configured search service (or the in-memory adapter's zero-cost equivalent) —
  worst case (§3.4 Race C) an additional, redundant but harmless `create_index` round trip if a
  stale-claim reclaim overlaps a legitimately slow original attempt; this is a rare, bounded,
  one-time cost per index, not a per-document one.
- Memory: `IndexTemplate`s (one small object per `config/index_templates/*.yaml` file) and
  `IndexRegistryEntry`s (one small object per provisioned index, not per document) are held in
  memory, bounded by index count, not document count — orders of magnitude smaller than any
  per-document structure elsewhere in this pipeline (story's own stated budget).
  `CachedIndexRegistry`'s own cache (Assumption A16) adds one further small in-memory dict, also
  bounded by index count (one cached `get()`/`list_by_vertical()` entry per `index_name`/
  `vertical` actually queried), not document count.
