"""Desktop authority and metered transport for the local semantic projection."""

from __future__ import annotations

import http.client
import os
import queue
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Any

from instance_manager import _publish_dreaming_directory
from quintessence_pcs.adapters.scope_recall.dreaming import (
    DreamingSemanticRecall,
    SemanticDrain,
    SemanticEmbedding,
    SemanticRecall,
    SemanticSource,
    projection_file_present,
    read_projection_json,
    require_dreaming_owner,
)
from quintessence_pcs.domains.memory.experience_bridge import role_for_origin
from quintessence_pcs.domains.refs import EventRef
from quintessence_pcs.foundation.errors import InvalidInput, RecordNotFound, Redacted, StorageError
from quintessence_pcs.foundation.jsonutil import canonical_json_text, sha256_hex
from quintessence_pcs.runtime.collab_protocol import ConsultSendPolicy
from quintessence_pcs.runtime.dreaming_model import DREAMING_PURPOSE
from quintessence_pcs.runtime.http_model import _RedirectRefused, _build_opener
from quintessence_pcs.store.experience import (
    COLLAB_INPUT,
    READABILITY_AVAILABLE,
    ExperienceView,
    _normalize_ref,
)

_CONFIG = "embedding.json"
_LEDGER = "embedding-budget.sqlite3"
_MAX_CONFIG_BYTES = 16_384
_MAX_PAGE = 20
_INDEX_ITEMS = 4
_WORK_SECONDS = 2.0
_SAVED_RETENTION = {"standard", "durable"}
_LIVE_TURN = {"accepted", "running", "waiting"}
_CANCELLED: ContextVar[Callable[[], bool]] = ContextVar("pcs_embedding_cancelled", default=lambda: False)


@contextmanager
def _cancel_on(check: Callable[[], bool]):
    token = _CANCELLED.set(check)
    try:
        yield
    finally:
        _CANCELLED.reset(token)


def _check_ledger(path: Path) -> bool:
    present = projection_file_present(path)
    for suffix in ("-wal", "-shm", "-journal"):
        if projection_file_present(path.with_name(path.name + suffix)) and not present:
            raise InvalidInput("Scope Recall embedding ledger has an unowned sidecar")
    return present


def _ledger_identity(path: Path) -> tuple[int, int] | None:
    if not _check_ledger(path):
        return None
    info = path.lstat()
    return info.st_dev, info.st_ino


def _initialize_ledger(path: Path, budget: Any) -> None:
    from scope_recall.runtime.model_budget import initialize_auxiliary_budget_ledger

    if _check_ledger(path):
        return
    descriptor, name = tempfile.mkstemp(prefix=".embedding-budget-", suffix=".sqlite3", dir=path.parent)
    os.close(descriptor)
    stage = Path(name)
    try:
        initialize_auxiliary_budget_ledger(stage, budget)
        try:
            if os.name == "nt":
                stage.rename(path)
            else:
                os.link(stage, path)
        except FileExistsError:
            pass
    finally:
        stage.unlink(missing_ok=True)
    _check_ledger(path)


class _DesktopEmbeddingTransport:
    """Adapt Scope Recall's HTTP port to the desktop's bounded opener.

    A worker only holds one already authorized request. The PCS owner
    rechecks the live guard before handing it over and can yield while it
    drains. No second call starts while this transport has an active worker.
    """

    def __init__(self, opener: Any | None = None) -> None:
        self._opener = _build_opener() if opener is None else opener
        self._inflight = threading.Lock()

    def _exchange(
        self, request: urllib.request.Request, *, timeout: float, limit: int, cancelled: Callable[[], bool],
    ) -> tuple[int, bytes]:
        from scope_recall.runtime.models import AuxiliaryModelError

        try:
            if cancelled():
                raise AuxiliaryModelError("cancelled")
            try:
                response = self._opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as error:
                response = error
            with closing(response):
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise AuxiliaryModelError("response_limit")
                return int(response.status), raw
        except _RedirectRefused as exc:
            raise AuxiliaryModelError("http_redirect") from exc
        except TimeoutError as exc:
            raise AuxiliaryModelError("timeout") from exc
        except urllib.error.URLError as exc:
            reason = "timeout" if isinstance(exc.reason, TimeoutError) else "network_error"
            raise AuxiliaryModelError(reason) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise AuxiliaryModelError("network_error") from exc

    def post(
        self,
        url: str,
        *,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> tuple[int, bytes]:
        from scope_recall.core.endpoint_policy import endpoint_scheme_allowed, is_credential_key
        from scope_recall.core.private import require_outbound
        from scope_recall.runtime.models import AuxiliaryModelError

        if not endpoint_scheme_allowed(url):
            raise AuxiliaryModelError("endpoint_invalid")
        if url.startswith("http:"):
            headers = {key: value for key, value in headers.items() if not is_credential_key(key)}
        deadline = time.monotonic() + timeout_seconds
        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        if not self._inflight.acquire(blocking=False):
            raise AuxiliaryModelError("transport_unavailable")
        outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
        cancelled = _CANCELLED.get()

        def exchange() -> None:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AuxiliaryModelError("timeout")
                result = self._exchange(request, timeout=remaining, limit=max_response_bytes, cancelled=cancelled)
                outcome.put_nowait((True, result))
            except BaseException as exc:
                outcome.put_nowait((False, exc))
            finally:
                self._inflight.release()

        try:
            require_outbound()
            if deadline <= time.monotonic():
                raise AuxiliaryModelError("timeout")
            threading.Thread(target=exchange, name="pcs-embedding-transport", daemon=True).start()
        except BaseException:
            self._inflight.release()
            raise
        while True:
            if cancelled():
                raise AuxiliaryModelError("cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AuxiliaryModelError("timeout")
            try:
                ok, result = outcome.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if not ok:
                raise result
            require_outbound()
            return result


class DreamingSemanticHost:
    """Own one instance's semantic capability and read authority."""

    def __init__(
        self,
        directory: Path,
        *,
        instance_id: str,
        scope_id: str,
        store: Any,
        history: Any,
        read_policy: Callable[[], ConsultSendPolicy],
        transport: Any | None = None,
    ) -> None:
        self._directory = Path(directory).absolute()
        self._instance_id = instance_id
        self._scope_id = scope_id
        self._store = store
        self._history = history
        self._read_policy = read_policy
        self._transport = transport
        self._settings: Any = None
        self._embedding: SemanticEmbedding | None = None
        self._budget_identity: tuple[int, int] | None = None

    def _read_settings(self) -> dict | None:
        require_dreaming_owner(self._directory, self._instance_id)
        settings = read_projection_json(self._directory, _CONFIG, max_bytes=_MAX_CONFIG_BYTES)
        if settings is None:
            return None
        if type(settings) is not dict or type(settings.get("enabled")) is not bool:
            raise InvalidInput("invalid Scope Recall embedding configuration")
        if not settings["enabled"]:
            return None
        if set(settings) != {"enabled", "embedding", "budget", "vector_threshold"}:
            raise InvalidInput("invalid Scope Recall embedding configuration")
        embedding = settings["embedding"]
        if type(embedding) is not dict or set(embedding) - {
            "credential_env", "model", "endpoint", "dimensions", "dialect", "dimensions_field",
        }:
            raise InvalidInput("invalid Scope Recall embedding route")
        return settings

    def projection_key(self) -> str | None:
        """Select a durable backfill checkpoint for this route and placement."""
        settings = self._read_settings()
        if settings is None:
            return None
        return sha256_hex(canonical_json_text([
            settings["embedding"], os.path.normcase(str(self._directory.resolve())),
        ]))

    def _route(self, settings: dict) -> SemanticEmbedding:
        from scope_recall.core.endpoint_policy import endpoint_scheme_allowed
        from scope_recall.core.recall_policy import embedding_space_id
        from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig
        from scope_recall.runtime.embedding_models import GeminiEmbeddingAdapter
        from scope_recall.runtime.model_budget import AuxiliaryBudgetLedger

        path = self._directory / _LEDGER
        identity = _ledger_identity(path)
        if identity is not None and self._budget_identity is not None and identity != self._budget_identity:
            raise InvalidInput("Scope Recall embedding ledger identity changed")
        if identity is not None and settings == self._settings and self._embedding is not None:
            return self._embedding
        config = AuxiliaryRuntimeConfig.from_mapping({
            "external_embedding": True,
            "external_consolidation": False,
            "embedding": settings["embedding"],
            "budget": settings["budget"],
        })
        route = config.embedding
        if route is None:
            raise InvalidInput("Scope Recall embedding route is missing")
        space = route.space()
        budget = config.budget
        if (
            not endpoint_scheme_allowed(space["endpoint"])
            or space["model"] not in budget.approved_models
            or any(cap is None or cap <= 0 for cap in (budget.total_call_cap, budget.total_input_cap))
            or budget.cap_micro_usd is None
            or type(settings["vector_threshold"]) not in (int, float)
            or not 0 <= settings["vector_threshold"] <= 1
        ):
            raise InvalidInput("Scope Recall embedding route requires an approved finite budget and endpoint")
        if identity is not None:
            # Validate an existing file as the upstream ledger before the
            # worker can open it writable. Reads cannot create SQLite sidecars.
            uri = path.as_uri() + "?mode=ro&immutable=1"
            with closing(sqlite3.connect(uri, uri=True, timeout=0)) as connection:
                connection.execute("SELECT id, status, body_sha256 FROM requests LIMIT 0")
        if self._transport is None:
            self._transport = _DesktopEmbeddingTransport()
        embedding = SemanticEmbedding(
            port=GeminiEmbeddingAdapter(
                route, ledger=AuxiliaryBudgetLedger(path, budget), transport=self._transport,
            ),
            space_id=embedding_space_id(space),
            dimensions=space["dimensions"],
            threshold=settings["vector_threshold"],
        )
        _initialize_ledger(path, budget)
        self._budget_identity = _ledger_identity(path)
        self._settings, self._embedding = settings, embedding
        return embedding

    def _ledger_is_live(self) -> bool:
        return (
            self._budget_identity is not None
            and _ledger_identity(self._directory / _LEDGER) == self._budget_identity
        )

    def _policy(self, purpose: str) -> ConsultSendPolicy | None:
        policy = self._read_policy()
        if not policy.disclose or purpose not in policy.purposes or policy.namespace != self._history.namespace:
            return None
        self._store.require_ready("Scope Recall semantic")
        return policy

    def _read_source(
        self, ref: Mapping[str, Any], *, policy: ConsultSendPolicy, purpose: str,
    ) -> SemanticSource | None:
        if self._policy(purpose) != policy or ref["namespace"] != policy.namespace:
            return None
        access = policy.to_experience_access()
        meta = self._store.dreaming_experience_ref(access, ref=ref)
        view = self._store.read_experience_for_dreaming(
            access, ref=ref, purpose=purpose, audit=self._history.audit,
        )
        if (
            meta is None or not isinstance(view, ExperienceView)
            or view.readability != READABILITY_AVAILABLE or not view.body
        ):
            return None
        role = role_for_origin(meta.ref["ref_type"], view.origin_kind)
        if not role:
            return None
        return SemanticSource(
            ref=meta.ref,
            text=view.body,
            occurred_at=meta.occurred_at.isoformat().replace("+00:00", "Z"),
            role=role,
            event_ref=None if meta.event_id is None else EventRef(policy.namespace, meta.event_id),
        )

    def _adapter(
        self,
        *,
        policy: ConsultSendPolicy,
        purpose: str,
        route: SemanticEmbedding,
    ) -> DreamingSemanticRecall:
        return DreamingSemanticRecall(
            self._directory,
            instance_id=self._instance_id,
            namespace=policy.namespace,
            scope_id=self._scope_id,
            read_source=lambda ref: self._read_source(ref, policy=policy, purpose=purpose),
            route=route,
            access_key=sha256_hex(canonical_json_text([
                policy.host_instance, policy.access_ref, sorted(policy.privacy_classes),
            ])),
            publish_directory=_publish_dreaming_directory,
            placement_key=sha256_hex(os.path.normcase(str(self._directory.resolve()))),
        )

    def maintain(
        self,
        refs: Sequence[Mapping[str, Any]],
        *,
        allow_work: Callable[[], bool],
        remaining_seconds: float = _WORK_SECONDS,
    ) -> SemanticDrain:
        """Project one existing Experience page and run bounded upstream work."""
        if len(refs) > _MAX_PAGE or type(remaining_seconds) not in (int, float) or not 0 < remaining_seconds <= 30:
            raise InvalidInput("invalid Scope Recall maintenance page")
        deadline = time.monotonic() + remaining_seconds
        if not allow_work():
            return SemanticDrain(gaps=("work_yielded",))
        admitted = examined = 0
        try:
            settings = self._read_settings()
            if settings is None:
                return SemanticDrain(gaps=("embedding_unconfigured",))
            policy = self._policy(DREAMING_PURPOSE)
            if policy is None:
                return SemanticDrain(gaps=("purpose_not_allowed",))
            adapter = self._adapter(policy=policy, purpose=DREAMING_PURPOSE, route=self._route(settings))

            def live() -> bool:
                return (
                    allow_work() and time.monotonic() < deadline
                    and self._policy(DREAMING_PURPOSE) == policy
                    and self._read_settings() == settings
                    and self._ledger_is_live()
                )

            # Leave most of the slice for the durable worker even when a
            # history page is longer than today's capture time budget.
            capture_deadline = time.monotonic() + max(0.0, deadline - time.monotonic()) / 2
            for ref in refs:
                if time.monotonic() >= capture_deadline:
                    break
                if not live():
                    return SemanticDrain(admitted=admitted, examined=examined, gaps=("work_yielded",))
                admitted += adapter.project(
                    ref, remaining_seconds=max(0.001, capture_deadline - time.monotonic()), allow_work=live,
                )
                examined += 1
            if not live():
                return SemanticDrain(admitted=admitted, examined=examined, gaps=("work_yielded",))
            with _cancel_on(lambda: not allow_work()):
                result = adapter.drain(
                    max_items=_INDEX_ITEMS, remaining_seconds=max(0.001, deadline - time.monotonic()), allow_work=live,
                )
            gaps = result.gaps
            if examined < len(refs):
                gaps = (*gaps, "projection_page_pending")
            return replace(result, admitted=admitted, examined=examined, gaps=gaps)
        except ImportError:
            return SemanticDrain(admitted=admitted, examined=examined, gaps=("scope_recall_unavailable",))
        except (ValueError, OSError, sqlite3.Error, StorageError, RuntimeError):
            return SemanticDrain(admitted=admitted, examined=examined, gaps=("embedding_unavailable",))

    def _query_is_live(
        self,
        policy: ConsultSendPolicy,
        *,
        query: str,
        input_ref: Mapping[str, Any],
        turn_id: str,
    ) -> bool:
        ref, kind, source_id, version = _normalize_ref(input_ref)
        if kind != COLLAB_INPUT or ref["namespace"] != policy.namespace:
            return False
        live = False
        try:
            turn = self._store.turn_for_host(
                namespace=policy.namespace, host_instance=policy.host_instance, turn_id=turn_id,
            )
            if turn.state in _LIVE_TURN and turn.input_ref == ref:
                row = self._store.load_input(namespace=policy.namespace, input_id=source_id, content_version=version)
                live = (
                    row.origin_kind == "user_statement"
                    and row.privacy_class in policy.privacy_classes
                    and row.retention in _SAVED_RETENTION
                    and row.body == query
                )
        except (Redacted, RecordNotFound):
            pass
        return live

    def recall(
        self,
        query: str,
        *,
        input_ref: Mapping[str, Any],
        turn_id: str,
        allow_query: Callable[[], bool],
        remaining_seconds: float = 2.0,
    ) -> SemanticRecall:
        """Embed the bound current user message at most once per invocation."""
        if type(remaining_seconds) not in (int, float) or not 0 < remaining_seconds <= 5:
            raise InvalidInput("invalid Scope Recall query time limit")
        if not allow_query():
            return SemanticRecall(gaps=("query_not_authorized",))
        deadline = time.monotonic() + remaining_seconds
        try:
            settings = self._read_settings()
            if settings is None:
                return SemanticRecall(gaps=("embedding_unconfigured",))
            policy = self._policy("consult")
            if policy is None:
                return SemanticRecall(gaps=("query_not_authorized",))

            def live() -> bool:
                return (
                    allow_query() and time.monotonic() < deadline
                    and self._policy("consult") == policy and self._read_settings() == settings
                    and self._query_is_live(policy, query=query, input_ref=input_ref, turn_id=turn_id)
                )

            if not live():
                return SemanticRecall(gaps=("query_not_authorized",))
            self._history.audit(policy.to_experience_access(), dict(input_ref), "consult", READABILITY_AVAILABLE)
            adapter = self._adapter(policy=policy, purpose="consult", route=self._route(settings))
            with _cancel_on(lambda: not allow_query()):
                return adapter.recall(
                    query, allow_query=lambda: live() and self._ledger_is_live(),
                    remaining_seconds=max(0.001, deadline - time.monotonic()),
                    exclude_ref=input_ref,
                )
        except ImportError:
            return SemanticRecall(gaps=("scope_recall_unavailable",))
        except (ValueError, OSError, sqlite3.Error, StorageError, RuntimeError):
            return SemanticRecall(gaps=("embedding_unavailable",))

    def rebind(
        self,
        result: SemanticRecall,
        *,
        query: str,
        input_ref: Mapping[str, Any],
        turn_id: str,
        allow_query: Callable[[], bool],
    ) -> SemanticRecall:
        """Recheck delivered refs on Context rebuild without calling a provider."""
        if not result.hits:
            return result
        try:
            policy = self._policy("consult")
            if (
                policy is None or not allow_query() or self._read_settings() != self._settings
                or not self._query_is_live(policy, query=query, input_ref=input_ref, turn_id=turn_id)
            ):
                return SemanticRecall(gaps=("query_not_authorized",))
            hits = tuple(
                hit for hit in result.hits
                if hit.matches(self._read_source(hit.source.ref, policy=policy, purpose="consult"))
            )
            if (
                self._policy("consult") != policy or not allow_query()
                or self._read_settings() != self._settings
                or not self._query_is_live(policy, query=query, input_ref=input_ref, turn_id=turn_id)
            ):
                return SemanticRecall(gaps=("query_not_authorized",))
            gaps = result.gaps if len(hits) == len(result.hits) else (*result.gaps, "pcs_source_changed")
            return replace(result, hits=hits, gaps=gaps)
        except (OSError, sqlite3.Error, StorageError, ValueError):
            return SemanticRecall(gaps=("embedding_unavailable",))
