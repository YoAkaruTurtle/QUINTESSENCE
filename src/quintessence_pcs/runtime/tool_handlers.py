"""Handlers that make the published tools do real work.

The catalog decides *what the model may ask for*; this module decides *what
happens when it asks*.  Three rules hold on every handler here:

1. **Authority is injected, never accepted.**  A handler receives the
   host-built access objects through :class:`CognitionToolkit`; nothing in the
   model's JSON can widen them.  The catalog already strips the authority
   field names, and these handlers never read a scope, actor or authorization
   from ``args``.
2. **Handlers call services; they never write the database themselves.**  Every
   mutation goes through ``CognitionChangeService`` (the one governed entry) or
   the Question service.  Reads go through the query/partition services, which
   apply privacy, scope and deletion gates.
3. **A failure is data.**  Handlers raise, and ``ToolCatalog.execute`` turns the
   exception into a structured ``TOOL_FAILED`` / ``TOOL_NOT_AUTHORIZED`` outcome
   so the model can read why and decide what to do next.

No handler returns ``reasoning_content`` or any other internal trace: the model
already knows its own reasoning, and the user must never be shown it.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..domains.cognition.beliefs import INFERENCE_BASIS_BY_CLAIM_TYPE
from ..domains.cognition.belief_page import CursorError, read_belief_page
from ..store.governance import CHANGE_OPERATION
from ..domains.cognition.epistemic import EpistemicAccess, build_epistemic_partition
from ..domains.cognition.query import (
    BeliefCurrentQuery,
    BeliefExactQuery,
    CognitionQueryService,
    QueryAccess,
)
from ..domains.cognition.questions import QuestionAccess, QuestionService
from ..domains.cognition.state_view import AttentionRequest
from ..domains.refs import EventRef, RecordRef
from ..foundation.jsonutil import canonical_json_text, sha256_hex
from .revision_contract import SURFACE_CHAT, check_revision_target
from .tool_catalog import ToolArgumentsInvalid, ToolOutcome
from .web_tools import WEB_NOT_CONFIGURED, WEB_TIMEOUT, WebToolError

Handler = Callable[[Mapping[str, Any]], Any]

#: Fields that must never travel to the model or the user.
_HIDDEN_FIELDS = frozenset({"reasoning_content", "payload_text", "body_text"})

#: One-sided quote characters that mark a quotation rather than a statement.
#: Used only for the ``collab_input`` evidence gate; the expression classifier
#: keeps its own paired-marker rules.
_QUOTE_MARKS = ("\u201c", "\u201d", "\u300c", "\u300d", "\u300e", "\u300f", '"')

#: Statuses the *current* belief query understands.  ``proposed`` is absent on
#: purpose: it is served by the partition, which is the honest outlet for
#: tentative cognition (J06 AUT-05).
_CURRENT_STATUSES = frozenset({"supported", "insufficient", "contradicted"})

#: Question transitions a single command may run without a change candidate.
#: ``resolve`` / ``close`` / ``reopen`` are absent: they carry impact and must
#: travel through the governed change bundle.
SINGLE_STEP_QUESTION_ACTIONS = (
    "accept",
    "wait",
    "pause",
    "resume",
    "wake",
    "update_triggers",
)


@dataclass(frozen=True)
class CognitionToolkit:
    """Everything one handler set needs, all built by trusted host code."""

    store: Any
    changes: Any
    queries: CognitionQueryService
    questions: QuestionService
    access: Any
    query_access: QueryAccess
    question_access: QuestionAccess
    epistemic_access: EpistemicAccess
    now: Callable[[], str]
    #: The scope every bundle is stamped with.  The model never supplies one.
    change_scope: Mapping[str, Any] = field(default_factory=dict)
    #: The Context this session runs in.  Also host state: the model cannot
    #: know a persisted context id and must not be asked to guess one.
    context_id: str = ""
    #: The turn this session runs in.  Host state, used only for a stable
    #: within-turn proposal identity; the model cannot supply it.
    turn_id: str = ""
    #: References this session has already returned to the model.  An evidence
    #: link that names anything else is an invented citation, so the handler
    #: refuses it instead of trusting the schema alone.  The set is shared across
    #: one turn's steps (``ToolRuntime.rebind``), and discarded next turn.
    known_refs: set[str] = field(default_factory=set)
    #: Events this turn's tool calls created, e.g. one per evidence fetch.  The
    #: commit path may advance the event watermark for exactly these, which is
    #: what lets a turn cite the page it just fetched.  Host state only: a
    #: handler adds an id after it successfully ingested that event.
    appended_events: set[str] = field(default_factory=set)
    #: Host policy: which auto-authorization, if any, backs one operation.
    #: Returning ``None`` is honest and produces ``NOT_AUTHORIZED`` downstream;
    #: the handler never invents an authorization.
    auto_authorization: Callable[[str], str | None] = lambda _operation: None
    #: RG05-F02: the host-owned expression-preference router for this turn.
    #: ``None`` keeps the pre-F02 behavior byte-for-byte.
    preference_router: Any = None
    extra: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# serialization
# ---------------------------------------------------------------------------


def to_jsonable(value: Any) -> Any:
    """Project a service value into plain JSON-able data for the model."""

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: to_jsonable(getattr(value, item.name))
            for item in dataclasses.fields(value)
            if item.name not in _HIDDEN_FIELDS
        }
    if isinstance(value, Mapping):
        return {
            str(key): to_jsonable(item)
            for key, item in value.items()
            if str(key) not in _HIDDEN_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "to_dict"):
        return to_jsonable(value.to_dict())
    return str(value)


def _request_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# cognition handlers
# ---------------------------------------------------------------------------


def build_cognition_handlers(
    toolkit: CognitionToolkit, *, register_refs: bool = True
) -> dict[str, Handler]:
    """Build the eleven cognition handlers the catalog publishes.

    ``register_refs=False`` is for the budgeted agent path: there the loop only
    learns whether a body was actually delivered *after* the result is shaped,
    so it registers delivered refs itself via ``run_agent_loop(on_delivered=)``.
    Registering here as well would mark a ref-only descriptor as citable
    evidence, which is exactly the confusion this flag exists to prevent.
    """

    handlers: dict[str, Handler] = {}

    def belief_read(args: Mapping[str, Any]) -> Mapping[str, Any]:
        limit = args.get("limit", 5)
        ref = args.get("ref")
        if ref is not None:
            if "cursor" in args:
                raise ToolArgumentsInvalid("cursor_invalid: ref cannot be combined with cursor")
            query = BeliefExactQuery(ref=_belief_ref(toolkit, ref), scope={})
            body = json.loads(toolkit.queries.query_exact_belief(toolkit.query_access, query).to_body())
            body.update(total=len(body["items"]), truncated=False, next_cursor=None)
            return {"result": body}
        try:
            result = read_belief_page(
                toolkit.queries, toolkit.query_access, status=args.get("status"),
                scope=toolkit.change_scope, limit=limit, cursor=args.get("cursor"),
            )
        except CursorError as exc:
            from .tool_catalog import ToolOutcome, TOOL_ARGUMENTS_INVALID
            return ToolOutcome(False, TOOL_ARGUMENTS_INVALID,
                               {"error": exc.cursor_code, "message": exc.cursor_code})
        return {"result": result}

    handlers["cognition.belief.read"] = belief_read

    def belief_history(args: Mapping[str, Any]) -> Mapping[str, Any]:
        record_id = str(args["record_id"])
        limit = int(args.get("limit") or 20)
        current = toolkit.store.get_current(toolkit.access.namespace, record_id)
        versions: list[dict[str, Any]] = []
        for version in range(1, int(current.record_version) + 1):
            row = toolkit.store.get_version(toolkit.access.namespace, record_id, version)
            body = row.body if isinstance(row.body, Mapping) else {}
            versions.append(
                {
                    "record_version": row.record_version,
                    "status": row.status,
                    "claim_type": body.get("claim_type"),
                    "strength": body.get("strength"),
                    "recorded_at": row.recorded_at,
                    "valid_until": body.get("valid_until"),
                    "supersedes": bool(body.get("supersedes")),
                }
            )
        truncated = len(versions) > limit
        return {
            "record_id": record_id,
            "versions": versions[-limit:],
            "truncated": truncated,
        }

    handlers["cognition.belief.history"] = belief_history

    def partition(args: Mapping[str, Any]) -> Mapping[str, Any]:
        view = build_epistemic_partition(
            toolkit.store,
            namespace=toolkit.access.namespace,
            access=toolkit.epistemic_access,
            limit=int(args.get("limit") or 20),
            include_questions=bool(args.get("include_questions", True)),
            question_access=toolkit.question_access,
        )
        payload = to_jsonable(view)
        payload["note"] = (
            "accepted means the domain derived support from qualified evidence; "
            "provisional means committed but unverified. Neither says the claim is true."
        )
        return {"result": payload}

    handlers["cognition.partition"] = partition

    def propose(args: Mapping[str, Any]) -> Mapping[str, Any]:
        changes = args.get("changes")
        if not isinstance(changes, list) or not changes:
            raise ValueError("changes must be a non-empty array")
        # RG05-F02: a user_preference proposal is routed by the host before a
        # candidate exists.  Explicit/single-shot/tentative/correction are host
        # decisions; the router returns None when the normal governed path must
        # run unchanged.
        if toolkit.preference_router is not None:
            routed = toolkit.preference_router.route(
                args, context_id=toolkit.context_id
            )
            if routed is not None:
                return {"result": routed}
        namespace = toolkit.access.namespace
        stamped: list[dict[str, Any]] = []
        for item in changes:
            if not isinstance(item, Mapping):
                raise ValueError("each change must be an object")
            change = dict(item)
            change.pop("scope", None)
            target = change.get("target")
            if isinstance(target, Mapping):
                change["target"] = {
                    "ref_type": "record",
                    "namespace": namespace,
                    "record_id": str(target.get("record_id")),
                    "record_version": int(target.get("record_version")),
                }
            if isinstance(change.get("body"), Mapping):
                change["body"] = _stamp_body(namespace, change["body"])
            # Only the belief-changing operations own a scope.  Stamping one on
            # retract/question changes makes the frozen bundle validator refuse
            # the change as an unknown field.
            if change.get("op") in ("propose_belief", "revise_belief"):
                change["scope"] = dict(toolkit.change_scope)
            stamped.append(change)
        bundle = {
            "operation": "cognition.change",
            "summary": str(args.get("summary") or ""),
            "changes": stamped,
        }
        if not toolkit.context_id:
            raise ValueError("this session has no bound Context; propose is unavailable")
        for index, change in enumerate(stamped):
            body = change.get("body")
            if isinstance(body, Mapping):
                claim_type = str(body.get("claim_type") or "")
                expected = INFERENCE_BASIS_BY_CLAIM_TYPE.get(claim_type)
                if expected is not None and body.get("inference_basis") != expected:
                    raise ToolArgumentsInvalid(
                        f"changes[{index}].body.inference_basis for claim_type "
                        f"{claim_type!r} must be {expected!r}; allowed pairs are "
                        + ", ".join(
                            f"{k}={v}"
                            for k, v in sorted(INFERENCE_BASIS_BY_CLAIM_TYPE.items())
                        )
                    )
                _require_known_refs(toolkit, body)
                _require_statement_evidence(toolkit, body)
            # A substantive revise is refused before any candidate exists.  The
            # shared check is the same one the maintenance path calls; the Tx B
            # claim freeze remains the final authority.
            refusal = check_revision_target(
                toolkit.store,
                namespace,
                change,
                index=index,
                surface=SURFACE_CHAT,
            )
            if refusal is not None:
                raise ToolArgumentsInvalid(refusal.message)
        # RG05: one stable identity per (turn, claim digest, target).  A second
        # identical proposal in the same turn is the same candidate, not a new
        # one; a different claim gets its own key and is never swallowed by a
        # turn-level de-duplication.
        request_id = _proposal_request_id(namespace, toolkit, stamped)
        lookup = getattr(toolkit.store, "candidate_for_request", None)
        existing = None if lookup is None else lookup(namespace, request_id)
        if existing is not None:
            return {"result": _existing_candidate(toolkit, existing)}
        result = toolkit.changes.propose(
            toolkit.access,
            context_id=toolkit.context_id,
            request_id=request_id,
            bundle=bundle,
        )
        # Registration is not the end of the governed path.  The host now asks
        # the policy what should happen and, when the policy says this change
        # may be committed under a pre-authorization, commits it -- the model
        # never holds a commit authority of its own.
        reviewed = toolkit.changes.review(
            toolkit.access,
            candidate_id=result.candidate_id,
            decision_id=_request_id(),
        )
        if reviewed.state == "COMMITTED":
            return {"result": to_jsonable(reviewed)}
        if reviewed.requires_human:
            # The review already decided this change needs a human.  Trying the
            # pre-authorization anyway cannot succeed (E2 is outside every
            # automatic grant) and would append a *second* decision row for the
            # same candidate, which pollutes the governance audit trail.
            return {"result": to_jsonable(reviewed)}
        # J06 grants name the *bundle* operation (``cognition.change``), not
        # the inner verb (``propose_belief``) and not "change.commit".  Two
        # different strings here is how a grant silently never matches.
        authorization = toolkit.auto_authorization(CHANGE_OPERATION)
        if authorization is None:
            return {"result": to_jsonable(reviewed)}
        from ..collaboration.policy import CommitAuthority

        committed = toolkit.changes.commit(
            toolkit.access,
            candidate_id=result.candidate_id,
            authority=CommitAuthority(auto_authorization_id=authorization),
            decision_id=_request_id(),
            appended_events=tuple(toolkit.appended_events),
        )
        if committed.state == "NOT_AUTHORIZED":
            # The candidate is real; the pre-authorization simply does not cover
            # it.  Report the candidate, not a failed commit.
            return {"result": to_jsonable(reviewed)}
        return {"result": to_jsonable(committed)}

    handlers["cognition.propose"] = propose

    def review(args: Mapping[str, Any]) -> Mapping[str, Any]:
        result = toolkit.changes.review(
            toolkit.access,
            candidate_id=str(args["candidate_id"]),
            decision_id=_request_id(),
        )
        return {"result": to_jsonable(result)}

    handlers["cognition.review"] = review

    def result_of(args: Mapping[str, Any]) -> Mapping[str, Any]:
        result = toolkit.changes.result(
            toolkit.access, candidate_id=str(args["candidate_id"])
        )
        return {"result": to_jsonable(result)}

    handlers["cognition.result"] = result_of

    def question_list(args: Mapping[str, Any]) -> Mapping[str, Any]:
        views = toolkit.questions.list_current(
            toolkit.question_access,
            statuses=(str(args.get("status") or "open"),),
            limit=int(args.get("limit") or 20),
        )
        return {"result": [to_jsonable(view) for view in views]}

    handlers["question.list"] = question_list

    def question_get(args: Mapping[str, Any]) -> Mapping[str, Any]:
        view = toolkit.questions.get(
            toolkit.question_access, ref=_record_ref(args["question_id"])
        )
        return {"result": to_jsonable(view)}

    handlers["question.get"] = question_get

    def question_create(args: Mapping[str, Any]) -> Mapping[str, Any]:
        authorization = toolkit.auto_authorization("question.create")
        result = toolkit.changes.question(
            toolkit.access,
            operation="question.create",
            context_id=toolkit.context_id,
            request_id=_request_id(),
            question=str(args["text"]),
            origin="system_proposed",
            scope={},
            privacy_class="private",
            priority=str(args.get("priority") or "normal"),
            auto_authorization_id=authorization,
        )
        return {"result": to_jsonable(result)}

    handlers["question.create"] = question_create

    def question_transition(args: Mapping[str, Any]) -> Mapping[str, Any]:
        action = str(args["action"])
        if action not in SINGLE_STEP_QUESTION_ACTIONS:
            raise ValueError(
                f"action {action!r} must travel through cognition.propose, not this tool"
            )
        operation = f"question.{action}"
        fields: dict[str, Any] = {
            "ref": _record_ref(args["question_id"]),
            "expected_version": int(args["expected_version"]),
            "auto_authorization_id": toolkit.auto_authorization(operation),
        }
        for name in ("waiting_for", "paused_by"):
            if args.get(name) is not None:
                fields[name] = args[name]
        refs = args.get("new_evidence_refs")
        if refs is not None:
            if not isinstance(refs, list):
                raise ValueError("new_evidence_refs must be an array")
            fields["new_evidence_refs"] = [
                _normalize_ref(toolkit.access.namespace, item) for item in refs
            ]
        result = toolkit.changes.question(
            toolkit.access,
            operation=operation,
            context_id=toolkit.context_id,
            request_id=_request_id(),
            **fields,
        )
        return {"result": to_jsonable(result)}

    handlers["question.transition"] = question_transition

    def attention_list(args: Mapping[str, Any]) -> Mapping[str, Any]:
        view = toolkit.questions.attention(
            toolkit.question_access,
            request=AttentionRequest(limit=int(args.get("limit") or 20)),
        )
        return {"result": to_jsonable(view)}

    handlers["attention.list"] = attention_list

    return {
        name: _remembering(toolkit, handler, register=register_refs)
        for name, handler in handlers.items()
    }


_REF_SHAPE = (
    'a record reference object like {"record_id": "<uuid>", '
    '"record_version": 1} (namespace is stamped by the host)'
)



def remember_refs(toolkit: "CognitionToolkit", value: Any) -> None:
    """Public wrapper: record the ids a tool just handed to the model."""

    _remember_refs(toolkit, value)


def _remembering(
    toolkit: "CognitionToolkit", handler: Handler, *, register: bool = True
) -> Handler:
    """Wrap a handler so every id it *delivers* becomes citable evidence."""

    def wrapped(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        result = handler(arguments)
        if register:
            _remember_refs(toolkit, result)
        return result

    return wrapped


def _remember_refs(toolkit: "CognitionToolkit", value: Any) -> None:
    """Record every record/event id whose *body* was handed to the model.

    A budget projection may describe a record by ref only.  Its ``ref`` names
    the record but carries no content, so registering that id would turn "I was
    told it exists" into "I checked the text" -- the model could then cite a
    body it never saw.  Any subtree marked ``body_included: False`` is skipped
    wholesale, and the id is registered only when the real body is delivered
    (either directly or by a later exact read).
    """

    if isinstance(value, Mapping):
        if value.get("body_included") is False:
            return
        for key, item in value.items():
            if key in ("record_id", "event_id") and isinstance(item, str):
                toolkit.known_refs.add(item)
            else:
                _remember_refs(toolkit, item)
    elif isinstance(value, list):
        for item in value:
            _remember_refs(toolkit, item)


def _require_known_refs(toolkit: "CognitionToolkit", body: Mapping[str, Any]) -> None:
    """An evidence link may only cite something this session actually returned.

    RG08 R-D adds exactly one more citable kind: the current turn's own
    ``collab_input``.  The Context already renders that exact object in
    ``inputs[].ref``, so citing it is not an invention.  Membership is checked
    as an exact ``(input_id, content_version)`` pair against the Context's
    stored source links; a previous turn's input, a wrong version, an absent
    Context and a fabricated id all stay refused.
    """

    links = body.get("evidence_links")
    if not isinstance(links, list):
        return
    allowed = set(toolkit.known_refs) | _context_ref_ids(toolkit)
    context_inputs = _context_input_refs(toolkit)
    for index, link in enumerate(links):
        if not isinstance(link, Mapping):
            continue
        ref = link.get("ref")
        if not isinstance(ref, Mapping):
            continue
        if ref.get("ref_type") == "collab_input":
            input_id = ref.get("input_id")
            version = ref.get("content_version")
            key = (
                input_id if isinstance(input_id, str) else "",
                version if type(version) is int else 0,
            )
            if key not in context_inputs:
                raise ToolArgumentsInvalid(
                    f"evidence_links[{index}].ref names a user input "
                    f"({key[0] or '?'}@{key[1] or '?'}) that this turn did not "
                    "render; only the current turn's input is citable"
                )
            continue
        identifier = ref.get("event_id") or ref.get("record_id")
        if not isinstance(identifier, str):
            continue
        if identifier not in allowed:
            raise ToolArgumentsInvalid(
                f"evidence_links[{index}].ref names {identifier!r}, which this "
                "session has not returned; evidence must come from a tool result "
                "or from this turn's Context"
            )


def _context_input_refs(toolkit: "CognitionToolkit") -> frozenset[tuple[str, int]]:
    """The exact ``collab_input`` versions the current Context rendered.

    Read from the Context's stored source links (the same closure the deletion
    barrier and the commit guard use), never from model text and never from a
    global input list.  A missing Context or an unreadable link grants nothing.
    """

    context_id = getattr(toolkit, "context_id", "")
    store = getattr(toolkit, "store", None)
    access = getattr(toolkit, "access", None)
    namespace = getattr(access, "namespace", "")
    if not context_id or store is None or not namespace:
        return frozenset()
    lookup = getattr(store, "context_collab_sources", None)
    if lookup is None:
        return frozenset()
    try:
        sources = lookup(namespace=namespace, context_id=context_id)
    except Exception:  # noqa: BLE001 - a missing/cleared Context grants nothing
        return frozenset()
    found: set[tuple[str, int]] = set()
    for source in sources or ():
        if not isinstance(source, Mapping):
            continue
        if source.get("ref_type") != "collab_input":
            continue
        input_id = source.get("input_id")
        version = source.get("content_version")
        if isinstance(input_id, str) and input_id and type(version) is int:
            found.add((input_id, version))
    return frozenset(found)


def _require_statement_evidence(toolkit: "CognitionToolkit", body: Mapping[str, Any]) -> None:
    """A user input may directly confirm the user's own preference only when it
    is a statement.

    RG08 R-D scope: this guard is deliberately **narrow**.  It applies only to
    ``claim_type == "user_preference"`` — the path whose evidence means "the
    user said this about themselves".  Other claim types keep their existing
    rules: the input must still be the current turn's exact input
    (``_require_known_refs``), the domain's ``_resolve_input_ref`` still checks
    ``origin_kind=user_statement``/readability, and Review/Tx B still decide
    whether the claim may be committed.  A question or a quoted/hypothetical
    sentence can therefore still be *material* for a non-preference claim; it
    just cannot be silently promoted to "the user's own preference".

    The event side already refuses a question event as evidence
    (``is_question_event_type``); a ``collab_input`` has no event type, so the
    preference path uses the existing deterministic directive classifier plus
    the whole-statement checks below.  Nothing here interprets the claim or the
    input's topic; that stays with Review and (for real-model semantics) R-F.
    """

    if body.get("claim_type") != "user_preference":
        return
    links = body.get("evidence_links")
    if not isinstance(links, list):
        return
    namespace = getattr(getattr(toolkit, "access", None), "namespace", "")
    store = getattr(toolkit, "store", None)
    for index, link in enumerate(links):
        if not isinstance(link, Mapping):
            continue
        ref = link.get("ref")
        if not isinstance(ref, Mapping) or ref.get("ref_type") != "collab_input":
            continue
        input_id = ref.get("input_id")
        version = ref.get("content_version")
        if not isinstance(input_id, str) or type(version) is not int:
            raise ToolArgumentsInvalid(
                f"evidence_links[{index}].ref needs input_id and content_version"
            )
        if store is None or not namespace:
            raise ToolArgumentsInvalid(
                f"evidence_links[{index}].ref cannot be checked without a Context"
            )
        try:
            row = store.load_input(
                namespace=namespace, input_id=input_id, content_version=version
            )
        except Exception:  # noqa: BLE001 - unreadable input is refused as data
            raise ToolArgumentsInvalid(
                f"evidence_links[{index}].ref names an input this host cannot read"
            ) from None
        if getattr(row, "origin_kind", "") != "user_statement":
            raise ToolArgumentsInvalid(
                f"evidence_links[{index}].ref is not a user statement"
            )
        # J11 consolidation: what the host checks here is *provenance*, not
        # meaning.  The ref must name an input this host can read, and that
        # input must be the user's own statement -- that is the evidence a
        # candidate is allowed to cite.  Whether the sentence is a question, a
        # quotation, a hedged guess or a report of what someone else said is a
        # reading of the text; the model proposes the claim and Review decides
        # its impact.  Choosing that reading with a lexicon is exactly what this
        # change removes, so no cue list and no directive classifier runs here.


def _context_ref_ids(toolkit: "CognitionToolkit") -> set[str]:
    """Record ids rendered into the current Context (RG05 working set).

    The model saw these exact versions in ``cognition``/``query_results``; a
    citation of one is not an invention.  Events are deliberately not included:
    nothing in the chat Context hands a model an event body as evidence.  A test
    double without a Context simply grants nothing extra.
    """

    context_id = getattr(toolkit, "context_id", "")
    store = getattr(toolkit, "store", None)
    if not context_id or store is None:
        return set()
    try:
        snapshot = store.load_context_snapshot(context_id)
    except Exception:  # noqa: BLE001 - a missing/cleared Context grants nothing
        return set()
    found: set[str] = set()
    for entry in getattr(snapshot, "read_set", ()) or ():
        ref = entry.get("ref") if isinstance(entry, Mapping) else None
        if isinstance(ref, Mapping):
            record_id = ref.get("record_id")
            if isinstance(record_id, str) and record_id:
                found.add(record_id)
    return found


_PROPOSAL_NAMESPACE = uuid.UUID("5b0f5e2a-6f1c-5d7a-9c1e-6b7a2f4d8c31")


def _proposal_request_id(
    namespace: str, toolkit: "CognitionToolkit", stamped: Sequence[Mapping[str, Any]]
) -> str:
    """A stable identity for one claim in one turn (RG05 G06)."""

    digest = sha256_hex(canonical_json_text(list(stamped)))
    target = ""
    for change in stamped:
        candidate = change.get("target")
        if isinstance(candidate, Mapping) and isinstance(
            candidate.get("record_id"), str
        ):
            target = candidate["record_id"]
            break
    turn = toolkit.turn_id or toolkit.context_id
    return str(
        uuid.uuid5(_PROPOSAL_NAMESPACE, f"{namespace}:{turn}:{digest}:{target}")
    )


def _existing_candidate(
    toolkit: "CognitionToolkit", row: Mapping[str, Any]
) -> dict[str, Any]:
    """Report the already-registered candidate instead of proposing twice."""

    from ..store.governance import GovernanceStore

    namespace = toolkit.access.namespace
    candidate_id = str(row["artifact_id"])
    version = int(row["artifact_version"])
    governance = GovernanceStore(toolkit.store)
    lifecycle = governance.lifecycle(namespace, candidate_id)
    decision = (
        governance.latest_decision(namespace, candidate_id, version)
        if lifecycle == "pending"
        else None
    )
    if lifecycle == "pending":
        state = "PROPOSED" if decision is None else str(decision.decision).upper()
    else:
        state = lifecycle.upper()
    return {
        "state": state,
        "candidate_id": candidate_id,
        "artifact_version": version,
        "deduped": True,
        "requires_human": bool(decision.requires_human) if decision else False,
        "impact": decision.impact if decision else None,
        "reason_code": decision.reason_code if decision else None,
    }


def _normalize_ref(namespace: str, value: Any) -> dict[str, Any]:
    """One place that turns any accepted ref shape into a stamped ref.

    Branching on the shape in more than one place is how namespace stamping
    drifted: a ref without ``ref_type`` took the event branch and produced a
    record whose namespace was None.
    """

    if isinstance(value, (RecordRef, EventRef)):
        return value.to_dict()
    if isinstance(value, str) or not isinstance(value, Mapping):
        raise ToolArgumentsInvalid(
            "a reference must be an object like {record_id, record_version} or "
            "{event_id}; a bare string is not accepted"
        )
    ref = dict(value)
    ref.pop("namespace", None)
    kind = ref.get("ref_type")
    if kind == "collab_input":
        input_id = ref.get("input_id")
        version = ref.get("content_version")
        if not isinstance(input_id, str) or not input_id:
            raise ToolArgumentsInvalid(
                "a collab_input reference needs a non-empty input_id"
            )
        if type(version) is not int or version < 1:
            raise ToolArgumentsInvalid(
                "a collab_input reference needs a positive integer content_version"
            )
        return {
            "ref_type": "collab_input",
            "namespace": namespace,
            "input_id": input_id,
            "content_version": version,
        }
    if kind == "event" or (kind is None and "event_id" in ref):
        event_id = ref.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ToolArgumentsInvalid("an event reference needs a non-empty event_id")
        return {"ref_type": "event", "namespace": namespace, "event_id": event_id}
    record_id = ref.get("record_id")
    if not isinstance(record_id, str) or not record_id:
        raise ToolArgumentsInvalid(
            "a record reference needs a non-empty record_id; accepted shapes are "
            "{record_id, record_version} or {event_id}"
        )
    return {
        "ref_type": "record",
        "namespace": namespace,
        "record_id": record_id,
        "record_version": int(ref.get("record_version") or 1),
    }


def _stamp_body(namespace: str, body: Any) -> dict[str, Any]:
    """Rewrite the two reference-bearing fields of a belief body."""

    if not isinstance(body, Mapping):
        raise ValueError("belief body must be an object")
    stamped = dict(body)
    links = stamped.get("evidence_links")
    if isinstance(links, list):
        new_links = []
        for item in links:
            if not isinstance(item, Mapping):
                raise ValueError("each evidence link must be an object")
            link = dict(item)
            link["ref"] = _normalize_ref(namespace, link.get("ref"))
            new_links.append(link)
        stamped["evidence_links"] = new_links
    premises = stamped.get("premise_refs")
    if isinstance(premises, list):
        stamped["premise_refs"] = [_normalize_ref(namespace, item) for item in premises]
    return stamped


def _belief_ref(toolkit: "CognitionToolkit", value: Any) -> Any:
    """Turn one model-supplied reference into the host-stamped record ref.

    The schema already describes this shape; this is the second gate so a call
    that bypasses validation still gets a refusal the model can act on instead
    of a bare ``ValueError``.
    """

    from ..domains.refs import RecordRef

    if isinstance(value, RecordRef):
        return value
    if not isinstance(value, Mapping):
        raise ValueError(f"ref must be {_REF_SHAPE}, not {type(value).__name__}")
    record_id = value.get("record_id")
    version = value.get("record_version")
    if not isinstance(record_id, str) or not record_id:
        raise ValueError(f"ref.record_id is required; ref must be {_REF_SHAPE}")
    if type(version) is not int or version < 1:
        raise ValueError(f"ref.record_version must be a positive integer; ref must be {_REF_SHAPE}")
    namespace = value.get("namespace") or toolkit.access.namespace
    if namespace != toolkit.access.namespace:
        raise ValueError("ref.namespace is stamped by the host and may not be overridden")
    return RecordRef.from_value(
        {
            "ref_type": "record",
            "namespace": toolkit.access.namespace,
            "record_id": record_id,
            "record_version": version,
        }
    )


def _record_ref(value: Any) -> Any:
    from ..domains.refs import RecordRef

    if isinstance(value, RecordRef):
        return value
    if isinstance(value, Mapping):
        return RecordRef.from_value(dict(value))
    raise ValueError("expected a record reference object")


# ---------------------------------------------------------------------------
# web handlers
# ---------------------------------------------------------------------------


def build_web_handlers(
    *,
    client: Any,
    store: Any,
    namespace: str,
    now: Callable[[], str],
    capture_default: str = "metadata",
    policy: Any = None,
    remember: Callable[[Any], None] | None = None,
    on_appended_event: Callable[[str], None] | None = None,
) -> dict[str, Handler]:
    """Build ``web.search`` and ``web.fetch``.

    ``web.fetch`` with ``capture="evidence"`` ingests one ``WebPageFetched:v1``
    event through the store's public ingest API, so a later ``external_report``
    can cite the quote against a real stored payload.  Nothing else is written:
    a plain search is not a cognition change and does not touch the database.

    A refusal is returned as data, not raised as an opaque crash: the provider's
    stable error code (``web_timeout``, ``web_forbidden``, ...) reaches the
    model, and a host that never configured a credential answers
    ``web_not_configured``.  The URL policy runs **before** the provider is
    called, so a model cannot use ``web.fetch`` to reach an address the host
    would not fetch itself.
    """

    handlers: dict[str, Handler] = {}

    def _unavailable() -> ToolOutcome:
        return ToolOutcome(
            False,
            WEB_NOT_CONFIGURED,
            {
                "message": (
                    "this host has no web credential configured; nothing was "
                    "searched or fetched"
                )
            },
        )

    def _failed(exc: WebToolError) -> ToolOutcome:
        return ToolOutcome(False, exc.code, {"message": exc.message})

    def search(args: Mapping[str, Any]) -> Any:
        if client is None:
            return _unavailable()
        try:
            payload = client.search(
                query=str(args["query"]), count=int(args.get("count") or 5)
            )
        except WebToolError as exc:
            return _failed(exc)
        except TimeoutError:
            return ToolOutcome(
                False, WEB_TIMEOUT, {"message": "the web request timed out"}
            )
        results = []
        for item in payload.get("results", []):
            results.append(
                {
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "summary": item.get("summary"),
                    "snippet": item.get("snippet"),
                    "date": item.get("date"),
                    "site_name": item.get("site_name"),
                    # Ranking hints, explicitly not evidence qualification.
                    "ranking_only": {
                        "authority": item.get("authority"),
                        "rerank_score": item.get("rerank_score"),
                    },
                }
            )
        return {
            "results": results,
            "note": (
                "authority and rerank_score are ordering hints from the search "
                "engine, not evidence qualification and not a verified fact."
            ),
        }

    handlers["web.search"] = search

    def fetch(args: Mapping[str, Any]) -> Any:
        if client is None:
            return _unavailable()
        capture = str(args.get("capture") or capture_default)
        if capture not in ("metadata", "evidence"):
            raise ToolArgumentsInvalid("capture must be 'metadata' or 'evidence'")
        target = str(args["url"])
        if policy is not None:
            # PermissionError below becomes ``tool_not_authorized`` in the
            # catalog: the model may ask, the host decides.
            target = policy.check(target)
        try:
            summary, event_payload = client.fetch(url=target, capture=capture)
        except WebToolError as exc:
            return _failed(exc)
        except TimeoutError:
            return ToolOutcome(
                False, WEB_TIMEOUT, {"message": "the web request timed out"}
            )
        answer: dict[str, Any] = {
            "url": summary.get("url"),
            "title": summary.get("title"),
            "content_sha256": summary.get("content_sha256"),
            "content_type": summary.get("content_type"),
            "captured": summary.get("captured"),
            "truncated": summary.get("truncated"),
        }
        if summary.get("captured"):
            event = store.ingest_event(
                namespace=namespace,
                source_kind="host_observation",
                source_instance="j07.web",
                source_event_id=f"web-{uuid.uuid4()}",
                event_type="WebPageFetched:v1",
                observed_at=now(),
                payload=event_payload,
                recorded_at=now(),
            )
            answer["event_ref"] = {
                "ref_type": "event",
                "namespace": namespace,
                "event_id": event.event_id,
            }
            if on_appended_event is not None:
                # Only the host may whitelist an event, and only right after it
                # really stored one.
                on_appended_event(event.event_id)
            captured = event_payload.get("text") or ""
            # The model sees a bounded head of the page; the stored event keeps
            # the whole text, and a quote is verified against *that*, not
            # against what the model was shown.
            answer["text"] = captured[:4000]
            answer["text_len"] = len(captured)
            answer["truncated"] = len(captured) > 4000
            answer["note"] = (
                "The stored text is what the host received at fetch time. A "
                "quote from it can be mechanically checked; that check does not "
                "mean the page's claim is true."
            )
        else:
            answer["note"] = (
                "capture='metadata' stored no page text, so this fetch cannot "
                "back an external_report that quotes it."
            )
        return answer

    handlers["web.fetch"] = fetch

    if remember is not None:
        for name, handler in list(handlers.items()):
            def make(n=name, h=handler):
                def run(arguments):
                    result = h(arguments)
                    remember(result)
                    return result
                return run
            handlers[name] = make()
    return handlers


def build_handlers(
    *,
    cognition: Mapping[str, Handler] | None = None,
    web: Mapping[str, Handler] | None = None,
) -> dict[str, Handler]:
    """Merge handler sets; unknown tool names are simply absent."""

    merged: dict[str, Handler] = {}
    for group in (cognition, web):
        if group:
            merged.update(group)
    return merged


__all__ = [
    "CognitionToolkit",
    "Handler",
    "SINGLE_STEP_QUESTION_ACTIONS",
    "build_cognition_handlers",
    "build_handlers",
    "build_web_handlers",
    "to_jsonable",
]
