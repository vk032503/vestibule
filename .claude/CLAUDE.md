# Ingestion Framework — House Rules (always loaded, keep under 40 lines)

## The Four Contracts (NON-NEGOTIABLE — no design or PR merges that violates these)

1. **Arrival Envelope** — every document enters via one normalized envelope:
   `{doc_id, source, blob_path, vertical?, scenario_id?, allowed_groups, trust_tier, content_hash, received_at}`.
   No component reads raw source-specific formats past the arrival boundary.

2. **Identity & Idempotency** — `doc_id = sha256(source + path)`, `chunk_id = sha256(doc_id + chunk_index)`.
   Every write is an upsert keyed on these. Re-ingestion deletes all chunks for doc_id first.
   Any operation must be safely re-runnable (at-least-once delivery is assumed everywhere).

3. **State Ledger** — every document has exactly one ledger row:
   `pending → analyzing → chunking → embedding → indexing → indexed | failed`.
   Every stage transition is recorded (stage, attempts, error_code, updated_at). No silent state.

4. **Failure Taxonomy** — every error is classified PERMANENT (mark failed, ack, never retry)
   or TRANSIENT (raise → queue retry → poison after max dequeue). Every module declares its
   error codes in config. Unclassified errors default to TRANSIENT.

## Code Standards
- Python 3.11, type hints everywhere, dataclasses/pydantic for models.
- Functions ≤ 40 lines, modules single-responsibility, adapters thin (wrap, never implement algorithms).
- All tunables from config/*.yaml; secrets from env only. No secrets, keys, or connection strings in code.
- Every external call: timeout + declared error classification. Every module: unit tests alongside.
- Never parse documents, implement chunking algorithms, or embed in-house — always via adapter interfaces.

## Token Discipline (this repo is developed on a Pro plan)
- Read only files relevant to the current task; never scan the whole repo unprompted.
- Reviews operate on the diff + directly touched files only.
- Detailed procedures live in .claude/agents/ role files — loaded per task, not here.
