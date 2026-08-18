"""VerticalLoader — resolves, then loads, a document's `Scenario` (REQ-009).

`VerticalLoader.resolve()` decides which vertical/scenario an `ArrivalEnvelope` belongs
to without ever guessing: a human's prior `ReviewQueue.assign()` decision (if any,
recorded durably on the ledger row) outranks everything else; failing that, an envelope
either carries an explicit signal (`scenario_id`/`vertical`), matches a configured source
mapping, or is parked for human review. `VerticalLoader.load()` resolves, then fetches
the full `Scenario` from the `ScenarioStore` — parking the document (via
`ReviewRegistry`/`LedgerStore`) when resolution fails.

Round-trip closure (spec-gap fix, see `resolve()`'s own docstring for the full mechanics):
a document parked with zero vertical signal that a human resolves via
`vestibule.vertical.review_queue.ReviewQueue.assign()` must actually resolve on
redelivery of the *same, unchanged* original envelope — not merely a freshly constructed
envelope that happens to carry an explicit `scenario_id`. `resolve()` therefore reads the
current `LedgerRow.assigned_scenario_id` (REQ-009's own extension to `LedgerRow`, see
`vestibule.ledger.store`) as a new, highest-priority step 0, before ever looking at the
envelope itself. Without this step, redelivering the identical original envelope after
`assign()` would resolve identically to the first attempt and park it again — an
infinite human-assign/re-park loop with no way out.

Deliberately-avoided reuse of `ScenarioStore.resolve()`: that method
(`vestibule.scenario.stores.base.ScenarioStore.resolve`) is a *fallback chain* — a
`scenario_id` that fails to resolve silently falls through to trying `vertical`, then
`default_scenario_id`, only raising `SCENARIO_NOT_FOUND` once every option is exhausted.
That is correct for its own purpose, but REQ-009's own resolution order requires each
explicit signal to either succeed or immediately error, with **no** silent fallthrough to
a less-explicit signal (AC5: a stated-but-missing vertical is a config error, not an
ambiguity to paper over by trying something else). Calling `resolve()` here would
therefore let a stated-but-missing `scenario_id` silently fall through to trying
`vertical` or `default_scenario_id` — collapsing exactly the ambiguity-vs-misconfiguration
distinction this component exists to keep separate. `VerticalLoader` calls
`ScenarioStore.get()`/`get_by_vertical()` directly instead, implementing its own strict
resolution order below. Do not "simplify" this back to `store.resolve()`.

`VERTICAL_UNRESOLVED` is PERMANENT even though a parked document is not dead — see
`vestibule.vertical.model`'s module docstring for the full reasoning, and `_park`'s own
docstring below for the raise-site restatement.
"""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NoReturn

from vestibule.envelope.model import ArrivalEnvelope
from vestibule.ledger.store import (
    LedgerStore,
    LedgerTransitionInvalid,
    Status,
    is_benign_concurrent_loss,
)
from vestibule.scenario.model import SCENARIO_NOT_FOUND, Scenario, ScenarioError
from vestibule.scenario.stores.base import ScenarioStore
from vestibule.vertical.model import (
    VERTICAL_MAPPING_INVALID,
    VERTICAL_UNRESOLVED,
    ResolutionSource,
    VerticalError,
    VerticalResolution,
)
from vestibule.vertical.review_queue import ReviewRegistry

logger = logging.getLogger(__name__)

_GLOB_METACHARACTERS = ("*", "?", "[")


@dataclass(frozen=True)
class SourceMappingRule:
    """One ordered `source -> vertical` mapping rule (`config/vertical_loader.yaml`).

    Attributes:
        match: Either an exact `envelope.source` string, or a glob pattern (containing
            `*`, `?`, or `[`) matched via `fnmatch.fnmatchcase` against
            `envelope.source`. Whether a rule is exact or glob is derived from `match`'s
            own contents at match-time — there is no separate "type" field.
        vertical: The vertical this rule maps to.
    """

    match: str
    vertical: str


@dataclass(frozen=True)
class VerticalLoaderConfig:
    """`VerticalLoader`'s tunables — mirrors `config/vertical_loader.yaml`.

    No config-loading mechanism exists in this codebase yet (established precedent,
    e.g. `config/scenario_store.yaml`); these literal defaults are this config's
    always-effective source of truth for a direct caller, kept in sync with
    `config/vertical_loader.yaml`'s own literals by a dedicated test.

    Attributes:
        source_mappings: Ordered rules tried when neither `scenario_id` nor `vertical`
            is set on the envelope. Exact-match rules take precedence over glob rules;
            among rules of the same kind, the first listed wins.
        park_unresolved: When `True` (default), an unresolved document is parked to
            `needs_review`. When `False`, `VerticalLoader.load()` raises
            `VERTICAL_UNRESOLVED` without any ledger transition at all — for
            deployments that don't want a review queue.
        suggest_verticals: Whether `VerticalLoader` populates
            `ReviewItem.suggested_verticals` when parking a document. When `False`,
            parked documents always get an empty `suggested_verticals` list.
    """

    source_mappings: tuple[SourceMappingRule, ...] = ()
    park_unresolved: bool = True
    suggest_verticals: bool = True


class VerticalLoader:
    """Resolves an `ArrivalEnvelope` to a vertical/scenario, then loads the `Scenario`.

    Constructed with a `ScenarioStore`, a `LedgerStore`, and a `VerticalLoaderConfig`
    (the story's own given constructor shape) plus one additional, required,
    keyword-only `review_registry` — see `__init__`'s own docstring for why this fourth
    dependency exists (a documented REQ-009 spec-gap resolution, not scope creep) and
    why it has no default.
    """

    def __init__(
        self,
        store: ScenarioStore,
        ledger: LedgerStore,
        config: VerticalLoaderConfig,
        *,
        review_registry: ReviewRegistry,
    ) -> None:
        """Initializes the loader, validating every configured source mapping.

        Spec-gap resolution (`review_registry`): the story's own `ReviewItem` shape
        (`doc_id`, `source`, `blob_path`, `reason`, `parked_at`, `suggested_verticals`)
        cannot be reconstructed from `LedgerRow` alone — `LedgerRow` (REQ-003) carries
        no `source`/`blob_path` field, by design (Contract #3's ledger is
        envelope-schema-agnostic). `ReviewRegistry` is the minimal, explicitly-flagged
        side-table this REQ introduces to bridge that gap: `VerticalLoader` records the
        parking metadata a `ReviewItem` needs at park-time; `ReviewQueue` reads it back
        at `list_pending()` time.

        `review_registry` is required, with no default — a composition root MUST wire
        the exact same `ReviewRegistry` instance into both `VerticalLoader` and
        `ReviewQueue`. This is deliberately not optional: an earlier revision defaulted
        it to a fresh, private `ReviewRegistry()` when omitted, which let a
        `VerticalLoader`/`ReviewQueue` pair constructed independently (without a
        composition root deliberately sharing one instance) silently degrade —
        `ReviewQueue.list_pending()` would return blank `source`/`blob_path`/
        `suggested_verticals` for every parked item, with nothing raising or logging
        anywhere. Making this argument required, with no default, makes that mistake
        structurally impossible instead of merely documented, matching this codebase's
        fail-fast convention for every other construction-time dependency (e.g.
        `VERTICAL_MAPPING_INVALID` above, `SCENARIO_NOT_FOUND`).

        Args:
            store: Read-only lookup for `Scenario`s by `scenario_id`/`vertical`.
            ledger: Where `pending -> needs_review` parking transitions are written, and
                where `resolve()`'s step-0 `LedgerRow.assigned_scenario_id` check reads
                from.
            config: Source mappings and parking tunables.
            review_registry: Shared parking-metadata side-table (see above) — the same
                instance passed to the paired `ReviewQueue`.

        Raises:
            VerticalError: `VERTICAL_MAPPING_INVALID` (PERMANENT) if any configured
                `source_mappings` rule references a vertical with no enabled scenario in
                `store` — fail fast, before any document is ever processed.
        """
        self._store = store
        self._ledger = ledger
        self._config = config
        self._review_registry = review_registry
        self._validate_mappings()

    def _validate_mappings(self) -> None:
        """Construction-time guard: every mapped vertical must have an enabled scenario.

        Raises:
            VerticalError: `VERTICAL_MAPPING_INVALID` (PERMANENT) on the first
                unsatisfied rule.
        """
        for rule in self._config.source_mappings:
            if self._store.get_by_vertical(rule.vertical) is None:
                raise VerticalError(
                    "",
                    f"source mapping match={rule.match!r} references "
                    f"vertical={rule.vertical!r}, which has no enabled scenario",
                    error_code=VERTICAL_MAPPING_INVALID,
                )

    def resolve(self, envelope: ArrivalEnvelope) -> VerticalResolution:
        """Decides which vertical/scenario `envelope` belongs to. Never writes the ledger.

        Resolution order (first match wins, most explicit first): step 0,
        `LedgerRow.assigned_scenario_id` (a prior human `ReviewQueue.assign()` decision,
        read from `envelope.doc_id`'s current ledger row — see this module's own
        docstring for why this outranks even an explicit `envelope.scenario_id`) ->
        `envelope.scenario_id` -> `envelope.vertical` -> a configured source mapping
        against `envelope.source` -> unresolved (parking is `load()`'s concern, not this
        method's).

        Step 0 deliberately takes priority over `envelope.scenario_id`: a human's
        explicit assignment via the review queue is a strictly more authoritative signal
        than whatever the envelope itself happens to carry (including a case where both
        are present and disagree — the assignment wins).

        Args:
            envelope: The envelope to resolve. Only `doc_id`/`scenario_id`/`vertical`/
                `source` are read (Contract #1 touchpoint) — never mutated. Reading
                `doc_id` only to look up the ledger row is not a Contract #1 violation:
                `doc_id` is itself part of the envelope, not a source-specific field.

        Returns:
            The resolved (or unresolved) `VerticalResolution`.

        Raises:
            ScenarioError: `SCENARIO_NOT_FOUND` (PERMANENT) if
                `LedgerRow.assigned_scenario_id`, `envelope.scenario_id`, or
                `envelope.vertical` is set but does not resolve to a `Scenario` — a
                stated-but-missing signal is a config error (AC5), never silently
                downgraded to parking.
        """
        assigned = self._assigned_scenario_id(envelope.doc_id)
        if assigned is not None:
            return self._resolve_review_assignment(assigned)
        if envelope.scenario_id is not None:
            return self._resolve_scenario_id(envelope.scenario_id)
        if envelope.vertical is not None:
            return self._resolve_vertical(envelope.vertical)
        matched = self._match_source_mapping(envelope.source)
        if matched is not None:
            return matched
        return _unresolved_resolution(envelope.source)

    def _assigned_scenario_id(self, doc_id: str) -> str | None:
        """Reads `doc_id`'s current `LedgerRow.assigned_scenario_id`, if any.

        Args:
            doc_id: The envelope's `doc_id`.

        Returns:
            The assigned `scenario_id`, or `None` if no ledger row exists for `doc_id`
            yet, or none was ever assigned.
        """
        row = self._ledger.get(doc_id)
        if row is None:
            return None
        return row.assigned_scenario_id

    def _resolve_review_assignment(self, scenario_id: str) -> VerticalResolution:
        """Resolves a human-assigned `scenario_id`, read from the ledger (step 0).

        Args:
            scenario_id: `LedgerRow.assigned_scenario_id`.

        Returns:
            The resolved `VerticalResolution`, `source=ResolutionSource.
            REVIEW_ASSIGNMENT`.

        Raises:
            ScenarioError: `SCENARIO_NOT_FOUND` (PERMANENT) if `scenario_id` no longer
                resolves to a `Scenario` (e.g. deleted after `ReviewQueue.assign()` was
                called).
        """
        scenario = self._store.get(scenario_id)
        if scenario is None:
            raise ScenarioError(
                scenario_id,
                f"ledger row carries assigned_scenario_id={scenario_id!r} (from a "
                "prior ReviewQueue.assign()), but no such scenario exists",
                error_code=SCENARIO_NOT_FOUND,
            )
        return VerticalResolution(
            vertical=scenario.vertical,
            scenario_id=scenario.scenario_id,
            source=ResolutionSource.REVIEW_ASSIGNMENT,
            confidence="explicit",
            reason=(
                f"human-assigned scenario_id={scenario_id!r} via ReviewQueue.assign()"
            ),
        )

    def _resolve_scenario_id(self, scenario_id: str) -> VerticalResolution:
        scenario = self._store.get(scenario_id)
        if scenario is None:
            raise ScenarioError(
                scenario_id,
                f"envelope declared scenario_id={scenario_id!r}, but no such scenario "
                "exists",
                error_code=SCENARIO_NOT_FOUND,
            )
        return VerticalResolution(
            vertical=scenario.vertical,
            scenario_id=scenario.scenario_id,
            source=ResolutionSource.ENVELOPE_SCENARIO_ID,
            confidence="explicit",
            reason=f"envelope declared scenario_id={scenario_id!r}",
        )

    def _resolve_vertical(self, vertical: str) -> VerticalResolution:
        scenario = self._store.get_by_vertical(vertical)
        if scenario is None:
            raise ScenarioError(
                vertical,
                f"envelope declared vertical={vertical!r}, but no enabled scenario "
                "exists for it",
                error_code=SCENARIO_NOT_FOUND,
            )
        return VerticalResolution(
            vertical=scenario.vertical,
            scenario_id=scenario.scenario_id,
            source=ResolutionSource.ENVELOPE_VERTICAL,
            confidence="explicit",
            reason=f"envelope declared vertical={vertical!r}",
        )

    def _match_source_mapping(self, source: str) -> VerticalResolution | None:
        rule = _select_source_rule(source, self._config.source_mappings)
        if rule is None:
            return None
        scenario = self._store.get_by_vertical(rule.vertical)
        if scenario is None:
            # Defensive only: __init__'s _validate_mappings already confirmed every
            # configured vertical has an enabled scenario at construction time. This
            # branch guards against a scenario being disabled *after* construction
            # (out of scope for this REQ to prevent), rather than silently matching.
            raise ScenarioError(
                rule.vertical,
                f"source mapping match={rule.match!r} matched, but "
                f"vertical={rule.vertical!r} no longer has an enabled scenario",
                error_code=SCENARIO_NOT_FOUND,
            )
        return VerticalResolution(
            vertical=scenario.vertical,
            scenario_id=scenario.scenario_id,
            source=ResolutionSource.SOURCE_MAPPING,
            confidence="inferred",
            reason=f"source={source!r} matched mapping rule match={rule.match!r}",
        )

    def load(self, envelope: ArrivalEnvelope) -> tuple[Scenario, VerticalResolution]:
        """Resolves `envelope`, then fetches the `Scenario`; parks it if unresolved.

        Args:
            envelope: The envelope to load.

        Returns:
            `(scenario, resolution)` on success.

        Raises:
            ScenarioError: `SCENARIO_NOT_FOUND` (PERMANENT) — see `resolve()`.
            VerticalError: `VERTICAL_UNRESOLVED` (PERMANENT) if `envelope` cannot be
                resolved. See `_park`'s own docstring for the parking sequence and the
                non-obvious PERMANENT-but-not-dead reasoning.
            LedgerTransitionInvalid: Propagated unchanged when the parking transition
                fails for a reason `is_benign_concurrent_loss` classifies as not benign.
        """
        resolution = self.resolve(envelope)
        if resolution.scenario_id is None:
            self._park(envelope, resolution)
        scenario = self._store.get(resolution.scenario_id)
        if scenario is None:
            # Defensive only (TOCTOU): resolve() just confirmed this scenario_id exists.
            raise ScenarioError(
                resolution.scenario_id,
                "scenario no longer exists (concurrent delete between resolve() and "
                "load())",
                error_code=SCENARIO_NOT_FOUND,
            )
        return scenario, resolution

    def _park(
        self, envelope: ArrivalEnvelope, resolution: VerticalResolution
    ) -> NoReturn:
        """Parks an unresolved document to `needs_review`, then raises.

        Sequence (story: "Parking, precisely"): (1) write `resolution.reason` onto the
        ledger row's `last_error_message`; (2) transition `pending -> needs_review`,
        triaged via `is_benign_concurrent_loss` exactly like every prior pipeline
        component's terminalize path; (3) raise `VERTICAL_UNRESOLVED`.

        `VERTICAL_UNRESOLVED` is classified PERMANENT even though this document is not
        dead. CLAUDE.md's Contract #4 usually reads PERMANENT as "mark failed, ack,
        never retry" — but a parked document is *not* marked failed; its ledger row
        moves to the deliberately non-terminal `needs_review` status and can still
        return to the pipeline. PERMANENT means something narrower here: "stop
        redelivering this exact message." Resolution is a pure function of the envelope
        plus configuration (Contract #2), so retrying `load()` on the identical envelope
        will deterministically reach `unresolved` again, every time — nothing about the
        document changes on redelivery. Only a human's `ReviewQueue.assign()` call,
        which supplies a `scenario_id` the original envelope never carried, changes the
        outcome. See `vestibule.vertical.model`'s module docstring for the full
        reasoning (including why TRANSIENT would be actively wrong here).

        When `self._config.park_unresolved` is `False`, this raises immediately without
        touching the ledger at all (for deployments that don't want a review queue).

        Args:
            envelope: The unresolved envelope.
            resolution: `resolve()`'s own `unresolved` result for `envelope`.

        Raises:
            VerticalError: `VERTICAL_UNRESOLVED`. Always.
            LedgerTransitionInvalid: If the parking transition fails non-benignly.
        """
        if not self._config.park_unresolved:
            raise VerticalError(
                envelope.doc_id, resolution.reason, error_code=VERTICAL_UNRESOLVED
            )
        suggested = (
            _suggest_verticals(envelope.source, self._config.source_mappings)
            if self._config.suggest_verticals
            else []
        )
        self._review_registry.record(
            envelope.doc_id,
            source=envelope.source,
            blob_path=envelope.blob_path,
            suggested_verticals=suggested,
        )
        try:
            self._ledger.transition(
                envelope.doc_id,
                to_status=Status.NEEDS_REVIEW,
                stage=Status.NEEDS_REVIEW,
                error_code=VERTICAL_UNRESOLVED,
                error_message=resolution.reason,
            )
        except LedgerTransitionInvalid as exc:
            if not is_benign_concurrent_loss(
                exc.observed_row, Status.NEEDS_REVIEW, Status.NEEDS_REVIEW
            ):
                raise
            # Benign: a concurrent duplicate worker already parked this doc_id (or the
            # document has since legitimately progressed past PENDING some other way) —
            # idempotent, per Contract #2 ("parking an already-parked document is a
            # benign no-op, not an error").
        raise VerticalError(
            envelope.doc_id, resolution.reason, error_code=VERTICAL_UNRESOLVED
        )


def _unresolved_resolution(source: str) -> VerticalResolution:
    """Builds the `unresolved` `VerticalResolution` for a document with no signal."""
    return VerticalResolution(
        vertical=None,
        scenario_id=None,
        source=ResolutionSource.UNRESOLVED,
        confidence="unresolved",
        reason=(
            f"no scenario_id/vertical on the envelope and no source mapping matched "
            f"source={source!r}"
        ),
    )


def _is_glob(pattern: str) -> bool:
    """Whether `pattern` contains a glob metacharacter (`*`, `?`, `[`)."""
    return any(char in pattern for char in _GLOB_METACHARACTERS)


def _select_source_rule(
    source: str, rules: Sequence[SourceMappingRule]
) -> SourceMappingRule | None:
    """Selects the source-mapping rule for `source`, per REQ-009's matching semantics.

    Exact-string rules are checked first, in list order (first-listed wins on a
    pathological duplicate); only if none match are glob rules checked, in list order,
    via `fnmatch.fnmatchcase` — case-sensitive, matching this codebase's established
    precedent of never case-folding string identity (e.g. `Scenario.index_name`'s own
    validation accepts lowercase only, with no normalization anywhere in this pipeline).

    Args:
        source: `envelope.source` to match.
        rules: Configured rules, in their original order.

    Returns:
        The first matching rule (exact rules checked before glob rules), or `None`.
    """
    for rule in rules:
        if not _is_glob(rule.match) and rule.match == source:
            return rule
    for rule in rules:
        if _is_glob(rule.match) and fnmatch.fnmatchcase(source, rule.match):
            return rule
    return None


def _suggest_verticals(source: str, rules: Sequence[SourceMappingRule]) -> list[str]:
    """Computes `ReviewItem.suggested_verticals` — a hint only, never a classifier.

    Story: "verticals whose source mappings *nearly* matched (same prefix before the
    first delimiter)." The delimiter is `:` (both example rules, `"blob:hr-*"` and
    `"sharepoint:legal-site"`, use it to separate a system prefix from the rest). A
    rule's vertical is suggested when its own prefix equals `source`'s prefix — this
    function is only ever called once a full match has already failed (see `_park`), so
    a rule that fully matched cannot also appear here.

    Args:
        source: `envelope.source` of the unresolved document.
        rules: Configured rules, in their original order.

    Returns:
        Deduplicated verticals whose rule prefix matches `source`'s prefix, in
        first-seen order (order is unspecified by the story; first-seen is chosen for
        determinism and to match `_select_source_rule`'s own "first listed wins" bias).
    """
    prefix = source.split(":", 1)[0]
    suggestions: list[str] = []
    seen: set[str] = set()
    for rule in rules:
        rule_prefix = rule.match.split(":", 1)[0]
        if rule_prefix == prefix and rule.vertical not in seen:
            seen.add(rule.vertical)
            suggestions.append(rule.vertical)
    return suggestions
