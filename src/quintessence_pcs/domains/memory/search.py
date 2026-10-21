"""C group: current-version Episode retrieval (memory guide §5.1/§5.2, J.3).

The retrieval policy is deliberately narrow and honest:

* only the **current** version of each Episode record is considered
  (``records.current_version``); old versions are unreachable from here;
* a caller-named ``state_revision`` must equal the current one, otherwise
  ``HISTORICAL_SEARCH_UNSUPPORTED``.  ``None`` means "use the current value";
  the current value is never substituted for an explicit, different one;
* matching runs over **structured Episode metadata only** — the derived event
  type, source kind, derived epistemic status, the observed/recorded times and
  the source-completeness label.  Event payload text is never read, copied or
  cached.  Consequence: a word that only appears inside an event body cannot be
  found by this search (documented blind spot, see the design document);
* scope filtering follows J.2's minimal rule exactly (``scope_matches``);
* deletion owns the result set: a deleted Episode never appears, an Episode
  whose every source event is deleted never appears, and a hit contains no
  deleted body text (it contains no event body text at all).

Ranking is deterministic: ``rank_score`` is the number of (query token,
metadata field) substring matches, and ties break on ``record_id``.  An empty
query, an over-long query, a limit outside 1..20 or an unknown purpose is a
structural error, not an empty success.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ...foundation import identifiers
from ...foundation.errors import CorruptStore, InvalidInput
from ...store.domain import DomainStore, EventHeader
from ...store.records import RecordVersion
from ..errors import HistoricalSearchUnsupported
from ..refs import EventRef, RecordRef
from ..scope import normalize_scope, scope_matches
from .episode_shape import (
    EPISODE_KIND,
    EPISODE_SCHEMA_VERSION,
    EPISODE_STATUS_ACTIVE,
    EpisodeShape,
    parse_episode_body,
    verify_summary_matches,
)

#: Version label of the retrieval policy implemented here.
RETRIEVAL_POLICY_VERSION = "pcs2-007.metadata.v1"

DEFAULT_LIMIT = 5
MAX_LIMIT = 20
MAX_QUERY_LENGTH = 1000
PURPOSES = ("reasoning", "history")

#: ``source_kind`` -> ``epistemic_status``.  The first four are fixed by the
#: handoff card; ``imported_memory`` and ``system_internal`` get explicit labels
#: instead of being folded into "observation".
EPISTEMIC_STATUS_BY_SOURCE_KIND = {
    "host_observation": "observation",
    "user_report": "report",
    "model_inference": "inference",
    "synthetic_fixture": "synthetic",
    "imported_memory": "imported",
    "system_internal": "system",
}

#: Reportable exclusion reasons; only aggregate counts, never content.
_OMISSION_REASONS = (
    "redacted",
    "scope_mismatch",
    "status_not_active",
    "unsupported_schema_version",
)


@dataclass(frozen=True)
class SearchOmission:
    """Aggregate, content-free reporting of material left out of the result."""

    reason: str
    count: int


@dataclass(frozen=True)
class EpisodeHit:
    """One retrieved Episode reference plus the labels a caller may rely on."""

    ref: RecordRef
    kind: str
    epistemic_status: str
    scope: dict[str, Any]
    excerpt: str | None
    rank_score: int
    source_refs: tuple[EventRef, ...]
    observed_at: str | None


@dataclass(frozen=True)
class SearchResult:
    """Result of one current-version search."""

    hits: tuple[EpisodeHit, ...]
    truncated: bool
    omissions: tuple[SearchOmission, ...]
    state_revision: int
    limit: int
    purpose: str
    retrieval_policy_version: str


@dataclass(frozen=True)
class _LiveSource:
    """A non-deleted source event of a candidate Episode (metadata only)."""

    event_id: str
    header: EventHeader


@dataclass(frozen=True)
class _Candidate:
    rank_score: int
    version: RecordVersion
    shape: EpisodeShape
    epistemic_status: str
    sources: tuple[_LiveSource, ...]


def search_episodes(
    store: DomainStore,
    *,
    namespace: str,
    query: str,
    scope: Mapping[str, Any],
    state_revision: int | None = None,
    limit: int = DEFAULT_LIMIT,
    purpose: str = "reasoning",
) -> SearchResult:
    """Retrieve current Episodes visible to ``scope`` and matching ``query``."""

    ns = identifiers.require_namespace(namespace)
    tokens = _require_tokens(query)
    query_scope = normalize_scope(scope)
    limit = _require_limit(limit)
    purpose = _require_purpose(purpose)
    expected_revision = _require_state_revision(state_revision)

    with store.read_snapshot():
        current_revision = store.current_state_revision(ns)
        if expected_revision is not None and expected_revision != current_revision:
            raise HistoricalSearchUnsupported(
                f"search supports only the current state_revision ({current_revision}); "
                f"revision {expected_revision} is not replayable"
            )

        candidates: list[_Candidate] = []
        omissions: Counter[str] = Counter()

        for version in store.current_versions_of_kind(ns, EPISODE_KIND):
            if version.schema_version != EPISODE_SCHEMA_VERSION:
                omissions["unsupported_schema_version"] += 1
                continue
            if version.status != EPISODE_STATUS_ACTIVE:
                omissions["status_not_active"] += 1
                continue
            # A record under a deletion barrier never reaches this loop: the
            # store read excludes it in SQL (the barrier also means its payload
            # may already be cleared).  Deleted Episodes therefore produce no
            # placeholder and no count, exactly like get_current.
            record_scope = version.scope or {}
            if not scope_matches(record_scope, query_scope):
                omissions["scope_mismatch"] += 1
                continue

            sources = store.episode_sources_for_record(ns, version.record_id)
            if not sources:
                raise CorruptStore(
                    f"episode {version.record_id} has no episode_sources mapping"
                )
            shape = parse_episode_body(version)
            if {source.event_id for source in sources} != {
                ref.event_id for ref in shape.event_refs
            }:
                raise CorruptStore(
                    f"episode {version.record_id} source mapping disagrees with its body"
                )

            live: list[_LiveSource] = []
            for source in sources:
                if store.event_is_deleted(ns, source.event_id):
                    continue
                header = store.event_header(ns, source.event_id)
                if header is None:
                    raise CorruptStore(
                        f"episode {version.record_id} references missing event "
                        f"{source.event_id}"
                    )
                live.append(_LiveSource(event_id=source.event_id, header=header))
            if not live:
                # Every source event is deleted: the Episode must not appear.
                omissions["redacted"] += 1
                continue

            epistemic_status = _epistemic_status(
                tuple(item.header.source_kind for item in live)
            )
            if len(live) == 1:
                verify_summary_matches(shape, live[0].header)

            rank_score = _rank(tokens, _searchable_fields(shape, epistemic_status))
            if rank_score is None:
                continue
            candidates.append(
                _Candidate(
                    rank_score=rank_score,
                    version=version,
                    shape=shape,
                    epistemic_status=epistemic_status,
                    sources=tuple(live),
                )
            )

        candidates.sort(key=lambda item: (-item.rank_score, item.version.record_id))
        truncated = len(candidates) > limit
        hits = tuple(
            EpisodeHit(
                ref=RecordRef(
                    namespace=ns,
                    record_id=candidate.version.record_id,
                    record_version=candidate.version.record_version,
                ),
                kind=candidate.version.kind,
                epistemic_status=candidate.epistemic_status,
                scope=dict(candidate.version.scope or {}),
                # No event body text is ever returned here; an excerpt would
                # have to be fetched through the EventRef under authorization.
                excerpt=None,
                rank_score=candidate.rank_score,
                source_refs=tuple(
                    EventRef(namespace=ns, event_id=source.event_id)
                    for source in candidate.sources
                ),
                observed_at=candidate.shape.observed_at,
            )
            for candidate in candidates[:limit]
        )

        return SearchResult(
            hits=hits,
            truncated=truncated,
            omissions=tuple(
                SearchOmission(reason=reason, count=omissions[reason])
                for reason in _OMISSION_REASONS
                if omissions.get(reason)
            ),
            state_revision=current_revision,
            limit=limit,
            purpose=purpose,
            retrieval_policy_version=RETRIEVAL_POLICY_VERSION,
        )


def _epistemic_status(source_kinds: Sequence[str]) -> str:
    distinct = set(source_kinds)
    if len(distinct) == 1:
        return EPISTEMIC_STATUS_BY_SOURCE_KIND[next(iter(distinct))]
    # Only possible if a future card maps several sources to one Episode.
    return "mixed"


def _searchable_fields(shape: EpisodeShape, epistemic_status: str) -> tuple[str, ...]:
    """The exact metadata fields this search can match (documented list)."""

    return (
        shape.event_type,
        shape.source_kind,
        epistemic_status,
        shape.observed_at or "",
        shape.recorded_at,
        shape.source_completeness,
    )


def _rank(tokens: Sequence[str], fields: Sequence[str]) -> int | None:
    folded = tuple(field.casefold() for field in fields if field)
    matched_tokens = 0
    score = 0
    for token in tokens:
        token_hit = False
        for field in folded:
            if token in field:
                token_hit = True
                score += 1
        if token_hit:
            matched_tokens += 1
    if matched_tokens != len(tokens):
        return None
    return score


def _require_tokens(query: Any) -> tuple[str, ...]:
    if not isinstance(query, str):
        raise InvalidInput("query must be a string")
    if len(query) > MAX_QUERY_LENGTH:
        raise InvalidInput(f"query must be at most {MAX_QUERY_LENGTH} characters")
    stripped = query.strip()
    if not stripped:
        raise InvalidInput("query must contain at least one non-whitespace character")
    # Duplicate tokens must not inflate the rank or change AND semantics.
    return tuple(dict.fromkeys(token.casefold() for token in stripped.split()))


def _require_limit(value: Any) -> int:
    if type(value) is not int:
        raise InvalidInput("limit must be an integer")
    if value < 1 or value > MAX_LIMIT:
        raise InvalidInput(f"limit must be between 1 and {MAX_LIMIT}")
    return value


def _require_purpose(value: Any) -> str:
    if value not in PURPOSES:
        raise InvalidInput(f"purpose must be one of {', '.join(PURPOSES)}")
    return value


def _require_state_revision(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise InvalidInput("state_revision must be an integer or null")
    if value < 0:
        raise InvalidInput("state_revision must not be negative")
    if value > identifiers.MAX_SAFE_INTEGER:
        raise InvalidInput(
            "state_revision exceeds the cross-language safe integer limit"
        )
    return value


__all__ = [
    "DEFAULT_LIMIT",
    "EPISTEMIC_STATUS_BY_SOURCE_KIND",
    "EpisodeHit",
    "MAX_LIMIT",
    "MAX_QUERY_LENGTH",
    "PURPOSES",
    "RETRIEVAL_POLICY_VERSION",
    "SearchOmission",
    "SearchResult",
    "search_episodes",
]
