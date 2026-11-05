"""J06 S8: the read-only ``EpistemicPartition`` projection.

This module answers one question for a future Context builder: *what does this
PCS instance currently hold, and how strongly?*  It sorts every live current
Belief into four disjoint buckets and never writes anything:

* ``accepted``   — ``status == "supported"``: the domain derived support from
  eligible evidence;
* ``provisional`` — ``status == "proposed"``: a committed but unverified
  judgment.  Persisting it is a legitimate outcome (J06 S3+); presenting it as
  an accepted fact is not;
* ``contested``  — ``contested`` / ``contradicted``: the evidence disagrees;
* ``stale``      — ``stale``: the claim passed its ``valid_until``.

Three boundaries shape the module.

* **It is a projection, not a second authority.**  Nothing is written, cached
  or remembered between calls.  ``accepted`` is defined by the *stored* status,
  which the commit path derived; this module never re-derives status, because a
  second derivation would drift from :func:`derive_status`.
* **It does not change C10.**  ``query.CURRENT_BELIEF_STATUSES`` keeps excluding
  ``proposed`` and this view is an additional outlet, not a replacement.  A
  provisional Belief stays invisible to the old current query and visible here.
* **Deletion and privacy govern it.**  Records under a record barrier never come
  back from ``current_versions_of_kind``; a Belief whose cited evidence has been
  deleted is dropped here exactly like the Context builder drops it.  A reader
  must never learn that a filtered-out Belief exists.

``retracted`` Beliefs are excluded from all four buckets: a retraction is not a
current judgment at all.  ``retracted_count`` reports how many were skipped so
an operator can tell "no retractions" from "the filter hid them".

RG05-F02 expression preferences are also excluded before bucketing: they are
stored as ``user_preference`` Beliefs with an ``expression_preference:*`` marker,
but they are expression-layer directives, not cognition.  ``expression_material``
continues to read the same records; nothing is migrated or rewritten.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ...foundation import identifiers
from ...foundation.errors import InvalidInput
from ...store.governance import DecisionRow, GovernanceStore
from ..persona.expression_directives import parse_preference_marker
from ..refs import RecordRef
from ..scope import scope_matches
from .beliefs import BELIEF_KIND, belief_evidence_is_live, parse_belief_body
from .questions import QuestionService

#: The four buckets cover exactly these stored statuses.  ``retracted`` is
#: deliberately absent; see the module docstring.
EPISTEMIC_STATUSES = ("supported", "proposed", "contested", "contradicted", "stale")


def is_expression_preference_body(body: Mapping[str, Any]) -> bool:
    """Whether this Belief body is F02's expression-preference storage.

    RG05-F02 stores a confirmed expression preference as a ``user_preference``
    Belief carrying an ``expression_preference:<dimension>=<value>`` marker.
    That record is an expression-layer directive, not a cognition judgement:
    publishing it as ``accepted``/``provisional`` cognition would let a length,
    format or tone rule masquerade as knowledge about the user.  The marker is
    the only thing inspected here; ``expression_material`` keeps reading the
    same record through its own path, so this is a projection rule, not a
    storage change.
    """

    if body.get("claim_type") != "user_preference":
        return False
    return parse_preference_marker(body.get("review_condition")) is not None


#: Deterministic ordering inside one bucket.  ``contested`` and ``contradicted``
#: share a rank so a contradiction is never presented as "less conflicting".
STATUS_RANK = {
    "supported": 0,
    "proposed": 1,
    "contested": 2,
    "contradicted": 2,
    "stale": 3,
}

RETRACTED_STATUS = "retracted"

#: Question statuses this view may surface alongside the belief buckets.
QUESTION_STATUSES = ("proposed", "open", "waiting")

MAX_LIMIT = 50


@dataclass(frozen=True)
class EpistemicAccess:
    """Trusted-host read authority for one partition call.

    As with every other access object in PCS this is not authentication: the
    host constructs it and this module only checks its shape.  ``privacy_classes``
    is the set of record privacy classes the caller may see.
    """

    namespace: str
    privacy_classes: frozenset[str]

    def __post_init__(self) -> None:
        identifiers.require_namespace(self.namespace)
        if not isinstance(self.privacy_classes, frozenset):
            raise InvalidInput("privacy_classes must be a frozenset")
        for privacy in sorted(self.privacy_classes):
            identifiers.require_privacy_class(privacy)


@dataclass(frozen=True)
class Provenance:
    """Which review decision committed this exact record version."""

    decision_id: str
    candidate_id: str
    authority_kind: str
    impact: str
    decided_at: str


@dataclass(frozen=True)
class EpistemicItem:
    """One current Belief together with how strongly it is held."""

    ref: RecordRef
    status: str
    provisional: bool
    claim: str
    claim_type: str
    strength: str
    scope: dict[str, Any]
    recorded_at: str
    evidence_refs: tuple[dict[str, Any], ...]
    evidence_links: tuple[dict[str, Any], ...]
    review_condition: str
    provenance: Provenance | None
    linkage: str
    """How the evidence matched the claim when this version was committed.

    ``verified`` only ever means "the evidence is the right *kind* of
    observation"; it is never a claim that the evidence proves the claim.
    ``not_applicable`` covers provisional claims and records committed
    before provenance existed.
    """


@dataclass(frozen=True)
class EpistemicPartition:
    """The four disjoint belief buckets plus the open questions beside them."""

    accepted: tuple[EpistemicItem, ...]
    provisional: tuple[EpistemicItem, ...]
    contested: tuple[EpistemicItem, ...]
    stale: tuple[EpistemicItem, ...]
    questions: tuple[Any, ...]
    truncated: bool
    state_revision: int
    retracted_count: int = 0


def build_epistemic_partition(
    store: Any,
    *,
    namespace: str,
    access: EpistemicAccess,
    limit: int = 20,
    include_questions: bool = True,
    question_access: Any = None,
    claim_types: Sequence[str] = (),
    scope: Mapping[str, Any] | None = None,
    newest_first: bool = False,
) -> EpistemicPartition:
    """Project the live current Beliefs into four disjoint buckets.

    ``limit`` truncates the *output* after a deterministic sort; it never moves
    an item into a different bucket.  ``include_questions`` without a
    ``question_access`` yields an empty ``questions`` tuple rather than guessing
    a J05 permission: the caller owns that authority.

    ``claim_types``/``scope`` are optional **read filters** (RG05): they drop an
    item before it is materialised, exactly like the privacy and live-evidence
    gates, and the defaults preserve the previous projection.  ``newest_first``
    keeps the status ranking but orders each bucket by ``recorded_at`` from
    newest to oldest, which is what a chat working set needs.

    The status buckets reflect the *stored* status.  This function never
    re-derives a status and never re-evaluates expiry: doing so at read time
    would create a second, drifting definition of "supported".
    """

    ns = identifiers.require_namespace(namespace)
    if not isinstance(access, EpistemicAccess):
        raise InvalidInput("access must be an EpistemicAccess")
    if access.namespace != ns:
        raise InvalidInput(
            f"access namespace {access.namespace!r} is not {ns!r}"
        )
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise InvalidInput(f"limit must be 1..{MAX_LIMIT}")
    if type(include_questions) is not bool:
        raise InvalidInput("include_questions must be a boolean")

    governance = (
        store if hasattr(store, "decision_for_record") else GovernanceStore(store)
    )

    items: list[EpistemicItem] = []
    retracted_count = 0
    wanted_types = tuple(str(item) for item in claim_types)
    scope_filter = dict(scope) if scope is not None else None
    for version in store.current_versions_of_kind(ns, BELIEF_KIND):
        if version.privacy_class not in access.privacy_classes:
            continue
        body = parse_belief_body(version)
        # F02's expression preferences are stored as user_preference Beliefs
        # but they are not cognition judgements; see the helper's docstring.
        # They stay readable through ``expression_material``.
        if is_expression_preference_body(body):
            continue
        if wanted_types and body.get("claim_type") not in wanted_types:
            continue
        if scope_filter and not scope_matches(
            version.scope or {}, scope_filter
        ):
            continue
        if not belief_evidence_is_live(store, namespace=ns, body=body):
            continue
        status = version.status
        if status == RETRACTED_STATUS:
            retracted_count += 1
            continue
        if status not in EPISTEMIC_STATUSES:
            raise InvalidInput(f"belief {version.record_id} has status {status!r}")
        items.append(_item(governance, ns, version, body, status))

    items.sort(
        key=lambda item: (
            STATUS_RANK[item.status],
            item.recorded_at,
            item.ref.record_id,
            item.ref.record_version,
        )
    )
    if newest_first:
        # Stable re-sorts: newest first inside each status bucket, status
        # ranking untouched.
        items.sort(key=lambda item: (item.ref.record_id, item.ref.record_version))
        items.sort(key=lambda item: item.recorded_at, reverse=True)
        items.sort(key=lambda item: STATUS_RANK[item.status])
    truncated = len(items) > limit
    kept = items[:limit]

    questions: tuple[Any, ...] = ()
    if include_questions and question_access is not None:
        questions = QuestionService(store).list_current(
            question_access,
            statuses=QUESTION_STATUSES,
            limit=limit,
        )

    return EpistemicPartition(
        accepted=tuple(item for item in kept if item.status == "supported"),
        provisional=tuple(item for item in kept if item.status == "proposed"),
        contested=tuple(
            item for item in kept if item.status in ("contested", "contradicted")
        ),
        stale=tuple(item for item in kept if item.status == "stale"),
        questions=questions,
        truncated=truncated,
        state_revision=store.current_state_revision(ns),
        retracted_count=retracted_count,
    )


def _item(
    governance: Any,
    namespace: str,
    version: Any,
    body: dict[str, Any],
    status: str,
) -> EpistemicItem:
    decision: DecisionRow | None = governance.decision_for_record(
        namespace, version.record_id, version.record_version
    )
    provenance = (
        None
        if decision is None
        else Provenance(
            decision_id=decision.decision_id,
            candidate_id=decision.candidate_id,
            authority_kind=decision.authority_kind,
            impact=decision.impact,
            decided_at=decision.decided_at,
        )
    )
    ref = RecordRef(
        namespace=namespace,
        record_id=version.record_id,
        record_version=version.record_version,
    )
    evidence = tuple(dict(link["ref"]) for link in body["evidence_links"])
    return EpistemicItem(
        ref=ref,
        status=status,
        provisional=status == "proposed",
        claim=str(body["claim"]),
        claim_type=str(body["claim_type"]),
        strength=str(body["strength"]),
        scope=dict(version.scope or {}),
        recorded_at=version.recorded_at,
        evidence_refs=evidence,
        evidence_links=tuple(dict(link) for link in body["evidence_links"]),
        review_condition=str(body["review_condition"]),
        provenance=provenance,
        linkage=(
            "not_applicable" if decision is None else str(decision.evidence_linkage)
        ),
    )


__all__ = [
    "EPISTEMIC_STATUSES",
    "EpistemicAccess",
    "EpistemicItem",
    "EpistemicPartition",
    "MAX_LIMIT",
    "Provenance",
    "QUESTION_STATUSES",
    "RETRACTED_STATUS",
    "STATUS_RANK",
    "build_epistemic_partition",
    "is_expression_preference_body",
]
