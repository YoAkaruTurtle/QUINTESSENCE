"""B group: build one Episode from one source event (single source, no model).

Faithfulness rules implemented here:

* the body's ``event_refs`` is the exact typed source reference;
* the summary is derived from the event *envelope* only (event type, source
  kind, observed/recorded time, generator label) and never copies payload text;
* an unknown source ``observed_at`` stays null — no time is invented;
* one source event maps to one Episode record.  The mapping is claimed through
  ``episode_sources``' primary key, **not** through a "check first, then
  create" sequence: two independent writers may both derive a body, only one
  mapping wins, and the loser re-reads the winner's mapping and returns its
  exact version.  A duplicate attempt rolls its own write back entirely, so it
  advances no ``state_revision`` either.

Cross-episode consolidation is PCS2-008/012; this card never merges events.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from ...foundation import identifiers
from ...foundation.errors import CorruptStore, EventNotFound, Redacted
from ...store.domain import (
    DomainStore,
    EpisodeSourceExists,
    require_authorization_ref,
)
from ...store.records import RecordVersion
from ..refs import EventRef
from ..scope import normalize_scope
from .episode_shape import (
    EPISODE_KIND,
    EPISODE_SCHEMA_VERSION,
    build_episode_body,
)
from .search import SearchResult, search_episodes


class _DuplicateSource(Exception):
    """Internal signal: the source key was claimed concurrently."""


class EpisodeService:
    """Episode construction and retrieval for one PCS store."""

    def __init__(self, store: DomainStore):
        self._store = store

    # ------------------------------------------------------------------ build

    def build_episode_from_event(
        self,
        *,
        namespace: str,
        event_id: str,
        scope: Mapping[str, Any],
        authorization_ref: str,
    ) -> RecordVersion:
        """Turn one stored event into one Episode version, idempotently.

        Rebuilding the same source event returns the exact version recorded in
        ``episode_sources`` (not a new one, not the current version of some
        other record).  Building from a deleted event is ``REDACTED``.
        """

        ns = identifiers.require_namespace(namespace)
        eid = identifiers.require_event_id(event_id)
        authorization = require_authorization_ref(authorization_ref)
        normalized_scope = normalize_scope(scope)
        store = self._store
        store.require_standalone("build_episode_from_event")

        try:
            with store.transaction():
                # PCS2-006 barrier: a deleted event must not become new content.
                if store.event_is_deleted(ns, eid):
                    raise Redacted(
                        f"event {eid} has been deleted; it cannot become an episode"
                    )
                header = store.event_header(ns, eid)
                if header is None:
                    raise EventNotFound(f"no event {eid} in namespace {ns!r}")
                body = build_episode_body(
                    event_ref=EventRef(namespace=ns, event_id=eid), header=header
                )
                appended = store.append_record_version(
                    namespace=ns,
                    record_id=str(uuid.uuid4()),
                    record_version=1,
                    kind=EPISODE_KIND,
                    body=body,
                    status="active",
                    schema_version=EPISODE_SCHEMA_VERSION,
                    scope=normalized_scope,
                )
                try:
                    store.insert_episode_source(
                        namespace=ns,
                        event_id=eid,
                        record_id=appended.record_id,
                        record_version=appended.record_version,
                    )
                except EpisodeSourceExists:
                    # Roll this whole attempt back and return the winner below.
                    raise _DuplicateSource() from None
                store.record_authorization(
                    namespace=ns,
                    operation="memory.episode.build",
                    target_kind="record",
                    target_id=appended.record_id,
                    target_version=appended.record_version,
                    authorization_ref=authorization,
                )
                return appended
        except _DuplicateSource:
            if store.event_is_deleted(ns, eid):
                raise Redacted(
                    f"event {eid} has been deleted; its episode remains unavailable"
                )
            existing = store.episode_source(ns, eid)
            if existing is None:
                raise CorruptStore(
                    f"source-key uniqueness fired for {eid} without a stored mapping"
                )
            if store.record_is_deleted(ns, existing.record_id):
                raise Redacted(
                    f"the episode built from {eid} has been deleted"
                )
            return store.get_version(
                ns, existing.record_id, existing.record_version
            )

    # ----------------------------------------------------------------- search

    def search_episodes(
        self,
        *,
        namespace: str,
        query: str,
        scope: Mapping[str, Any],
        state_revision: int | None = None,
        limit: int = 5,
        purpose: str = "reasoning",
    ) -> SearchResult:
        """Current-version Episode retrieval; see ``memory/search.py``."""

        return search_episodes(
            self._store,
            namespace=namespace,
            query=query,
            scope=scope,
            state_revision=state_revision,
            limit=limit,
            purpose=purpose,
        )


__all__ = ["EpisodeService"]
