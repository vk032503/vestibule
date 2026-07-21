# Contributing

Thanks for the interest. Read the four contracts in `.claude/CLAUDE.md` before proposing changes;
PRs that violate a contract will be rejected regardless of implementation quality.

## Development setup

    git clone https://github.com/vk032503/rag-ingestion-framework
    cd rag-ingestion-framework
    python -m venv .venv && source .venv/bin/activate
    pip install -e ".[dev]"
    pytest

Python 3.11+ required.

## Pull request checklist

- [ ] Branch named `req/<id>-<slug>` or `fix/<slug>` or `docs/<slug>`
- [ ] Conventional Commits throughout
- [ ] `ruff check` and `ruff format --check` clean
- [ ] `mypy --strict` clean
- [ ] `pytest` passes; new code has ≥ 90% coverage
- [ ] **`CHANGELOG.md` updated under `## [Unreleased]`**
- [ ] PR body links the LLD and issue (`Closes #N`)

## Four non-negotiable contracts

1. Arrival Envelope — one normalized shape at entry
2. Identity & Idempotency — deterministic IDs, upsert everywhere
3. State Ledger — every stage transition recorded
4. Failure Taxonomy — every error PERMANENT or TRANSIENT