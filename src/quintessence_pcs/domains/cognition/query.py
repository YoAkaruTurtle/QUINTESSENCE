"""C10: deterministic, read-only Belief and Episode queries.

The query service is a *projection*, not a new authority.  It reads one
consistent store snapshot, applies the caller's trusted-host ``QueryAccess``
and returns a :class:`QueryResult`; it never writes a record, a command, a
Context or a collaboration row.  QueryResult persistence (the
``kind="query_result"`` artifact) belongs to C11.

Three properties drive the implementation:

* **One snapshot, one revision, one clock.**  ``query`` opens a single
  ``read_snapshot`` and every read -- the revision, exact versions, current
  pointers, deletion barriers, the Episode search and the EventRef disclosure
  checks -- shares it.  ``now`` is sampled once at the entry point, so two
  expired items in the same query cannot be judged against different clocks.
* **Applicability is a projection.**  Storage status is never rewritten.  An
  expired ``supported`` Belief still says ``supported``; the item carries
  ``expired=true`` and ``applicability="expired"`` instead.  An exact read may
  return a historical version (``current=false``, ``applicability=
  "historical"``); a current query only ever returns current-pointer versions.
* **Material status is a policy state.**  ``material_status`` is derived from
  ``QueryAccess.privacy_classes`` alone: the full frozen privacy set is
  ``complete`` and any missing class is ``partial``, regardless of how many
  restricted records actually exist.  It therefore cannot be used as a probe
  for the existence or number of hidden records.  Deletions, filtered
  EventRefs and unavailable exact refs only feed the *internal* omission
  counts.

Model-visible text is limited to :meth:`QueryResult.to_body`.  That projection
drops every sensitive omission reason (``not_authorized``,
``source_ref_not_authorized``, ``source_ref_redacted``, ``redacted``,
``unavailable``), contains no event body, evidence text, ``access_ref`` or
request ID, and never contains the global ``state_revision``.  The revision is
internal snapshot metadata: exposing it would let a naturally advancing store
signal the existence or number of hidden records through an otherwise clean
body.  :meth:`QueryResult.to_artifact_body` is the separate internal form
(model-visible fields plus ``state_revision``) for C11's artifact persistence
and audit; it must never be sent to a model or copied into a model-visible
Context.  Missing, deleted, unauthorized and non-Belief exact refs all
produce the same ``unavailable`` result, so the result cannot distinguish
"does not exist" from "exists but is not visible".

Purposes are intentionally narrow.  Belief queries require ``"consult"``
(the C09 consult read authority); Episode queries require both ``"consult"``
and the requested ``"reasoning"``/``"history"`` purpose.  This is not an ACL
or a persistent authorization table: ``QueryAccess`` is a per-call value the
trusted host builds, and a failure is the existing ``SCOPE_VIOLATION`` code,
never a new contract code.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ...foundation import identifiers
from ...foundation.errors import (
    AuthorizationRequired,
    CorruptStore,
    InvalidInput,
    RecordNotFound,
    ScopeViolation,
)
from ...foundation.jsonutil import canonical_json_text
from ...store.cognition import CognitionStore
from ...store.records import RecordVersion
from ..memory.search import (
    MAX_LIMIT as MAX_QUERY_LIMIT,
    MAX_QUERY_LENGTH,
    PURPOSES as EPISODE_PURPOSES,
    search_episodes,
)
from ..refs import EventRef, RecordRef
from ..scope import normalize_scope, scope_matches
from .beliefs import (
    BELIEF_KIND,
    BELIEF_SCHEMA_VERSION,
    CLAIM_TYPES,
    parse_belief_body,
)

#: Version label carried by every result for C11 and the audit trail.
QUERY_POLICY_VERSION = "collab.v1.query.v1"

#: The purposes C10 understands.  ``access.purposes`` may carry other C09
#: labels (for example ``"review"``); only these three change query behaviour.
QUERY_PURPOSES = ("consult", "reasoning", "history")

#: The only record statuses a *current* Belief query may request.  ``retracted``
#: and the derived intermediate statuses are deliberately outside this set.
CURRENT_BELIEF_STATUSES = ("supported", "insufficient", "contradicted")

QUERY_KIND_BELIEF = "belief"
QUERY_KIND_EPISODE = "episode"
QUERY_STATUS_OK = "ok"
QUERY_STATUS_EMPTY = "empty"
QUERY_STATUS_UNAVAILABLE = "unavailable"
MATERIAL_STATUS_COMPLETE = "complete"
MATERIAL_STATUS_PARTIAL = "partial"
MATERIAL_STATUS_UNAVAILABLE = "unavailable"
APPLICABILITY_CURRENT = "current"
APPLICABILITY_EXPIRED = "expired"
APPLICABILITY_HISTORICAL = "historical"

#: The frozen privacy vocabulary; a complete policy covers exactly this set.
ALL_PRIVACY = frozenset(identifiers.PRIVACY_CLASSES)

#: Omission reasons that may appear in ``to_body()``.  These describe query
#: mechanics (scope, status, schema) and never identify an unauthorized object.
PUBLIC_OMISSION_REASONS = (
    "scope_mismatch",
    "expired",
    "status_not_current",
    "status_not_active",
    "unsupported_schema_version",
)

#: Omission reasons kept only in ``QueryResult.omissions`` for C11/audit.
INTERNAL_OMISSION_REASONS = (
    "not_authorized",
    "source_ref_not_authorized",
    "source_ref_redacted",
    "redacted",
    "unavailable",
)

#: Deterministic order used for both the internal tuple and the public subset.
OMISSION_ORDER = PUBLIC_OMISSION_REASONS + INTERNAL_OMISSION_REASONS

#: Matches the C05/C09 purpose bound; a purpose is a short host label.
MAX_PURPOSE_LENGTH = 64


# --------------------------------------------------------------------------
# request / access DTOs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryAccess:
    """Trusted-host read authority for one query.

    ``privacy_classes`` is the set of record/event privacy classes the caller
    may see.  ``purposes`` is the set of declared uses; Belief queries need
    ``"consult"`` and Episode queries also need their own purpose.  This value
    is built by trusted host code, never from model JSON, and is not an ACL or
    a persisted authorization: it only narrows one call's projection.
    """

    namespace: str
    privacy_classes: frozenset[str]
    purposes: frozenset[str]

    def __post_init__(self) -> None:
        identifiers.require_namespace(self.namespace)
        if not isinstance(self.privacy_classes, frozenset):
            raise InvalidInput("access.privacy_classes must be a frozenset")
        for privacy in self.privacy_classes:
            identifiers.require_privacy_class(privacy)
        if not isinstance(self.purposes, frozenset):
            raise InvalidInput("access.purposes must be a frozenset")
        for purpose in self.purposes:
            _require_text(purpose, "access.purposes[]", MAX_PURPOSE_LENGTH)


@dataclass(frozen=True)
class BeliefExactQuery:
    """Read one exact Belief version (current or historical)."""

    ref: RecordRef
    scope: Mapping[str, Any]


@dataclass(frozen=True)
class BeliefCurrentQuery:
    """Enumerate current Beliefs satisfying every necessary condition."""

    claim_types: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ("supported", "insufficient", "contradicted")
    scope: Mapping[str, Any] = field(default_factory=dict)
    include_expired: bool = True
    limit: int = 5


@dataclass(frozen=True)
class EpisodeQuery:
    """Reuse the existing current-Episode metadata retrieval."""

    query: str
    scope: Mapping[str, Any] = field(default_factory=dict)
    purpose: str = "reasoning"
    limit: int = 5


# --------------------------------------------------------------------------
# result DTOs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BeliefItem:
    """One authorized, applicability-projected Belief version."""

    ref: RecordRef
    claim: str
    claim_type: str
    stored_status: str
    strength: str
    scope: dict[str, Any]
    valid_until: str | None
    current: bool
    expired: bool
    applicability: str
    source_refs: tuple[RecordRef, ...]


@dataclass(frozen=True)
class EpisodeItem:
    """One current Episode hit; its EventRefs are already disclosure-filtered."""

    ref: RecordRef
    epistemic_status: str
    scope: dict[str, Any]
    observed_at: str | None
    rank_score: int
    source_refs: tuple[EventRef, ...]


@dataclass(frozen=True)
class QueryOmission:
    """Internal exact count for one exclusion reason."""

    reason: str
    count: int


@dataclass(frozen=True)
class QueryResult:
    """One deterministic query answer.

    ``material_status`` reflects the access policy only, ``omissions`` is the
    internal exact accounting and ``state_revision`` is internal snapshot
    metadata; none of them is serialized without an explicit projection.

    There are exactly two serializations:

    * :meth:`to_body` -- model-visible canonical JSON.  It never contains the
      global ``state_revision``.
    * :meth:`to_artifact_body` -- trusted internal artifact body (the same
      fields plus ``state_revision``) for C11 ``register_artifact``
      persistence and audit.  It must never be placed in a Prompt, a Context
      ``rendered_input``, an ExperienceView body or any model-visible field.
    """

    query_kind: str
    status: str
    material_status: str
    items: tuple[BeliefItem | EpisodeItem, ...]
    omissions: tuple[QueryOmission, ...]
    truncated: bool
    state_revision: int
    query_policy_version: str
    source_refs: tuple[RecordRef | EventRef, ...]

    def _projection(self, *, include_revision: bool) -> dict[str, Any]:
        """The one field set both serializations share."""

        body: dict[str, Any] = {
            "query_kind": self.query_kind,
            "status": self.status,
            "material_status": self.material_status,
            "items": [_item_to_dict(item) for item in self.items],
            "omissions": [
                {"reason": omission.reason, "count": omission.count}
                for omission in self.omissions
                if omission.reason in PUBLIC_OMISSION_REASONS
            ],
            "truncated": self.truncated,
            "query_policy_version": self.query_policy_version,
            "source_refs": [ref.to_dict() for ref in self.source_refs],
        }
        if include_revision:
            body["state_revision"] = self.state_revision
        return body

    def to_body(self) -> str:
        """Model-visible canonical projection; never contains state_revision.

        C11 may put this string into a prompt or a query projection; it must
        not add internal fields (the revision or the internal omissions).
        """

        return canonical_json_text(self._projection(include_revision=False))

    def to_artifact_body(self) -> str:
        """Trusted internal artifact body: model fields plus state_revision.

        Only C11's ``register_artifact`` persistence and audit may use this
        string.  It must never be sent to a model or copied into a
        model-visible Context or Experience field.
        """

        return canonical_json_text(self._projection(include_revision=True))


# --------------------------------------------------------------------------
# service
# --------------------------------------------------------------------------


class CognitionQueryService:
    """Read-only Belief/Episode queries for one PCS cognition store."""

    def __init__(
        self,
        store: CognitionStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock if clock is not None else _utc_now

    # ---------------------------------------------------------------- public

    def query(
        self,
        access: QueryAccess,
        request: BeliefExactQuery | BeliefCurrentQuery | EpisodeQuery,
    ) -> QueryResult:
        """Answer one query inside one read snapshot and one fixed clock."""

        _require_access(access)
        now = self._clock()
        with self._store.read_snapshot():
            return self._query_locked(access, request, now=now)

    def query_exact_belief(
        self, access: QueryAccess, query: BeliefExactQuery
    ) -> QueryResult:
        """Read one exact Belief version; see the module docstring."""

        _require_access(access)
        if not isinstance(query, BeliefExactQuery):
            raise InvalidInput("query must be a BeliefExactQuery")
        now = self._clock()
        with self._store.read_snapshot():
            return self._exact_locked(access, query, now=now)

    def query_current_beliefs(
        self, access: QueryAccess, query: BeliefCurrentQuery
    ) -> QueryResult:
        """Enumerate current Beliefs matching every necessary condition."""

        _require_access(access)
        if not isinstance(query, BeliefCurrentQuery):
            raise InvalidInput("query must be a BeliefCurrentQuery")
        now = self._clock()
        with self._store.read_snapshot():
            return self._current_locked(access, query, now=now)

    def query_episodes(
        self, access: QueryAccess, query: EpisodeQuery
    ) -> QueryResult:
        """Retrieve current Episodes with the existing metadata search."""

        _require_access(access)
        if not isinstance(query, EpisodeQuery):
            raise InvalidInput("query must be an EpisodeQuery")
        now = self._clock()
        with self._store.read_snapshot():
            return self._episodes_locked(access, query, now=now)

    # ------------------------------------------------------------ dispatcher

    def _query_locked(
        self,
        access: QueryAccess,
        request: Any,
        *,
        now: datetime,
    ) -> QueryResult:
        if isinstance(request, BeliefExactQuery):
            return self._exact_locked(access, request, now=now)
        if isinstance(request, BeliefCurrentQuery):
            return self._current_locked(access, request, now=now)
        if isinstance(request, EpisodeQuery):
            return self._episodes_locked(access, request, now=now)
        raise InvalidInput(
            "request must be a BeliefExactQuery, BeliefCurrentQuery or EpisodeQuery"
        )

    # ----------------------------------------------------------- exact Belief

    def _exact_locked(
        self, access: QueryAccess, query: BeliefExactQuery, *, now: datetime
    ) -> QueryResult:
        ref = _require_record_ref(query.ref, field="query.ref")
        if ref.namespace != access.namespace:
            raise ScopeViolation(
                f"query ref belongs to namespace {ref.namespace!r}, "
                f"not {access.namespace!r}"
            )
        query_scope = _normalize_query_scope(query.scope)
        _require_consult(access, what="Belief queries")

        ns = access.namespace
        revision = self._store.current_state_revision(ns)

        # The deletion barrier is checked before the payload read: after the
        # barrier commits, a cleanup may already have cleared the body, and a
        # deleted ref must stay "unavailable", never become "corrupt".
        if self._store.record_is_deleted(ns, ref.record_id):
            return _unavailable(revision)
        try:
            version = self._store.get_version(
                ns, ref.record_id, ref.record_version
            )
        except RecordNotFound:
            return _unavailable(revision)

        # Missing, deleted, non-Belief and unauthorized all answer with the
        # same shape.  No reason distinguishes them, in the body or the
        # internal counts.
        if version.kind != BELIEF_KIND:
            return _unavailable(revision)
        if version.privacy_class not in access.privacy_classes:
            return _unavailable(revision)

        if not scope_matches(version.scope or {}, query_scope):
            return QueryResult(
                query_kind=QUERY_KIND_BELIEF,
                status=QUERY_STATUS_EMPTY,
                material_status=_material_status(access),
                items=(),
                omissions=_omissions({"scope_mismatch": 1}),
                truncated=False,
                state_revision=revision,
                query_policy_version=QUERY_POLICY_VERSION,
                source_refs=(),
            )

        body = parse_belief_body(version)
        pointer = self._store.record_pointer(ns, ref.record_id)
        if pointer is None:  # pragma: no cover - a version exists without a record
            raise CorruptStore(
                f"record {ref.record_id} has a version but no current pointer"
            )
        current = pointer.current_version == version.record_version
        expired = _is_expired(body["valid_until"], now)
        item = _belief_item(
            version, body, current=current, expired=expired
        )
        return QueryResult(
            query_kind=QUERY_KIND_BELIEF,
            status=QUERY_STATUS_OK,
            material_status=_material_status(access),
            items=(item,),
            omissions=(),
            truncated=False,
            state_revision=revision,
            query_policy_version=QUERY_POLICY_VERSION,
            source_refs=(ref,),
        )

    # --------------------------------------------------------- current Belief

    def _current_locked(
        self,
        access: QueryAccess,
        query: BeliefCurrentQuery,
        *,
        now: datetime,
    ) -> QueryResult:
        claim_types = _require_claim_types(query.claim_types)
        statuses = _require_statuses(query.statuses)
        query_scope = _normalize_query_scope(query.scope)
        include_expired = _require_include_expired(query.include_expired)
        limit = _require_limit(query.limit)
        _require_consult(access, what="Belief queries")

        ns = access.namespace
        revision = self._store.current_state_revision(ns)
        omissions: Counter[str] = Counter()
        candidates: list[tuple[RecordVersion, dict[str, Any], bool]] = []

        for version in self._store.current_versions_of_kind(ns, BELIEF_KIND):
            if version.schema_version != BELIEF_SCHEMA_VERSION:
                omissions["unsupported_schema_version"] += 1
                continue
            if version.privacy_class not in access.privacy_classes:
                omissions["not_authorized"] += 1
                continue
            if not scope_matches(version.scope or {}, query_scope):
                omissions["scope_mismatch"] += 1
                continue

            body = parse_belief_body(version)
            if claim_types and body["claim_type"] not in claim_types:
                continue
            if version.status not in statuses:
                omissions["status_not_current"] += 1
                continue
            expired = _is_expired(body["valid_until"], now)
            if expired and not include_expired:
                omissions["expired"] += 1
                continue
            candidates.append((version, body, expired))

        # A current query never returns an older version: the read above is the
        # current-pointer join.  Sorting by record ID makes truncation stable.
        candidates.sort(key=lambda candidate: candidate[0].record_id)
        truncated = len(candidates) > limit
        items = tuple(
            _belief_item(version, body, current=True, expired=expired)
            for version, body, expired in candidates[:limit]
        )
        return QueryResult(
            query_kind=QUERY_KIND_BELIEF,
            status=QUERY_STATUS_OK if items else QUERY_STATUS_EMPTY,
            material_status=_material_status(access),
            items=items,
            omissions=_omissions(omissions),
            truncated=truncated,
            state_revision=revision,
            query_policy_version=QUERY_POLICY_VERSION,
            source_refs=_stable_source_refs(
                ref for item in items for ref in item.source_refs
            ),
        )

    # --------------------------------------------------------------- Episodes

    def _episodes_locked(
        self, access: QueryAccess, query: EpisodeQuery, *, now: datetime
    ) -> QueryResult:
        query_text = _require_query_text(query.query)
        query_scope = _normalize_query_scope(query.scope)
        purpose = _require_episode_purpose(query.purpose)
        limit = _require_limit(query.limit)
        _require_episode_access(access, purpose)

        ns = access.namespace
        revision = self._store.current_state_revision(ns)
        result = search_episodes(
            self._store,
            namespace=ns,
            query=query_text,
            scope=query_scope,
            limit=limit,
            purpose=purpose,
            privacy_classes=access.privacy_classes,
        )
        if result.state_revision != revision:
            raise CorruptStore(
                "the Episode search returned a different state_revision than "
                "the query snapshot"
            )

        omissions: Counter[str] = Counter(
            {omission.reason: omission.count for omission in result.omissions}
        )
        items: list[EpisodeItem] = []
        for hit in result.hits:
            authorized: list[EventRef] = []
            for source_ref in hit.source_refs:
                if source_ref.namespace != ns:  # pragma: no cover - search invariant
                    raise CorruptStore(
                        "an Episode search hit references another namespace"
                    )
                # Defense in depth: the search already drops deleted sources,
                # but C10 must never re-emit a deleted EventRef even if a
                # future multi-source search path returns one.
                if self._store.event_is_deleted(ns, source_ref.event_id):
                    omissions["source_ref_redacted"] += 1
                    continue
                header = self._store.event_header(ns, source_ref.event_id)
                if header is None:
                    raise CorruptStore(
                        f"episode {hit.ref.record_id} references missing event "
                        f"{source_ref.event_id}"
                    )
                if header.privacy_class not in access.privacy_classes:
                    omissions["source_ref_not_authorized"] += 1
                    continue
                authorized.append(source_ref)
            items.append(
                EpisodeItem(
                    ref=hit.ref,
                    epistemic_status=hit.epistemic_status,
                    scope=dict(hit.scope),
                    observed_at=hit.observed_at,
                    rank_score=hit.rank_score,
                    source_refs=tuple(authorized),
                )
            )

        return QueryResult(
            query_kind=QUERY_KIND_EPISODE,
            status=QUERY_STATUS_OK if items else QUERY_STATUS_EMPTY,
            material_status=_material_status(access),
            items=tuple(items),
            omissions=_omissions(omissions),
            truncated=result.truncated,
            state_revision=revision,
            query_policy_version=QUERY_POLICY_VERSION,
            source_refs=_stable_source_refs(
                ref for item in items for ref in item.source_refs
            ),
        )


# --------------------------------------------------------------------------
# validation helpers
# --------------------------------------------------------------------------


def _require_access(access: Any) -> None:
    if not isinstance(access, QueryAccess):
        raise InvalidInput("access must be a QueryAccess")


def _require_consult(access: QueryAccess, *, what: str) -> None:
    if "consult" not in access.purposes:
        raise AuthorizationRequired(
            f"{what} require the 'consult' purpose"
        )


def _require_episode_access(access: QueryAccess, purpose: str) -> None:
    if "consult" not in access.purposes:
        raise AuthorizationRequired(
            "Episode queries require the 'consult' purpose"
        )
    if purpose not in access.purposes:
        raise AuthorizationRequired(
            f"Episode queries require the requested {purpose!r} purpose"
        )


def _require_record_ref(value: Any, *, field: str) -> RecordRef:
    if not isinstance(value, RecordRef):
        raise InvalidInput(f"{field} must be a RecordRef")
    return RecordRef(
        namespace=identifiers.require_namespace(value.namespace),
        record_id=identifiers.require_record_id(value.record_id),
        record_version=identifiers.require_record_version(value.record_version),
    )


def _normalize_query_scope(value: Any) -> dict[str, Any]:
    """Normalize a query scope, reporting structural errors as INVALID_SCHEMA.

    The J.2 store helper raises ``SCOPE_VIOLATION``; C10's request DTO contract
    says every structural request error is ``InvalidInput``.  The original
    exception is chained so the diagnosis is not lost.
    """

    try:
        return normalize_scope(value)
    except ScopeViolation as exc:
        raise InvalidInput(f"scope is not a valid J.2 scope: {exc}") from exc


def _require_claim_types(value: Any) -> frozenset[str]:
    if not isinstance(value, tuple):
        raise InvalidInput("claim_types must be a tuple of claim type strings")
    selected: set[str] = set()
    for item in value:
        if not isinstance(item, str) or item not in CLAIM_TYPES:
            raise InvalidInput(
                "claim_types must use "
                + ", ".join(CLAIM_TYPES)
            )
        selected.add(item)
    return frozenset(selected)


def _require_statuses(value: Any) -> frozenset[str]:
    if not isinstance(value, tuple):
        raise InvalidInput("statuses must be a tuple of status strings")
    selected: set[str] = set()
    for item in value:
        if not isinstance(item, str) or item not in CURRENT_BELIEF_STATUSES:
            raise InvalidInput(
                "statuses must use "
                + ", ".join(CURRENT_BELIEF_STATUSES)
            )
        selected.add(item)
    return frozenset(selected)


def _require_include_expired(value: Any) -> bool:
    if type(value) is not bool:
        raise InvalidInput("include_expired must be a boolean")
    return value


def _require_limit(value: Any) -> int:
    if type(value) is not int:
        raise InvalidInput("limit must be an integer")
    if value < 1 or value > MAX_QUERY_LIMIT:
        raise InvalidInput(
            f"limit must be between 1 and {MAX_QUERY_LIMIT}"
        )
    return value


def _require_query_text(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidInput("query must be a string")
    if len(value) > MAX_QUERY_LENGTH:
        raise InvalidInput(
            f"query must be at most {MAX_QUERY_LENGTH} characters"
        )
    if not value.strip():
        raise InvalidInput("query must contain at least one non-whitespace character")
    return value


def _require_episode_purpose(value: Any) -> str:
    if value not in EPISODE_PURPOSES:
        raise InvalidInput(
            "purpose must be one of " + ", ".join(EPISODE_PURPOSES)
        )
    return value


def _require_text(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(f"{field} must be a non-empty string")
    if len(value) > max_length:
        raise InvalidInput(f"{field} must be at most {max_length} characters")
    return value


# --------------------------------------------------------------------------
# projection helpers
# --------------------------------------------------------------------------


def _material_status(access: QueryAccess) -> str:
    """Policy completeness: only the access set matters, never the data."""

    if access.privacy_classes == ALL_PRIVACY:
        return MATERIAL_STATUS_COMPLETE
    return MATERIAL_STATUS_PARTIAL


def _unavailable(revision: int) -> QueryResult:
    """The single indistinguishable answer for an unusable exact ref."""

    return QueryResult(
        query_kind=QUERY_KIND_BELIEF,
        status=QUERY_STATUS_UNAVAILABLE,
        material_status=MATERIAL_STATUS_UNAVAILABLE,
        items=(),
        omissions=(QueryOmission(reason="unavailable", count=1),),
        truncated=False,
        state_revision=revision,
        query_policy_version=QUERY_POLICY_VERSION,
        source_refs=(),
    )


def _belief_item(
    version: RecordVersion,
    body: Mapping[str, Any],
    *,
    current: bool,
    expired: bool,
) -> BeliefItem:
    if not current:
        applicability = APPLICABILITY_HISTORICAL
    elif expired:
        applicability = APPLICABILITY_EXPIRED
    else:
        applicability = APPLICABILITY_CURRENT
    ref = RecordRef(
        namespace=version.namespace,
        record_id=version.record_id,
        record_version=version.record_version,
    )
    return BeliefItem(
        ref=ref,
        claim=body["claim"],
        claim_type=body["claim_type"],
        stored_status=version.status,
        strength=body["strength"],
        scope=dict(version.scope or {}),
        valid_until=body["valid_until"],
        current=current,
        expired=expired,
        applicability=applicability,
        source_refs=(ref,),
    )


def _omissions(counts: Mapping[str, int]) -> tuple[QueryOmission, ...]:
    known = [reason for reason in OMISSION_ORDER if counts.get(reason)]
    extra = sorted(
        reason for reason in counts if reason not in OMISSION_ORDER and counts[reason]
    )
    return tuple(
        QueryOmission(reason=reason, count=counts[reason])
        for reason in known + extra
    )


def _stable_source_refs(
    refs: Iterable[RecordRef | EventRef],
) -> tuple[RecordRef | EventRef, ...]:
    """Deduplicate by canonical wire form and sort deterministically."""

    unique: dict[str, RecordRef | EventRef] = {}
    for ref in refs:
        unique.setdefault(canonical_json_text(ref.to_dict()), ref)
    ordered = sorted(
        unique.values(),
        key=lambda ref: (ref.ref_type, canonical_json_text(ref.to_dict())),
    )
    return tuple(ordered)


def _item_to_dict(item: BeliefItem | EpisodeItem) -> dict[str, Any]:
    if isinstance(item, BeliefItem):
        return {
            "ref": item.ref.to_dict(),
            "claim": item.claim,
            "claim_type": item.claim_type,
            "stored_status": item.stored_status,
            "strength": item.strength,
            "scope": dict(item.scope),
            "valid_until": item.valid_until,
            "current": item.current,
            "expired": item.expired,
            "applicability": item.applicability,
            "source_refs": [ref.to_dict() for ref in item.source_refs],
        }
    return {
        "ref": item.ref.to_dict(),
        "epistemic_status": item.epistemic_status,
        "scope": dict(item.scope),
        "observed_at": item.observed_at,
        "rank_score": item.rank_score,
        "source_refs": [ref.to_dict() for ref in item.source_refs],
    }


def _is_expired(valid_until: str | None, now: datetime) -> bool:
    if valid_until is None:
        return False
    return _parse_timestamp(valid_until) <= now.astimezone(timezone.utc)


def _parse_timestamp(text: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:  # pragma: no cover - stored bodies are validated
        raise CorruptStore(
            "a stored Belief valid_until is not an RFC 3339 timestamp"
        ) from exc
    if parsed.tzinfo is None:  # pragma: no cover - stored bodies are validated
        raise CorruptStore("a stored Belief valid_until has no UTC offset")
    return parsed.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "ALL_PRIVACY",
    "APPLICABILITY_CURRENT",
    "APPLICABILITY_EXPIRED",
    "APPLICABILITY_HISTORICAL",
    "BeliefCurrentQuery",
    "BeliefExactQuery",
    "BeliefItem",
    "CURRENT_BELIEF_STATUSES",
    "CognitionQueryService",
    "EpisodeItem",
    "EpisodeQuery",
    "INTERNAL_OMISSION_REASONS",
    "MATERIAL_STATUS_COMPLETE",
    "MATERIAL_STATUS_PARTIAL",
    "MATERIAL_STATUS_UNAVAILABLE",
    "OMISSION_ORDER",
    "PUBLIC_OMISSION_REASONS",
    "QUERY_KIND_BELIEF",
    "QUERY_KIND_EPISODE",
    "QUERY_POLICY_VERSION",
    "QUERY_PURPOSES",
    "QUERY_STATUS_EMPTY",
    "QUERY_STATUS_OK",
    "QUERY_STATUS_UNAVAILABLE",
    "QueryAccess",
    "QueryOmission",
    "QueryResult",
]
