"""PCS2-009 direct HTTP model adapter (standard library only, tool-free).

Safety properties that are structural rather than comments:

* **No silent network fallback.**  ``preflight`` resolves an active
  authorization and reads the API key from the environment before any Context
  is built or socket opened; a missing/expired/mismatched grant is a refusal.
* **No redirect following.**  A custom redirect handler raises on 3xx, so the
  ``Authorization`` header cannot be forwarded to another host.  The opener also
  disables environment proxies for the same reason.
* **The key never leaves memory.**  It is read from ``manifest.api_key_env``,
  placed only in the request header, and is redacted if a provider echoes it
  back.  It is never written to a payload, a model attempt row or an exception.
* **Every HTTP try is reserved before sending.**  ``reserve_model_attempt``
  commits a ``reserved`` row and charges the budget; ``finish_model_attempt``
  settles exactly that row.  A crash leaves the reservation, never a free call.
* **Every HTTP try is re-checked before it is reserved.**  ``propose`` re-reads
  the persisted Context, requires every exact source to still be live and the
  ``privacy_epoch`` not to have moved, and only then reserves the attempt.
  The check runs before ``reserve_model_attempt``, so a refusal sends zero
  bytes, writes zero attempt rows and charges no budget.  It runs on the first
  request, on a transport retry and on a format-repair request alike.
* **The check is not atomic with the socket write.**  A deletion committed
  between a passing check and the bytes leaving the process can still be
  transmitted.  This adapter only promises that a source already deleted at the
  check point is never sent; the single-driver experiment lease narrows that
  window but is not a guarantee.
* **HTTP 200 is not success.**  ``finish_reason`` must be ``stop``, ``content``
  must be a non-empty string, and any ``tool_calls`` / ``function_call`` key is
  a violation.  ``reasoning_content`` is stored as received but never parsed as
  a proposal.

C09 adds one explicit protocol choice.  The default stays ``"pcs2-008"`` and
keeps the legacy one-user-message request byte-for-byte; ``"collab.v1"`` sends
the versioned system declaration plus the exact consult Context and requires a
trusted-host :class:`ConsultSendPolicy`.  Both protocols share the same
``_send``, reservation, settlement, retry and error mapping -- there is no
second HTTP client.

The collab send gate runs before *every* HTTP try and re-proves the current
consult Context, the exact Profile/SelfView record versions, and the Read/Use/
Disclose basis for every rendered collaboration input.  A refusal is zero HTTP
bytes, zero reservations and zero attempt rows.

Local deletion boundary: the deletion barrier and ``cleanup_deletions`` protect
only the PCS locally persisted copies (Context text and attempt request/response
payloads).  Bytes already sent to an external provider cannot be withdrawn by
this adapter, and nothing here claims otherwise.
"""

from __future__ import annotations

import http.client
import os
import socket
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any, Callable

from ..domains.cognition.context_guard import (
    require_current_consult_context,
    require_live_context,
)
from ..domains.refs import RecordRef
from ..foundation.errors import RecordNotFound, StorageError
from ..foundation.jsonutil import canonical_json_text, strict_json_loads
from ..store.cognition import CognitionStore
from ..store.experience import READABILITY_AVAILABLE, ExperienceView
from ..store.model import (
    MODEL_ATTEMPT_FAILED,
    MODEL_ATTEMPT_MALFORMED,
    MODEL_ATTEMPT_OK,
    MODEL_ATTEMPT_TIMEOUT,
    ModelAttempt,
    ModelAttemptConflict,
    ModelAttemptReservation,
    ModelAuthorization,
    ModelBudgetExceeded,
    ModelLedgerError,
    ModelNotAuthorized,
)
from .collab_protocol import (
    COLLAB_MODEL_POLICY_VERSION,
    ConsultSendPolicy,
    build_collab_request_body,
    render_collab_messages,
)
from .context import ConsultContextEnvelope
from .manifest import ModelManifest, build_request_body
from .model_port import (
    ModelAdapterError,
    ModelBudgetExceededError,
    ModelConfigurationError,
    ModelNotAuthorizedError,
    ModelResponseError,
    ModelTransportError,
)

DIRECT_HTTP_ADAPTER_KIND = "direct-http"
DIRECT_HTTP_POLICY_VERSION = "pcs2-009.direct-http.v1"
DIRECT_HTTP_LEGACY_PROTOCOL = "pcs2-008"
DIRECT_HTTP_COLLAB_PROTOCOL = "collab.v1"
DIRECT_HTTP_PROTOCOLS = (
    DIRECT_HTTP_LEGACY_PROTOCOL,
    DIRECT_HTTP_COLLAB_PROTOCOL,
)

#: One retry for 429/5xx/timeout only.  4xx (except 429) is not retried.
MAX_RETRIES = 1

#: Bound on one provider response body, matching the store's attempt-payload
#: limit.  A larger body is a failure, not an unbounded read.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

#: Never use a socket timeout below this; the wall budget is still checked
#: separately by the reservation row.
MIN_SOCKET_TIMEOUT = 0.05

_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


class _RedirectRefused(Exception):
    """Raised inside the redirect handler; never becomes a second request."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise _RedirectRefused(f"HTTP {code} redirect refused")


class _HttpFailure(Exception):
    """One transport try failed (the caller decides whether to retry)."""

    def __init__(
        self,
        *,
        outcome: str,
        label: str,
        retryable: bool,
        response_body: str | None = None,
    ) -> None:
        super().__init__(label)
        self.outcome = outcome
        self.label = label
        self.retryable = retryable
        self.response_body = response_body


class _Timeout(Exception):
    """The socket/wall timeout fired; stored as outcome='timeout'."""


class _ResponseFailure(Exception):
    """HTTP 200 with an envelope that is not a usable proposal.

    ``metadata`` carries whatever the supplier did return (model, fingerprint,
    usage, usage_known) even though the response is rejected.  An invalid JSON
    envelope has no verifiable metadata; the fields stay empty and
    ``usage_known`` stays false.
    """

    def __init__(
        self,
        label: str,
        finish_reason: str | None,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(label)
        self.label = label
        self.finish_reason = finish_reason
        self.metadata = dict(metadata or {})


@dataclass(frozen=True)
class _ValidatedResponse:
    content: str
    model_returned: str | None
    system_fingerprint: str | None
    usage: dict[str, Any] | None
    usage_known: bool
    finish_reason: str


class DirectHttpModel:
    """Tool-free OpenAI-compatible chat-completions adapter (PCS2-009)."""

    adapter_kind = DIRECT_HTTP_ADAPTER_KIND
    policy_version = DIRECT_HTTP_POLICY_VERSION

    def __init__(
        self,
        store: CognitionStore,
        manifest: ModelManifest,
        *,
        protocol: str = DIRECT_HTTP_LEGACY_PROTOCOL,
        consult_policy: ConsultSendPolicy | None = None,
        opener: Any | None = None,
        send_scope: Callable[[ModelAttempt, str], Any] | None = None,
    ) -> None:
        if not isinstance(manifest, ModelManifest):
            raise ModelConfigurationError("manifest must be a ModelManifest")
        if protocol not in DIRECT_HTTP_PROTOCOLS:
            raise ModelConfigurationError(
                "protocol must be one of: " + ", ".join(DIRECT_HTTP_PROTOCOLS)
            )
        if protocol == DIRECT_HTTP_COLLAB_PROTOCOL:
            if not isinstance(consult_policy, ConsultSendPolicy):
                raise ModelConfigurationError(
                    "the collab.v1 protocol requires a ConsultSendPolicy"
                )
        self._store = store
        self._manifest = manifest
        self.protocol = protocol
        self.policy_version = (
            COLLAB_MODEL_POLICY_VERSION
            if protocol == DIRECT_HTTP_COLLAB_PROTOCOL
            else DIRECT_HTTP_POLICY_VERSION
        )
        self._consult_policy = consult_policy
        self.model_ref = (
            f"{DIRECT_HTTP_ADAPTER_KIND}:{manifest.provider}:{manifest.model}"
        )
        self._opener = opener if opener is not None else _build_opener()
        self._send_scope = send_scope
        self._active: tuple[str, Any] | None = None
        #: Last successful assistant text plus its exact Context, used only to
        #: make a format-repair request explicit (assistant turn + feedback).
        #: Keyed by run_id; a repair must reuse the same run *and* Context.
        self._last_content: dict[str, tuple[str, str]] = {}

    # ------------------------------------------------------------- preflight

    def preflight(self, *, namespace: str) -> None:
        """Refuse before a Context is built if this call cannot be authorized."""

        authorization = self._authorization(namespace)
        self._api_key()
        effective_calls = min(
            self._manifest.max_calls, authorization.max_calls
        )
        used = self._store.count_model_attempts(authorization.authorization_id)
        if used >= effective_calls:
            # The attempt count, not the status, is authoritative (it survives
            # a crash); when the authorization's own cap is what binds, make
            # the human-readable status agree with it before refusing.
            if authorization.max_calls <= self._manifest.max_calls:
                self._store.mark_model_authorization_exhausted(
                    authorization.authorization_id
                )
            raise ModelBudgetExceededError(
                f"model authorization has used {used} of {effective_calls} "
                "allowed call(s)"
            )

    # ---------------------------------------------------------------- binding

    @contextmanager
    def bind_run(self, *, namespace: str, context: Any):
        """Bind the persisted Context for one ``propose`` call.

        The binding is what makes it possible to keep the ModelPort signature
        frozen: the adapter records ``context_id`` / ``run_id`` from the
        server-built Context instead of trusting model-visible text.
        """

        if self._active is not None:
            raise ModelConfigurationError("a model run is already bound")
        if getattr(context, "namespace", None) != namespace:
            raise ModelConfigurationError("the bound Context belongs to another namespace")
        if self.protocol == DIRECT_HTTP_COLLAB_PROTOCOL:
            if not isinstance(context, ConsultContextEnvelope):
                raise ModelConfigurationError(
                    "the collab.v1 protocol requires a ConsultContextEnvelope"
                )
            if (
                context.adapter_kind != self.adapter_kind
                or context.model_input_complete is not True
            ):
                raise ModelConfigurationError(
                    "the bound consult Context was not built for the "
                    "direct-http adapter"
                )
        elif getattr(context, "adapter_kind", None) != self.adapter_kind:
            raise ModelConfigurationError(
                "the bound Context was not built for the direct-http adapter"
            )
        self._active = (namespace, context)
        try:
            yield
        finally:
            self._active = None

    # ---------------------------------------------------------------- propose

    def propose(
        self,
        *,
        rendered_input: str,
        references: Sequence[Mapping[str, Any]],
        feedback: str | None = None,
    ) -> str:
        del references  # the exact rendered text contains the available refs
        if self._active is None:
            raise ModelConfigurationError(
                "DirectHttpModel.propose requires bind_run(context)"
            )
        namespace, context = self._active
        if rendered_input != getattr(context, "rendered_input", None):
            raise ModelConfigurationError(
                "propose() must send the exact persisted rendered_input; "
                "a hidden prompt or example is not allowed"
            )

        authorization = self._authorization(namespace)
        api_key = self._api_key()
        max_tokens = min(
            self._manifest.max_output_tokens_per_call,
            authorization.max_output_tokens_per_call,
        )
        previous_output = None
        if feedback is not None:
            recorded = self._last_content.get(context.run_id)
            if recorded is None or recorded[0] != context.context_id:
                raise ModelConfigurationError(
                    "a format repair requires the previous assistant output "
                    "from the same run and Context"
                )
            previous_output = recorded[1]

        if self.protocol == DIRECT_HTTP_COLLAB_PROTOCOL:
            messages = render_collab_messages(
                context,
                previous_output=previous_output,
                feedback=feedback,
            )
            body_text = build_collab_request_body(
                self._manifest, messages=messages, max_tokens=max_tokens
            )
        else:
            body_text = build_request_body(
                self._manifest,
                rendered_input=rendered_input,
                max_tokens=max_tokens,
                previous_output=previous_output,
                feedback=feedback,
            )

        last_failure: _HttpFailure | None = None
        for retry_index in range(MAX_RETRIES + 1):
            # PCS2-010-P0 C: prove the Context basis again before *every* HTTP
            # try (first request, transport retry, format repair).  The check
            # is before the reservation, so a refusal is 0 HTTP / 0 attempt.
            self._require_send_basis(namespace, context)
            try:
                reservation = self._store.reserve_model_attempt(
                    run_id=context.run_id,
                    namespace=namespace,
                    context_id=context.context_id,
                    authorization_id=authorization.authorization_id,
                    provider=self._manifest.provider,
                    model_requested=self._manifest.model,
                    manifest_hash=self._manifest.manifest_hash,
                    adapter_kind=self.adapter_kind,
                    policy_version=self.policy_version,
                    request_body=body_text,
                    manifest_max_calls=self._manifest.max_calls,
                    manifest_max_output_tokens_per_call=(
                        self._manifest.max_output_tokens_per_call
                    ),
                    manifest_max_wall_seconds=self._manifest.max_wall_seconds,
                )
            except ModelLedgerError as exc:
                raise _translate_store_error(exc) from exc

            timeout = max(
                MIN_SOCKET_TIMEOUT, float(reservation.remaining_wall_seconds)
            )
            scope = (
                self._send_scope(
                    reservation.attempt,
                    "repair" if feedback is not None else "proposal",
                )
                if self._send_scope is not None
                else nullcontext()
            )
            try:
                with scope:
                    response_text = self._send(
                        body_text, timeout=timeout, api_key=api_key
                    )
            except _Timeout as exc:
                self._finish(
                    reservation.attempt,
                    outcome=MODEL_ATTEMPT_TIMEOUT,
                    error_label="TIMEOUT",
                )
                if retry_index < MAX_RETRIES:
                    continue
                raise ModelTransportError(
                    "the provider did not answer before the wall-clock budget"
                ) from exc
            except _HttpFailure as exc:
                self._finish(
                    reservation.attempt,
                    outcome=exc.outcome,
                    response_body=exc.response_body,
                    error_label=exc.label,
                )
                if exc.retryable and retry_index < MAX_RETRIES:
                    last_failure = exc
                    continue
                raise ModelTransportError(
                    f"provider call failed: {exc.label}"
                ) from exc

            try:
                validated = _validate_response(response_text)
            except _ResponseFailure as exc:
                metadata = exc.metadata
                self._finish(
                    reservation.attempt,
                    outcome=MODEL_ATTEMPT_MALFORMED,
                    response_body=response_text,
                    finish_reason=exc.finish_reason,
                    error_label=exc.label,
                    model_returned=metadata.get("model_returned"),
                    system_fingerprint=metadata.get("system_fingerprint"),
                    usage=metadata.get("usage"),
                    usage_known=metadata.get("usage_known", False),
                    cost_known=False,
                    cost_estimate=None,
                )
                raise ModelResponseError(
                    f"provider response is not a usable proposal: {exc.label}"
                ) from exc

            self._finish(
                reservation.attempt,
                outcome=MODEL_ATTEMPT_OK,
                response_body=response_text,
                model_returned=validated.model_returned,
                system_fingerprint=validated.system_fingerprint,
                finish_reason=validated.finish_reason,
                usage=validated.usage,
                usage_known=validated.usage_known,
                cost_known=False,
                cost_estimate=None,
                error_label=None,
            )
            # Keep only a small repair memory; a Context/run is short-lived.
            if len(self._last_content) >= 8:
                self._last_content.clear()
            self._last_content[context.run_id] = (
                context.context_id,
                validated.content,
            )
            return validated.content

        # Defensive: the loop either returns or raises.
        label = last_failure.label if last_failure is not None else "UNKNOWN"
        raise ModelTransportError(f"provider call failed: {label}")

    # --------------------------------------------------------------- internals

    def _require_send_basis(self, namespace: str, context: Any) -> None:
        """Refuse the next HTTP try unless the persisted basis is still live.

        The dispatch is explicit: the legacy protocol keeps the PCS2-010-P0
        review check unchanged, while collab.v1 re-proves the current consult
        Context, its exact Profile/SelfView record versions and every rendered
        collaboration source.  Both run before the reservation on the first
        request, on every transport retry and on every format-repair request.

        The check and the socket write are not atomic.  This method is the
        promise boundary: deleted-before-check never leaves the process;
        deleted-after-check may.
        """

        if self.protocol == DIRECT_HTTP_COLLAB_PROTOCOL:
            self._require_collab_send_basis(namespace, context)
            return
        self._require_legacy_send_basis(namespace, context)

    def _require_legacy_send_basis(self, namespace: str, context: Any) -> None:
        """The unchanged PCS2-010-P0 review send gate."""

        try:
            snapshot = require_live_context(
                self._store, context_id=context.context_id, namespace=namespace
            )
        except StorageError as exc:
            raise ModelAdapterError(
                "pre-send check refused: the Context basis is no longer live "
                f"({type(exc).__name__})"
            ) from exc
        if (
            snapshot.namespace != namespace
            or snapshot.run_id != getattr(context, "run_id", None)
            or snapshot.content_hash != getattr(context, "content_hash", None)
            or snapshot.privacy_epoch != getattr(context, "privacy_epoch", None)
        ):
            raise ModelAdapterError(
                "pre-send check refused: the stored Context row does not match "
                "the Context this call was bound to"
            )
        current = self._store.context_watermarks(namespace)
        if current["privacy_epoch"] != context.privacy_epoch:
            raise ModelAdapterError(
                "pre-send check refused: privacy_epoch moved from "
                f"{context.privacy_epoch} to {current['privacy_epoch']}"
            )

    def _require_collab_send_basis(self, namespace: str, context: Any) -> None:
        """C09: current consult Context + exact Read/Use/Disclose basis.

        Every body source of the final ``rendered_input`` is covered:

        * the pinned Profile and (when fresh) SelfView are read through the
          *exact frozen record versions* in ``read_set``/``sources`` -- never
          through ``get_active_profile`` or ``get_self_view`` current pointers;
        * every rendered collaboration input is re-read through
          ``read_experience(..., purpose="consult")`` with the trusted-host
          policy, and must still be ``available``.

        A profile/self-view reference missing from ``read_set``/``sources``, a
        duplicate, or any extra record/event source is refused as an unknown
        source.  A failure here happens before ``reserve_model_attempt``, so it
        is zero HTTP, zero attempts and zero charged budget.
        """

        policy = self._consult_policy
        if not isinstance(policy, ConsultSendPolicy):
            raise ModelConfigurationError(
                "the collab.v1 protocol requires a ConsultSendPolicy"
            )
        if not isinstance(context, ConsultContextEnvelope):
            raise ModelConfigurationError(
                "the collab.v1 protocol requires a ConsultContextEnvelope"
            )
        if context.namespace != namespace or context.adapter_kind != self.adapter_kind:
            raise ModelAdapterError(
                "pre-send check refused: the bound consult Context does not "
                "belong to this adapter/namespace"
            )
        if policy.namespace != namespace:
            raise ModelNotAuthorizedError(
                "pre-send check refused: the send policy namespace does not "
                "match the consult Context"
            )
        if "consult" not in policy.purposes:
            raise ModelNotAuthorizedError(
                "pre-send check refused: the send policy does not allow the "
                "consult purpose"
            )
        if policy.disclose is not True:
            raise ModelNotAuthorizedError(
                "pre-send check refused: disclosure is disabled for this send "
                "policy"
            )

        try:
            snapshot = require_current_consult_context(
                self._store, context_id=context.context_id, namespace=namespace
            )
        except StorageError as exc:
            raise ModelAdapterError(
                "pre-send check refused: the consult Context is no longer "
                f"current ({type(exc).__name__})"
            ) from exc
        profile_value = (
            context.profile_ref.to_dict()
            if isinstance(context.profile_ref, RecordRef)
            else context.profile_ref
        )
        if (
            snapshot.context_kind != "consult"
            or snapshot.namespace != context.namespace
            or snapshot.run_id != context.run_id
            or snapshot.content_hash != context.content_hash
            or snapshot.state_revision != context.state_revision
            or snapshot.context_event_seq != context.context_event_seq
            or snapshot.privacy_epoch != context.privacy_epoch
            or snapshot.profile_ref != profile_value
            or snapshot.adapter_kind != self.adapter_kind
            or snapshot.model_input_complete is not True
            or canonical_json_text(
                [dict(item) for item in snapshot.read_set]
            )
            != canonical_json_text(
                [entry.to_dict() for entry in context.read_set]
            )
            or _sources_identity(snapshot.sources)
            != _sources_identity(context.sources)
        ):
            raise ModelAdapterError(
                "pre-send check refused: the stored consult Context does not "
                "match the Context this call was bound to"
            )
        self._require_collab_source_accounting(namespace, context, policy)

    def _require_collab_source_accounting(
        self,
        namespace: str,
        context: ConsultContextEnvelope,
        policy: ConsultSendPolicy,
    ) -> None:
        """Prove every rendered record/input against the exact Context refs."""

        profile_ref = context.profile_ref
        if not isinstance(profile_ref, RecordRef) or profile_ref.namespace != namespace:
            raise ModelAdapterError(
                "pre-send check refused: the Context profile is not an exact "
                "record in this namespace"
            )

        read_set = tuple(context.read_set)
        profile_entries = [entry for entry in read_set if entry.ref == profile_ref]
        if len(profile_entries) != 1:
            raise ModelAdapterError(
                "pre-send check refused: the Context profile must appear "
                "exactly once in read_set"
            )
        if profile_entries[0].usage != "pinned_asset":
            raise ModelAdapterError(
                "pre-send check refused: the Context profile read_set usage "
                "is not pinned_asset"
            )
        current_entries = [entry for entry in read_set if entry.usage == "current"]
        if len(current_entries) > 1:
            raise ModelAdapterError(
                "pre-send check refused: the Context has more than one "
                "current read_set record"
            )
        self_view_ref = current_entries[0].ref if current_entries else None
        if self_view_ref is not None and self_view_ref.namespace != namespace:
            raise ModelAdapterError(
                "pre-send check refused: the Context self view is not an "
                "exact record in this namespace"
            )

        expected_entries = {
            (profile_ref.record_id, profile_ref.record_version, "pinned_asset")
        }
        if self_view_ref is not None:
            expected_entries.add(
                (self_view_ref.record_id, self_view_ref.record_version, "current")
            )
        if len(read_set) != len(expected_entries):
            raise ModelAdapterError(
                "pre-send check refused: read_set contains a record this "
                "Context did not render"
            )
        for entry in read_set:
            key = (entry.ref.record_id, entry.ref.record_version, entry.usage)
            if key not in expected_entries:
                raise ModelAdapterError(
                    "pre-send check refused: read_set contains an unknown record"
                )

        if any(source.ref_kind != "record" for source in context.sources):
            raise ModelAdapterError(
                "pre-send check refused: the Context has a body source that "
                "is not an exact record"
            )
        profile_key = (profile_ref.record_id, profile_ref.record_version)
        source_keys = [
            (source.ref_id, int(source.ref_version)) for source in context.sources
        ]
        if source_keys.count(profile_key) != 1:
            raise ModelAdapterError(
                "pre-send check refused: the Context profile source must "
                "appear exactly once"
            )
        expected_keys = {profile_key}
        if self_view_ref is not None:
            self_view_key = (self_view_ref.record_id, self_view_ref.record_version)
            expected_keys.add(self_view_key)
            if source_keys.count(self_view_key) != 1:
                raise ModelAdapterError(
                    "pre-send check refused: the Context self-view source "
                    "must appear exactly once"
                )
        if len(source_keys) != len(expected_keys):
            raise ModelAdapterError(
                "pre-send check refused: the Context sources contain a "
                "duplicate or unknown record"
            )
        for key in source_keys:
            if key not in expected_keys:
                raise ModelAdapterError(
                    "pre-send check refused: the Context sources contain an "
                    "unknown record"
                )

        profile_version = self._exact_record_version(
            namespace,
            profile_ref.record_id,
            profile_ref.record_version,
            what="profile",
        )
        if profile_version.kind != "persona_profile":
            raise ModelAdapterError(
                "pre-send check refused: the Context profile source is not a "
                "persona_profile"
            )
        self_view_version = None
        if self_view_ref is not None:
            self_view_version = self._exact_record_version(
                namespace,
                self_view_ref.record_id,
                self_view_ref.record_version,
                what="self view",
            )
            if self_view_version.kind != "self_view":
                raise ModelAdapterError(
                    "pre-send check refused: the Context current read_set "
                    "record is not a self_view"
                )
        if profile_version.privacy_class not in policy.privacy_classes:
            raise ModelNotAuthorizedError(
                "pre-send check refused: the profile privacy class is not "
                "allowed by the send policy"
            )
        if (
            self_view_version is not None
            and self_view_version.privacy_class not in policy.privacy_classes
        ):
            raise ModelNotAuthorizedError(
                "pre-send check refused: the self-view privacy class is not "
                "allowed by the send policy"
            )

        stored_refs = self._store.context_collab_sources(
            namespace=namespace, context_id=context.context_id
        )
        if _reference_multiset(stored_refs) != _reference_multiset(
            context.collab_source_refs
        ):
            raise ModelAdapterError(
                "pre-send check refused: the bound Context collaboration "
                "sources do not match the stored Context"
            )
        access = policy.to_experience_access()
        for ref in context.collab_source_refs:
            try:
                view = self._store.read_experience(
                    access, ref=ref, purpose="consult"
                )
            except StorageError as exc:
                raise ModelAdapterError(
                    "pre-send check refused: a collaboration source could not "
                    f"be re-read ({type(exc).__name__})"
                ) from exc
            if (
                not isinstance(view, ExperienceView)
                or view.readability != READABILITY_AVAILABLE
                or view.body is None
            ):
                raise ModelNotAuthorizedError(
                    "pre-send check refused: a collaboration source is not "
                    "readable under the current send policy"
                )

    def _exact_record_version(
        self,
        namespace: str,
        record_id: str,
        record_version: int,
        *,
        what: str,
    ):
        """Read exactly one frozen version; never follow a current pointer."""

        try:
            version = self._store.get_version(
                namespace, record_id, record_version
            )
        except RecordNotFound as exc:
            raise ModelAdapterError(
                f"pre-send check refused: the {what} record version is missing"
            ) from exc
        # A deletion barrier keeps the row; the version is readable through the
        # archival API but must never be sent.  Check it after the exact read so
        # one call covers both "missing" and "deleted".
        if self._store.record_is_deleted(namespace, record_id):
            raise ModelAdapterError(
                f"pre-send check refused: the {what} record has been deleted"
            )
        if version.namespace != namespace:
            raise ModelAdapterError(
                f"pre-send check refused: the {what} record is in another namespace"
            )
        return version

    def _authorization(self, namespace: str) -> ModelAuthorization:
        authorization = self._store.active_model_authorization(
            namespace=namespace,
            provider=self._manifest.provider,
            model=self._manifest.model,
            manifest_hash=self._manifest.manifest_hash,
        )
        if authorization is None:
            raise ModelNotAuthorizedError(
                "no active model authorization covers this provider/model/manifest"
            )
        return authorization

    def _api_key(self) -> str:
        value = os.environ.get(self._manifest.api_key_env)
        if value is None or value == "":
            raise ModelNotAuthorizedError(
                f"{self._manifest.api_key_env} is not set; refusing to call a provider"
            )
        if value != value.strip():
            raise ModelConfigurationError(
                "the API key environment value has surrounding whitespace"
            )
        if not value.isascii() or any(
            ord(char) < 0x20 or ord(char) == 0x7F for char in value
        ):
            raise ModelConfigurationError(
                "the API key must be printable ASCII without control characters"
            )
        return value

    def _send(self, body_text: str, *, timeout: float, api_key: str) -> str:
        payload = body_text.encode("utf-8")
        request = urllib.request.Request(
            self._manifest.endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "quintessence-pcs/0.0.0 (tool-free direct-http)",
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                status = int(response.status)
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except _RedirectRefused as exc:
            raise _HttpFailure(
                outcome=MODEL_ATTEMPT_FAILED,
                label="REDIRECT_REFUSED",
                retryable=False,
            ) from exc
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                raw = exc.read(MAX_RESPONSE_BYTES + 1)
            finally:
                exc.close()
            if len(raw) > MAX_RESPONSE_BYTES:
                raise _HttpFailure(
                    outcome=MODEL_ATTEMPT_FAILED,
                    label="RESPONSE_TOO_LARGE",
                    retryable=False,
                ) from exc
            text = _redact(_decode_body(raw), api_key)
            if status in (301, 302, 303, 307, 308):
                raise _HttpFailure(
                    outcome=MODEL_ATTEMPT_FAILED,
                    label="REDIRECT_REFUSED",
                    retryable=False,
                    response_body=text,
                ) from exc
            retryable = status == 429 or 500 <= status < 600
            raise _HttpFailure(
                outcome=MODEL_ATTEMPT_FAILED,
                label=f"HTTP_{status}",
                retryable=retryable,
                response_body=text,
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise _Timeout() from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise _Timeout() from exc
            raise _HttpFailure(
                outcome=MODEL_ATTEMPT_FAILED,
                label="NETWORK_ERROR",
                retryable=False,
            ) from exc
        except (http.client.HTTPException, OSError) as exc:
            raise _HttpFailure(
                outcome=MODEL_ATTEMPT_FAILED,
                label="NETWORK_ERROR",
                retryable=False,
            ) from exc

        if len(raw) > MAX_RESPONSE_BYTES:
            raise _HttpFailure(
                outcome=MODEL_ATTEMPT_FAILED,
                label="RESPONSE_TOO_LARGE",
                retryable=False,
            )
        text = _decode_body(raw)
        if text is None:
            raise _HttpFailure(
                outcome=MODEL_ATTEMPT_FAILED,
                label="INVALID_UTF8",
                retryable=False,
            )
        if status != 200:
            raise _HttpFailure(
                outcome=MODEL_ATTEMPT_FAILED,
                label=f"HTTP_{status}",
                retryable=False,
                response_body=_redact(text, api_key),
            )
        # A provider that echoes the bearer token must not get it into the
        # database.  Refuse without storing the response at all.
        if api_key in text:
            raise _HttpFailure(
                outcome=MODEL_ATTEMPT_FAILED,
                label="KEY_ECHO_REDACTED",
                retryable=False,
            )
        return text

    def _finish(self, attempt: ModelAttempt, **kwargs: Any) -> ModelAttempt:
        try:
            return self._store.finish_model_attempt(
                attempt_id=attempt.attempt_id, **kwargs
            )
        except ModelAttemptConflict:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the reservation, report safely
            raise ModelAdapterError(
                "the model attempt could not be settled; its reservation still counts"
            ) from exc


def _reference_multiset(refs: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Order-insensitive canonical identity for a Context's collaboration refs."""

    return tuple(sorted(canonical_json_text(dict(ref)) for ref in refs))


def _sources_identity(sources: Sequence[Any]) -> tuple[tuple[str, str, int], ...]:
    """Exact (kind, id, version) identities for stored/bound Context sources."""

    return tuple(
        sorted(
            (source.ref_kind, str(source.ref_id), int(source.ref_version))
            for source in sources
        )
    )


def _supplier_metadata(document: Mapping[str, Any]) -> dict[str, Any]:
    """Whatever verifiable supplier metadata an HTTP 200 envelope carries."""

    usage_obj = document.get("usage")
    if isinstance(usage_obj, dict):
        usage = dict(usage_obj)
        usage_known = all(
            type(usage.get(key)) is int and usage[key] >= 0
            for key in _USAGE_KEYS
        )
    else:
        usage = None
        usage_known = False
    return {
        "model_returned": _optional_text(document.get("model"), 256),
        "system_fingerprint": _optional_text(
            document.get("system_fingerprint"), 256
        ),
        "usage": usage,
        "usage_known": usage_known,
    }


def _validate_response(text: str) -> _ValidatedResponse:
    try:
        document = strict_json_loads(text)
    except ValueError as exc:
        raise _ResponseFailure("INVALID_JSON", None) from exc
    if not isinstance(document, dict):
        raise _ResponseFailure("INVALID_RESPONSE_SHAPE", None)
    # Metadata is read before the structural checks: a length-truncated
    # response, a tool-call response or an empty-content response still came
    # from a real supplier call and its usage/model must not be lost.
    metadata = _supplier_metadata(document)
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices:
        raise _ResponseFailure("MISSING_CHOICES", None, metadata=metadata)
    choice = choices[0]
    if not isinstance(choice, dict):
        raise _ResponseFailure("INVALID_RESPONSE_SHAPE", None, metadata=metadata)
    message = choice.get("message")
    if not isinstance(message, dict):
        raise _ResponseFailure("MISSING_MESSAGE", None, metadata=metadata)
    # Presence alone is a violation, even if the value is null/empty: the
    # request is tool-free, so the provider must not introduce tool machinery.
    if "tool_calls" in message or "function_call" in message:
        raise _ResponseFailure(
            "TOOL_CALLS_PRESENT",
            _bound_reason(choice.get("finish_reason")),
            metadata=metadata,
        )
    if "tool_calls" in choice or "function_call" in choice:
        raise _ResponseFailure(
            "TOOL_CALLS_PRESENT",
            _bound_reason(choice.get("finish_reason")),
            metadata=metadata,
        )

    raw_reason = choice.get("finish_reason")
    finish_reason = _bound_reason(raw_reason)
    if finish_reason is None:
        raise _ResponseFailure("MISSING_FINISH_REASON", None, metadata=metadata)
    if finish_reason != "stop":
        label = "FINISH_REASON_" + "".join(
            char if char.isalnum() else "_" for char in finish_reason.upper()
        )[:100]
        raise _ResponseFailure(label, finish_reason, metadata=metadata)

    content = message.get("content")
    if not isinstance(content, str) or content == "":
        raise _ResponseFailure("EMPTY_CONTENT", finish_reason, metadata=metadata)

    return _ValidatedResponse(
        content=content,
        model_returned=metadata["model_returned"],
        system_fingerprint=metadata["system_fingerprint"],
        usage=metadata["usage"],
        usage_known=metadata["usage_known"],
        finish_reason=finish_reason,
    )


def _bound_reason(value: Any) -> str | None:
    if not isinstance(value, str) or value == "":
        return None
    return value[:64]


def _optional_text(value: Any, max_length: int) -> str | None:
    if not isinstance(value, str) or value == "":
        return None
    return value[:max_length]


def _decode_body(raw: bytes) -> str | None:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _redact(text: str | None, api_key: str) -> str | None:
    """Remove an echoed bearer token before a body can be persisted."""

    if text is None or api_key not in text:
        return text
    return text.replace(api_key, "[REDACTED]")


def _translate_store_error(exc: ModelLedgerError) -> ModelAdapterError:
    if isinstance(exc, ModelBudgetExceeded):
        return ModelBudgetExceededError(str(exc))
    if isinstance(exc, ModelNotAuthorized):
        return ModelNotAuthorizedError(str(exc))
    if isinstance(exc, ModelAttemptConflict):
        return ModelAdapterError(str(exc))
    return ModelAdapterError(str(exc))


def _build_opener() -> urllib.request.OpenerDirector:
    # No environment proxies: a proxy would see the Authorization header and
    # could redirect the call, defeating both the redirect and HTTPS rules.
    context = ssl.create_default_context()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        _NoRedirect(),
    )


__all__ = [
    "DIRECT_HTTP_ADAPTER_KIND",
    "DIRECT_HTTP_COLLAB_PROTOCOL",
    "DIRECT_HTTP_LEGACY_PROTOCOL",
    "DIRECT_HTTP_POLICY_VERSION",
    "DIRECT_HTTP_PROTOCOLS",
    "MAX_RESPONSE_BYTES",
    "MAX_RETRIES",
    "MIN_SOCKET_TIMEOUT",
    "DirectHttpModel",
]
