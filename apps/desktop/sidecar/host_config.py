"""Trusted-host configuration for the QUINTESSENCE desktop Consult bridge.

The sidecar never derives authority from a WebView frame.  Namespace, host
instance, access reference, privacy/purpose sets, retention, policy, the
deterministic query plan and the destination all come from this private host
configuration file, which lives outside the source checkout.

Only two things are expanded from the environment: ``store.database`` and
``destination.manifest_path``.  Secrets are never stored in the file; the model
manifest names an environment variable and the adapter reads it at call time.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from quintessence_pcs.domains.cognition import BeliefCurrentQuery
from quintessence_pcs.collaboration.facade import FACADE_PERMISSIONS
from quintessence_pcs.foundation import identifiers
from quintessence_pcs.foundation.errors import InvalidInput
from quintessence_pcs.runtime import ModelManifest

APP_NAME = "quintessence-desktop"
APP_VERSION = "0.1.0"
PROTOCOL_VERSION = "desktop.v1"
CONFIG_SCHEMA = "desktop.host.v1"
DESTINATION_SCHEMA = "desktop.destination.v1"

MAX_QUESTION_CHARS = 8000
MAX_CONFIG_BYTES = 256 * 1024
RETENTION_VALUES = ("session", "standard", "durable")
TRANSPORT_KINDS = ("http", "scripted")
PROFILES = ("lab", "production")
MODEL_PROTOCOL = "collab.v1"
REVIEW_PURPOSE = "review"
REVIEW_PERMISSION_VALUES = tuple(sorted(FACADE_PERMISSIONS))

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """A configuration failure that is safe to describe to the UI."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# public DTOs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HostAuthority:
    """The host-owned authority fields for one desktop session."""

    namespace: str
    host_instance: str
    operator: str
    access_ref: str
    privacy_classes: frozenset[str]
    purposes: frozenset[str]
    disclose: bool
    retention: str
    policy_ref: str
    input_privacy_class: str
    review_permissions: frozenset[str]

    def public_summary(self) -> dict[str, Any]:
        """The non-secret identity summary shown in the UI."""

        return {
            "namespace": self.namespace,
            "host_instance": self.host_instance,
            "operator": self.operator,
            "retention": self.retention,
            "policy_ref": self.policy_ref,
        }


@dataclass(frozen=True)
class DestinationDescriptor:
    """The stable, non-secret destination identity derived by the host."""

    destination_id: str
    provider: str
    model: str
    external: bool
    confirmation_required: bool
    manifest_hash: str
    policy_ref: str
    config_fingerprint: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "destination_id": self.destination_id,
            "provider": self.provider,
            "model": self.model,
            "external": self.external,
            "kind": "external" if self.external else "local",
            "confirmation_required": self.confirmation_required,
            "manifest_hash": self.manifest_hash,
            "policy_ref": self.policy_ref,
        }


@dataclass(frozen=True)
class ModelSpec:
    """The host-configured model transport (no secret material)."""

    provider: str
    model: str
    model_protocol: str
    external: bool
    manifest: ModelManifest
    transport_kind: str
    script: tuple[dict[str, Any], ...]
    review_script: tuple[dict[str, Any], ...]
    delay_ms: int


@dataclass(frozen=True)
class LabSeed:
    """Synthetic Lab fixtures; never present in a production configuration."""

    profile_id: str
    profile_version: str
    authorization_ref: str
    belief_claim: str
    belief_claim_type: str
    belief_strength: str
    belief_scope: dict[str, Any]
    belief_reason: str


@dataclass(frozen=True)
class HostConfig:
    """One validated private host configuration."""

    path: Path
    profile: str
    label: str
    ai_display_name: str | None
    database_path: Path
    authority: HostAuthority
    query_plan: BeliefCurrentQuery | None
    max_question_chars: int
    destination: DestinationDescriptor
    model: ModelSpec
    lab: LabSeed | None
    managed: bool = False
    provision: bool = False

    @property
    def is_lab(self) -> bool:
        return self.profile == "lab"


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def load_config(path: str | Path) -> HostConfig:
    """Read and validate one private host configuration file."""

    resolved = Path(path).expanduser()
    try:
        resolved = resolved.resolve(strict=True)
    except OSError as exc:
        raise ConfigError("CONFIG_MISSING", "host configuration was not found") from exc
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise ConfigError("CONFIG_UNREADABLE", "host configuration is not readable") from exc
    if not raw or len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError(
            "CONFIG_INVALID", "host configuration has an invalid size"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError("CONFIG_INVALID", "host configuration is not UTF-8") from exc
    try:
        document = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except _DuplicateKey as exc:
        raise ConfigError("CONFIG_INVALID", "host configuration repeats a key") from exc
    except ValueError as exc:
        raise ConfigError("CONFIG_INVALID", "host configuration is not JSON") from exc
    if not isinstance(document, dict):
        raise ConfigError("CONFIG_INVALID", "host configuration must be an object")
    return _parse(document, resolved)


def _parse(document: Mapping[str, Any], path: Path) -> HostConfig:
    _exact(
        document,
        required={"schema", "app", "store", "host", "consult", "destination"},
        optional={"lab", "review", "desktop"},
        where="configuration",
    )
    if document["schema"] != CONFIG_SCHEMA:
        raise ConfigError("CONFIG_INVALID", "host configuration schema is unsupported")

    app = _object(
        document["app"], "app", {"profile"}, {"label", "ai_display_name"}
    )
    profile = _choice(app["profile"], "app.profile", PROFILES)
    label = _text(app.get("label", "QUINTESSENCE"), "app.label", 120)
    ai_display_name = None
    if "ai_display_name" in app and app["ai_display_name"] is not None:
        ai_display_name = _text(
            app["ai_display_name"], "app.ai_display_name", 40
        )

    store = _object(document["store"], "store", {"database"}, set())
    database = _resolve_path(
        _expand_env(_text(store["database"], "store.database", 4096), "store.database"),
        path.parent,
        "store.database",
    )
    if database.name in ("", ".", ".."):
        raise ConfigError("CONFIG_INVALID", "store.database must name a file")

    host_object = _object(
        document["host"],
        "host",
        {
            "namespace",
            "host_instance",
            "operator",
            "access_ref",
            "privacy_classes",
            "purposes",
            "disclose",
            "retention",
            "policy_ref",
        },
        {"input_privacy_class"},
    )
    namespace = _namespace(host_object["namespace"], "host.namespace")
    privacy_classes = _string_set(
        host_object["privacy_classes"], "host.privacy_classes", identifiers.PRIVACY_CLASSES
    )
    input_privacy = _choice(
        host_object.get("input_privacy_class", sorted(privacy_classes)[0]),
        "host.input_privacy_class",
        identifiers.PRIVACY_CLASSES,
    )
    if input_privacy not in privacy_classes:
        raise ConfigError(
            "CONFIG_INVALID",
            "host.input_privacy_class must be one of host.privacy_classes",
        )
    purposes = _string_set(
        host_object["purposes"], "host.purposes", None
    )
    if "consult" not in purposes:
        raise ConfigError(
            "CONFIG_INVALID", "host.purposes must contain the consult purpose"
        )
    review_object = _object(
        document.get("review", {}), "review", set(), {"permissions"}
    )
    review_permissions = (
        frozenset()
        if "permissions" not in review_object
        else _string_set(
            review_object["permissions"],
            "review.permissions",
            REVIEW_PERMISSION_VALUES,
        )
    )
    if review_permissions and REVIEW_PURPOSE not in purposes:
        raise ConfigError(
            "CONFIG_INVALID",
            "host.purposes must contain the review purpose when review "
            "permissions are configured",
        )
    authority = HostAuthority(
        namespace=namespace,
        host_instance=_text(host_object["host_instance"], "host.host_instance", 256),
        operator=_text(host_object["operator"], "host.operator", 256),
        access_ref=_text(host_object["access_ref"], "host.access_ref", 512),
        privacy_classes=privacy_classes,
        purposes=purposes,
        disclose=_boolean(host_object["disclose"], "host.disclose"),
        retention=_choice(host_object["retention"], "host.retention", RETENTION_VALUES),
        policy_ref=_text(host_object["policy_ref"], "host.policy_ref", 512),
        input_privacy_class=input_privacy,
        review_permissions=review_permissions,
    )

    consult = _object(
        document["consult"], "consult", {"query_plan"}, {"max_question_chars"}
    )
    query_plan = _query_plan(consult["query_plan"])
    max_question = _integer(
        consult.get("max_question_chars", MAX_QUESTION_CHARS),
        "consult.max_question_chars",
        1,
        MAX_QUESTION_CHARS,
    )

    model = _parse_destination(document["destination"], path.parent)
    destination = _destination_descriptor(
        model, authority.policy_ref, profile
    )

    lab = None
    if "lab" in document:
        if profile != "lab":
            raise ConfigError(
                "CONFIG_INVALID",
                "the lab fixture section is only valid for the lab profile",
            )
        if document["lab"].get("seed") is not True:
            raise ConfigError(
                "CONFIG_INVALID", "the lab section requires seed=true"
            )
        lab = _lab_seed(document["lab"], authority.namespace)

    desktop = _object(
        document.get("desktop", {}),
        "desktop",
        set(),
        {"managed", "provision"},
    )
    managed = (
        _boolean(desktop["managed"], "desktop.managed")
        if "managed" in desktop
        else False
    )
    provision = (
        _boolean(desktop["provision"], "desktop.provision")
        if "provision" in desktop
        else False
    )
    if provision and not managed:
        raise ConfigError(
            "CONFIG_INVALID",
            "desktop.provision requires desktop.managed=true",
        )
    if managed and profile != "production":
        raise ConfigError(
            "CONFIG_INVALID",
            "desktop.managed is only available for the production profile",
        )

    if profile == "production" and model.transport_kind == "scripted":
        raise ConfigError(
            "CONFIG_INVALID",
            "the scripted transport is only available in the lab profile",
        )

    return HostConfig(
        path=path,
        profile=profile,
        label=label,
        ai_display_name=ai_display_name,
        database_path=database,
        authority=authority,
        query_plan=query_plan,
        max_question_chars=max_question,
        destination=destination,
        model=model,
        lab=lab,
        managed=managed,
        provision=provision,
    )


def _parse_destination(value: Any, base: Path) -> ModelSpec:
    destination = _object(
        value,
        "destination",
        {"provider", "model", "model_protocol", "external"},
        {"manifest", "manifest_path", "transport"},
    )
    provider = _text(destination["provider"], "destination.provider", 128)
    model = _text(destination["model"], "destination.model", 128)
    protocol = _text(
        destination["model_protocol"], "destination.model_protocol", 64
    )
    if protocol != MODEL_PROTOCOL:
        raise ConfigError(
            "CONFIG_INVALID", f"destination.model_protocol must be {MODEL_PROTOCOL!r}"
        )
    external = _boolean(destination["external"], "destination.external")

    has_manifest = "manifest" in destination
    has_path = "manifest_path" in destination
    if has_manifest == has_path:
        raise ConfigError(
            "CONFIG_INVALID",
            "destination must carry exactly one of manifest or manifest_path",
        )
    if has_path:
        manifest_path = _resolve_path(
            _expand_env(
                _text(destination["manifest_path"], "destination.manifest_path", 4096),
                "destination.manifest_path",
            ),
            base,
            "destination.manifest_path",
        )
        try:
            manifest_raw = manifest_path.read_bytes()
        except OSError as exc:
            raise ConfigError(
                "CONFIG_INVALID", "destination.manifest_path is not readable"
            ) from exc
        if not manifest_raw or len(manifest_raw) > MAX_CONFIG_BYTES:
            raise ConfigError(
                "CONFIG_INVALID", "destination.manifest_path has an invalid size"
            )
        try:
            manifest_document = json.loads(
                manifest_raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys
            )
        except (_DuplicateKey, UnicodeDecodeError, ValueError) as exc:
            raise ConfigError(
                "CONFIG_INVALID", "destination.manifest_path is not strict JSON"
            ) from exc
    else:
        manifest_document = destination["manifest"]
    try:
        manifest = ModelManifest.from_mapping(manifest_document)
    except Exception as exc:  # noqa: BLE001 - any manifest refusal is a config error
        raise ConfigError(
            "CONFIG_INVALID", f"the model manifest is invalid ({type(exc).__name__})"
        ) from exc
    if manifest.provider != provider or manifest.model != model:
        raise ConfigError(
            "CONFIG_INVALID",
            "destination provider/model must match the model manifest",
        )

    transport_value = destination.get("transport", {"kind": "http"})
    transport = _object(
        transport_value,
        "destination.transport",
        {"kind"},
        {"script", "delay_ms", "review_script"},
    )
    kind = _choice(transport["kind"], "destination.transport.kind", TRANSPORT_KINDS)
    script: tuple[dict[str, Any], ...] = ()
    review_script: tuple[dict[str, Any], ...] = ()
    delay_ms = 0
    if kind == "http":
        if "script" in transport or "delay_ms" in transport:
            raise ConfigError(
                "CONFIG_INVALID",
                "the http transport does not accept script or delay_ms",
            )
        if "review_script" in transport:
            raise ConfigError(
                "CONFIG_INVALID",
                "the http transport does not accept review_script",
            )
    else:
        raw_script = transport.get("script")
        if not isinstance(raw_script, list) or not raw_script:
            raise ConfigError(
                "CONFIG_INVALID",
                "the scripted transport requires a non-empty script list",
            )
        entries: list[dict[str, Any]] = []
        for index, item in enumerate(raw_script):
            if not isinstance(item, dict):
                raise ConfigError(
                    "CONFIG_INVALID", f"transport.script[{index}] must be an object"
                )
            action = item.get("action")
            if action not in ("none", "query", "wait", "propose"):
                raise ConfigError(
                    "CONFIG_INVALID",
                    f"transport.script[{index}].action is not supported",
                )
            entries.append(dict(item))
        script = tuple(entries)
        delay_ms = _integer(
            transport.get("delay_ms", 0), "destination.transport.delay_ms", 0, 600_000
        )
        if "review_script" in transport:
            raw_review = transport["review_script"]
            if not isinstance(raw_review, list) or not raw_review:
                raise ConfigError(
                    "CONFIG_INVALID",
                    "destination.transport.review_script must be a non-empty list",
                )
            review_entries: list[dict[str, Any]] = []
            for index, item in enumerate(raw_review):
                if not isinstance(item, dict):
                    raise ConfigError(
                        "CONFIG_INVALID",
                        f"transport.review_script[{index}] must be an object",
                    )
                review_entries.append(dict(item))
            review_script = tuple(review_entries)

    return ModelSpec(
        provider=provider,
        model=model,
        model_protocol=protocol,
        external=external,
        manifest=manifest,
        transport_kind=kind,
        script=script,
        review_script=review_script,
        delay_ms=delay_ms,
    )


def _destination_descriptor(
    model: ModelSpec,
    policy_ref: str,
    profile: str,
) -> DestinationDescriptor:
    config_fingerprint = _sha256(
        {
            "schema": "desktop.model-config.v1",
            "provider": model.provider,
            "model": model.model,
            "model_protocol": model.model_protocol,
            "manifest_hash": model.manifest.manifest_hash,
            "external": model.external,
            "transport": model.transport_kind,
            "base_url_sha256": _sha256(model.manifest.base_url),
            "max_calls": model.manifest.max_calls,
            "max_output_tokens_per_call": model.manifest.max_output_tokens_per_call,
            "max_wall_seconds": model.manifest.max_wall_seconds,
            "thinking": model.manifest.thinking,
            "temperature": model.manifest.temperature,
            "profile": profile,
        }
    )
    destination_id = "sha256:" + _sha256(
        {
            "schema": DESTINATION_SCHEMA,
            "provider": model.provider,
            "model": model.model,
            "model_protocol": model.model_protocol,
            "manifest_hash": model.manifest.manifest_hash,
            "policy_ref": policy_ref,
            "config_fingerprint": config_fingerprint,
        }
    )
    return DestinationDescriptor(
        destination_id=destination_id,
        provider=model.provider,
        model=model.model,
        external=model.external,
        confirmation_required=model.external,
        manifest_hash=model.manifest.manifest_hash,
        policy_ref=policy_ref,
        config_fingerprint=config_fingerprint,
    )


def _lab_seed(value: Any, namespace: str) -> LabSeed:
    lab = _object(
        value,
        "lab",
        {"seed", "profile_id", "profile_version", "authorization_ref", "belief"},
        set(),
    )
    belief = _object(
        lab["belief"],
        "lab.belief",
        {"claim", "claim_type", "strength", "scope", "reason"},
        {"inference_basis"},
    )
    scope = belief["scope"]
    if not isinstance(scope, dict):
        raise ConfigError("CONFIG_INVALID", "lab.belief.scope must be an object")
    return LabSeed(
        profile_id=_text(lab["profile_id"], "lab.profile_id", 128),
        profile_version=_text(lab["profile_version"], "lab.profile_version", 32),
        authorization_ref=_text(
            lab["authorization_ref"], "lab.authorization_ref", 512
        ),
        belief_claim=_text(belief["claim"], "lab.belief.claim", 512),
        belief_claim_type=_text(belief["claim_type"], "lab.belief.claim_type", 64),
        belief_strength=_text(belief["strength"], "lab.belief.strength", 32),
        belief_scope=dict(scope),
        belief_reason=_text(belief["reason"], "lab.belief.reason", 512),
    )


# --------------------------------------------------------------------------
# validation helpers
# --------------------------------------------------------------------------


class _DuplicateKey(Exception):
    pass


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = item
    return result


def _exact(
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str],
    where: str,
) -> None:
    keys = set(value)
    missing = sorted(required - keys)
    unknown = sorted(keys - required - optional)
    if missing:
        raise ConfigError(
            "CONFIG_INVALID", f"{where} is missing: {', '.join(missing)}"
        )
    if unknown:
        raise ConfigError(
            "CONFIG_INVALID", f"{where} has unknown keys: {', '.join(unknown)}"
        )


def _object(
    value: Any, where: str, required: set[str], optional: set[str]
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError("CONFIG_INVALID", f"{where} must be an object")
    _exact(value, required=required, optional=optional, where=where)
    return value


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError("CONFIG_INVALID", f"{field} must be a non-empty string")
    if len(value) > maximum:
        raise ConfigError("CONFIG_INVALID", f"{field} is too long")
    if "\x00" in value:
        raise ConfigError("CONFIG_INVALID", f"{field} must not contain NUL")
    return value


def _choice(value: Any, field: str, choices: tuple[str, ...]) -> str:
    text = _text(value, field, 64)
    if text not in choices:
        raise ConfigError("CONFIG_INVALID", f"{field} must be one of {', '.join(choices)}")
    return text


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ConfigError("CONFIG_INVALID", f"{field} must be a boolean")
    return value


def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigError(
            "CONFIG_INVALID", f"{field} must be an integer between {minimum} and {maximum}"
        )
    return value


def _namespace(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ConfigError("CONFIG_INVALID", f"{field} must be a string")
    try:
        return identifiers.require_namespace(value)
    except InvalidInput as exc:
        raise ConfigError("CONFIG_INVALID", f"{field} is not a valid namespace") from exc


def _string_set(
    value: Any, field: str, allowed: tuple[str, ...] | None
) -> frozenset[str]:
    if not isinstance(value, list) or not value:
        raise ConfigError("CONFIG_INVALID", f"{field} must be a non-empty list")
    items: list[str] = []
    for index, item in enumerate(value):
        text = _text(item, f"{field}[{index}]", 64)
        if allowed is not None and text not in allowed:
            raise ConfigError(
                "CONFIG_INVALID", f"{field}[{index}] must be one of {', '.join(allowed)}"
            )
        items.append(text)
    if len(set(items)) != len(items):
        raise ConfigError("CONFIG_INVALID", f"{field} must not repeat a value")
    return frozenset(items)


def _query_plan(value: Any) -> BeliefCurrentQuery | None:
    if value is None:
        return None
    plan = _object(
        value,
        "consult.query_plan",
        {"kind", "claim_types", "scope", "limit"},
        set(),
    )
    if plan["kind"] != "belief_current":
        raise ConfigError(
            "CONFIG_INVALID", "consult.query_plan.kind must be 'belief_current'"
        )
    raw_types = plan["claim_types"]
    if not isinstance(raw_types, list):
        raise ConfigError(
            "CONFIG_INVALID", "consult.query_plan.claim_types must be a list"
        )
    claim_types = tuple(
        _text(item, "consult.query_plan.claim_types[]", 64) for item in raw_types
    )
    scope = plan["scope"]
    if not isinstance(scope, dict):
        raise ConfigError("CONFIG_INVALID", "consult.query_plan.scope must be an object")
    limit = _integer(plan["limit"], "consult.query_plan.limit", 1, 100)
    try:
        return BeliefCurrentQuery(
            claim_types=claim_types, scope=dict(scope), limit=limit
        )
    except Exception as exc:  # noqa: BLE001 - a plan refusal is a config error
        raise ConfigError(
            "CONFIG_INVALID", f"consult.query_plan is invalid ({type(exc).__name__})"
        ) from exc


def _expand_env(value: str, field: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = os.environ.get(name)
        if not resolved:
            raise ConfigError(
                "CONFIG_ENV_MISSING",
                f"{field} references an unset environment variable {name}",
            )
        return resolved

    expanded = _ENV_PATTERN.sub(replace, value)
    if "${" in expanded:
        raise ConfigError("CONFIG_INVALID", f"{field} has an invalid environment reference")
    return expanded


def _resolve_path(value: str, base: Path, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    try:
        return path.resolve()
    except OSError as exc:
        raise ConfigError("CONFIG_INVALID", f"{field} cannot be resolved") from exc


def _sha256(value: Any) -> str:
    if isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def destination_id_for(config: HostConfig) -> str:
    return config.destination.destination_id


__all__ = [
    "APP_NAME",
    "APP_VERSION",
    "CONFIG_SCHEMA",
    "MAX_QUESTION_CHARS",
    "PROTOCOL_VERSION",
    "ConfigError",
    "DestinationDescriptor",
    "HostAuthority",
    "HostConfig",
    "LabSeed",
    "ModelSpec",
    "destination_id_for",
    "load_config",
]
