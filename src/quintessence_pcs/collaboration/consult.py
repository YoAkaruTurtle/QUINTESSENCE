"""C11: bounded consult orchestration over the formal Context and query path.

One :meth:`ConsultFacade.interact` call is one bounded consult interaction::

    formal Context -> model (<= 2 logical generations) ->
    optional deterministic query (<= 1) -> new formal Context -> final output

The facade is an adapter over the existing trusted APIs.  It adds no contract,
migration, error code or second HTTP client, and it never commits a proposal,
creates work/Beliefs or treats model text as an authority.

Three boundaries define the module.

* **The formal Context is the only model basis.**  A deterministic query
  answer enters the second generation solely through
  :class:`~quintessence_pcs.runtime.context.ConsultQueryMaterial`, which renders
  exactly ``QueryResult.to_body()``.  The internal ``state_revision`` and the
  internal omission reasons never reach the Prompt, the Context or the public
  :class:`ConsultResult`; the query artifact body (``to_artifact_body``) is
  written only to artifact storage.
* **Every generation is tied to one exact model attempt.**  ``_generate`` reads
  ``last_attempt_evidence`` immediately after ``propose`` returns, requires it
  to be the settled ``ok`` attempt of *this* run/Context, and requires its
  content to equal the returned raw text.  A missing, stale or guessed attempt
  is a failure with no reply artifact.
* **The lifecycle is explicit.**  The interaction is created as soon as the
  initial Context exists, the final Context is bound only through
  :meth:`CognitionStore.bind_interaction_context`, and every terminal state is
  written through ``complete_interaction``.  ``incomplete`` is non-terminal and
  never carries ``completed_at``.

Deletion/authorization normal case: the query service already omits
unauthorized/expired material and never exposes an existence oracle; C11
persists that empty/partial result honestly instead of reconstructing state
from a previous round.  Nothing here promises that bytes sent to an external
provider can be withdrawn.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..domains.cognition.query import (
    BeliefCurrentQuery,
    BeliefExactQuery,
    BeliefItem,
    CognitionQueryService,
    EpisodeItem,
    EpisodeQuery,
    QueryAccess,
    QueryResult,
)
from ..domains.refs import EventRef, RecordRef
from ..foundation import identifiers
from ..foundation.errors import (
    AuthorizationRequired,
    ContextStale,
    CorruptStore,
    DbBusy,
    InvalidInput,
    ScopeViolation,
    StandaloneTransactionRequired,
    StorageError,
)
from ..foundation.jsonutil import canonical_json_text, strict_json_loads
from ..runtime.collab_protocol import (
    CollabOutput,
    ConsultSendPolicy,
    parse_collab_output,
    repair_feedback,
)
from ..runtime.context import (
    CONTEXT_ADAPTERS,
    ConsultContextEnvelope,
    ConsultQueryMaterial,
    ContextService,
    ReadSetEntry,
)
from ..store.collaboration import (
    ACTIVATION_KINDS,
    RETENTION_ORDER,
    ArtifactRow,
)
from ..store.experience import ExperienceAccess

#: One interaction may make at most two logical generations and one query.
#: A format repair consumes a generation; a query consumes the query budget.
MAX_GENERATIONS = 2
MAX_QUERIES = 1

#: The public consult lifecycle vocabulary.  ``incomplete`` is deliberately
#: non-terminal (no ``completed_at``); every other state closes the interaction.
CONSULT_STATES = ("answered", "waiting", "failed", "incomplete", "stale")

TERMINAL_CONSULT_STATES = ("answered", "waiting", "failed", "stale")

#: The host access purpose C11 requires for every call.
CONSULT_PURPOSE = "consult"
#: Episode queries additionally require the C10 reasoning purpose.
EPISODE_PURPOSE = "reasoning"

#: The trusted-host capability label recorded as the interaction policy.
CONSULT_POLICY_REF = "policy:collab.v1"

#: A typed plan is a host value, never model output.  Only the three C10 query
#: shapes are accepted; anything else is a structural caller error.
CognitionQuery = BeliefExactQuery | BeliefCurrentQuery | EpisodeQuery

_MAX_HOST_INSTANCE = 256
_MAX_ACCESS_REF = 512
_MAX_POLICY_REF = 512
_MAX_PURPOSE = 64
_ATTEMPT_REF_KEYS = frozenset(
    {"authorization_id", "attempt_id", "attempt_no", "request_hash"}
)


# --------------------------------------------------------------------------
# public DTOs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsultHostAccess:
    """Trusted-host authority for one consult call.

    This is not an ACL and not a persisted permission: the trusted host builds
    it per call.  ``purposes`` must contain ``"consult"``; an Episode query
    additionally requires ``"reasoning"`` (checked at the call entry).  The
    value derives the three narrower authorities C08/C09/C10 already use.
    """

    namespace: str
    host_instance: str
    access_ref: str
    privacy_classes: frozenset[str]
    purposes: frozenset[str]
    disclose: bool

    def __post_init__(self) -> None:
        identifiers.require_namespace(self.namespace)
        _require_text(self.host_instance, "host_instance", _MAX_HOST_INSTANCE)
        _require_text(self.access_ref, "access_ref", _MAX_ACCESS_REF)
        if not isinstance(self.privacy_classes, frozenset):
            raise InvalidInput("access.privacy_classes must be a frozenset")
        for privacy in self.privacy_classes:
            identifiers.require_privacy_class(privacy)
        if not isinstance(self.purposes, frozenset) or not self.purposes:
            raise InvalidInput("access.purposes must be a non-empty frozenset")
        for purpose in self.purposes:
            _require_text(purpose, "access.purposes[]", _MAX_PURPOSE)
        if CONSULT_PURPOSE not in self.purposes:
            raise AuthorizationRequired(
                "consult host access requires the 'consult' purpose"
            )
        if type(self.disclose) is not bool:
            raise InvalidInput("access.disclose must be a boolean")

    def to_experience_access(self) -> ExperienceAccess:
        return ExperienceAccess(
            namespace=self.namespace,
            host_instance=self.host_instance,
            access_ref=self.access_ref,
            privacy_classes=self.privacy_classes,
            purposes=self.purposes,
        )

    def to_send_policy(self) -> ConsultSendPolicy:
        return ConsultSendPolicy(
            namespace=self.namespace,
            host_instance=self.host_instance,
            access_ref=self.access_ref,
            privacy_classes=self.privacy_classes,
            purposes=self.purposes,
            disclose=self.disclose,
        )

    def to_query_access(self) -> QueryAccess:
        return QueryAccess(
            namespace=self.namespace,
            privacy_classes=self.privacy_classes,
            purposes=self.purposes,
        )


@dataclass(frozen=True)
class ConsultRequest:
    """One consult request.  ``query_plan`` is the host's deterministic plan.

    ``input_refs`` must already be registered by C05b; C11 never registers an
    input.  A model may only express a query *intent*; it cannot supply
    ``query_plan``, ``context_id``, ``state_revision`` or an authorization.
    """

    input_refs: tuple[dict[str, Any], ...]
    query_plan: CognitionQuery | None
    policy_ref: str
    activation_kind: str = "user_input"
    retention: str = "standard"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "input_refs", _normalise_input_refs(self.input_refs)
        )
        if self.query_plan is not None and not isinstance(
            self.query_plan,
            (BeliefExactQuery, BeliefCurrentQuery, EpisodeQuery),
        ):
            raise InvalidInput(
                "query_plan must be None, a BeliefExactQuery, a "
                "BeliefCurrentQuery or an EpisodeQuery"
            )
        _require_text(self.policy_ref, "policy_ref", _MAX_POLICY_REF)
        if self.activation_kind not in ACTIVATION_KINDS:
            raise InvalidInput(
                "activation_kind must be one of " + ", ".join(ACTIVATION_KINDS)
            )
        if self.retention not in RETENTION_ORDER:
            raise InvalidInput(
                "retention must be one of " + ", ".join(RETENTION_ORDER)
            )


@dataclass(frozen=True)
class ConsultResult:
    """The public consult conclusion (N1: no internal QueryResult fields).

    ``query_body`` is exactly :meth:`QueryResult.to_body` and contains only the
    model-visible projection.  ``query_source_refs`` are the disclosable exact
    source refs.  No ``state_revision``, internal omission, full
    ``QueryResult`` or ``to_artifact_body`` value is representable here.
    """

    interaction_id: str | None
    context_id: str | None
    state: str
    reply: str | None
    query_body: str | None
    query_source_refs: tuple[dict[str, Any], ...]
    query_result_ref: dict[str, Any] | None
    reply_artifact_ref: dict[str, Any] | None
    generations: int
    query_count: int
    detail: str | None

    def __post_init__(self) -> None:
        if self.state not in CONSULT_STATES:
            raise InvalidInput(f"unknown consult state {self.state!r}")


# --------------------------------------------------------------------------
# internal run state / abort
# --------------------------------------------------------------------------


@dataclass
class _ConsultRun:
    """Mutable orchestration state; never exposed to a caller."""

    namespace: str
    interaction_id: str
    context_id: str
    context: ConsultContextEnvelope
    generations: int = 0
    query_count: int = 0
    query_body: str | None = None
    query_source_refs: tuple[dict[str, Any], ...] = ()
    query_result_ref: dict[str, Any] | None = None
    reply_artifact_ref: dict[str, Any] | None = None
    reply: str | None = None
    state: str = "failed"
    detail: str | None = None
    evidence: Any = field(default=None, repr=False)


class _ConsultAbort(Exception):
    """A bounded conclusion that is not a successful reply."""

    def __init__(self, state: str, detail: str) -> None:
        super().__init__(detail)
        self.state = state
        self.detail = detail


# --------------------------------------------------------------------------
# facade
# --------------------------------------------------------------------------


class ConsultFacade:
    """The trusted host's bounded consult entry point (C11)."""

    def __init__(
        self,
        store: Any,
        *,
        context_service: ContextService,
        model: Any,
        query_service: CognitionQueryService,
        clock: Any = None,
    ) -> None:
        if not hasattr(store, "connection"):
            raise InvalidInput("store must be a CognitionStore")
        self._store = store
        self._contexts = context_service
        self._model = model
        self._queries = query_service
        self._clock = clock

    # ---------------------------------------------------------------- public

    def interact(
        self, access: ConsultHostAccess, request: ConsultRequest
    ) -> ConsultResult:
        """Run one bounded consult interaction and persist its outcome."""

        self._require_entry(access, request)
        problem = self._capability_problem()
        if problem is not None:
            # A model/query service that does not expose the required surface
            # cannot produce an exact attempt; fail before any Context or HTTP
            # call instead of fabricating one.
            return _failed_result("capability", problem)

        preflight = getattr(self._model, "preflight", None)
        if callable(preflight):
            try:
                preflight(namespace=access.namespace)
            except (CorruptStore, DbBusy):
                raise
            except Exception as exc:  # noqa: BLE001 - refusal is a result
                return _failed_result("preflight", exc)

        try:
            context = self._contexts.build_consult_context(
                namespace=access.namespace,
                access=access.to_experience_access(),
                input_refs=list(request.input_refs),
                adapter_kind=self._model.adapter_kind,
                query_materials=(),
            )
        except (CorruptStore, DbBusy):
            raise
        except StorageError as exc:
            return _failed_result("context", exc)

        try:
            interaction_id = self._store.register_interaction(
                namespace=access.namespace,
                mode="consult",
                activation_kind=request.activation_kind,
                host_instance=access.host_instance,
                access_ref=access.access_ref,
                policy_ref=request.policy_ref,
                input_refs=list(request.input_refs),
                context_id=context.context_id,
            )
        except (CorruptStore, DbBusy):
            raise
        except StorageError as exc:
            return _failed_result("interaction", exc)

        run = _ConsultRun(
            namespace=access.namespace,
            interaction_id=interaction_id,
            context_id=context.context_id,
            context=context,
        )
        try:
            self._orchestrate(run, access, request)
        except _ConsultAbort as abort:
            self._complete(run, abort.state)
            return _result(run, abort.state, abort.detail)
        self._complete(run, run.state)
        return _result(run, run.state, run.detail)

    # -------------------------------------------------------------- orchestration

    def _orchestrate(
        self,
        run: _ConsultRun,
        access: ConsultHostAccess,
        request: ConsultRequest,
    ) -> None:
        output, evidence = self._generate(run, access, request, run.context)
        if output.action == "query":
            self._execute_query(run, access, request)
            output, evidence = self._generate(
                run, access, request, run.context
            )
            if output.action == "query":
                raise _ConsultAbort("failed", "second_query")
            if output.action == "propose":
                raise _ConsultAbort("failed", "propose")
        elif output.action == "propose":  # pragma: no cover - parser refuses
            raise _ConsultAbort("failed", "propose")
        self._finalize(run, access, request, output, evidence)

    def _execute_query(
        self,
        run: _ConsultRun,
        access: ConsultHostAccess,
        request: ConsultRequest,
    ) -> None:
        if request.query_plan is None:
            # The model expressed a query intent but the host supplied no
            # deterministic plan: persist the non-terminal incomplete state.
            raise _ConsultAbort("incomplete", "query_plan_missing")
        if run.generations >= MAX_GENERATIONS:
            # A repair already spent the second generation; a query would need
            # a third logical generation, which this card forbids.
            raise _ConsultAbort("failed", "generation_budget")
        if run.query_count >= MAX_QUERIES:
            raise _ConsultAbort("failed", "query_budget")
        run.query_count += 1
        try:
            result = self._queries.query(
                access.to_query_access(), request.query_plan
            )
        except (CorruptStore, DbBusy):
            raise
        except StorageError as exc:
            raise _ConsultAbort("failed", _phase_label("query", exc)) from exc
        if not isinstance(result, QueryResult):
            raise _ConsultAbort("failed", "query_result")

        material = _query_material(result)
        run.query_body = material.body
        run.query_source_refs = tuple(ref.to_dict() for ref in result.source_refs)

        try:
            final_context = self._contexts.build_consult_context(
                namespace=access.namespace,
                access=access.to_experience_access(),
                input_refs=list(request.input_refs),
                adapter_kind=self._model.adapter_kind,
                query_materials=(material,),
            )
        except (CorruptStore, DbBusy):
            raise
        except ContextStale as exc:
            raise _ConsultAbort("stale", _phase_label("context", exc)) from exc
        except StorageError as exc:
            raise _ConsultAbort("stale", _phase_label("context", exc)) from exc

        try:
            self._store.bind_interaction_context(
                access.namespace, run.interaction_id, final_context.context_id
            )
        except (CorruptStore, DbBusy):
            raise
        except StorageError as exc:
            raise _ConsultAbort("stale", _phase_label("bind", exc)) from exc

        run.context = final_context
        run.context_id = final_context.context_id

        # Section 4.4: only a current final Context that is still bound to an
        # open interaction may produce a query artifact.  These checks run
        # immediately before registration, so a concurrent finish or a moved
        # watermark cannot leave an artifact attached to a stale basis.
        self._require_current_final_context(final_context, access)
        self._require_open_bound_interaction(run, final_context)

        try:
            artifact = self._store.register_artifact(
                namespace=access.namespace,
                kind="query_result",
                interaction_id=run.interaction_id,
                context_id=final_context.context_id,
                origin_kind="deterministic_query",
                body=result.to_artifact_body(),
                attempt_ref=None,
                retention=request.retention,
                sources=_artifact_sources(final_context),
            )
        except (CorruptStore, DbBusy):
            raise
        except StorageError as exc:
            raise _ConsultAbort(
                "failed", _phase_label("query_artifact", exc)
            ) from exc
        run.query_result_ref = _artifact_ref(access.namespace, artifact)

    def _generate(
        self,
        run: _ConsultRun,
        access: ConsultHostAccess,
        request: ConsultRequest,
        context: ConsultContextEnvelope,
        *,
        feedback: str | None = None,
    ) -> tuple[CollabOutput, Any]:
        if run.generations >= MAX_GENERATIONS:
            raise _ConsultAbort("failed", "generation_budget")
        run.generations += 1
        allowed = _allowed_references(context)
        try:
            binder = self._model.bind_run(
                namespace=access.namespace, context=context
            )
            with binder:
                raw = self._model.propose(
                    rendered_input=context.rendered_input,
                    references=allowed,
                    feedback=feedback,
                )
        except (CorruptStore, DbBusy):
            raise
        except Exception as exc:  # noqa: BLE001 - model refusal is a result
            raise _ConsultAbort("failed", _phase_label("model", exc)) from exc

        try:
            evidence = self._model.last_attempt_evidence(
                run_id=context.run_id, context_id=context.context_id
            )
        except (CorruptStore, DbBusy):
            raise
        except Exception as exc:  # noqa: BLE001 - a broken adapter is a result
            raise _ConsultAbort(
                "failed", _phase_label("attempt_evidence", exc)
            ) from exc
        if evidence is None:
            raise _ConsultAbort("failed", "attempt_evidence_missing")
        if evidence.content != raw:
            raise _ConsultAbort("failed", "attempt_evidence_mismatch")
        self._require_exact_attempt(context, evidence)
        run.evidence = evidence

        try:
            output = parse_collab_output(
                raw, mode="consult", allowed_references=allowed
            )
        except InvalidInput as exc:
            if _declared_action(raw) == "propose":
                # A propose is a semantic refusal, not a format slip: C11 never
                # commits cognition and never repairs this into an answer.
                raise _ConsultAbort("failed", "propose") from exc
            if run.generations < MAX_GENERATIONS:
                return self._generate(
                    run,
                    access,
                    request,
                    context,
                    feedback=repair_feedback(exc),
                )
            raise _ConsultAbort(
                "failed", _phase_label("format", exc)
            ) from exc
        return output, evidence

    def _require_exact_attempt(
        self, context: ConsultContextEnvelope, evidence: Any
    ) -> None:
        """Prove the evidence is the settled attempt of this exact generation."""

        ref = evidence.attempt_ref
        if not isinstance(ref, Mapping) or set(ref) != _ATTEMPT_REF_KEYS:
            raise _ConsultAbort("failed", "attempt_evidence_mismatch")
        attempt_id = ref.get("attempt_id")
        if not isinstance(attempt_id, str):
            raise _ConsultAbort("failed", "attempt_evidence_mismatch")
        try:
            _require_uuid_text(attempt_id, "attempt_id")
        except StorageError as exc:
            raise _ConsultAbort(
                "failed", "attempt_evidence_mismatch"
            ) from exc
        attempts = self._store.model_attempts_for_run(context.run_id)
        matches = [
            attempt
            for attempt in attempts
            if attempt.attempt_id == attempt_id
        ]
        if len(matches) != 1:
            raise _ConsultAbort("failed", "attempt_evidence_mismatch")
        attempt = matches[0]
        if (
            attempt.namespace != context.namespace
            or attempt.context_id != context.context_id
            or attempt.authorization_id != ref.get("authorization_id")
            or attempt.attempt_no != ref.get("attempt_no")
            or attempt.request_hash != ref.get("request_hash")
            or attempt.outcome != "ok"
            or not attempt.finished
        ):
            raise _ConsultAbort("failed", "attempt_evidence_mismatch")
        # The evidence must be the attempt this generation just produced, not
        # an older successful attempt of the same run/Context.
        if not attempts or attempts[-1].attempt_no != attempt.attempt_no:
            raise _ConsultAbort("failed", "attempt_evidence_mismatch")

    def _require_current_final_context(
        self, context: ConsultContextEnvelope, access: ConsultHostAccess
    ) -> None:
        """Prove the final Context is still the current model basis (4.4)."""

        checker = getattr(
            self._contexts, "require_current_consult_context", None
        )
        if not callable(checker):  # a synthetic service may not expose it
            return
        try:
            checker(
                context_id=context.context_id, namespace=access.namespace
            )
        except (CorruptStore, DbBusy):
            raise
        except StorageError as exc:
            raise _ConsultAbort(
                "stale", _phase_label("current_context", exc)
            ) from exc

    def _require_open_bound_interaction(
        self, run: _ConsultRun, context: ConsultContextEnvelope
    ) -> None:
        """Prove the interaction is still open and bound to this Context."""

        row = self._store.interaction(
            namespace=run.namespace, interaction_id=run.interaction_id
        )
        if row is None:
            raise _ConsultAbort("failed", "interaction_missing")
        if row.context_id != context.context_id:
            raise _ConsultAbort("stale", "interaction_context_mismatch")
        if row.result not in (None, "incomplete"):
            raise _ConsultAbort("stale", "interaction_terminal")

    def _finalize(
        self,
        run: _ConsultRun,
        access: ConsultHostAccess,
        request: ConsultRequest,
        output: CollabOutput,
        evidence: Any,
    ) -> None:
        reply = output.reply
        if output.action == "propose":  # pragma: no cover - parser refuses
            raise _ConsultAbort("failed", "propose")
        if output.action == "none":
            if reply is None:
                raise _ConsultAbort("failed", "no_reply")
            state = "answered"
        elif output.action == "wait":
            state = "waiting"
        else:
            raise _ConsultAbort("failed", "invalid_action")

        if reply is not None:
            try:
                artifact = self._store.register_artifact(
                    namespace=access.namespace,
                    kind="reply",
                    interaction_id=run.interaction_id,
                    context_id=run.context_id,
                    origin_kind="model_output",
                    body=reply,
                    attempt_ref=evidence.attempt_ref,
                    retention=request.retention,
                    sources=_artifact_sources(run.context),
                )
            except (CorruptStore, DbBusy):
                raise
            except StorageError as exc:
                raise _ConsultAbort(
                    "failed", _phase_label("reply_artifact", exc)
                ) from exc
            run.reply_artifact_ref = _artifact_ref(access.namespace, artifact)
        run.reply = reply
        run.state = state

    def _complete(self, run: _ConsultRun, state: str) -> None:
        self._store.complete_interaction(
            namespace=run.namespace,
            interaction_id=run.interaction_id,
            result=state,
        )

    # ------------------------------------------------------------------ entry

    def _require_entry(
        self, access: ConsultHostAccess, request: ConsultRequest
    ) -> None:
        depth = getattr(self._store, "_tx_depth", 0)
        connection = getattr(self._store, "connection", None)
        if depth != 0 or (
            connection is not None and getattr(connection, "in_transaction", False)
        ):
            raise StandaloneTransactionRequired(
                "ConsultFacade.interact owns its transactions and cannot join "
                "an open caller transaction"
            )
        if not isinstance(access, ConsultHostAccess):
            raise InvalidInput("access must be a ConsultHostAccess")
        if not isinstance(request, ConsultRequest):
            raise InvalidInput("request must be a ConsultRequest")
        for ref in request.input_refs:
            if ref.get("namespace") != access.namespace:
                raise ScopeViolation(
                    "a consult input reference belongs to another namespace"
                )
        if isinstance(request.query_plan, EpisodeQuery) and (
            EPISODE_PURPOSE not in access.purposes
        ):
            raise AuthorizationRequired(
                "Episode queries require the 'reasoning' purpose"
            )

    def _capability_problem(self) -> InvalidInput | None:
        """Return a failed-result cause when the adapter surface is incomplete."""

        for name in ("bind_run", "propose", "last_attempt_evidence"):
            if not callable(getattr(self._model, name, None)):
                return InvalidInput(f"model must expose a callable {name}")
        adapter_kind = getattr(self._model, "adapter_kind", None)
        if adapter_kind not in CONTEXT_ADAPTERS:
            return InvalidInput(
                "model.adapter_kind must be one of "
                + ", ".join(CONTEXT_ADAPTERS)
            )
        if not callable(getattr(self._queries, "query", None)):
            return InvalidInput("query_service must expose query")
        return None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _require_uuid_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or identifiers.UUID_PATTERN.fullmatch(value) is None:
        raise InvalidInput(f"{field} must be a canonical lower-case UUID")
    return value


def _normalise_input_refs(value: Any) -> tuple[dict[str, Any], ...]:
    """Validate the structural shape of already-registered C05b inputs."""

    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise InvalidInput("input_refs must be a sequence of input references")
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise InvalidInput(f"input_refs[{index}] must be an object")
        if set(item) != {"ref_type", "namespace", "input_id", "content_version"}:
            raise InvalidInput(
                "input_refs entries must have exactly ref_type, namespace, "
                "input_id and content_version"
            )
        if item["ref_type"] != "collab_input":
            raise InvalidInput('input_refs[].ref_type must be "collab_input"')
        namespace = identifiers.require_namespace(item["namespace"])
        input_id = _require_uuid_text(item["input_id"], "input_id")
        version = identifiers.require_record_version(item["content_version"])
        key = (input_id, version)
        if key in seen:
            raise InvalidInput("input_refs must not contain duplicates")
        seen.add(key)
        refs.append(
            {
                "ref_type": "collab_input",
                "namespace": namespace,
                "input_id": input_id,
                "content_version": version,
            }
        )
    return tuple(refs)


def _query_material(result: QueryResult) -> ConsultQueryMaterial:
    """Build the one formal Context material from a C10 QueryResult."""

    entries: dict[tuple[str, int], ReadSetEntry] = {}
    events: dict[str, EventRef] = {}
    for item in result.items:
        if isinstance(item, BeliefItem):
            usage = "current" if item.current else "historical"
        elif isinstance(item, EpisodeItem):
            usage = "current"
        else:  # pragma: no cover - QueryResult is a closed union
            raise _ConsultAbort("failed", "query_result")
        key = (item.ref.record_id, item.ref.record_version)
        entries.setdefault(key, ReadSetEntry(item.ref, usage))
        for ref in item.source_refs:
            if isinstance(ref, EventRef):
                events.setdefault(ref.event_id, ref)
    read_set = tuple(
        entries[key] for key in sorted(entries)
    )
    event_sources = tuple(events[event_id] for event_id in sorted(events))
    try:
        return ConsultQueryMaterial(
            body=result.to_body(),
            state_revision=result.state_revision,
            read_set=read_set,
            event_sources=event_sources,
        )
    except StorageError as exc:
        raise _ConsultAbort("failed", _phase_label("query_material", exc)) from exc


def _allowed_references(
    context: ConsultContextEnvelope,
) -> tuple[dict[str, Any], ...]:
    """The parser's closed reference vocabulary, from the formal Context only."""

    refs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(reference: Mapping[str, Any]) -> None:
        value = dict(reference)
        key = canonical_json_text(value)
        if key not in seen:
            seen.add(key)
            refs.append(value)

    for reference in context.collab_source_refs:
        add(reference)
    for entry in context.read_set:
        add(entry.ref.to_dict())
    for source in context.sources:
        if source.ref_kind == "event":
            add(
                EventRef(
                    namespace=context.namespace, event_id=source.ref_id
                ).to_dict()
            )
    return tuple(refs)


def _artifact_sources(context: ConsultContextEnvelope) -> list[dict[str, Any]]:
    """Every exact source the final Context actually used, including inputs."""

    refs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(reference: Mapping[str, Any]) -> None:
        value = dict(reference)
        key = canonical_json_text(value)
        if key not in seen:
            seen.add(key)
            refs.append(value)

    for reference in context.collab_source_refs:
        add(reference)
    for source in context.sources:
        if source.ref_kind == "event":
            add(
                EventRef(
                    namespace=context.namespace, event_id=source.ref_id
                ).to_dict()
            )
        else:
            add(
                RecordRef(
                    namespace=context.namespace,
                    record_id=source.ref_id,
                    record_version=int(source.ref_version),
                ).to_dict()
            )
    return refs


def _declared_action(raw: Any) -> str | None:
    """The action a rejected JSON object declared, if it is one object."""

    if not isinstance(raw, str) or not raw:
        return None
    try:
        document = strict_json_loads(raw)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    action = document.get("action")
    return action if isinstance(action, str) else None


def _artifact_ref(namespace: str, artifact: ArtifactRow) -> dict[str, Any]:
    return {
        "ref_type": "collab_artifact",
        "namespace": namespace,
        "artifact_id": artifact.artifact_id,
        "artifact_version": artifact.artifact_version,
    }


def _failed_result(phase: str, exc: BaseException) -> ConsultResult:
    return ConsultResult(
        interaction_id=None,
        context_id=None,
        state="failed",
        reply=None,
        query_body=None,
        query_source_refs=(),
        query_result_ref=None,
        reply_artifact_ref=None,
        generations=0,
        query_count=0,
        detail=_phase_label(phase, exc),
    )


def _result(run: _ConsultRun, state: str, detail: str | None) -> ConsultResult:
    return ConsultResult(
        interaction_id=run.interaction_id,
        context_id=run.context_id,
        state=state,
        reply=run.reply if state in ("answered", "waiting") else None,
        query_body=run.query_body,
        query_source_refs=run.query_source_refs,
        query_result_ref=run.query_result_ref,
        reply_artifact_ref=run.reply_artifact_ref,
        generations=run.generations,
        query_count=run.query_count,
        detail=detail,
    )


def _phase_label(phase: str, exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    label = code if isinstance(code, str) and code else type(exc).__name__
    return f"{phase}:{label}"


def _require_text(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidInput(f"{field} must be a non-empty string")
    if len(value) > max_length:
        raise InvalidInput(f"{field} must be at most {max_length} characters")
    return value


__all__ = [
    "CONSULT_POLICY_REF",
    "CONSULT_STATES",
    "CognitionQuery",
    "ConsultFacade",
    "ConsultHostAccess",
    "ConsultRequest",
    "ConsultResult",
    "MAX_GENERATIONS",
    "MAX_QUERIES",
    "TERMINAL_CONSULT_STATES",
]
