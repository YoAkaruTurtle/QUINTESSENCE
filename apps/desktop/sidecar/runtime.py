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
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from quintessence_pcs.collaboration import (
    ConsultFacade,
    ConsultHostAccess,
    ConsultRequest,
)
from quintessence_pcs.domains.cognition import (
    CognitionQueryService,
    SelfViewService,
)
from quintessence_pcs.domains.persona.profiles import (
    REVIEW_APPROVE_EVENT_TYPE,
    PersonaProfileService,
)
from quintessence_pcs.foundation.timeutil import utc_now_rfc3339
from quintessence_pcs.runtime import (
    ConsultSendPolicy,
    ContextService,
    DirectHttpModel,
)
from quintessence_pcs.store import CognitionStore

from host_config import HostConfig, ModelSpec

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
            self.facade = ConsultFacade(
                self.store,
                context_service=self.contexts,
                model=self.model,
                query_service=self.queries,
            )
            self._access = ConsultHostAccess(
                namespace=config.authority.namespace,
                host_instance=config.authority.host_instance,
                access_ref=config.authority.access_ref,
                privacy_classes=config.authority.privacy_classes,
                purposes=config.authority.purposes,
                disclose=config.authority.disclose,
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


def _build_model(config: HostConfig, store: CognitionStore) -> Any:
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
        return ScriptedHttpModel(store, spec, policy=policy)
    return DirectHttpModel(
        store,
        spec.manifest,
        protocol=spec.model_protocol,
        consult_policy=policy,
    )


class ScriptedHttpModel(DirectHttpModel):
    """``DirectHttpModel`` with a deterministic in-process transport.

    Only ``_send`` is replaced.  Everything that makes an attempt auditable --
    the C09 send gate, the reservation, the request hash, the response
    validation, settlement and ``last_attempt_evidence`` -- stays in the real
    adapter.  The script is host configuration, not model output.
    """

    def __init__(
        self,
        store: CognitionStore,
        spec: ModelSpec,
        *,
        policy: ConsultSendPolicy,
    ) -> None:
        super().__init__(
            store,
            spec.manifest,
            protocol=spec.model_protocol,
            consult_policy=policy,
        )
        self._spec = spec
        self._script = [dict(item) for item in spec.script]
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
    "RuntimeStartupError",
    "ScriptedHttpModel",
]
