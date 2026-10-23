"""The trusted Python host wiring behind the desktop Consult sidecar.

This module is an adapter, not a second cognition implementation.  It wires the
existing public PCS services:

* ``CognitionStore`` and its numbered SQL migrations;
* ``PersonaProfileService`` / ``SelfViewService`` / ``ContextService``;
* ``DirectHttpModel`` (``collab.v1``) and ``CognitionQueryService``;
* ``ConsultFacade``.

The only optional substitution is the Lab-only scripted model transport: the
real adapter still builds the request, re-checks the C09 send gate, reserves an
attempt, validates the provider-shaped response and records the attempt.  The
script never writes a Belief, an interaction or an artifact itself.

A production configuration never seeds fixtures and never accepts the scripted
transport; that combination is refused by :mod:`host_config`.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from quintessence_pcs.collaboration import (
    ConsultFacade,
    ConsultHostAccess,
    ConsultRequest,
    HostAccess,
    ReviewFacade,
)
from quintessence_pcs.domains.cognition import (
    CognitionQueryService,
    SelfViewService,
)
from quintessence_pcs.domains.persona.profiles import (
    REVIEW_APPROVE_EVENT_TYPE,
    PersonaProfileService,
)
from quintessence_pcs.foundation.errors import RecordNotFound
from quintessence_pcs.foundation.timeutil import utc_now_rfc3339
from quintessence_pcs.runtime import (
    ConsultSendPolicy,
    ContextService,
    DirectHttpModel,
)
from quintessence_pcs.runtime.http_model import DIRECT_HTTP_LEGACY_PROTOCOL
from quintessence_pcs.store import CognitionStore

from host_config import REVIEW_PURPOSE, HostConfig, ModelSpec

LAB_KEY_PREFIX = "lab-"
_MAX_PUBLIC_ITEMS = 100


class RuntimeStartupError(Exception):
    """The trusted runtime cannot be built; the bridge stays not-ready."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InputRegistrationError(Exception):
    """The C05b input registration failed; no interaction was created."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ReviewRequestError(Exception):
    """A structured C12c review refusal before C07 is called.

    The code is a ``desktop.v1`` review label (for example ``CANDIDATE_STALE``),
    never a new PCS error code.  It is safe to show to the operator: it carries
    no body, candidate text, prompt, path or internal identifier.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DesktopRuntime:
    """One sidecar process's store, host services and real ConsultFacade."""

    def __init__(self, config: HostConfig) -> None:
        self.config = config
        self._lab_key_env = _ensure_synthetic_key(config)
        self.store: CognitionStore | None = None
        try:
            self.store = CognitionStore.open(
                config.database_path, migrations_dir=_migrations_dir()
            )
        except Exception as exc:  # noqa: BLE001 - startup refusal is a host state
            self._remove_synthetic_key()
            raise RuntimeStartupError(
                "STORE_UNAVAILABLE", f"the PCS store could not be opened ({type(exc).__name__})"
            ) from exc

        try:
            row = self.store.connection.execute(
                "SELECT MAX(migration_id) FROM schema_migrations"
            ).fetchone()
            self.migrations_version = (
                0 if row is None or row[0] is None else int(row[0])
            )
            self.profiles = PersonaProfileService(self.store)
            self.self_view = SelfViewService(self.store)
            self.contexts = ContextService(
                self.store,
                self_view_service=self.self_view,
                profile_service=self.profiles,
            )
            self.queries = CognitionQueryService(self.store)
            if config.lab is not None:
                self._seed_lab(config)
            self.model = _build_model(config, self.store)
            self.review_model = _build_model(config, self.store, review=True)
            self.facade = ConsultFacade(
                self.store,
                context_service=self.contexts,
                model=self.model,
                query_service=self.queries,
            )
            self.review_facade = ReviewFacade(
                self.store,
                context_service=self.contexts,
                model=self.review_model,
            )
            self._access = ConsultHostAccess(
                namespace=config.authority.namespace,
                host_instance=config.authority.host_instance,
                access_ref=config.authority.access_ref,
                privacy_classes=config.authority.privacy_classes,
                purposes=config.authority.purposes,
                disclose=config.authority.disclose,
            )
            self._review_access = HostAccess(
                namespace=config.authority.namespace,
                host_instance=config.authority.host_instance,
                operator=config.authority.operator,
                permissions=config.authority.review_permissions,
                privacy_classes=config.authority.privacy_classes,
                purposes=config.authority.purposes,
                access_ref=config.authority.access_ref,
            )
        except RuntimeStartupError:
            self.store.close()
            self._remove_synthetic_key()
            raise
        except Exception as exc:  # noqa: BLE001 - startup refusal is a host state
            self.store.close()
            self._remove_synthetic_key()
            raise RuntimeStartupError(
                "RUNTIME_UNAVAILABLE",
                f"the trusted runtime could not be built ({type(exc).__name__})",
            ) from exc

        self.ready, self.reason_code, self.reason_detail = self._readiness()

    # ------------------------------------------------------------------ seed

    def _seed_lab(self, config: HostConfig) -> None:
        seed = config.lab
        assert seed is not None
        namespace = config.authority.namespace
        active = self.profiles.get_active_profile(namespace)
        if active.ref is None:
            receipt = self.store.ingest_event(
                namespace=namespace,
                source_kind="user_report",
                source_instance="desktop-lab-synthetic-admin",
                source_event_id=f"desktop-lab-profile-{seed.profile_id}",
                event_type=REVIEW_APPROVE_EVENT_TYPE,
                observed_at=utc_now_rfc3339(),
                payload={"decision": "approve", "synthetic": True},
            )
            review_ref = {
                "ref_type": "event",
                "namespace": namespace,
                "event_id": receipt.event_id,
            }
            body = _profile_body(seed)
            body["review_ref"] = review_ref
            version = self.profiles.publish_profile(
                namespace=namespace,
                profile_id=seed.profile_id,
                body=body,
                review_ref=review_ref,
                authorization_ref=seed.authorization_ref,
            )
            self.profiles.activate_profile(
                namespace=namespace,
                record_id=version.record_id,
                record_version=version.record_version,
                authorization_ref=seed.authorization_ref,
            )
        self.self_view.build_self_view(namespace=namespace)

        belief_body = {
            "claim": seed.belief_claim,
            "claim_type": seed.belief_claim_type,
            "evidence_links": [],
            "strength": seed.belief_strength,
            "strength_reason": seed.belief_reason,
            "inference_basis": "observation",
            "premise_refs": [],
            "alternative_group_id": None,
            "valid_until": None,
            "review_condition": "review when the lab fixture changes",
        }
        if not self.store.current_versions_of_kind(namespace, "belief"):
            self.store.append_record_version(
                namespace=namespace,
                record_id=str(
                    _uuid5(f"desktop-lab-belief:{seed.belief_claim}")
                ),
                record_version=1,
                kind="belief",
                body=belief_body,
                status="supported",
                schema_version="1",
                privacy_class=config.authority.input_privacy_class,
                scope=dict(seed.belief_scope),
            )

        manifest = config.model.manifest
        grants = self.store.model_authorizations(
            namespace=namespace, status="active"
        )
        matching = [
            grant
            for grant in grants
            if grant.provider == config.model.provider
            and grant.model == config.model.model
            and grant.manifest_hash == manifest.manifest_hash
        ]
        if not matching:
            self.store.grant_model_authorization(
                namespace=namespace,
                authorization_ref=seed.authorization_ref,
                provider=config.model.provider,
                model=config.model.model,
                manifest_hash=manifest.manifest_hash,
                max_calls=manifest.max_calls,
                max_output_tokens_per_call=manifest.max_output_tokens_per_call,
                max_wall_seconds=manifest.max_wall_seconds,
            )

    # --------------------------------------------------------------- consult

    def consult(self, question: str) -> dict[str, Any]:
        """Register the input through C05b, then run the real ConsultFacade."""

        config = self.config
        namespace = config.authority.namespace
        try:
            input_row = self.store.register_input(
                namespace=namespace,
                origin_kind="user_statement",
                body=question,
                privacy_class=config.authority.input_privacy_class,
                retention=config.authority.retention,
                content_version=1,
            )
        except Exception as exc:  # noqa: BLE001 - registration failure is a result
            raise InputRegistrationError(
                "INPUT_REGISTRATION_FAILED",
                f"the host could not register the input ({type(exc).__name__})",
            ) from exc

        input_ref = {
            "ref_type": "collab_input",
            "namespace": namespace,
            "input_id": input_row.input_id,
            "content_version": input_row.content_version,
        }
        request = ConsultRequest(
            input_refs=(input_ref,),
            query_plan=config.query_plan,
            policy_ref=config.authority.policy_ref,
            activation_kind="user_input",
            retention=config.authority.retention,
        )
        result = self.facade.interact(self._access, request)
        return self._public_result(result, input_ref)

    def _public_result(
        self, result: Any, input_ref: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "interaction_id": result.interaction_id,
            "context_id": result.context_id,
            "state": result.state,
            "reply": result.reply,
            "detail": result.detail,
            "query": _query_summary(result.query_body),
            "query_source_refs": list(result.query_source_refs),
            "query_result_ref": result.query_result_ref,
            "reply_artifact_ref": result.reply_artifact_ref,
            "input": {
                "input_ref": input_ref,
                "origin_kind": "user_statement",
                "privacy_class": self.config.authority.input_privacy_class,
                "retention": self.config.authority.retention,
            },
            "destination": self.config.destination.to_public_dict(),
        }

    # ---------------------------------------------------------------- review

    def review_capabilities(self) -> dict[str, bool]:
        """The host-derived review booleans ``app.info`` may disclose.

        They are derived from the private host configuration and the startup
        readiness; no token, grant, access reference or permission list leaves
        the host.  ``can_*`` flags from a candidate detail are *not* included:
        they never replace these host permissions.
        """

        authority = self.config.authority
        read = bool(
            self.ready
            and {"work.scan", "experience.read"}
            <= authority.review_permissions
            and REVIEW_PURPOSE in authority.purposes
        )
        return {
            "read": read,
            "generate": bool(read and "review.generate" in authority.review_permissions),
            "approve": bool(read and "review.approve" in authority.review_permissions),
            "reject": bool(read and "review.reject" in authority.review_permissions),
            "result": bool(read and "result.read" in authority.review_permissions),
        }

    def review_workspace_reason(self) -> str | None:
        """One public label explaining why Review is unavailable, if it is."""

        if not self.ready:
            return self.reason_code or "HOST_NOT_READY"
        authority = self.config.authority
        if not authority.review_permissions:
            return "REVIEW_NOT_CONFIGURED"
        if not {"work.scan", "experience.read"} <= authority.review_permissions:
            return "REVIEW_READ_PERMISSION_MISSING"
        if REVIEW_PURPOSE not in authority.purposes:
            return "REVIEW_PURPOSE_MISSING"
        return None

    def review_work_list(self) -> dict[str, Any]:
        """Managed work (with a review control) plus the read-only legacy set."""

        managed = self.review_facade.managed_work(self._review_access)
        legacy = self.review_facade.legacy_work(self._review_access)
        return {
            "managed": [
                {
                    "work_id": item.work_id,
                    "event_id": item.event_id,
                    "generation": item.generation,
                    "blocked": bool(item.blocked),
                    # The reference value is an authorization reference.  The
                    # host exposes only its existence; C12a §8.6 forbids
                    # showing the reference itself.
                    "rerun_registered": item.rerun_authorization_ref is not None,
                }
                for item in managed
            ],
            "legacy": [
                {
                    "work_id": item.work_id,
                    "event_id": item.event_id,
                    "kind": item.kind,
                    "status": item.status,
                    "created_at": item.created_at,
                }
                for item in legacy
            ],
        }

    def review_candidates(
        self, *, work_id: str, generation: int | None = None
    ) -> dict[str, Any]:
        rows = self.review_facade.candidates(
            self._review_access, work_id=work_id, generation=generation
        )
        return {"candidates": [_candidate_summary(row) for row in rows]}

    def review_candidate_detail(
        self, *, candidate_id: str, artifact_version: int
    ) -> dict[str, Any]:
        detail = self.review_facade.candidate_detail(
            self._review_access,
            candidate_id=candidate_id,
            artifact_version=artifact_version,
        )
        return _candidate_detail(detail)

    def review_generate(self, *, work_id: str, event_id: str) -> dict[str, Any]:
        """Start a review only for a managed Work that has no live candidate.

        Legacy Work is read-only in C12c: it has no collaboration control and
        the host must not turn it into a candidate.  A managed Work with a
        pending or committed candidate is not started twice; a rejected Work
        remains gated by C05d/C07, which alone decides whether a registered
        rerun authorization is still valid.
        """

        managed = {
            item.work_id: item
            for item in self.review_facade.managed_work(self._review_access)
        }
        item = managed.get(work_id)
        if item is None:
            legacy = {
                legacy_item.work_id
                for legacy_item in self.review_facade.legacy_work(self._review_access)
            }
            if work_id in legacy:
                raise ReviewRequestError(
                    "LEGACY_WORK_READ_ONLY",
                    "旧工作项没有审查控制行；本窗口不把它转成候选",
                )
            raise ReviewRequestError(
                "WORK_NOT_PENDING",
                "该工作项已不是待处理的审查工作",
            )
        if item.event_id != event_id:
            raise ReviewRequestError(
                "INVALID_PARAMS",
                "事件不属于该工作项",
            )
        rows = self.review_facade.candidates(
            self._review_access, work_id=work_id, generation=item.generation
        )
        if any(row.status == "pending" for row in rows):
            raise ReviewRequestError(
                "CANDIDATE_EXISTS",
                "该工作项已有待决定的候选；请继续当前审查",
            )
        if any(row.status == "committed" for row in rows):
            raise ReviewRequestError(
                "WORK_ALREADY_RESOLVED",
                "该工作项已有已提交的候选；不能重复发起审查",
            )
        view = self.review_facade.review(
            self._review_access,
            work_id=work_id,
            trigger_event_id=event_id,
            activation_kind="manual_review",
        )
        return _review_view(view)

    def review_resolve(
        self,
        *,
        candidate_id: str,
        artifact_version: int,
        decision: str,
    ) -> dict[str, Any]:
        """Re-prove the exact candidate/version/Work/current binding, then C07.

        This is a pre-check, not a second authority: C07 still re-checks the
        candidate, generation, Approval, Context, deletion barrier and receipt
        inside its own transaction.  The host never supplies an Approval, a
        grant, a candidate body, a model or a provider.
        """

        if decision not in ("approve", "reject"):
            raise ReviewRequestError(
                "INVALID_PARAMS", "decision must be approve or reject"
            )
        try:
            detail = self.review_facade.candidate_detail(
                self._review_access,
                candidate_id=candidate_id,
                artifact_version=artifact_version,
            )
        except RecordNotFound:
            raise ReviewRequestError(
                "CANDIDATE_VERSION_MISMATCH",
                "候选版本已变化或不存在；请刷新候选",
            ) from None
        summary = detail.candidate
        ref = summary.candidate
        if (
            ref.candidate_id != candidate_id
            or ref.artifact_version != artifact_version
        ):
            raise ReviewRequestError(
                "CANDIDATE_VERSION_MISMATCH",
                "候选身份与所选版本不一致；请刷新候选",
            )
        if detail.redacted:
            raise ReviewRequestError(
                "CANDIDATE_STALE",
                "候选内容不可读；没有提交任何决定",
            )
        if ref.artifact_version != 1:
            raise ReviewRequestError(
                "UNSUPPORTED_VERSION",
                "此候选版本不在 C12c 支持范围；不能批准或拒绝",
            )
        if summary.status == "committed":
            # A repeated click (or a retried request) finds the committed
            # candidate.  C07 is the authority that returns its stored receipt;
            # this pre-check only proves the exact Work generation again.
            self._require_review_work(ref)
            view = self.review_facade.resolve(
                self._review_access,
                candidate_id=candidate_id,
                decision=decision,
                decision_id=f"decision:{uuid.uuid4()}",
            )
            return _review_view(view)
        if summary.status != "pending":
            raise ReviewRequestError(
                "CANDIDATE_STALE",
                "候选已不是待决定状态；请刷新候选",
            )
        if detail.eligibility_reason == "stale_generation":
            raise ReviewRequestError(
                "CANDIDATE_STALE",
                "工作轮次已变化；请刷新候选",
            )
        if detail.eligibility_reason == "unsupported_version":
            raise ReviewRequestError(
                "UNSUPPORTED_VERSION",
                "此候选版本不在 C12c 支持范围；不能批准或拒绝",
            )
        if decision == "approve":
            if not detail.can_approve:
                reason = detail.eligibility_reason or "not_approvable"
                if reason in (
                    "target_moved",
                    "current_redacted",
                    "current_missing",
                    "current_terminal",
                ):
                    raise ReviewRequestError(
                        "REVISION_CONFLICT",
                        "当前目标版本已变化或不可用；请刷新权威状态",
                    )
                raise ReviewRequestError(
                    "CANDIDATE_STALE",
                    "候选当前不可批准；请刷新候选",
                )
            if detail.current_state in ("redacted", "missing"):
                raise ReviewRequestError(
                    "REVISION_CONFLICT",
                    "当前目标版本不可读或不存在；请刷新权威状态",
                )
        else:
            if not detail.can_reject:
                raise ReviewRequestError(
                    "CANDIDATE_STALE",
                    "候选当前不可拒绝；请刷新候选",
                )

        self._require_review_work(ref)

        view = self.review_facade.resolve(
            self._review_access,
            candidate_id=candidate_id,
            decision=decision,
            decision_id=f"decision:{uuid.uuid4()}",
        )
        return _review_view(view)

    def _require_review_work(self, ref: Any) -> None:
        managed = {
            item.work_id: item
            for item in self.review_facade.managed_work(self._review_access)
        }
        work = managed.get(ref.work_id)
        if work is None:
            raise ReviewRequestError(
                "WORK_NOT_PENDING",
                "该工作项已不是待处理状态；没有提交决定",
            )
        if work.generation != ref.work_generation:
            raise ReviewRequestError(
                "CANDIDATE_STALE",
                "工作轮次与候选不一致；请刷新候选",
            )

    def review_result(self, *, interaction_id: str) -> dict[str, Any]:
        view = self.review_facade.result(
            self._review_access, interaction_id=interaction_id
        )
        return _review_view(view)

    # -------------------------------------------------------------- readiness

    def _readiness(self) -> tuple[bool, str | None, str | None]:
        config = self.config
        namespace = config.authority.namespace
        if config.model.transport_kind == "http":
            key = os.environ.get(config.model.manifest.api_key_env, "")
            if not key:
                return (
                    False,
                    "MODEL_KEY_MISSING",
                    "the host has not provided the configured model credential",
                )
        try:
            active = self.profiles.get_active_profile(namespace)
        except Exception:  # noqa: BLE001 - a broken profile read is not ready
            active = None
        if active is None or active.ref is None:
            return (
                False,
                "PROFILE_MISSING",
                "the store has no active host profile",
            )
        authorization = self.store.active_model_authorization(
            namespace=namespace,
            provider=config.model.provider,
            model=config.model.model,
            manifest_hash=config.model.manifest.manifest_hash,
        )
        if authorization is None:
            return (
                False,
                "MODEL_AUTHORIZATION_MISSING",
                "no active host authorization matches the configured destination",
            )
        preflight = getattr(self.model, "preflight", None)
        if callable(preflight):
            try:
                preflight(namespace=namespace)
            except Exception as exc:  # noqa: BLE001 - preflight refusal is a state
                code = getattr(exc, "code", None)
                return (
                    False,
                    str(code) if isinstance(code, str) else type(exc).__name__,
                    "the configured model cannot start a call",
                )
        return True, None, None

    # -------------------------------------------------------------- lifecycle

    def close(self) -> None:
        try:
            if self.store is not None:
                self.store.close()
        finally:
            self._remove_synthetic_key()

    def _remove_synthetic_key(self) -> None:
        if self._lab_key_env is not None:
            os.environ.pop(self._lab_key_env, None)
            self._lab_key_env = None


def _ensure_synthetic_key(config: HostConfig) -> str | None:
    """Give the Lab scripted transport a process-only placeholder credential.

    A production configuration must supply the real credential through the
    environment named by its manifest; this helper never touches that profile.
    """

    if not config.is_lab or config.model.transport_kind != "scripted":
        return None
    name = config.model.manifest.api_key_env
    if os.environ.get(name):
        return None
    os.environ[name] = LAB_KEY_PREFIX + secrets.token_hex(24)
    return name


def _build_model(
    config: HostConfig, store: CognitionStore, *, review: bool = False
) -> Any:
    """Build the consult model or the legacy review model.

    Consult uses the configured ``collab.v1`` destination and C09 policy.
    Review keeps the C07 ModelPort shape: the same provider/model/manifest and
    model authorization, but the legacy ``pcs2-008`` request/renderer the
    review generator has always used.  No second destination or provider is
    invented, and the model authorization + pre-send Context gate stay in
    ``DirectHttpModel``.
    """

    spec = config.model
    policy = ConsultSendPolicy(
        namespace=config.authority.namespace,
        host_instance=config.authority.host_instance,
        access_ref=config.authority.access_ref,
        privacy_classes=config.authority.privacy_classes,
        purposes=config.authority.purposes,
        disclose=config.authority.disclose,
    )
    if spec.transport_kind == "scripted":
        if not config.is_lab:
            raise RuntimeStartupError(
                "CONFIG_INVALID", "the scripted transport requires the lab profile"
            )
        return ScriptedHttpModel(
            store,
            spec,
            policy=policy,
            protocol=(
                DIRECT_HTTP_LEGACY_PROTOCOL if review else spec.model_protocol
            ),
            script=(
                (spec.review_script or spec.script) if review else None
            ),
        )
    return DirectHttpModel(
        store,
        spec.manifest,
        protocol=(
            DIRECT_HTTP_LEGACY_PROTOCOL if review else spec.model_protocol
        ),
        consult_policy=policy,
    )


class ScriptedHttpModel(DirectHttpModel):
    """``DirectHttpModel`` with a deterministic in-process transport.

    Only ``_send`` is replaced.  Everything that makes an attempt auditable --
    the send gate, the reservation, the request hash, the response validation,
    settlement and ``last_attempt_evidence`` -- stays in the real adapter.  The
    script is host configuration, not model output.
    """

    def __init__(
        self,
        store: CognitionStore,
        spec: ModelSpec,
        *,
        policy: ConsultSendPolicy,
        protocol: str | None = None,
        script: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            store,
            spec.manifest,
            protocol=spec.model_protocol if protocol is None else protocol,
            consult_policy=policy,
        )
        self._spec = spec
        self._script = [
            dict(item) for item in (spec.script if script is None else script)
        ]
        self._script_lock = threading.Lock()
        self._script_index = 0
        self.transport_calls = 0
        self.last_request_sha256: str | None = None

    def _send(self, body_text: str, *, timeout: float, api_key: str) -> str:
        del timeout
        del api_key
        if self._spec.delay_ms:
            time.sleep(self._spec.delay_ms / 1000.0)
        with self._script_lock:
            if self._script_index >= len(self._script):
                raise RuntimeError("the configured model script is exhausted")
            output = self._script[self._script_index]
            self._script_index += 1
            self.transport_calls += 1
            self.last_request_sha256 = hashlib.sha256(
                body_text.encode("utf-8")
            ).hexdigest()
        content = json.dumps(
            output, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        envelope = {
            "id": "chatcmpl-desktop-lab",
            "object": "chat.completion",
            "created": 0,
            "model": self._spec.model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ],
            "usage": {
                "prompt_tokens": 128,
                "completion_tokens": 32,
                "total_tokens": 160,
            },
        }
        return json.dumps(
            envelope, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )


def _profile_body(seed: Any) -> dict[str, Any]:
    return {
        "profile_id": seed.profile_id,
        "profile_version": seed.profile_version,
        "identity_summary": (
            "Synthetic desktop Lab profile. No real persona text is present."
        ),
        "principles": [
            "State what is unknown instead of inventing an answer.",
            "Do not claim a tool ran without evidence.",
        ],
        "style": {
            "language": "zh-CN",
            "tone": "plain",
            "verbosity_by_context": {"casual": "short", "technical": "detailed"},
        },
        "attention_preferences": [
            {
                "topic": "synthetic desktop Consult verification",
                "reason": "Lab fixture",
            }
        ],
        "examples": [
            {
                "example_id": "desktop-lab-example-1",
                "scene": "synthetic desktop consult",
                "user": "Synthetic user line.",
                "assistant": "Synthetic assistant line.",
                "synthetic": True,
            }
        ],
        "asset_hash": "0" * 64,
        "renderer_compatibility": "pcs.renderer.synthetic-v1",
    }


def _query_summary(query_body: str | None) -> dict[str, Any] | None:
    """Project only the public, displayable part of ``QueryResult.to_body``.

    The internal omission counts and the state revision never enter this
    projection.  Items are reduced to their exact refs plus the public
    current/expired flags so the source drawer can label a ref as current or
    historical without re-running a query.
    """

    if not query_body:
        return None
    try:
        document = json.loads(query_body)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    items: list[dict[str, Any]] = []
    raw_items = document.get("items")
    if isinstance(raw_items, list):
        for item in raw_items[:_MAX_PUBLIC_ITEMS]:
            if not isinstance(item, dict):
                continue
            ref = item.get("ref")
            if not isinstance(ref, dict) or ref.get("ref_type") != "record":
                continue
            items.append(
                {
                    "ref": {
                        "ref_type": "record",
                        "namespace": ref.get("namespace"),
                        "record_id": ref.get("record_id"),
                        "record_version": ref.get("record_version"),
                    },
                    "current": bool(item.get("current")) if "current" in item else None,
                    "expired": bool(item.get("expired")) if "expired" in item else None,
                }
            )
    return {
        "kind": document.get("query_kind"),
        "status": document.get("status"),
        "material_status": document.get("material_status"),
        "truncated": bool(document.get("truncated")),
        "policy_version": document.get("query_policy_version"),
        "items": items,
    }


# --------------------------------------------------------------------------
# review wire projections
# --------------------------------------------------------------------------

_REF_KEYS: dict[str, tuple[str, ...]] = {
    "event": ("ref_type", "namespace", "event_id"),
    "record": ("ref_type", "namespace", "record_id", "record_version"),
    "collab_input": ("ref_type", "namespace", "input_id", "content_version"),
    "collab_artifact": (
        "ref_type",
        "namespace",
        "artifact_id",
        "artifact_version",
    ),
}


def _public_ref(ref: Any) -> dict[str, Any] | None:
    """Return one exact, allow-listed reference object or ``None``."""

    if not isinstance(ref, Mapping):
        return None
    keys = _REF_KEYS.get(ref.get("ref_type"))
    if keys is None or any(key not in ref for key in keys):
        return None
    return {key: ref[key] for key in keys}


def _evidence_link(link: Any) -> dict[str, Any] | None:
    """Project an evidence link without internal grouping fields."""

    if not isinstance(link, Mapping):
        return None
    ref = _public_ref(link.get("ref"))
    if ref is None:
        return None
    relation = link.get("relation")
    return {
        "ref": ref,
        "relation": relation if isinstance(relation, str) else None,
    }


def _proposal_view(proposal: Any) -> dict[str, Any]:
    links = [
        projected
        for projected in (
            _evidence_link(link) for link in proposal.evidence_links
        )
        if projected is not None
    ]
    return {
        "operation": proposal.operation,
        "target_ref": (
            None if proposal.target_ref is None else proposal.target_ref.to_dict()
        ),
        "claim": proposal.claim,
        "claim_type": proposal.claim_type,
        "scope": None if proposal.scope is None else dict(proposal.scope),
        "evidence_links": links,
        "strength": proposal.strength,
        "strength_reason": proposal.strength_reason,
        "valid_until": proposal.valid_until,
    }


def _current_view(current: Any) -> dict[str, Any]:
    return {
        "ref": current.ref.to_dict(),
        "claim": current.claim,
        "claim_type": current.claim_type,
        "status": current.status,
        "strength": current.strength,
        "scope": dict(current.scope),
        "valid_until": current.valid_until,
        "applicability": current.applicability,
    }


def _diff_value(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field == "evidence_links":
        if not isinstance(value, (list, tuple)):
            return []
        return [
            projected
            for projected in (
                _evidence_link(link) for link in value
            )
            if projected is not None
        ]
    if field == "scope" and isinstance(value, Mapping):
        return dict(value)
    return value


def _diff_field(row: Any) -> dict[str, Any]:
    return {
        "field": row.field,
        "before": _diff_value(row.field, row.before),
        "after": _diff_value(row.field, row.after),
        "changed": bool(row.changed),
    }


def _source_view(source: Any) -> dict[str, Any]:
    return {
        "ref": _public_ref(source.ref),
        "kind": source.kind,
        "availability": source.availability,
        "reason": source.reason,
        "body": source.body,
        "ref_version": source.ref_version,
    }


def _candidate_summary(summary: Any) -> dict[str, Any]:
    ref = summary.candidate
    attempt = summary.attempt_ref if isinstance(summary.attempt_ref, dict) else {}
    attempt_id = attempt.get("attempt_id")
    attempt_no = attempt.get("attempt_no")
    return {
        "candidate_id": ref.candidate_id,
        "artifact_version": ref.artifact_version,
        "work_id": ref.work_id,
        "work_generation": ref.work_generation,
        "interaction_id": summary.interaction_id,
        "context_id": summary.context_id,
        "status": summary.status,
        "created_at": summary.created_at,
        "decided_at": summary.decided_at,
        "attempt_id": attempt_id if isinstance(attempt_id, str) else None,
        "attempt_no": attempt_no if type(attempt_no) is int else None,
    }


def _candidate_detail(detail: Any) -> dict[str, Any]:
    return {
        "candidate": _candidate_summary(detail.candidate),
        "proposal": (
            None if detail.proposal is None else _proposal_view(detail.proposal)
        ),
        "structural_validation": detail.structural_validation,
        "current": None if detail.current is None else _current_view(detail.current),
        "current_state": detail.current_state,
        "diff": [_diff_field(row) for row in detail.diff],
        "sources": [_source_view(source) for source in detail.sources],
        "can_approve": bool(detail.can_approve),
        "can_reject": bool(detail.can_reject),
        "eligibility_reason": detail.eligibility_reason,
        "redacted": bool(detail.redacted),
        "detail": detail.detail,
    }


def _receipt(receipt: Any) -> dict[str, Any]:
    """Project a receipt without the internal ``state_revision`` field."""

    return {
        "request_id": receipt.request_id,
        "context_id": receipt.context_id,
        "operation": receipt.operation,
        "outcome": receipt.outcome,
        "committed_at": receipt.committed_at,
        "record_refs": [
            {
                "record_id": ref.record_id,
                "record_version": ref.record_version,
            }
            for ref in receipt.record_refs
        ],
    }


def _review_view(view: Any) -> dict[str, Any]:
    return {
        "state": view.state,
        "interaction_id": view.interaction_id,
        "candidate_id": view.candidate_id,
        "receipt": None if view.receipt is None else _receipt(view.receipt),
        "redacted": bool(view.redacted),
        "detail": view.detail,
        "model_adapter": view.model_adapter,
    }


def _migrations_dir() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "resources" / "migrations"
    import quintessence_pcs

    return (
        Path(quintessence_pcs.__file__).resolve().parent
        / "resources"
        / "migrations"
    )


def _uuid5(value: str) -> Any:
    import uuid

    return uuid.uuid5(uuid.NAMESPACE_URL, value)


__all__ = [
    "DesktopRuntime",
    "InputRegistrationError",
    "ReviewRequestError",
    "RuntimeStartupError",
    "ScriptedHttpModel",
]
