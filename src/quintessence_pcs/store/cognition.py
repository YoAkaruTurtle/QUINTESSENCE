"""PCS2-008 store surface: full Context snapshots and work completion.

Two things are added on top of the PCS2-007 domain plane, without changing any
004/005/006/007 behaviour:

* **Context accounting.**  A PCS2-008 Context is one row of the existing
  ``context_snapshots`` table carrying every J.5 field, plus a row per exact
  source in ``context_sources`` and a controlled payload holding the exact
  rendered input.  Writing it does not advance ``state_revision`` (J.7:
  Context archival is not a domain commit).  A PCS2-006 snapshot (new columns
  all NULL) is refused with ``NEEDS_CONTEXT`` instead of being interpreted.
* **Work completion.**  ``work_progress`` records that one ``pending_work``
  row is finished.  ``pending_work`` itself is never updated (0002's frozen
  ``CHECK (status = 'pending')``).  ``TxScope.complete_work`` writes this row
  inside the same transaction as the domain append, which is what closes the
  "commit then record progress" crash window.

Deletion is extended in one direction only: after a target's own payloads are
cleared, ``_after_target_cleared_locked`` finds Contexts whose
``context_sources`` mention that target and clears their rendered payload.  The
``content_hash`` column is retained; the original text is not recoverable.

As with every store entry point in this project, this is an API boundary, not
a security boundary (``docs/PCS2-006-TXB.md`` section 4.11).
"""

from __future__ import annotations

import secrets
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..foundation import identifiers
from ..foundation.errors import (
    CorruptStore,
    InvalidInput,
    RecordNotFound,
    Redacted,
    ScopeViolation,
    SnapshotNotFound,
)
from ..foundation.jsonutil import (
    canonical_json_text,
    new_payload_ref,
    sha256_hex,
    strict_json_loads,
)
from ..foundation.timeutil import utc_now_rfc3339
from .collaboration import CollaborationMixin
from .deletion import DELETION_EVENT, DELETION_RECORD, DeletionTarget
from .domain import DomainStore
from .errors import ContextTooLarge, NeedsContext, WorkAlreadyCompleted
from .events import PendingWork
from .model import ModelLedgerMixin

WORK_OUTCOME_COMMITTED = "committed"
WORK_OUTCOME_CHECKED_NO_BELIEF = "checked_no_belief"
#: PCS2-008-R1: a source event was deleted after Tx A created its work item and
#: before derivation.  The work is terminal; it never had a Context.
WORK_OUTCOME_SKIPPED = "skipped"
WORK_SKIP_REASON_SOURCE_REDACTED = "source_redacted"
WORK_OUTCOMES = (
    WORK_OUTCOME_COMMITTED,
    WORK_OUTCOME_CHECKED_NO_BELIEF,
    WORK_OUTCOME_SKIPPED,
)
#: Outcomes a normal Tx B ``TxScope.complete_work`` may write.
TX_WORK_OUTCOMES = (WORK_OUTCOME_COMMITTED, WORK_OUTCOME_CHECKED_NO_BELIEF)

CONTEXT_ADAPTER_FAKE_MODEL = "fake-model"
#: PCS2-009: the direct HTTP adapter also persists the exact request bytes for
#: every attempt, which is what ``model_input_complete`` asserts.  Adding a new
#: adapter here is a semantic claim, not just a label.
CONTEXT_ADAPTER_DIRECT_HTTP = "direct-http"
CONTEXT_ADAPTERS = (CONTEXT_ADAPTER_FAKE_MODEL, CONTEXT_ADAPTER_DIRECT_HTTP)

#: Byte budget for one rendered Context (persona guide / J.5 default brief).
MAX_CONTEXT_BYTES = 24 * 1024

CONTEXT_REF_KIND_EVENT = "event"
CONTEXT_REF_KIND_RECORD = "record"
CONTEXT_REF_KINDS = (CONTEXT_REF_KIND_EVENT, CONTEXT_REF_KIND_RECORD)

#: Columns that make a context_snapshots row a PCS2-008 Context.  Every one of
#: them is written by this card; a NULL therefore means "written before
#: PCS2-008", never "empty list" or "not applicable".  ``adapter_kind`` is not
#: in this list on purpose: a context assembled without a model call legitimately
#: leaves it NULL (C1/C3).
_CONTEXT_REQUIRED_COLUMNS = (
    "run_id",
    "profile_ref_json",
    "read_set_json",
    "memory_hits_json",
    "belief_refs_json",
    "question_refs_json",
    "focus_refs_json",
    "unknown_execution_ids_json",
    "permission_view_ref",
    "budget_view_json",
    "pending_source_event_refs_json",
    "omissions_json",
    "index_generation",
    "index_coverage_json",
    "content_hash",
    "model_input_complete",
    "rendered_input_payload_ref",
)

_CONTEXT_COLUMNS = (
    "context_id, namespace, state_revision, context_event_seq, privacy_epoch, created_at, "
    "run_id, trigger_ref_json, profile_ref_json, read_set_json, memory_hits_json, "
    "belief_refs_json, question_refs_json, focus_refs_json, "
    "unknown_execution_ids_json, permission_view_ref, budget_view_json, "
    "pending_source_event_refs_json, omissions_json, index_generation, "
    "index_coverage_json, content_hash, model_input_complete, "
    "rendered_input_payload_ref, adapter_kind, context_kind"
)

_SOURCE_COLUMNS = "context_id, ref_kind, ref_id, ref_version"

_WORK_COLUMNS = (
    "work_id, namespace, outcome, context_id, completed_state_revision, reason, updated_at"
)


@dataclass(frozen=True)
class ContextSourceInput:
    """One exact source a Context used, as written by the builder."""

    ref_kind: str
    ref_id: str
    ref_version: int = 0


@dataclass(frozen=True)
class ContextSource:
    """One stored ``context_sources`` row."""

    context_id: str
    ref_kind: str
    ref_id: str
    ref_version: int


@dataclass(frozen=True)
class ContextInsert:
    """Everything needed to persist one full PCS2-008 Context.

    The rendered input is passed as text; the store allocates a fresh opaque
    payload row for it (no content-addressed de-duplication, so clearing one
    Context's payload cannot silently redact another Context with identical
    text).  ``content_hash`` must equal ``sha256:<hex>`` of that exact text.
    """

    namespace: str
    run_id: str
    trigger_ref: Mapping[str, Any]
    state_revision: int
    context_event_seq: int
    privacy_epoch: int
    profile_ref: Mapping[str, Any]
    read_set: Sequence[Mapping[str, Any]]
    memory_hits: Sequence[Mapping[str, Any]]
    belief_refs: Sequence[Mapping[str, Any]]
    question_refs: Sequence[Mapping[str, Any]]
    focus_refs: Sequence[Mapping[str, Any]]
    unknown_execution_ids: Sequence[str]
    permission_view_ref: str
    budget_view: Mapping[str, Any]
    pending_source_event_refs: Sequence[Mapping[str, Any]]
    omissions: Sequence[Mapping[str, Any]]
    index_generation: int
    index_coverage: Mapping[str, Any]
    content_hash: str
    model_input_complete: bool
    rendered_input: str
    adapter_kind: str | None
    sources: Sequence[ContextSourceInput]


@dataclass(frozen=True)
class ContextSnapshot:
    """One stored, verified PCS2-008 Context (raw JSON fields decoded)."""

    context_id: str
    namespace: str
    run_id: str
    trigger_ref: dict[str, Any]
    state_revision: int
    context_event_seq: int
    privacy_epoch: int
    profile_ref: dict[str, Any]
    read_set: tuple[dict[str, Any], ...]
    memory_hits: tuple[dict[str, Any], ...]
    belief_refs: tuple[dict[str, Any], ...]
    question_refs: tuple[dict[str, Any], ...]
    focus_refs: tuple[dict[str, Any], ...]
    unknown_execution_ids: tuple[str, ...]
    permission_view_ref: str
    budget_view: dict[str, Any]
    pending_source_event_refs: tuple[dict[str, Any], ...]
    omissions: tuple[dict[str, Any], ...]
    index_generation: int
    index_coverage: dict[str, Any]
    content_hash: str
    model_input_complete: bool
    rendered_input_payload_ref: str
    adapter_kind: str | None
    rendered_input: str
    created_at: str
    sources: tuple[ContextSource, ...] = ()
    context_kind: str = "review"


@dataclass(frozen=True)
class WorkProgress:
    """Completion of one ``pending_work`` row.

    ``skipped`` rows have no Context (``context_id is None``) and name the
    deletion cause in ``reason``; normal completions always have a Context and
    ``reason is None``.
    """

    work_id: str
    namespace: str
    outcome: str
    context_id: str | None
    completed_state_revision: int
    updated_at: str
    reason: str | None = None


@dataclass(frozen=True)
class ReconcileReport:
    """Result of one deterministic recovery of redacted pending work."""

    skipped: tuple[WorkProgress, ...]

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)


class ContextStateMixin:
    """Context snapshots, exact source accounting and work completion."""

    # ------------------------------------------------------------- watermarks

    def context_watermarks(self, namespace: str) -> dict[str, int]:
        """Read the three watermarks inside the caller's transaction/handle."""

        ns = identifiers.require_namespace(namespace)
        return self._watermarks_locked(ns)

    # -------------------------------------------------------------- payloads

    def store_context_payload_locked(self, rendered_input: str) -> str:
        """Insert one controlled payload row holding the exact rendered text.

        The row is deliberately **not** de-duplicated: two Contexts with
        identical rendered text must be independently clearable when one of
        them references a deleted source.  Content addressing would make the
        deletion barrier leak into an unrelated Context.
        """

        if self._tx_depth == 0:
            raise RuntimeError(
                "store_context_payload_locked must run inside the caller's transaction"
            )
        if not isinstance(rendered_input, str):
            raise InvalidInput("rendered_input must be a string")
        body_text = canonical_json_text({"rendered_input": rendered_input})
        payload_ref = new_payload_ref()
        self._require_connection().execute(
            "INSERT INTO private_payloads (payload_ref, sha256, body_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (payload_ref, sha256_hex(body_text), body_text, utc_now_rfc3339()),
        )
        return payload_ref

    # ---------------------------------------------------------------- insert

    def insert_context_snapshot_locked(
        self, entry: ContextInsert, *, context_kind: str = "review"
    ) -> ContextSnapshot:
        """Persist one full Context; caller supplies the surrounding transaction.

        The snapshot row, its source rows and the controlled payload all commit
        (or roll back) together.  ``state_revision`` is never touched.
        """

        if self._tx_depth == 0:
            raise RuntimeError(
                "insert_context_snapshot_locked must run inside the caller's transaction"
            )
        ns = identifiers.require_namespace(entry.namespace)
        run_id = _require_bounded_text(entry.run_id, "run_id", 256)
        if context_kind not in ("review", "consult"):
            raise InvalidInput("context_kind must be 'review' or 'consult'")
        raw_trigger = entry.trigger_ref
        if raw_trigger is None or (
            isinstance(raw_trigger, Mapping) and not raw_trigger
        ):
            trigger_ref = {}
            trigger_json: str | None = None
        else:
            trigger_ref = _require_plain_object(raw_trigger, "trigger_ref")
            trigger_json = canonical_json_text(trigger_ref)
        if context_kind == "consult":
            if trigger_json is not None:
                raise InvalidInput("a consult Context has no trigger")
        elif trigger_json is None:
            raise InvalidInput("a complete review Context requires a trigger")
        profile_ref = _require_plain_object(entry.profile_ref, "profile_ref")
        read_set = _require_object_list(entry.read_set, "read_set")
        memory_hits = _require_object_list(entry.memory_hits, "memory_hits")
        belief_refs = _require_object_list(entry.belief_refs, "belief_refs")
        question_refs = _require_object_list(entry.question_refs, "question_refs")
        focus_refs = _require_object_list(entry.focus_refs, "focus_refs")
        unknown_execution_ids = _require_text_list(
            entry.unknown_execution_ids, "unknown_execution_ids", 256
        )
        permission_view_ref = _require_bounded_text(
            entry.permission_view_ref, "permission_view_ref", 512
        )
        budget_view = _require_plain_object(entry.budget_view, "budget_view")
        pending_sources = _require_object_list(
            entry.pending_source_event_refs, "pending_source_event_refs"
        )
        omissions = _require_object_list(entry.omissions, "omissions")
        index_generation = _require_non_negative_int(
            entry.index_generation, "index_generation"
        )
        index_coverage = _require_plain_object(entry.index_coverage, "index_coverage")
        content_hash = _require_content_hash(entry.content_hash)
        model_input_complete = _require_bool(
            entry.model_input_complete, "model_input_complete"
        )
        rendered_input = entry.rendered_input
        if not isinstance(rendered_input, str):
            raise InvalidInput("rendered_input must be a string")
        if content_hash != context_content_hash(rendered_input):
            raise InvalidInput(
                "content_hash does not match the exact rendered input that was supplied"
            )
        adapter_kind = entry.adapter_kind
        if adapter_kind is not None:
            adapter_kind = _require_bounded_text(adapter_kind, "adapter_kind", 64)
        # C3/009: model_input_complete may only be true for an adapter that
        # persists the exact request bytes it sends.  Both the FakeModel path
        # and the direct HTTP path do; the raw payload is always stored below.
        if model_input_complete != (adapter_kind in CONTEXT_ADAPTERS):
            raise InvalidInput(
                "model_input_complete is only true for a known full-input adapter"
            )

        current = self._watermarks_locked(ns)
        if (
            entry.state_revision != current["state_revision"]
            or entry.context_event_seq != current["last_event_seq"]
            or entry.privacy_epoch != current["privacy_epoch"]
        ):
            raise CorruptStore(
                "context watermarks changed inside the transaction that rendered the context"
            )

        sources = _normalize_sources(entry.sources)
        rendered_bytes = len(rendered_input.encode("utf-8"))
        if rendered_bytes > MAX_CONTEXT_BYTES:
            raise ContextTooLarge(
                f"rendered context is {rendered_bytes} bytes; the budget is "
                f"{MAX_CONTEXT_BYTES}"
            )

        payload_ref = self.store_context_payload_locked(rendered_input)
        context_id = str(uuid.uuid4())
        created_at = utc_now_rfc3339()
        stored_sources = tuple(
            ContextSource(
                context_id=context_id,
                ref_kind=source.ref_kind,
                ref_id=source.ref_id,
                ref_version=source.ref_version,
            )
            for source in sources
        )
        self._require_connection().execute(
            "INSERT INTO context_snapshots ("
            "context_id, namespace, state_revision, context_event_seq, privacy_epoch, "
            "created_at, run_id, trigger_ref_json, profile_ref_json, read_set_json, "
            "memory_hits_json, belief_refs_json, question_refs_json, focus_refs_json, "
            "unknown_execution_ids_json, permission_view_ref, budget_view_json, "
            "pending_source_event_refs_json, omissions_json, index_generation, "
            "index_coverage_json, content_hash, model_input_complete, "
            "rendered_input_payload_ref, adapter_kind, context_kind"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                context_id,
                ns,
                entry.state_revision,
                entry.context_event_seq,
                entry.privacy_epoch,
                created_at,
                run_id,
                trigger_json,
                canonical_json_text(profile_ref),
                canonical_json_text(read_set),
                canonical_json_text(memory_hits),
                canonical_json_text(belief_refs),
                canonical_json_text(question_refs),
                canonical_json_text(focus_refs),
                canonical_json_text(unknown_execution_ids),
                permission_view_ref,
                canonical_json_text(budget_view),
                canonical_json_text(pending_sources),
                canonical_json_text(omissions),
                index_generation,
                canonical_json_text(index_coverage),
                content_hash,
                1 if model_input_complete else 0,
                payload_ref,
                adapter_kind,
                context_kind,
            ),
        )
        for source in stored_sources:
            self._require_connection().execute(
                "INSERT INTO context_sources (context_id, ref_kind, ref_id, ref_version) "
                "VALUES (?, ?, ?, ?)",
                (source.context_id, source.ref_kind, source.ref_id, source.ref_version),
            )
        return ContextSnapshot(
            context_id=context_id,
            namespace=ns,
            run_id=run_id,
            trigger_ref=trigger_ref,
            state_revision=entry.state_revision,
            context_event_seq=entry.context_event_seq,
            privacy_epoch=entry.privacy_epoch,
            profile_ref=profile_ref,
            read_set=tuple(read_set),
            memory_hits=tuple(memory_hits),
            belief_refs=tuple(belief_refs),
            question_refs=tuple(question_refs),
            focus_refs=tuple(focus_refs),
            unknown_execution_ids=tuple(unknown_execution_ids),
            permission_view_ref=permission_view_ref,
            budget_view=budget_view,
            pending_source_event_refs=tuple(pending_sources),
            omissions=tuple(omissions),
            index_generation=index_generation,
            index_coverage=index_coverage,
            content_hash=content_hash,
            model_input_complete=model_input_complete,
            rendered_input_payload_ref=payload_ref,
            adapter_kind=adapter_kind,
            rendered_input=rendered_input,
            created_at=created_at,
            sources=stored_sources,
            context_kind=context_kind,
        )

    # ------------------------------------------------------------------ read

    def load_context_snapshot(self, context_id: str) -> ContextSnapshot:
        """Read back one full Context and re-verify its rendered input.

        Failure modes are deliberate:

        * unknown ID -> ``NOT_FOUND``;
        * a 006-era row (any required new column NULL) -> ``NEEDS_CONTEXT``;
        * a cleared rendered payload -> ``REDACTED`` (the hash stays in the row);
        * a missing payload, digest mismatch, non-canonical bytes or hash
          mismatch -> ``CORRUPT_STORE``.
        """

        cid = identifiers.require_context_id(context_id)
        row = self._require_connection().execute(
            f"SELECT {_CONTEXT_COLUMNS} FROM context_snapshots WHERE context_id = ?",
            (cid,),
        ).fetchone()
        if row is None:
            raise SnapshotNotFound(f"no persisted context {cid!r}")
        missing = [name for name in _CONTEXT_REQUIRED_COLUMNS if row[name] is None]
        if missing:
            raise NeedsContext(
                f"context {cid} predates PCS2-008 and lacks "
                f"{', '.join(missing)}; it cannot be used as a Context"
            )
        context_kind = row["context_kind"]
        if context_kind not in ("review", "consult"):
            raise CorruptStore(f"context {cid} has an unknown context_kind")
        if context_kind == "review" and row["trigger_ref_json"] is None:
            raise CorruptStore(f"context {cid} is a review Context without a trigger")
        if context_kind == "consult" and row["trigger_ref_json"] is not None:
            raise CorruptStore(f"context {cid} is a consult Context with a trigger")

        payload_ref = row["rendered_input_payload_ref"]
        payload_row = self._require_connection().execute(
            "SELECT sha256, body_json FROM private_payloads WHERE payload_ref = ?",
            (payload_ref,),
        ).fetchone()
        if payload_row is None:
            raise CorruptStore(
                f"context {cid} references missing payload {payload_ref}"
            )
        if payload_row["body_json"] == "":
            raise Redacted(
                f"the rendered input of context {cid} has been cleared by a deletion"
            )
        _digest, payload, _payload_text = self._load_payload(
            payload_ref, owner=f"context {cid}"
        )
        if set(payload) != {"rendered_input"}:
            raise CorruptStore(
                f"context payload {payload_ref} has an unexpected shape"
            )
        rendered_input = payload["rendered_input"]
        if not isinstance(rendered_input, str):
            raise CorruptStore(
                f"context payload {payload_ref} does not hold a rendered text"
            )
        expected_hash = context_content_hash(rendered_input)
        if row["content_hash"] != expected_hash:
            raise CorruptStore(
                f"context {cid} content_hash does not match its rendered input"
            )

        document = strict_json_loads(rendered_input)
        if not isinstance(document, dict):
            raise CorruptStore(f"context {cid} rendered input is not a JSON object")
        if document.get("run_id") != row["run_id"]:
            raise CorruptStore(f"context {cid} rendered input names another run")
        if document.get("namespace") != row["namespace"]:
            raise CorruptStore(f"context {cid} rendered input names another namespace")

        trigger_ref = (
            {}
            if context_kind == "consult"
            else _decode_object(row["trigger_ref_json"], "trigger_ref_json", cid)
        )
        profile_ref = _decode_object(row["profile_ref_json"], "profile_ref_json", cid)
        read_set = _decode_object_list(row["read_set_json"], "read_set_json", cid)
        memory_hits = _decode_object_list(row["memory_hits_json"], "memory_hits_json", cid)
        belief_refs = _decode_object_list(row["belief_refs_json"], "belief_refs_json", cid)
        question_refs = _decode_object_list(
            row["question_refs_json"], "question_refs_json", cid
        )
        focus_refs = _decode_object_list(row["focus_refs_json"], "focus_refs_json", cid)
        unknown_execution_ids = _decode_text_list(
            row["unknown_execution_ids_json"], "unknown_execution_ids_json", cid
        )
        budget_view = _decode_object(row["budget_view_json"], "budget_view_json", cid)
        pending_sources = _decode_object_list(
            row["pending_source_event_refs_json"], "pending_source_event_refs_json", cid
        )
        omissions = _decode_object_list(row["omissions_json"], "omissions_json", cid)
        index_coverage = _decode_object(
            row["index_coverage_json"], "index_coverage_json", cid
        )
        model_input_complete = bool(int(row["model_input_complete"]))
        adapter_kind = row["adapter_kind"]
        if model_input_complete and adapter_kind not in CONTEXT_ADAPTERS:
            raise CorruptStore(
                f"context {cid} claims complete model input without a known adapter"
            )
        content_hash = _require_content_hash(row["content_hash"])
        index_generation = _require_non_negative_int(
            row["index_generation"], "index_generation"
        )
        permission_view_ref = _require_bounded_text(
            row["permission_view_ref"], "permission_view_ref", 512
        )

        return ContextSnapshot(
            context_id=row["context_id"],
            namespace=row["namespace"],
            run_id=row["run_id"],
            trigger_ref=trigger_ref,
            state_revision=int(row["state_revision"]),
            context_event_seq=int(row["context_event_seq"]),
            privacy_epoch=int(row["privacy_epoch"]),
            profile_ref=profile_ref,
            read_set=tuple(read_set),
            memory_hits=tuple(memory_hits),
            belief_refs=tuple(belief_refs),
            question_refs=tuple(question_refs),
            focus_refs=tuple(focus_refs),
            unknown_execution_ids=tuple(unknown_execution_ids),
            permission_view_ref=permission_view_ref,
            budget_view=budget_view,
            pending_source_event_refs=tuple(pending_sources),
            omissions=tuple(omissions),
            index_generation=index_generation,
            index_coverage=index_coverage,
            content_hash=content_hash,
            model_input_complete=model_input_complete,
            rendered_input_payload_ref=payload_ref,
            adapter_kind=adapter_kind,
            rendered_input=rendered_input,
            created_at=row["created_at"],
            sources=self.context_sources(cid),
            context_kind=context_kind,
        )

    def context_sources(self, context_id: str) -> tuple[ContextSource, ...]:
        """Every exact source recorded for one Context, in stable order."""

        cid = identifiers.require_context_id(context_id)
        rows = self._require_connection().execute(
            f"SELECT {_SOURCE_COLUMNS} FROM context_sources "
            "WHERE context_id = ? ORDER BY ref_kind, ref_id, ref_version",
            (cid,),
        ).fetchall()
        return tuple(
            ContextSource(
                context_id=row["context_id"],
                ref_kind=row["ref_kind"],
                ref_id=row["ref_id"],
                ref_version=int(row["ref_version"]),
            )
            for row in rows
        )

    # ------------------------------------------------------------------ work

    def unfinished_work(self, namespace: str) -> tuple[PendingWork, ...]:
        """Pending work that is still processable.

        A work item whose source event is inside a committed deletion barrier is
        excluded even if a previous process (or a pre-R1 database) has not
        written its ``skipped`` row yet.  That is a defensive read: the durable
        terminal state is ``work_progress(outcome='skipped')``; this filter
        only ensures the loop cannot block on a deleted source in the window
        before reconciliation.
        """

        ns = identifiers.require_namespace(namespace)
        rows = self._require_connection().execute(
            "SELECT p.work_id, p.namespace, p.kind, p.event_id, p.source_event_id, "
            "p.status, p.created_at "
            "FROM pending_work p "
            "LEFT JOIN work_progress w ON w.work_id = p.work_id "
            "WHERE p.namespace = ? AND p.status = 'pending' AND w.work_id IS NULL "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM deletion_targets dt "
            "    WHERE dt.target_kind = 'event' AND dt.namespace = p.namespace "
            "      AND dt.target_id = p.event_id"
            "  ) "
            "ORDER BY p.created_at, p.work_id",
            (ns,),
        ).fetchall()
        return tuple(_pending_work_from_row(row) for row in rows)

    def unfinished_work_for_event(
        self, namespace: str, event_id: str
    ) -> tuple[PendingWork, ...]:
        """Processable pending-work rows created by one event."""

        ns = identifiers.require_namespace(namespace)
        eid = identifiers.require_event_id(event_id)
        rows = self._require_connection().execute(
            "SELECT p.work_id, p.namespace, p.kind, p.event_id, p.source_event_id, "
            "p.status, p.created_at "
            "FROM pending_work p "
            "LEFT JOIN work_progress w ON w.work_id = p.work_id "
            "WHERE p.namespace = ? AND p.event_id = ? AND p.status = 'pending' "
            "  AND w.work_id IS NULL "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM deletion_targets dt "
            "    WHERE dt.target_kind = 'event' AND dt.namespace = p.namespace "
            "      AND dt.target_id = p.event_id"
            "  ) "
            "ORDER BY p.created_at, p.work_id",
            (ns, eid),
        ).fetchall()
        return tuple(_pending_work_from_row(row) for row in rows)

    def redacted_source_omissions(
        self, namespace: str, *, candidate_event_ids: Sequence[str]
    ) -> tuple[PendingWork, ...]:
        """Deleted sources from **this round's** candidate set, not all history.

        R1.1 boundary: ``work_progress(outcome='skipped')`` remains the durable
        audit record, but a Context only reports a ``source_redacted`` omission
        when the deleted source is in the candidate range the builder derived
        from this round's trusted trigger, current pending batch or explicitly
        validated source dependencies (causation refs).  Historical skipped work
        that is unrelated to this round is neither enumerated nor rendered, so
        it cannot consume the 24 KiB budget of an unrelated new event.

        Only the event reference is returned; the Context renders
        ``{ref, reason: "source_redacted"}`` and never any event body.
        """

        ns = identifiers.require_namespace(namespace)
        if isinstance(candidate_event_ids, (str, bytes)) or not isinstance(
            candidate_event_ids, Sequence
        ):
            raise InvalidInput(
                "candidate_event_ids must be a sequence of event IDs"
            )
        ids: list[str] = []
        seen_ids: set[str] = set()
        for value in candidate_event_ids:
            event_id = identifiers.require_event_id(value)
            if event_id in seen_ids:
                continue
            seen_ids.add(event_id)
            ids.append(event_id)
        if not ids:
            return ()

        rows: list[Any] = []
        # Keep the SQL variable count below SQLite's usual 999 limit.  The
        # candidate set is bounded by this round's batch and its explicit
        # causation edges, but a large batch is still chunked.
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            placeholders = ", ".join("?" for _ in chunk)
            rows.extend(
                self._require_connection()
                .execute(
                    "SELECT p.work_id, p.namespace, p.kind, p.event_id, "
                    "p.source_event_id, p.status, p.created_at "
                    "FROM pending_work p "
                    "JOIN deletion_targets dt "
                    "  ON dt.target_kind = 'event' AND dt.namespace = p.namespace "
                    " AND dt.target_id = p.event_id "
                    "LEFT JOIN work_progress w ON w.work_id = p.work_id "
                    "WHERE p.namespace = ? AND p.status = 'pending' "
                    f"  AND p.event_id IN ({placeholders}) "
                    "  AND (w.work_id IS NULL OR w.outcome = 'skipped') "
                    "ORDER BY p.created_at, p.work_id",
                    (ns, *chunk),
                )
                .fetchall()
            )
        by_work: dict[str, PendingWork] = {}
        for row in rows:
            work = _pending_work_from_row(row)
            by_work.setdefault(work.work_id, work)
        return tuple(
            sorted(by_work.values(), key=lambda work: (work.created_at, work.work_id))
        )

    def _pending_work_without_progress_locked(
        self, namespace: str, event_id: str
    ) -> tuple[PendingWork, ...]:
        """Every unfinished work row of an event, including a deleted one.

        This is the Tx1 deletion hook's read.  It must not use the defensive
        deletion filter of ``unfinished_work_for_event``: at hook time the
        barrier row already exists and the whole point is to find the work that
        must be marked ``skipped``.
        """

        rows = self._require_connection().execute(
            "SELECT p.work_id, p.namespace, p.kind, p.event_id, p.source_event_id, "
            "p.status, p.created_at "
            "FROM pending_work p "
            "LEFT JOIN work_progress w ON w.work_id = p.work_id "
            "WHERE p.namespace = ? AND p.event_id = ? AND p.status = 'pending' "
            "  AND w.work_id IS NULL "
            "ORDER BY p.created_at, p.work_id",
            (namespace, event_id),
        ).fetchall()
        return tuple(_pending_work_from_row(row) for row in rows)

    def work_progress(self, work_id: str) -> WorkProgress | None:
        """Completion row for one work item, if any."""

        wid = _require_uuid(work_id, "work_id")
        row = self._require_connection().execute(
            f"SELECT {_WORK_COLUMNS} FROM work_progress WHERE work_id = ?", (wid,)
        ).fetchone()
        return None if row is None else _work_progress_from_row(row)

    def work_progress_rows(
        self, *, namespace: str | None = None
    ) -> tuple[WorkProgress, ...]:
        """All completion rows, optionally filtered by namespace (diagnostics)."""

        sql = f"SELECT {_WORK_COLUMNS} FROM work_progress"
        parameters: list[Any] = []
        if namespace is not None:
            sql += " WHERE namespace = ?"
            parameters.append(identifiers.require_namespace(namespace))
        sql += " ORDER BY updated_at, work_id"
        rows = self._require_connection().execute(sql, tuple(parameters)).fetchall()
        return tuple(_work_progress_from_row(row) for row in rows)

    def reconcile_redacted_work(
        self, *, namespace: str | None = None
    ) -> ReconcileReport:
        """Give every redacted-but-unfinished work item its terminal ``skipped`` row.

        This is the deterministic recovery path for historic barriers (a
        deletion committed before R1, or a work row left behind by an older
        writer).  It is:

        * idempotent — a second call finds nothing and writes nothing;
        * watermark-neutral — it never advances ``state_revision``,
          ``privacy_epoch`` or ``last_event_seq``;
        * bounded — one pass over the pending queue, no retry loop;
        * deletion-safe — only work whose event already has a committed
          ``deletion_targets`` row is considered.

        ``CognitionStore.open`` calls it once after migrations, so a restart
        completes the recovery even if the deleting process died before
        writing the skip.
        """

        self._require_standalone("reconcile_redacted_work")
        ns_filter = None if namespace is None else identifiers.require_namespace(namespace)
        skipped: list[WorkProgress] = []
        with self.transaction():
            sql = (
                "SELECT p.work_id, p.namespace, p.kind, p.event_id, p.source_event_id, "
                "p.status, p.created_at "
                "FROM pending_work p "
                "JOIN deletion_targets dt "
                "  ON dt.target_kind = 'event' AND dt.namespace = p.namespace "
                " AND dt.target_id = p.event_id "
                "LEFT JOIN work_progress w ON w.work_id = p.work_id "
                "WHERE p.status = 'pending' AND w.work_id IS NULL"
            )
            parameters: list[Any] = []
            if ns_filter is not None:
                sql += " AND p.namespace = ?"
                parameters.append(ns_filter)
            sql += " ORDER BY p.created_at, p.work_id"
            rows = self._require_connection().execute(sql, tuple(parameters)).fetchall()
            revisions: dict[str, int] = {}
            for row in rows:
                work = _pending_work_from_row(row)
                revision = revisions.get(work.namespace)
                if revision is None:
                    revision = self._watermarks_locked(work.namespace)["state_revision"]
                    revisions[work.namespace] = revision
                try:
                    skipped.append(
                        self._insert_skipped_work_locked(
                            namespace=work.namespace,
                            work_id=work.work_id,
                            completed_state_revision=revision,
                        )
                    )
                except WorkAlreadyCompleted:  # pragma: no cover - PK backstop
                    continue
        return ReconcileReport(skipped=tuple(skipped))

    def _insert_work_progress_locked(
        self,
        *,
        namespace: str,
        work_id: str,
        context_id: str,
        outcome: str,
        completed_state_revision: int,
    ) -> WorkProgress:
        """Insert one normal completion row; caller owns the transaction."""

        if outcome not in TX_WORK_OUTCOMES:
            raise InvalidInput(
                f"outcome must be one of {', '.join(TX_WORK_OUTCOMES)}"
            )
        cid = identifiers.require_context_id(context_id)
        return self._insert_work_progress_row_locked(
            namespace=namespace,
            work_id=work_id,
            outcome=outcome,
            context_id=cid,
            completed_state_revision=completed_state_revision,
            reason=None,
        )

    def _insert_skipped_work_locked(
        self,
        *,
        namespace: str,
        work_id: str,
        completed_state_revision: int,
    ) -> WorkProgress:
        """Insert the terminal ``skipped`` row for a deleted source.

        No Context exists and none is fabricated.  The row records the current
        domain revision without advancing it.
        """

        return self._insert_work_progress_row_locked(
            namespace=namespace,
            work_id=work_id,
            outcome=WORK_OUTCOME_SKIPPED,
            context_id=None,
            completed_state_revision=completed_state_revision,
            reason=WORK_SKIP_REASON_SOURCE_REDACTED,
        )

    def _insert_work_progress_row_locked(
        self,
        *,
        namespace: str,
        work_id: str,
        outcome: str,
        context_id: str | None,
        completed_state_revision: int,
        reason: str | None,
    ) -> WorkProgress:
        """Shared completion insert; the table CHECK enforces the combination.

        The primary key is the concurrency backstop: a second transaction that
        reaches this point gets :class:`WorkAlreadyCompleted` and must roll back
        everything it did, including any record versions it appended.
        """

        if self._tx_depth == 0:
            raise RuntimeError(
                "_insert_work_progress_row_locked must run inside the caller's transaction"
            )
        ns = identifiers.require_namespace(namespace)
        wid = _require_uuid(work_id, "work_id")
        if outcome not in WORK_OUTCOMES:
            raise InvalidInput(f"outcome must be one of {', '.join(WORK_OUTCOMES)}")
        if outcome == WORK_OUTCOME_SKIPPED:
            if context_id is not None or reason != WORK_SKIP_REASON_SOURCE_REDACTED:
                raise InvalidInput(
                    "a skipped outcome has no context and reason 'source_redacted'"
                )
        else:
            if context_id is None or reason is not None:
                raise InvalidInput(
                    "a normal work outcome requires a context and no skip reason"
                )
        revision = _require_non_negative_int(
            completed_state_revision, "completed_state_revision"
        )
        row = self._require_connection().execute(
            "SELECT namespace FROM pending_work WHERE work_id = ?", (wid,)
        ).fetchone()
        if row is None:
            raise RecordNotFound(f"no pending work {wid}")
        if row["namespace"] != ns:
            raise ScopeViolation(
                f"work {wid} belongs to namespace {row['namespace']!r}, not {ns!r}"
            )
        existing = self._require_connection().execute(
            "SELECT 1 FROM work_progress WHERE work_id = ?", (wid,)
        ).fetchone()
        if existing is not None:
            raise WorkAlreadyCompleted(f"work {wid} already has a completion row")
        updated_at = utc_now_rfc3339()
        try:
            self._require_connection().execute(
                "INSERT INTO work_progress ("
                "work_id, namespace, outcome, context_id, completed_state_revision, "
                "reason, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (wid, ns, outcome, context_id, revision, reason, updated_at),
            )
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if "UNIQUE constraint failed" in message:
                raise WorkAlreadyCompleted(
                    f"work {wid} was completed concurrently"
                ) from exc
            raise
        return WorkProgress(
            work_id=wid,
            namespace=ns,
            outcome=outcome,
            context_id=context_id,
            completed_state_revision=revision,
            updated_at=updated_at,
            reason=reason,
        )

    # -------------------------------------------------------------- deletion

    def context_ids_for_source(
        self, *, namespace: str, ref_kind: str, ref_id: str
    ) -> tuple[str, ...]:
        """Contexts whose exact source list names this target (reverse lookup)."""

        ns = identifiers.require_namespace(namespace)
        kind = _require_ref_kind(ref_kind)
        target = _require_uuid(ref_id, "ref_id")
        rows = self._require_connection().execute(
            "SELECT DISTINCT cs.context_id "
            "FROM context_sources cs "
            "JOIN context_snapshots snap ON snap.context_id = cs.context_id "
            "WHERE snap.namespace = ? AND cs.ref_kind = ? AND cs.ref_id = ? "
            "ORDER BY cs.context_id",
            (ns, kind, target),
        ).fetchall()
        return tuple(str(row["context_id"]) for row in rows)

    def _after_deletion_barrier_locked(self, target: DeletionTarget) -> int:
        """Tx1 hook: turn a deleted event's unfinished work into ``skipped``.

        Runs inside the same transaction that inserts the ``deletion_targets``
        row and advances ``privacy_epoch``/``state_revision`` exactly once.
        The skip rows do not move any watermark and do not create a Context.
        If this insert fails, the deletion barrier rolls back as a whole: the
        system never commits a deletion while leaving its work permanently
        unfinished.
        """

        if target.target_kind != DELETION_EVENT:
            return 0
        revision = self._watermarks_locked(target.namespace)["state_revision"]
        skipped = 0
        for work in self._pending_work_without_progress_locked(
            target.namespace, target.target_id
        ):
            try:
                self._insert_skipped_work_locked(
                    namespace=target.namespace,
                    work_id=work.work_id,
                    completed_state_revision=revision,
                )
            except WorkAlreadyCompleted:  # pragma: no cover - PK backstop
                continue
            skipped += 1
        return skipped

    def _after_target_cleared_locked(self, target: DeletionTarget) -> int:
        """Clear rendered Context payloads that used a now-cleaned target.

        Called by the PCS2-006 cleanup transaction that marks a deletion target
        ``cleaned``.  The reverse lookup goes through ``context_sources``; the
        Context row keeps its ``content_hash`` (evidence that a text existed)
        while the text itself becomes unrecoverable (``REDACTED`` on read).
        """

        if target.target_kind not in (DELETION_RECORD, DELETION_EVENT):
            return super()._after_target_cleared_locked(target)
        ref_kind = (
            CONTEXT_REF_KIND_RECORD
            if target.target_kind == DELETION_RECORD
            else CONTEXT_REF_KIND_EVENT
        )
        rows = self._require_connection().execute(
            "SELECT DISTINCT snap.rendered_input_payload_ref AS payload_ref "
            "FROM context_sources cs "
            "JOIN context_snapshots snap ON snap.context_id = cs.context_id "
            "WHERE snap.namespace = ? AND cs.ref_kind = ? AND cs.ref_id = ? "
            "AND snap.rendered_input_payload_ref IS NOT NULL",
            (target.namespace, ref_kind, target.target_id),
        ).fetchall()
        cleared = 0
        for row in rows:
            cursor = self._require_connection().execute(
                "UPDATE private_payloads SET body_json = '', sha256 = ? "
                "WHERE payload_ref = ? AND body_json <> ''",
                (secrets.token_hex(32), row["payload_ref"]),
            )
            cleared += 1 if cursor.rowcount else 0
        return cleared


class CognitionStore(ModelLedgerMixin, CollaborationMixin, ContextStateMixin, DomainStore):
    """PCS2-008/009 store: domain + Contexts + work + model attempt ledger."""

    @classmethod
    def open(
        cls,
        db_path: str | Path,
        *,
        migrations_dir: str | Path | None = None,
        apply_migrations: bool = True,
    ) -> "CognitionStore":
        """Open/migrate the database, then finish redacted work recovery.

        The one-shot reconciliation is bounded and idempotent.  It is what
        makes a process restart finish a deletion whose original writer died
        before it could write the ``skipped`` terminal rows (and it also
        upgrades a pre-R1 database whose barriers are already committed).
        """

        store = super().open(
            db_path,
            migrations_dir=migrations_dir,
            apply_migrations=apply_migrations,
        )
        try:
            store.reconcile_redacted_work()
        except BaseException:
            store.close()
            raise
        return store

    def create_correction_work(
        self,
        *,
        namespace: str,
        event_id: str,
        source_event_id: str,
    ) -> str:
        """Create one additional pending derivation for a completed trigger.

        A two-stage correction uses the same question event twice: the first
        model call retracts, the second must see a Context rebuilt after that
        retraction.  This creates only the second work item; it does not create
        an event or fabricate a proposal.
        """

        ns = identifiers.require_namespace(namespace)
        eid = identifiers.require_event_id(event_id)
        sid = str(source_event_id)
        if not sid:
            raise InvalidInput("source_event_id must be non-empty")
        work_id = str(uuid.uuid4())
        with self.transaction():
            if self.event_header(ns, eid) is None:
                raise InvalidInput(
                    f"trigger event {eid} does not exist in {ns!r}"
                )
            self._require_connection().execute(
                "INSERT INTO pending_work ("
                "work_id, namespace, kind, event_id, source_event_id, status, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    work_id,
                    ns,
                    "event.derive.correction",
                    eid,
                    sid,
                    "pending",
                    utc_now_rfc3339(),
                ),
            )
        return work_id

    def work_source_event_id(self, work_id: str) -> str | None:
        row = self._require_connection().execute(
            "SELECT source_event_id FROM pending_work WHERE work_id = ?",
            (str(work_id),),
        ).fetchone()
        return None if row is None else str(row["source_event_id"])


# --------------------------------------------------------------- helpers


def context_content_hash(rendered_input: str) -> str:
    """``sha256:<hex>`` of the exact rendered text (UTF-8)."""

    if not isinstance(rendered_input, str):
        raise InvalidInput("rendered_input must be a string")
    return "sha256:" + sha256_hex(rendered_input)


def _require_content_hash(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
    ):
        raise InvalidInput("content_hash must be 'sha256:<64 lower-case hex>'")
    suffix = value[len("sha256:") :]
    if any(char not in "0123456789abcdef" for char in suffix):
        raise InvalidInput("content_hash must be 'sha256:<64 lower-case hex>'")
    return value


def _require_ref_kind(value: Any) -> str:
    if value not in CONTEXT_REF_KINDS:
        raise InvalidInput(f"ref_kind must be one of {', '.join(CONTEXT_REF_KINDS)}")
    return value


def _require_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str) or identifiers.UUID_PATTERN.fullmatch(value) is None:
        raise InvalidInput(f"{field} must be a canonical lower-case UUID")
    return value


def _require_bounded_text(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(f"{field} must be a non-empty string")
    if len(value) > max_length:
        raise InvalidInput(f"{field} must be at most {max_length} characters")
    return value


def _require_non_negative_int(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise InvalidInput(f"{field} must be a non-negative integer")
    if value > identifiers.MAX_SAFE_INTEGER:
        raise InvalidInput(f"{field} exceeds the cross-language safe integer limit")
    return value


def _require_bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise InvalidInput(f"{field} must be a boolean")
    return value


def _require_plain_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidInput(f"{field} must be a JSON object")
    result = dict(value)
    canonical_json_text(result)
    return result


def _require_object_list(
    value: Any, field: str
) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise InvalidInput(f"{field} must be an array of JSON objects")
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise InvalidInput(f"{field}[{index}] must be a JSON object")
        result.append(dict(item))
    canonical_json_text(result)
    return result


def _require_text_list(value: Any, field: str, max_length: int) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise InvalidInput(f"{field} must be an array of strings")
    result = []
    for index, item in enumerate(value):
        result.append(_require_bounded_text(item, f"{field}[{index}]", max_length))
    return result


def _normalize_sources(
    sources: Sequence[ContextSourceInput],
) -> tuple[ContextSource, ...]:
    if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence):
        raise InvalidInput("sources must be an array of ContextSourceInput")
    seen: set[tuple[str, str, int]] = set()
    normalized: list[ContextSource] = []
    for item in sources:
        if not isinstance(item, ContextSourceInput):
            raise InvalidInput("sources entries must be ContextSourceInput objects")
        kind = _require_ref_kind(item.ref_kind)
        target = _require_uuid(item.ref_id, "source.ref_id")
        version = item.ref_version
        if type(version) is not int or version < 0:
            raise InvalidInput("source.ref_version must be a non-negative integer")
        if kind == CONTEXT_REF_KIND_EVENT and version != 0:
            raise InvalidInput("event sources carry ref_version 0")
        if kind == CONTEXT_REF_KIND_RECORD and version < 1:
            raise InvalidInput("record sources require an exact record_version")
        key = (kind, target, version)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            ContextSource(
                context_id="",  # filled by the caller of the insert
                ref_kind=kind,
                ref_id=target,
                ref_version=version,
            )
        )
    return tuple(normalized)


def _decode_object(text: str, field: str, context_id: str) -> dict[str, Any]:
    try:
        value = strict_json_loads(text)
    except ValueError as exc:
        raise CorruptStore(f"context {context_id} has invalid {field}") from exc
    if not isinstance(value, dict):
        raise CorruptStore(f"context {context_id} has non-object {field}")
    return value


def _decode_object_list(
    text: str, field: str, context_id: str
) -> list[dict[str, Any]]:
    try:
        value = strict_json_loads(text)
    except ValueError as exc:
        raise CorruptStore(f"context {context_id} has invalid {field}") from exc
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise CorruptStore(f"context {context_id} has invalid {field}")
    return value


def _decode_text_list(text: str, field: str, context_id: str) -> list[str]:
    try:
        value = strict_json_loads(text)
    except ValueError as exc:
        raise CorruptStore(f"context {context_id} has invalid {field}") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CorruptStore(f"context {context_id} has invalid {field}")
    return value


def _pending_work_from_row(row: Any) -> PendingWork:
    return PendingWork(
        work_id=row["work_id"],
        namespace=row["namespace"],
        kind=row["kind"],
        event_id=row["event_id"],
        source_event_id=row["source_event_id"],
        status=row["status"],
        created_at=row["created_at"],
    )


def _work_progress_from_row(row: Any) -> WorkProgress:
    return WorkProgress(
        work_id=row["work_id"],
        namespace=row["namespace"],
        outcome=row["outcome"],
        context_id=row["context_id"],
        completed_state_revision=int(row["completed_state_revision"]),
        updated_at=row["updated_at"],
        reason=row["reason"],
    )


__all__ = [
    "CONTEXT_ADAPTER_DIRECT_HTTP",
    "CONTEXT_ADAPTER_FAKE_MODEL",
    "CONTEXT_ADAPTERS",
    "CONTEXT_REF_KIND_EVENT",
    "CONTEXT_REF_KIND_RECORD",
    "CONTEXT_REF_KINDS",
    "CognitionStore",
    "ContextInsert",
    "ContextSnapshot",
    "ContextSource",
    "ContextSourceInput",
    "ContextStateMixin",
    "MAX_CONTEXT_BYTES",
    "TX_WORK_OUTCOMES",
    "WORK_OUTCOMES",
    "WORK_OUTCOME_CHECKED_NO_BELIEF",
    "WORK_OUTCOME_COMMITTED",
    "WORK_OUTCOME_SKIPPED",
    "WORK_SKIP_REASON_SOURCE_REDACTED",
    "ReconcileReport",
    "WorkProgress",
    "context_content_hash",
]
