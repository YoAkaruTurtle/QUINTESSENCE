"""RG03: the closed tool surface a chat turn may use.

Three rules shape this module:

1. **The catalog is host-generated.**  Only the read-only tools below (plus the
   opt-in ``web.*`` pair) are published; a model can never name a bridge
   method, SQL, path or another tool.  ``ToolCatalog.execute`` refuses an
   unknown name, invalid arguments and (via the handlers) missing authority
   before anything runs.
2. **Authority is injected.**  Every access object is built by the trusted host
   from the private configuration; nothing in the model JSON can widen it.
3. **A tool result is data.**  The exact record/event references a handler
   returned are collected with their versions so the result artifact can cite
   them as sources; a deletion of the read record then clears the result
   through the existing source closure.  Bare ids are not sources.

``cognition.propose`` is deliberately absent: candidate formation and Review
belong to RG05.  ``question.get`` is absent because its published schema (a
bare string) does not match its handler (a record reference) -- a known
defect recorded for RG05/a micro-commit.

RG08 adds ``web.search`` / ``web.fetch`` to the **content** stage only, and only
when the host configuration opted in.  The expression stage is built with
``tool_definitions=()`` in the orchestrator and can never declare one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ..domains.cognition.epistemic import EpistemicAccess
from ..domains.cognition.query import CognitionQueryService, QueryAccess
from ..domains.cognition.questions import QuestionAccess, QuestionService
from ..foundation.errors import InvalidInput
from ..foundation.jsonutil import canonical_json_text
from ..runtime.agent_protocol import ToolCall
from ..runtime.tool_catalog import (
    COGNITION_TOOLS,
    MEMORY_TOOLS,
    TRANSCRIPT_TOOLS,
    WEB_TOOLS,
    ToolArgumentsInvalid,
    ToolCatalog,
    ToolOutcome,
)
from ..runtime.tool_handlers import (
    CognitionToolkit,
    build_cognition_handlers,
    build_web_handlers,
)

#: RG04: five cognition readers plus the transcript-archive reader.
TOOL_WHITELIST: tuple[str, ...] = (
    "cognition.belief.read",
    "cognition.belief.history",
    "cognition.partition",
    "question.list",
    "attention.list",
    "conversation.search",
)

#: RG08: the only tools that cross the host boundary to the network.  They are
#: published to the **content** stage only, and only when the host enabled them;
#: the expression stage is always tool-free.
WEB_TOOL_NAMES: tuple[str, ...] = ("web.search", "web.fetch")

#: RG05: the one write-shaped tool a chat turn may use.  It never commits on
#: its own: the host's pre-authorization decides, and the model never holds it.
PROPOSE_TOOL = "cognition.propose"

#: RG08: read-only long-term memory search.  Published only when the host has a
#: configured Scope Recall bridge; the default catalog stays unchanged.
MEMORY_TOOL = "memory.search"

MAX_TOOL_ARGUMENTS_CHARS = 8192
#: The tool excerpt the model sees is bounded to roughly 1 KiB.  Two items
#: with a short provenance label and a clipped body fit without being cut
#: mid-JSON; a broader answer needs a narrower query or the next page.
MAX_MEMORY_TOOL_ITEMS = 2
MAX_MEMORY_TOOL_CONTENT = 112
#: A page cursor is a short host token, not the upstream envelope: the real
#: cursor is ~280 characters, which would evict the page itself from the 1 KiB
#: excerpt.  The host keeps the upstream cursor and hands the model this.
MAX_MEMORY_TOOL_CURSOR = 96


@dataclass(frozen=True)
class WebToolConfig:
    """The host-built web surface for one runtime.

    ``client`` is a fully configured provider client or ``None`` when the host
    opted in but did not supply a credential -- the handlers then answer
    ``web_not_configured`` instead of pretending.  ``policy`` is the host's
    ``web.fetch`` egress policy; the model never chooses it.
    """

    client: Any
    policy: Any = None
    search: bool = True
    fetch: bool = True
    capture_default: str = "metadata"

    def __post_init__(self) -> None:
        if self.capture_default not in ("metadata", "evidence"):
            raise ValueError("capture_default must be 'metadata' or 'evidence'")

    def names(self) -> tuple[str, ...]:
        names: list[str] = []
        if self.search:
            names.append("web.search")
        if self.fetch:
            names.append("web.fetch")
        return tuple(names)


def build_catalog(
    *,
    include_propose: bool = False,
    include_memory: bool = False,
    web_names: Sequence[str] = (),
) -> ToolCatalog:
    """One catalog holding exactly the enabled specs.

    ``cognition.propose`` is published only when the host opted in *and* has an
    authorized change scope; ``memory.search`` only when a memory bridge is
    configured.  Web tools are appended only when the host named them.  With
    all flags off the catalog is byte-identical to RG03/RG04.
    """

    requested_web = tuple(web_names)
    unknown = sorted(set(requested_web) - set(WEB_TOOL_NAMES))
    if unknown:  # pragma: no cover - caller drift
        raise ValueError(f"unknown web tool(s): {', '.join(unknown)}")
    allowed = (
        TOOL_WHITELIST
        + ((PROPOSE_TOOL,) if include_propose else ())
        + ((MEMORY_TOOL,) if include_memory else ())
        + requested_web
    )
    specs = tuple(
        spec
        for spec in COGNITION_TOOLS + TRANSCRIPT_TOOLS + MEMORY_TOOLS + WEB_TOOLS
        if spec.name in allowed
    )
    if len(specs) != len(allowed):  # pragma: no cover - source drift
        raise ValueError("the tool whitelist no longer matches the catalogs")
    return ToolCatalog(specs)


@dataclass(frozen=True)
class ToolRuntime:
    """The host-owned catalog/handlers for one step.

    ``rebind`` returns a runtime for the *same turn*: the deliverable-ref
    registry (``known_refs``/``appended_events``) and the service objects are
    shared, only the current Context id and handlers are rebuilt.  Without it a
    ``cognition.propose`` could never cite a belief an earlier step read.
    """

    catalog: ToolCatalog
    handlers: Mapping[str, Callable[[Mapping[str, Any]], Any]]
    toolkit: CognitionToolkit
    _rebuild: Callable[[str, str], "ToolRuntime"] | None = field(
        default=None, repr=False, compare=False
    )

    def rebind(self, context_id: str, turn_id: str = "") -> "ToolRuntime":
        if self._rebuild is None:  # pragma: no cover - always built with one
            return self
        return self._rebuild(context_id, turn_id)


def build_tool_runtime(
    store: Any,
    *,
    queries: CognitionQueryService,
    questions: QuestionService,
    access: Any,
    query_access: QueryAccess,
    question_access: QuestionAccess,
    epistemic_access: EpistemicAccess,
    change_scope: Mapping[str, Any],
    context_id: str,
    auto_authorization: Callable[[str], str | None] = lambda _operation: None,
    transcript_access: Any = None,
    transcript_host_instance: str | None = None,
    minimum_retention: str = "standard",
    include_propose: bool = False,
    known_refs: set[str] | None = None,
    appended_events: set[str] | None = None,
    turn_id: str = "",
    preference_router: Any = None,
    memory_reader: Callable[..., Any] | None = None,
    web: WebToolConfig | None = None,
) -> ToolRuntime:
    """Build the host-owned toolkit/handlers for one step's Context."""

    refs = set() if known_refs is None else known_refs
    events = set() if appended_events is None else appended_events
    toolkit = CognitionToolkit(
        store=store,
        changes=_change_service(store),
        queries=queries,
        questions=questions,
        access=access,
        query_access=query_access,
        question_access=question_access,
        epistemic_access=epistemic_access,
        now=_utc_now,
        change_scope=dict(change_scope),
        context_id=context_id,
        known_refs=refs,
        appended_events=events,
        auto_authorization=auto_authorization,
        turn_id=turn_id,
        preference_router=preference_router,
    )
    handlers = dict(build_cognition_handlers(toolkit))
    handlers["conversation.search"] = _transcript_search_handler(
        store,
        access=transcript_access,
        host_instance=transcript_host_instance,
        minimum_retention=minimum_retention,
    )
    include_memory = memory_reader is not None
    if include_memory:
        handlers[MEMORY_TOOL] = _memory_search_handler(memory_reader)
    web_names: tuple[str, ...] = () if web is None else web.names()
    if web_names:
        web_handlers = build_web_handlers(
            client=web.client,
            store=store,
            namespace=toolkit.access.namespace,
            now=_utc_now,
            capture_default=web.capture_default,
            policy=web.policy,
            on_appended_event=_remember_web_event(events, refs),
        )
        for name in web_names:
            handlers[name] = web_handlers[name]
    catalog = build_catalog(
        include_propose=include_propose,
        include_memory=include_memory,
        web_names=web_names,
    )
    enabled = (
        TOOL_WHITELIST
        + ((PROPOSE_TOOL,) if include_propose else ())
        + ((MEMORY_TOOL,) if include_memory else ())
        + web_names
    )
    handlers = {name: handlers[name] for name in enabled}

    def rebuild(new_context_id: str, new_turn_id: str = "") -> ToolRuntime:
        return build_tool_runtime(
            store,
            queries=queries,
            questions=questions,
            access=access,
            query_access=query_access,
            question_access=question_access,
            epistemic_access=epistemic_access,
            change_scope=change_scope,
            context_id=new_context_id,
            auto_authorization=auto_authorization,
            transcript_access=transcript_access,
            transcript_host_instance=transcript_host_instance,
            minimum_retention=minimum_retention,
            include_propose=include_propose,
            known_refs=refs,
            appended_events=events,
            turn_id=new_turn_id or turn_id,
            preference_router=preference_router,
            memory_reader=memory_reader,
            web=web,
        )

    return ToolRuntime(
        catalog=catalog,
        handlers=handlers,
        toolkit=toolkit,
        _rebuild=rebuild,
    )


def _transcript_search_handler(
    store: Any,
    *,
    access: Any,
    host_instance: str | None,
    minimum_retention: str,
) -> Callable[[Mapping[str, Any]], Any]:
    """A read-only closure over the host-owned transcript access."""

    def handler(arguments: Mapping[str, Any]) -> Any:
        if access is None or host_instance is None:
            raise ToolArgumentsInvalid("transcript access is not configured")
        query = arguments.get("query")
        limit = arguments.get("limit", 5)
        conversation_id = arguments.get("conversation_id")
        before = arguments.get("before")
        include_full = arguments.get("include_full", False)
        try:
            page = store.search_transcript(
                namespace=access.namespace,
                host_instance=host_instance,
                access=access,
                query=query,
                limit=limit,
                before=before,
                conversation_id=conversation_id,
                include_full=include_full,
                minimum_retention=minimum_retention,
            )
        except InvalidInput as exc:
            raise ToolArgumentsInvalid(str(exc)) from None
        return page.as_dict()

    return handler


def _memory_search_handler(
    reader: Callable[..., Any],
) -> Callable[[Mapping[str, Any]], Any]:
    """A read-only closure over the host-owned Scope Recall search.

    The reader is the same sidecar service the user-side panel calls; the tool
    gets no path, executable, scope, agent or session.  A refusal is a value
    the model can read, never an exception carrying internals.

    One call returns **one literal-match page**.  ``cursor`` continues the same
    query from where the previous page stopped; it is optional, and a stale or
    unknown cursor is its own state so the model retries from the start instead
    of concluding the archive is empty.
    """

    def handler(arguments: Mapping[str, Any]) -> Any:
        query = arguments.get("query")
        limit = arguments.get("limit", MAX_MEMORY_TOOL_ITEMS)
        cursor = arguments.get("cursor")
        if not isinstance(query, str) or not query.strip():
            raise ToolArgumentsInvalid("query must be a non-empty string")
        if type(limit) is not int or not 1 <= limit <= MAX_MEMORY_TOOL_ITEMS:
            raise ToolArgumentsInvalid(
                f"limit must be an integer between 1 and {MAX_MEMORY_TOOL_ITEMS}"
            )
        if cursor is not None and (
            not isinstance(cursor, str)
            or not 1 <= len(cursor) <= MAX_MEMORY_TOOL_CURSOR
        ):
            raise ToolArgumentsInvalid(
                "cursor must be a page cursor from a previous result"
            )
        try:
            if cursor is None:
                page = reader(query=query, limit=limit)
            else:
                page = reader(query=query, limit=limit, cursor=cursor)
        except PermissionError:
            raise
        except Exception as exc:  # noqa: BLE001 - tool failure is data
            if getattr(exc, "code", "") == "MEMORY_CURSOR_STALE":
                return ToolOutcome(
                    False,
                    "memory_cursor_stale",
                    {
                        "message": (
                            "this page cursor is unknown or has expired; repeat "
                            "the search from the beginning"
                        )
                    },
                )
            return ToolOutcome(
                False,
                "memory_unavailable",
                {"message": "memory search is not available for this instance"},
            )
        if not isinstance(page, Mapping):
            return ToolOutcome(
                False,
                "memory_unavailable",
                {"message": "memory search returned an invalid page"},
            )
        return _memory_tool_payload(page)

    return handler


def _memory_tool_payload(page: Mapping[str, Any]) -> Mapping[str, Any]:
    """Shape one page for the model: bounded, attributed, no PCS claims.

    ``memory.search`` returns stored conversation events, not an abstract
    knowledge base, so every item carries the speaker the host resolved
    (``user``/``assistant``/``tool``/``system``) and the exact experience ref it
    came from.  Without that label an assistant sentence looks like something
    the user said.  ``examined``/``shown``/``stop_reason`` travel together so a
    zero-hit answer is distinguishable from a query that never ran, and
    ``next_cursor`` marks the page as one page of a larger literal match.
    """

    raw_items = page.get("items")
    if not isinstance(raw_items, (list, tuple)):
        raw_items = ()
    items = []
    for raw in raw_items[:MAX_MEMORY_TOOL_ITEMS]:
        if not isinstance(raw, Mapping):
            continue
        content = raw.get("content")
        content = content if isinstance(content, str) else ""
        sources = []
        raw_sources = raw.get("sources")
        if isinstance(raw_sources, (list, tuple)) and raw_sources:
            first = raw_sources[0]
            if isinstance(first, Mapping):
                sources.append(
                    {
                        "entry_id": str(first.get("entry_id") or "unknown")[:48],
                        "occurred_at": str(
                            first.get("occurred_at") or first.get("recorded_at") or ""
                        )[:40],
                    }
                )
        revision = raw.get("revision")
        items.append(
            {
                "kind": str(raw.get("kind") or ""),
                "ref": str(raw.get("ref") or "")[:72],
                "revision": revision if type(revision) is int else 0,
                "state": str(raw.get("state") or "")[:32],
                "subject": str(raw.get("subject") or "")[:16],
                "subject_ref": str(raw.get("subject_ref") or "")[:56],
                "sources": sources,
                "content": content[:MAX_MEMORY_TOOL_CONTENT],
                "content_truncated": bool(raw.get("content_truncated"))
                or len(content) > MAX_MEMORY_TOOL_CONTENT,
            }
        )
    scope_value = ""
    if raw_items and isinstance(raw_items[0], Mapping):
        candidate = raw_items[0].get("scope_id")
        if isinstance(candidate, str):
            scope_value = candidate[:64]
    next_cursor = page.get("next_cursor")
    return {
        "source": "scope_recall",
        "scope_id": scope_value,
        "items": items,
        "examined": page.get("examined") if type(page.get("examined")) is int else 0,
        "shown": len(items),
        "has_more": next_cursor is not None or len(raw_items) > len(items),
        "next_cursor": str(next_cursor)[:MAX_MEMORY_TOOL_CURSOR]
        if isinstance(next_cursor, str) and next_cursor
        else "",
        "stop_reason": str(page.get("stop_reason") or "")[:32],
        "note": (
            "Scope Recall events, both speakers; one literal page. "
            "subject = who said it; state = freshness."
        ),
    }
def _remember_web_event(
    appended_events: set[str], known_refs: set[str]
) -> Callable[[str], None]:
    """Host callback after a ``WebPageFetched:v1`` event was really stored.

    The event id is both the turn's appended-event watermark entry and a ref the
    model may cite: it saw that exact page text in the fetch result.
    """

    def remember(event_id: str) -> None:
        appended_events.add(event_id)
        known_refs.add(event_id)

    return remember


def _change_service(store: Any) -> Any:
    from ..collaboration.change_service import CognitionChangeService

    return CognitionChangeService(store)


def _utc_now() -> str:
    from ..foundation.timeutil import utc_now_rfc3339

    return utc_now_rfc3339()


def tool_call_from_output(tool_call: Mapping[str, Any]) -> ToolCall:
    """Normalise a parsed v2 ``tool_call`` into the catalog's input type."""

    if not isinstance(tool_call, Mapping):
        raise ValueError("tool_call must be an object")
    name = tool_call.get("name")
    arguments = tool_call.get("arguments")
    if not isinstance(name, str) or not name:
        raise ValueError("tool_call.name must be a non-empty string")
    if not isinstance(arguments, Mapping):
        raise ValueError("tool_call.arguments must be an object")
    text = canonical_json_text(dict(arguments))
    if len(text) > MAX_TOOL_ARGUMENTS_CHARS:
        raise ValueError("tool_call.arguments is too large")
    return ToolCall(call_id=str(uuid.uuid4()), name=name, arguments=text)


def execute_tool(
    runtime: ToolRuntime, tool_call: Mapping[str, Any]
) -> tuple[ToolCall, ToolOutcome]:
    """Run one validated call; a refusal is a value, never an exception."""

    call = tool_call_from_output(tool_call)
    return call, runtime.catalog.execute(call, handlers=runtime.handlers)


# --------------------------------------------------------------------------
# exact reference collection (deletion-barrier closure for tool results)
# --------------------------------------------------------------------------


def collect_exact_refs(
    payload: Any, namespace: str
) -> tuple[dict[str, Any], ...]:
    """Collect the exact record/event refs a handler actually returned.

    Only references with a version are accepted: a bare id names existence, not
    the text that was delivered, and must not become a derivation source.  The
    collector is host-side over handler output (never model text) and does not
    resolve existence; ``register_artifact`` re-proves every source before it
    writes the link.
    """

    found: dict[str, dict[str, Any]] = {}

    def add(reference: dict[str, Any]) -> None:
        found.setdefault(canonical_json_text(reference), reference)

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            ref = _ref_from_mapping(value, namespace)
            if ref is not None:
                add(ref)
            for child in value.values():
                walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child)

    walk(payload)
    return tuple(found.values())


def _ref_from_mapping(
    value: Mapping[str, Any], namespace: str
) -> dict[str, Any] | None:
    ref_namespace = value.get("namespace")
    if ref_namespace is not None and ref_namespace != namespace:
        return None
    ref_type = value.get("ref_type")
    if ref_type == "event":
        event_id = value.get("event_id")
        if isinstance(event_id, str) and event_id:
            return {
                "ref_type": "event",
                "namespace": namespace,
                "event_id": event_id,
            }
        return None
    # RG04: a transcript-search result cites the exact public messages it read,
    # so a deletion of one of them clears the step result through the existing
    # artifact source closure.  Only host-shaped refs with a version qualify.
    if ref_type == "collab_input":
        input_id = value.get("input_id")
        content_version = value.get("content_version")
        if (
            isinstance(input_id, str)
            and input_id
            and type(content_version) is int
            and content_version >= 1
        ):
            return {
                "ref_type": "collab_input",
                "namespace": namespace,
                "input_id": input_id,
                "content_version": content_version,
            }
        return None
    if ref_type == "collab_artifact":
        artifact_id = value.get("artifact_id")
        artifact_version = value.get("artifact_version")
        if (
            isinstance(artifact_id, str)
            and artifact_id
            and type(artifact_version) is int
            and artifact_version >= 1
        ):
            return {
                "ref_type": "collab_artifact",
                "namespace": namespace,
                "artifact_id": artifact_id,
                "artifact_version": artifact_version,
            }
        return None
    if ref_type not in (None, "record"):
        return None
    record_id = value.get("record_id")
    record_version = value.get("record_version")
    if (
        isinstance(record_id, str)
        and record_id
        and type(record_version) is int
        and record_version >= 1
        and (
            ref_type == "record"
            or ("record_id" in value and "record_version" in value)
        )
    ):
        return {
            "ref_type": "record",
            "namespace": namespace,
            "record_id": record_id,
            "record_version": record_version,
        }
    return None


def merge_tool_sources(
    context_sources: Sequence[Mapping[str, Any]],
    read_refs: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Union the Context's material sources with the tool's read refs."""

    merged: dict[str, dict[str, Any]] = {}
    for reference in tuple(context_sources) + tuple(read_refs):
        merged.setdefault(canonical_json_text(dict(reference)), dict(reference))
    return list(merged.values())


__all__ = [
    "MAX_TOOL_ARGUMENTS_CHARS",
    "MEMORY_TOOL",
    "PROPOSE_TOOL",
    "TOOL_WHITELIST",
    "WEB_TOOL_NAMES",
    "ToolRuntime",
    "WebToolConfig",
    "build_catalog",
    "build_tool_runtime",
    "collect_exact_refs",
    "execute_tool",
    "merge_tool_sources",
    "tool_call_from_output",
]
