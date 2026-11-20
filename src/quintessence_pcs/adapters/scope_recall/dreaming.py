"""Scope Recall's worker and retrieval over an instance-local PCS projection."""

from __future__ import annotations

import re
import sqlite3
import stat
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack, closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from ...domains.refs import EventRef
from ...foundation import identifiers
from ...foundation.errors import CorruptStore, InvalidInput
from ...foundation.jsonutil import canonical_json_text, sha256_hex, strict_json_loads
from ...store.experience import COLLAB_ARTIFACT, COLLAB_INPUT, _normalize_ref
from .cli import (
    MAX_RECALL_BUDGET_UNITS,
    MAX_RECALL_CONTENT_CHARS,
    MAX_RECALL_ITEMS,
    MAX_RECALL_QUERY_CHARS,
)

_OWNER = "owner.json"
_DREAMING_SCHEMA = "quintessence.dreaming.instance.v1"
_SCHEMA = "quintessence.dreaming.scope-recall.v1"
_KEY_PREFIX = "pcs-dreaming-v1"
_KEY_FIELDS = {
    COLLAB_INPUT: ("input_id", "content_version"),
    COLLAB_ARTIFACT: ("artifact_id", "artifact_version"),
}
_ORIGINS = {
    "user": "human_direct",
    "assistant": "assistant_visible",
    "tool": "tool_observation",
    "system": "host_generated",
}
_VECTOR_DB = "vectors.sqlite3"
_MEMORY_DB = "memory.sqlite3"
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
# The upstream worker needs part of its slice after provider I/O to record the
# verdict and fence vector publication. The provider receives only this cap.
_FINALIZE_SECONDS = 0.15


class EmbeddingPort(Protocol):
    def embed_text(self, text: str, *, remaining_seconds: float) -> Sequence[float]: ...

    def embed_query(self, text: str, *, remaining_seconds: float) -> Sequence[float]: ...


@dataclass(frozen=True)
class SemanticEmbedding:
    """A host-authorized embedding port and its upstream space identity."""

    port: EmbeddingPort
    space_id: str
    dimensions: int
    threshold: float

    def __post_init__(self) -> None:
        if not isinstance(self.space_id, str) or not re.fullmatch(r"[0-9a-f]{64}", self.space_id):
            raise InvalidInput("invalid Scope Recall embedding space")
        if type(self.dimensions) is not int or not 1 <= self.dimensions <= 32768:
            raise InvalidInput("invalid Scope Recall embedding dimensions")
        if type(self.threshold) not in (int, float) or not -1 <= self.threshold <= 1:
            raise InvalidInput("invalid Scope Recall vector threshold")


@dataclass(frozen=True)
class SemanticSource:
    """The host's current authorized view of one exact PCS experience version."""

    ref: Mapping[str, Any]
    text: str
    occurred_at: str
    role: str
    event_ref: EventRef | None = None


@dataclass(frozen=True)
class SemanticHit:
    source: SemanticSource
    channel: Literal["keyword", "vector"]
    excerpt: str
    source_digest: str

    def matches(self, source: SemanticSource | None) -> bool:
        return (
            source is not None
            and sha256_hex(source.text) == self.source_digest
            and replace(source, text=self.excerpt) == self.source
        )


def _occurrence(value: str) -> str:
    stamp = identifiers.require_recorded_at(value)
    return datetime.fromisoformat(stamp).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class SemanticRecall:
    hits: tuple[SemanticHit, ...] = ()
    vector_available: bool = False
    gaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class SemanticDrain:
    processed: int = 0
    indexed: int = 0
    skipped: int = 0
    gaps: tuple[str, ...] = ()
    admitted: int = 0
    examined: int = 0


def _require_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise CorruptStore("Scope Recall projection needs an owned instance directory") from exc
    if not stat.S_ISDIR(info.st_mode) or _is_reparse_point(info) or path.resolve() != path:
        raise CorruptStore("Scope Recall projection needs an owned instance directory")


def projection_file_present(path: Path) -> bool:
    """Check a projection-owned file without following a final link."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or _is_reparse_point(info)
    ):
        raise CorruptStore("Scope Recall projection path is occupied")
    return True


def _is_reparse_point(info: Any) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _require_owner(directory: Path, expected: dict[str, Any]) -> None:
    value = read_projection_json(directory, _OWNER, max_bytes=4096)
    if value == expected:
        return
    raise CorruptStore("Scope Recall projection directory ownership is unverified")


def read_projection_json(directory: Path, filename: str, *, max_bytes: int) -> Any:
    """Read bounded configuration from a regular, instance-local file."""
    _require_directory(directory)
    path = directory / filename
    if not projection_file_present(path):
        return None
    with path.open("rb") as file:
        raw = file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise CorruptStore("Scope Recall projection configuration is too large")
    try:
        return strict_json_loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise CorruptStore("Scope Recall projection configuration is invalid") from exc


def require_dreaming_owner(directory: Path, instance_id: str) -> None:
    _require_owner(directory, {"schema": _DREAMING_SCHEMA, "instance_id": instance_id})


class _GuardedEmbed:
    """Use upstream's one-source worker and fence both outbound and publication."""

    def __init__(self, owner: DreamingSemanticRecall, port: Any, allow_work: Callable[[], bool]):
        self._owner = owner
        self._port = port
        self._allow_work = allow_work
        self.skipped: set[tuple[str, int]] = set()

    def _live(self, source: Any) -> bool:
        if self._owner._rebind(source) is not None:
            return True
        self.skipped.add((source.ref, source.revision))
        return False

    def prepare_source(self, source: Any, *, remaining_seconds: float) -> Any:
        from scope_recall.contracts import ContractError
        from scope_recall.core.private import using_outbound_check

        def blocked() -> bool:
            return not self._allow_work() or not self._live(source)

        try:
            # Scope Recall carries this guard through its metered HTTP send.
            with using_outbound_check(blocked):
                if remaining_seconds <= _FINALIZE_SECONDS:
                    raise ContractError("DEADLINE_EXCEEDED")
                return self._port.prepare_source(
                    source, remaining_seconds=remaining_seconds - _FINALIZE_SECONDS,
                )
        except ContractError as exc:
            if (source.ref, source.revision) in self.skipped:
                raise ContractError("DERIVATION_INVALID", "pcs_source_unavailable") from exc
            raise

    def publish_source(
        self,
        prepared: Any,
        *,
        source: Any,
        lease_token: int,
        lease_owner: str,
        lease_guard: Callable[[], bool],
        remaining_seconds: float,
    ) -> None:
        self._port.publish_source(
            prepared,
            source=source,
            lease_token=lease_token,
            lease_owner=lease_owner,
            lease_guard=lambda: lease_guard() and self._allow_work() and self._live(source),
            remaining_seconds=remaining_seconds,
        )


class DreamingSemanticRecall:
    """An isolated, rebuildable index over current PCS-authorized experience.

    Run on the host's PCS worker. ``read_source`` must read PCS live and reject
    the wrong instance, scope, purpose, privacy, retention or source version.
    ``allow_query`` must recheck the current query's disclosure authority. An
    injected remote embedding port must honor Scope Recall's outbound guard.
    """

    def __init__(
        self,
        directory: Path,
        *,
        instance_id: str,
        namespace: str,
        read_source: Callable[[Mapping[str, Any]], SemanticSource | None],
        route: SemanticEmbedding | None,
        scope_id: str | None = None,
        access_key: str | None = None,
        publish_directory: Callable[[Path, Path], None] | None = None,
        placement_key: str | None = None,
    ) -> None:
        self._directory = Path(directory).absolute()
        self._instance_id = identifiers.require_event_id(instance_id)
        self._namespace = identifiers.require_namespace(namespace)
        self._scope = namespace if scope_id is None else scope_id
        if type(self._scope) is not str or not self._scope.strip() or len(self._scope) > 240:
            raise InvalidInput("invalid Scope Recall scope")
        self._read_source = read_source
        self._route = route
        self._publish_directory = publish_directory
        self._owner = {
            "schema": _SCHEMA,
            "instance_id": instance_id,
            "namespace": namespace,
            "scope_id": self._scope,
            "space_id": route.space_id if route is not None else None,
            "dimensions": route.dimensions if route is not None else None,
        }
        if access_key is not None:
            if not isinstance(access_key, str) or re.fullmatch(r"[0-9a-f]{64}", access_key) is None:
                raise InvalidInput("invalid Scope Recall access partition")
            self._owner["access_key"] = access_key
        if placement_key is not None:
            self._owner["placement_key"] = placement_key
        self._projection_id = sha256_hex(canonical_json_text(self._owner))
        self._path = self._directory / ("scope-recall-" + self._projection_id[:24])

    def _check_projection(self, *, create: bool = False) -> bool:
        require_dreaming_owner(self._directory, self._instance_id)
        try:
            self._path.lstat()
        except FileNotFoundError:
            if not create:
                return False
            self._publish_projection()
        _require_owner(self._path, self._owner)
        memory_present = False
        for name in (_MEMORY_DB, _VECTOR_DB):
            present = projection_file_present(self._path / name)
            if name == _MEMORY_DB:
                memory_present = present
            for suffix in _SQLITE_SIDECARS:
                if projection_file_present(self._path / (name + suffix)) and not present:
                    raise CorruptStore("Scope Recall projection has an unowned SQLite sidecar")
        for name in (".truth-writer.lease", ".truth-writer.lease.info"):
            projection_file_present(self._path / name)
        return memory_present

    def _publish_projection(self) -> None:
        # A complete marker travels with the directory, so a crash cannot
        # leave a live, unowned projection name. A random staging directory
        # contains only the marker and never holds any private source.
        staging = self._directory / (".scope-recall-" + uuid4().hex + ".pending")
        staging.mkdir(mode=0o700)
        try:
            marker = staging / _OWNER
            marker.write_text(canonical_json_text(self._owner), encoding="utf-8")
            try:
                if self._publish_directory is None:
                    if self._path.exists():
                        return
                    staging.rename(self._path)
                else:
                    self._publish_directory(staging, self._path)
            except FileExistsError:
                pass
        finally:
            if staging.exists():
                (staging / _OWNER).unlink(missing_ok=True)
                staging.rmdir()

    def _core(self, vectors: Any = None) -> tuple[Any, Any]:
        from scope_recall.contracts import InstanceBinding, TrustedContext
        from scope_recall.core import CoreConfig, MemoryCore
        from scope_recall.core.recall_policy import RecallPolicy

        assert self._route is not None
        scopes = frozenset({self._scope})
        binding = InstanceBinding(self._namespace, self._projection_id, self._path, scopes)
        context = TrustedContext(binding, "pcs-dreaming", scopes, "host_generated")
        policy = RecallPolicy(
            vector_threshold=self._route.threshold,
            embedding_space_id=self._route.space_id,
        )
        return MemoryCore(CoreConfig(binding), vectors=vectors, retrieval_policy=policy), context

    def _vector_store(self) -> Any:
        from scope_recall.vector.sqlite_store import SQLiteBruteForceVectorStore

        assert self._route is not None
        return SQLiteBruteForceVectorStore(
            self._path / _VECTOR_DB, table_name="pcs_experience", dimensions=self._route.dimensions,
        )

    def _live(self, ref: Mapping[str, Any]) -> SemanticSource | None:
        if ref["namespace"] != self._namespace:
            return None
        current = self._read_source(dict(ref))
        if current is None or current.ref != ref or not isinstance(current.text, str) or not current.text.strip():
            return None
        if current.role not in _ORIGINS or (
            current.event_ref is not None and current.event_ref.namespace != self._namespace
        ):
            return None
        return replace(current, ref=dict(ref))

    @staticmethod
    def _content(source: SemanticSource) -> str | None:
        from scope_recall.core.capture_filters import sanitize_source_capture_text
        from scope_recall.core.events import capture_contains_secret

        if capture_contains_secret(source.text):
            return None
        return sanitize_source_capture_text(source.text)[:MAX_RECALL_QUERY_CHARS]

    def _rebind(self, projected: Any) -> SemanticSource | None:
        if projected is None or projected.scope_id != self._scope or projected.suppressed:
            return None
        prefix, _, key = projected.event["source_event_key"].partition(":")
        kind, _, source_id = key.partition(":")
        if prefix != _KEY_PREFIX or kind not in _KEY_FIELDS:
            return None
        id_field, version_field = _KEY_FIELDS[kind]
        try:
            ref, _, _, _ = _normalize_ref({
                "ref_type": kind, "namespace": self._namespace,
                id_field: source_id, version_field: projected.revision,
            })
        except InvalidInput:
            return None
        source = self._live(ref)
        if source is None or (
            source.role != projected.event["role"]
            or _occurrence(source.occurred_at) != projected.event["occurred_at"]
            or self._content(source) != projected.event["content"]
        ):
            return None
        return source

    def project(
        self,
        ref: Mapping[str, Any],
        *,
        remaining_seconds: float = 1.0,
        allow_work: Callable[[], bool] = lambda: True,
    ) -> bool:
        """Admit one exact, authorized source to Scope Recall's durable queue."""
        if self._route is None or not allow_work():
            return False
        if type(remaining_seconds) not in (int, float) or not 0 < remaining_seconds <= 30:
            raise InvalidInput("invalid Scope Recall projection time limit")
        from scope_recall.contracts import ContractError
        from scope_recall.core.deadline import RequestDeadline, using_request_deadline

        normalized, kind, source_id, version = _normalize_ref(ref)
        source = self._live(normalized)
        if source is None or not (content := self._content(source)) or not content.strip():
            return False
        origin = _ORIGINS[source.role]
        event = {
            "protocol_version": "1.1",
            "source_event_key": f"{_KEY_PREFIX}:{kind}:{source_id}",
            "source_revision": version,
            "role": source.role,
            "origin": origin,
            "content": content,
            "occurred_at": _occurrence(source.occurred_at),
            "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "time_precision": "instant",
            "capture_state": "complete",
            "evidence_refs": [],
        }
        with using_request_deadline(RequestDeadline.from_budget(remaining_seconds)):
            ready = self._check_projection(create=True)
            core, context = self._core()
            if not ready:
                core.initialize()
            if not allow_work() or self._live(normalized) != source or not allow_work():
                return False
            try:
                receipt = core.record_event(replace(context, actor_origin=origin), event, scope_id=self._scope)
            except ContractError as exc:
                if exc.code == "ACCESS_DENIED":
                    return False
                raise
        if receipt.durability == "unknown":
            raise CorruptStore("Scope Recall capture durability is uncertain")
        return receipt.disposition == "inserted" and receipt.durability == "persisted"

    def drain(
        self,
        *,
        max_items: int = 4,
        remaining_seconds: float = 5.0,
        allow_work: Callable[[], bool] = lambda: True,
    ) -> SemanticDrain:
        """Use Scope Recall's bounded worker to turn admitted sources into vectors."""
        if type(max_items) is not int or not 1 <= max_items <= 32:
            raise InvalidInput("invalid Scope Recall drain item limit")
        if type(remaining_seconds) not in (int, float) or not 0 < remaining_seconds <= 30:
            raise InvalidInput("invalid Scope Recall drain time limit")
        if self._route is None:
            return SemanticDrain(gaps=("embedding_unconfigured",))
        if not allow_work() or not self._check_projection():
            return SemanticDrain()
        from scope_recall.core.deadline import RequestDeadline, using_request_deadline
        from scope_recall.runtime.lance_port import LanceEmbedPort

        core, context = self._core()
        vector = self._vector_store()
        with using_request_deadline(RequestDeadline.from_budget(remaining_seconds)) as deadline, closing(vector):
            if projection_file_present(self._path / _VECTOR_DB):
                vector.open_existing()
                vector.close()
            vector.open()
            embed = LanceEmbedPort(
                vector,
                lambda source, *, remaining_seconds: self._route.port.embed_text(
                    source.event["content"], remaining_seconds=remaining_seconds,
                ),
                agent_id=context.binding.agent_id,
                installation_id=context.binding.installation_id,
                embedding_space=self._route.space_id,
            )
            guarded = _GuardedEmbed(self, embed, allow_work)
            receipt = core.drain_worker(
                context, embed=guarded, max_items=max_items, remaining_seconds=deadline.remaining(),
            )
            gaps = tuple(dict.fromkeys(
                item.error_code for item in receipt.items
                if item.error_code and item.error_detail != "pcs_source_unavailable"
            ))
            if deadline.exhausted():
                gaps = (*gaps, "projection_worker_deadline")
            return SemanticDrain(
                processed=receipt.processed,
                indexed=sum(item.work_type == "embed" and item.disposition == "completed" for item in receipt.items),
                skipped=len(guarded.skipped),
                gaps=gaps,
            )

    def recall(
        self,
        query: str,
        *,
        allow_query: Callable[[], bool],
        max_items: int = MAX_RECALL_ITEMS,
        use_vector: bool = True,
        remaining_seconds: float = 2.0,
        exclude_ref: Mapping[str, Any] | None = None,
    ) -> SemanticRecall:
        """Return only ranked upstream hits that can be rebound to live PCS sources."""
        if type(max_items) is not int or not 1 <= max_items <= MAX_RECALL_ITEMS:
            raise InvalidInput("invalid Scope Recall recall item limit")
        if type(remaining_seconds) not in (int, float) or not 0 < remaining_seconds <= 5:
            raise InvalidInput("invalid Scope Recall recall time limit")
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_RECALL_QUERY_CHARS:
            return SemanticRecall(gaps=("query_unavailable",))
        if not allow_query():
            return SemanticRecall(gaps=("query_not_authorized",))
        if self._route is None:
            return SemanticRecall(gaps=("embedding_unconfigured",))
        if not self._check_projection():
            return SemanticRecall(gaps=("projection_unavailable",))
        from scope_recall.core.deadline import RequestDeadline, using_request_deadline
        from scope_recall.core.private import using_outbound_check
        from scope_recall.runtime.lance_port import LanceVectorPort
        from scope_recall.vector import VectorStoreCompatibilityError

        attempted = False
        embedded: tuple[float, ...] = ()

        def embed_query(text: str, *, remaining_seconds: float) -> Sequence[float]:
            nonlocal attempted, embedded
            if text != query:
                return ()
            if not attempted:
                attempted = True
                with using_outbound_check(lambda: not allow_query()):
                    embedded = tuple(self._route.port.embed_query(text, remaining_seconds=remaining_seconds))
            return embedded

        with using_request_deadline(RequestDeadline.from_budget(remaining_seconds)) as deadline, ExitStack() as resources:
            vectors = None
            if use_vector:
                store = self._vector_store()
                resources.callback(store.close)
                try:
                    store.open_existing()
                except (OSError, sqlite3.Error, VectorStoreCompatibilityError):
                    pass
                else:
                    vectors = LanceVectorPort(store, embed_query, expected_embedding_space=self._route.space_id)
            core, context = self._core(vectors)
            result = core.recall(
                context,
                {
                    "protocol_version": "1.1", "request_id": uuid4().hex, "mode": "auto",
                    "query": query, "max_items": max_items, "budget_tokens": MAX_RECALL_BUDGET_UNITS,
                },
                background_without_evidence=False,
                deadline_seconds=deadline.remaining(),
            )
            if not allow_query():
                return SemanticRecall(gaps=("query_not_authorized",))
            hits = []
            gaps = list(result.gaps)
            for item in result.candidates:
                if deadline.exhausted():
                    gaps.append("deadline_exceeded_pcs_rebind")
                    break
                if item.kind != "event" or item.source == "background":
                    continue
                projected = core.source(context, item.ref, item.revision)
                if (source := self._rebind(projected)) is not None:
                    if source.ref == exclude_ref:
                        continue
                    excerpt = projected.event["content"][:MAX_RECALL_CONTENT_CHARS]
                    hits.append(SemanticHit(
                        replace(source, text=excerpt),
                        "vector" if item.source == "vector" else "keyword",
                        excerpt, sha256_hex(source.text),
                    ))
                    if len(hits) == max_items:
                        break
            if any(not hit.matches(self._live(hit.source.ref)) for hit in hits):
                return SemanticRecall(gaps=("pcs_source_changed",))
            if deadline.exhausted():
                return SemanticRecall(gaps=("deadline_exceeded_pcs_rebind",))
            if not allow_query():
                return SemanticRecall(gaps=("query_not_authorized",))
            return SemanticRecall(
                hits=tuple(hits),
                vector_available=bool(embedded) and "vector_unavailable" not in result.gaps,
                gaps=tuple(gaps),
            )
