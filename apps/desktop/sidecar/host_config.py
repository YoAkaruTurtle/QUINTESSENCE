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
import ipaddress
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from entry_compat import check_entry_compatibility, required_actions

from quintessence_pcs.domains.cognition import BeliefCurrentQuery
from quintessence_pcs.collaboration.facade import FACADE_PERMISSIONS
from quintessence_pcs.conversation.persona import (
    PersonaExpectation,
    Speaker,
    parse_expectation,
    parse_speaker,
)
from quintessence_pcs.conversation.sdrpe import (
    SDRPE_MODES,
    SDRPE_POLICY_VERSION,
)
from quintessence_pcs.foundation import identifiers
from quintessence_pcs.foundation.errors import InvalidInput
from quintessence_pcs.runtime import ModelManifest
from quintessence_pcs.runtime.budget import SAFETY_MARGIN_UNITS

APP_NAME = "quintessence-desktop"
APP_VERSION = "0.1.0"
PROTOCOL_VERSION = "desktop.v1"
CONFIG_SCHEMA = "desktop.host.v1"
INSTANCES_SCHEMA = "pcs.instances.v1"
INSTANCES_FILE = "instances.json"
DESTINATION_SCHEMA = "desktop.destination.v1"

MAX_QUESTION_CHARS = 8000
MAX_CONFIG_BYTES = 256 * 1024
RETENTION_VALUES = ("session", "standard", "durable")
TRANSPORT_KINDS = ("http", "scripted")
PROFILES = ("lab", "production")
MODEL_PROTOCOLS = ("collab.v1", "collab.v2")
DEFAULT_MODEL_PROTOCOL = "collab.v1"
#: Backwards-compatible alias (only ``_parse_destination`` reads it).
MODEL_PROTOCOL = DEFAULT_MODEL_PROTOCOL
REVIEW_PURPOSE = "review"
MEMORY_BRIDGE_PERMISSION = "memory_bridge"

#: J11: the smallest per-call output budget that leaves room for an answer when
#: ``thinking`` is enabled.  Measured on the managed model: a structured
#: proposal used 490 answer tokens and 438 reasoning tokens at a 8192 cap, while
#: a 2048 cap was consumed entirely by reasoning and produced ``length``.
MIN_THINKING_OUTPUT_TOKENS = 8192
MAINTENANCE_PERMISSION = "maintenance"
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
class MemoryBridgeSpec:
    """Optional J04 capture bridge; disabled unless the host opts in."""

    enabled: bool
    python_executable: str | None = None
    entry: str | None = None
    package_path: str | None = None
    data_directory: Path | None = None
    test_mode: bool = True
    agent_id: str | None = None
    installation_id: str | None = None
    scope_id: str | None = None
    session_id: str | None = None
    capture_timeout_seconds: int = 10
    batch_timeout_seconds: int = 60
    lease_seconds: int = 60
    max_attempts: int = 5
    retry_backoff_seconds: int = 30
    max_batch: int = 8
    permissions: frozenset[str] = frozenset()
    #: J11: one-shot adoption of a copied instance's recorded directory.
    #: ``None`` means "never adopt", so a mismatch fails at startup instead of
    #: surfacing as ``IDENTITY_UNBOUND`` at the first recall.
    adopt_by: str | None = None
    adopt_reason: str | None = None


@dataclass(frozen=True)
class MemoryRecallSpec:
    """RG08 R-B: literal automatic recall bounds; off unless opted in."""

    enabled: bool = False
    max_items: int = 4
    budget_units: int = 2048
    timeout_seconds: float = 2.0
    present: bool = False


@dataclass(frozen=True)
class MemoryTimeSpec:
    """RG08 R-C1: host rollback switch for time parsing.

    This is not a daily user preference and has no settings-page control; it is
    reported in ``app.info`` so an operator can disable time parsing without
    rolling back code.
    """

    enabled: bool = True
    present: bool = False


@dataclass(frozen=True)
class MemorySpec:
    """RG08: memory-wide settings shared by recall and time retrieval."""

    timezone: str = "Asia/Shanghai"
    timezone_present: bool = False
    recall: MemoryRecallSpec = field(default_factory=MemoryRecallSpec)
    time: MemoryTimeSpec = field(default_factory=MemoryTimeSpec)
    present: bool = False


@dataclass(frozen=True)
class MaintenanceSpec:
    """Optional bounded-maintenance entry; disabled unless the host opts in."""

    enabled: bool
    permissions: frozenset[str] = frozenset()
    max_events: int = 1
    slice_seconds: float = 90.0
    model_timeout_seconds: float = 30.0


@dataclass(frozen=True)
class CognitionSpec:
    """RG05: whether a chat turn may form candidates, and under what bounds.

    ``propose`` is the only model-facing write surface; the working set is a
    read.  ``auto_authorization_id`` names an existing J06 commit
    authorization; the host validates it at startup and the model never sees it.
    """

    enabled: bool = False
    propose: bool = False
    auto_authorization_id: str | None = None
    working_set_limit: int = 5
    pending_limit: int = 3


@dataclass(frozen=True)
class SdrpeSpec:
    """RG07: the configured expression mode.

    ``auto`` is a reserved config position: the parser accepts it so the setting
    can be reported as "unavailable", but every auto turn records a real
    ``skipped(auto_not_implemented)`` stage instead of a trigger policy.
    """

    mode: str = "off"
    policy_version: str = SDRPE_POLICY_VERSION


@dataclass(frozen=True)
class WebSpec:
    """RG08: the opt-in web-tool surface.

    The block is inert unless ``enabled`` is true; the host never reaches the
    network on its own.  ``allowed_hosts`` narrows ``web.fetch`` to named hosts
    (an empty tuple means "any public https host", still refusing IP literals,
    local/special-use names, userinfo and non-default ports).

    The managed settings writer preserves an already-present ``web`` block on
    rewrite, so a production instance can opt in by adding the block once.
    """

    enabled: bool = False
    search: bool = True
    fetch: bool = True
    timeout_seconds: int = 20
    allowed_hosts: tuple[str, ...] = ()
    capture_default: str = "metadata"
    #: The environment variable the credential is read from; never the key.
    api_key_env: str = "DEEPSEEK_API_KEY"
    #: Whether the host configuration actually carried a ``web`` block.  A
    #: missing block and an explicit ``enabled=false`` are both "off", but the
    #: settings page must not call the first one "disabled by you".
    present: bool = False


@dataclass(frozen=True)
class HostConfig:
    """One validated private host configuration."""

    path: Path
    profile: str
    label: str
    ai_display_name: str | None
    instance_root: Path
    database_path: Path
    request_budget_units: int
    output_reserve_units: int
    authority: HostAuthority
    query_plan: BeliefCurrentQuery | None
    max_question_chars: int
    destination: DestinationDescriptor
    model: ModelSpec
    lab: LabSeed | None
    managed: bool = False
    provision: bool = False
    memory_bridge: MemoryBridgeSpec | None = None
    memory: MemorySpec = MemorySpec()
    maintenance: MaintenanceSpec | None = None
    deletion_ledger_dir: Path | None = None
    deletion_anchor_dir: Path | None = None
    deletion_adopt: dict[str, str] | None = None
    ps06_unprotected: dict[str, str] | None = None
    pcs_context_max_bytes: int = 65536
    pcs_belief_coverage: bool = True
    history_max_turns: int = 8
    history_max_bytes: int = 8192
    max_model_requests_per_turn: int = 8
    max_tool_calls_per_turn: int = 6
    turn_wall_seconds: int = 180
    tool_wall_seconds: int = 30
    commentary_max_per_turn: int = 2
    commentary_max_chars: int = 60
    tool_result_excerpt_bytes: int = 1536
    tool_results_total_bytes: int = 6144
    tools_enabled: bool = True
    persona_expected: PersonaExpectation | None = None
    speaker: Speaker | None = None
    persona_control_publish: bool = False
    persona_control_activate: bool = False
    persona_examples_max_items: int = 2
    persona_examples_max_bytes: int = 2048
    cognition: CognitionSpec = CognitionSpec()
    sdrpe: SdrpeSpec = SdrpeSpec()
    web: WebSpec = WebSpec()

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


def _parse(document: Mapping[str, Any], path: Path, *, bind_instance: bool = True) -> HostConfig:
    _exact(
        document,
        required={"schema", "app", "store", "host", "consult", "destination", "budget"},
        optional={
            "lab",
            "review",
            "desktop",
            "memory_bridge",
            "memory",
            "maintenance",
            "conversation",
            "cognition",
            "web",
        },
        where="configuration",
    )
    if document["schema"] != CONFIG_SCHEMA:
        raise ConfigError("CONFIG_INVALID", "host configuration schema is unsupported")

    app = _object(
        document["app"],
        "app",
        {"profile"},
        {"label", "ai_display_name", "persona_expected", "speaker", "persona_control"},
    )
    profile = _choice(app["profile"], "app.profile", PROFILES)
    label = _text(app.get("label", "QUINTESSENCE"), "app.label", 120)
    ai_display_name = None
    if "ai_display_name" in app and app["ai_display_name"] is not None:
        ai_display_name = _text(
            app["ai_display_name"], "app.ai_display_name", 40
        )
    persona_expected = _persona_expected_block(app.get("persona_expected"))
    speaker = _persona_speaker_block(app.get("speaker"))
    persona_control_publish, persona_control_activate = _persona_control_block(
        app.get("persona_control")
    )
    if persona_expected is not None and speaker is None:
        raise ConfigError(
            "CONFIG_INVALID",
            "app.persona_expected requires app.speaker "
            "(use identity 'unknown' when the speaker is not identified)",
        )

    store = _object(
        document["store"],
        "store",
        {"root"},
        {"database", "deletion_ledger_dir", "deletion_anchor_dir", "adoption",
         "unprotected"},
    )
    instance_root = _instance_root(
        _expand_env(_text(store["root"], "store.root", 4096), "store.root")
    )
    if "database" in store:
        database = _resolve_path(
            _expand_env(
                _text(store["database"], "store.database", 4096), "store.database"
            ),
            instance_root,
            "store.database",
        )
    else:
        database = instance_root / "pcs.sqlite"
    if database.name in ("", ".", ".."):
        raise ConfigError("CONFIG_INVALID", "store.database must name a file")
    if not database.is_relative_to(instance_root):
        raise ConfigError(
            "CONFIG_INVALID", "store.database must live inside store.root"
        )
    # Keep the default safety assets with the instance. This protects against
    # partial restores, but cannot detect a rollback of the entire instance.
    # Explicit external directories remain supported for independent backups.
    deletion_ledger_dir = instance_root / "ledger"
    if "deletion_ledger_dir" in store:
        value = store["deletion_ledger_dir"]
        if value is False:
            # Explicit "no ledger": only meaningful together with
            # ``store.unprotected``, which the store enforces on its side.
            deletion_ledger_dir = None
        else:
            deletion_ledger_dir = _resolve_path(
                _expand_env(
                    _text(value, "store.deletion_ledger_dir", 4096),
                    "store.deletion_ledger_dir",
                ),
                instance_root,
                "store.deletion_ledger_dir",
            )
    deletion_anchor_dir = instance_root / "anchor"
    if "deletion_anchor_dir" in store:
        deletion_anchor_dir = _resolve_path(
            _expand_env(
                _text(store["deletion_anchor_dir"], "store.deletion_anchor_dir", 4096),
                "store.deletion_anchor_dir",
            ),
            instance_root,
            "store.deletion_anchor_dir",
        )
    request_budget_units, output_reserve_units = _budget(document["budget"])

    # J10 W1: adopting an existing instance is an explicit, audited action.  It
    # is off unless the host names both the operator and the reason.
    deletion_adopt = None
    if "adoption" in store:
        adoption = _object(
            store["adoption"], "store.adoption", {"by", "reason"}, set()
        )
        deletion_adopt = {
            "by": _text(adoption["by"], "store.adoption.by", 128),
            "reason": _text(adoption["reason"], "store.adoption.reason", 512),
        }

    # Running without a deletion ledger is a deliberate experiment, not a
    # fallback: it needs the same operator+reason pair, and the store refuses
    # every deletion while it is on.
    ps06_unprotected = None
    if "unprotected" in store:
        unprotected = _object(
            store["unprotected"], "store.unprotected", {"by", "reason"}, set()
        )
        ps06_unprotected = {
            "by": _text(unprotected["by"], "store.unprotected.by", 128),
            "reason": _text(unprotected["reason"], "store.unprotected.reason", 512),
        }


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

    memory_bridge = _memory_bridge(
        document.get("memory_bridge"), instance_root, purposes
    )
    memory = _memory(document.get("memory"))
    maintenance = _maintenance(document.get("maintenance"))

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

    conversation = _object(
        document.get("conversation", {}),
        "conversation",
        set(),
        {
            "history_max_turns",
            "history_max_bytes",
            "max_model_requests_per_turn",
            "max_tool_calls_per_turn",
            "turn_wall_seconds",
            "tool_wall_seconds",
            "commentary_max_per_turn",
            "commentary_max_chars",
            "tool_result_excerpt_bytes",
            "tool_results_total_bytes",
            "tools_enabled",
            "persona_examples_max_items",
            "persona_examples_max_bytes",
            "sdrpe",
        },
    )
    context_max_bytes = int(document["budget"].get("pcs_context_max_bytes", 65536))
    history_max_turns = _integer(
        conversation.get("history_max_turns", 8),
        "conversation.history_max_turns",
        1,
        64,
    )
    history_max_bytes = _integer(
        conversation.get("history_max_bytes", 8192),
        "conversation.history_max_bytes",
        1024,
        max(1024, context_max_bytes - 2048),
    )
    if history_max_bytes > context_max_bytes - 2048:
        raise ConfigError(
            "CONFIG_INVALID",
            "conversation.history_max_bytes must leave room inside "
            "budget.pcs_context_max_bytes",
        )
    max_model_requests = _integer(
        conversation.get("max_model_requests_per_turn", 8),
        "conversation.max_model_requests_per_turn",
        1,
        16,
    )
    max_tool_calls = _integer(
        conversation.get("max_tool_calls_per_turn", 6),
        "conversation.max_tool_calls_per_turn",
        0,
        6,
    )
    turn_wall = _integer(
        conversation.get("turn_wall_seconds", 180),
        "conversation.turn_wall_seconds",
        30,
        600,
    )
    tool_wall = _integer(
        conversation.get("tool_wall_seconds", 30),
        "conversation.tool_wall_seconds",
        5,
        120,
    )
    commentary_count = _integer(
        conversation.get("commentary_max_per_turn", 2),
        "conversation.commentary_max_per_turn",
        0,
        4,
    )
    commentary_chars = _integer(
        conversation.get("commentary_max_chars", 60),
        "conversation.commentary_max_chars",
        1,
        200,
    )
    excerpt_bytes = _integer(
        conversation.get("tool_result_excerpt_bytes", 1536),
        "conversation.tool_result_excerpt_bytes",
        256,
        8192,
    )
    total_bytes = _integer(
        conversation.get("tool_results_total_bytes", 6144),
        "conversation.tool_results_total_bytes",
        1024,
        16384,
    )
    if excerpt_bytes > total_bytes:
        raise ConfigError(
            "CONFIG_INVALID",
            "conversation.tool_result_excerpt_bytes must not exceed "
            "conversation.tool_results_total_bytes",
        )
    if total_bytes > context_max_bytes - 2048:
        raise ConfigError(
            "CONFIG_INVALID",
            "conversation.tool_results_total_bytes must leave room inside "
            "budget.pcs_context_max_bytes",
        )
    tools_enabled = conversation.get("tools_enabled", True)
    if type(tools_enabled) is not bool:
        raise ConfigError(
            "CONFIG_INVALID", "conversation.tools_enabled must be a boolean"
        )
    persona_examples_max_items = _integer(
        conversation.get("persona_examples_max_items", 2),
        "conversation.persona_examples_max_items",
        0,
        4,
    )
    persona_examples_max_bytes = _integer(
        conversation.get("persona_examples_max_bytes", 2048),
        "conversation.persona_examples_max_bytes",
        0,
        8192,
    )
    if persona_examples_max_bytes > context_max_bytes - 2048:
        raise ConfigError(
            "CONFIG_INVALID",
            "conversation.persona_examples_max_bytes must leave room inside "
            "budget.pcs_context_max_bytes",
        )

    cognition = _cognition(
        document.get("cognition"), query_plan, context_max_bytes
    )
    sdrpe = _sdrpe(conversation.get("sdrpe"))
    web = _web(document.get("web"), tool_wall)

    if bind_instance:
        _bind_instance(path, namespace, instance_root)

    return HostConfig(
        path=path,
        profile=profile,
        label=label,
        ai_display_name=ai_display_name,
        instance_root=instance_root,
        database_path=database,
        request_budget_units=request_budget_units,
        output_reserve_units=output_reserve_units,
        authority=authority,
        query_plan=query_plan,
        max_question_chars=max_question,
        destination=destination,
        model=model,
        lab=lab,
        managed=managed,
        provision=provision,
        deletion_ledger_dir=deletion_ledger_dir,
        deletion_anchor_dir=deletion_anchor_dir,
        deletion_adopt=deletion_adopt,
        ps06_unprotected=ps06_unprotected,
        memory_bridge=memory_bridge,
        memory=memory,
        maintenance=maintenance,
        pcs_context_max_bytes=document["budget"].get("pcs_context_max_bytes", 65536),
        pcs_belief_coverage=document["budget"].get("pcs_belief_coverage", True),
        history_max_turns=history_max_turns,
        history_max_bytes=history_max_bytes,
        max_model_requests_per_turn=max_model_requests,
        max_tool_calls_per_turn=max_tool_calls,
        turn_wall_seconds=turn_wall,
        tool_wall_seconds=tool_wall,
        commentary_max_per_turn=commentary_count,
        commentary_max_chars=commentary_chars,
        tool_result_excerpt_bytes=excerpt_bytes,
        tool_results_total_bytes=total_bytes,
        tools_enabled=tools_enabled,
        persona_expected=persona_expected,
        speaker=speaker,
        persona_control_publish=persona_control_publish,
        persona_control_activate=persona_control_activate,
        persona_examples_max_items=persona_examples_max_items,
        persona_examples_max_bytes=persona_examples_max_bytes,
        cognition=cognition,
        sdrpe=sdrpe,
        web=web,
    )


def _persona_expected_block(value: Any) -> PersonaExpectation | None:
    """Parse ``app.persona_expected``; shape rules live in the persona module."""

    try:
        return parse_expectation(value)
    except InvalidInput as exc:
        raise ConfigError("CONFIG_INVALID", str(exc)) from exc


def _persona_speaker_block(value: Any) -> Speaker | None:
    try:
        return parse_speaker(value)
    except InvalidInput as exc:
        raise ConfigError("CONFIG_INVALID", str(exc)) from exc


def _persona_control_block(value: Any) -> tuple[bool, bool]:
    """Closed ``{publish, activate}`` capability block; both default to false."""

    if value is None:
        return False, False
    block = _object(value, "app.persona_control", set(), {"publish", "activate"})
    publish = block.get("publish", False)
    activate = block.get("activate", False)
    if type(publish) is not bool or type(activate) is not bool:
        raise ConfigError(
            "CONFIG_INVALID",
            "app.persona_control.publish/activate must be booleans",
        )
    return publish, activate


def _memory_timezone(value: Any) -> str:
    name = _text(value, "memory.timezone", 64)
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
        return name
    except Exception:  # noqa: BLE001 - Windows may lack the IANA database
        if name in ("Asia/Shanghai", "UTC"):
            return name
        raise ConfigError(
            "CONFIG_INVALID", "memory.timezone is not a resolvable time zone"
        ) from None


def _memory_recall(value: Any) -> MemoryRecallSpec:
    if value is None:
        return MemoryRecallSpec()
    spec = _object(
        value,
        "memory.recall",
        set(),
        {"enabled", "max_items", "budget_units", "timeout_seconds"},
    )
    timeout = spec.get("timeout_seconds", 2.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ConfigError("CONFIG_INVALID", "memory.recall.timeout_seconds must be a number")
    timeout = float(timeout)
    if not 0.25 <= timeout <= 2.0:
        raise ConfigError(
            "CONFIG_INVALID",
            "memory.recall.timeout_seconds must be between 0.25 and 2.0",
        )
    return MemoryRecallSpec(
        enabled=_boolean(
            spec.get("enabled", False), "memory.recall.enabled"
        ),
        max_items=_integer(
            spec.get("max_items", 4), "memory.recall.max_items", 1, 4
        ),
        budget_units=_integer(
            spec.get("budget_units", 2048),
            "memory.recall.budget_units",
            64,
            2048,
        ),
        timeout_seconds=timeout,
        present=True,
    )


def _memory_time(value: Any) -> MemoryTimeSpec:
    """The host rollback switch; absent means enabled (no user preference)."""

    if value is None:
        return MemoryTimeSpec()
    spec = _object(value, "memory.time", set(), {"enabled"})
    return MemoryTimeSpec(
        enabled=_boolean(spec.get("enabled", True), "memory.time.enabled"),
        present=True,
    )


def _memory(value: Any) -> MemorySpec:
    if value is None:
        return MemorySpec()
    spec = _object(value, "memory", set(), {"timezone", "recall", "time"})
    timezone = "Asia/Shanghai"
    timezone_present = False
    if "timezone" in spec:
        timezone = _memory_timezone(spec["timezone"])
        timezone_present = True
    return MemorySpec(
        timezone=timezone,
        timezone_present=timezone_present,
        recall=_memory_recall(spec.get("recall")),
        time=_memory_time(spec.get("time")),
        present=True,
    )


def _maintenance(value: Any) -> MaintenanceSpec | None:
    """Parse the optional bounded-maintenance block.

    The entry is off unless the host enables it *and* grants the
    ``maintenance`` permission; a half-configured block is a configuration
    error rather than a silent default, because a maintenance entry that runs
    without an explicit opt-in would be exactly the "it just appeared" surprise
    the J08 handover warned about.
    """

    if value is None:
        return None
    spec = _object(
        value,
        "maintenance",
        {"enabled"},
        {"permissions", "max_events", "slice_seconds", "model_timeout_seconds"},
    )
    enabled = _boolean(spec["enabled"], "maintenance.enabled")
    if not enabled:
        return MaintenanceSpec(enabled=False)
    permissions = _string_set(
        spec.get("permissions", []),
        "maintenance.permissions",
        (MAINTENANCE_PERMISSION,),
    )
    if MAINTENANCE_PERMISSION not in permissions:
        raise ConfigError(
            "CONFIG_INVALID",
            "maintenance.permissions must contain maintenance",
        )
    return MaintenanceSpec(
        enabled=True,
        permissions=permissions,
        max_events=_integer(spec.get("max_events", 1), "maintenance.max_events", 1, 100),
        slice_seconds=_seconds(
            spec.get("slice_seconds", 90.0), "maintenance.slice_seconds"
        ),
        model_timeout_seconds=_seconds(
            spec.get("model_timeout_seconds", 30.0),
            "maintenance.model_timeout_seconds",
        ),
    )


def _memory_bridge(
    value: Any, instance_root: Path, purposes: frozenset[str]
) -> MemoryBridgeSpec | None:
    if value is None:
        return None
    spec = _object(
        value,
        "memory_bridge",
        {"enabled"},
        {
            "python_executable",
            "entry",
            "package_path",
            "data_directory",
            "agent_id",
            "installation_id",
            "scope_id",
            "session_id",
            "capture_timeout_seconds",
            "batch_timeout_seconds",
            "lease_seconds",
            "max_attempts",
            "retry_backoff_seconds",
            "max_batch",
            "permissions",
            "adopt_data_directory",
            "test_mode",
        },
    )
    enabled = _boolean(spec["enabled"], "memory_bridge.enabled")
    if not enabled:
        return MemoryBridgeSpec(enabled=False)
    if "memory.bridge" not in purposes:
        raise ConfigError(
            "CONFIG_INVALID",
            "host.purposes must contain memory.bridge when the bridge is enabled",
        )
    for field in (
        "python_executable",
        "entry",
        "package_path",
        "data_directory",
        "agent_id",
        "installation_id",
        "scope_id",
        "session_id",
    ):
        if field not in spec:
            raise ConfigError(
                "CONFIG_INVALID", f"memory_bridge.{field} is required"
            )
    python_executable = _text(
        spec["python_executable"], "memory_bridge.python_executable", 4096
    )
    entry = _text(spec["entry"], "memory_bridge.entry", 4096)
    package_path = _text(spec["package_path"], "memory_bridge.package_path", 4096)
    for value_text, field in (
        (python_executable, "memory_bridge.python_executable"),
        (entry, "memory_bridge.entry"),
        (package_path, "memory_bridge.package_path"),
    ):
        if field == "memory_bridge.entry" and value_text == "builtin:scope-recall":
            continue
        if not Path(value_text).is_absolute():
            raise ConfigError("CONFIG_INVALID", f"{field} must be an absolute path")
    data_directory = _resolve_path(
        _text(spec["data_directory"], "memory_bridge.data_directory", 4096),
        instance_root,
        "memory_bridge.data_directory",
    )
    if not data_directory.is_relative_to(instance_root):
        raise ConfigError(
            "CONFIG_INVALID",
            "memory_bridge.data_directory must live inside store.root",
        )
    permissions = _string_set(
        spec.get("permissions", []),
        "memory_bridge.permissions",
        (MEMORY_BRIDGE_PERMISSION,),
    )
    if MEMORY_BRIDGE_PERMISSION not in permissions:
        raise ConfigError(
            "CONFIG_INVALID",
            "memory_bridge.permissions must contain memory_bridge",
        )
    return MemoryBridgeSpec(
        enabled=True,
        python_executable=python_executable,
        entry=entry,
        package_path=package_path,
        data_directory=data_directory,
        test_mode=_boolean(spec.get("test_mode", True), "memory_bridge.test_mode"),
        agent_id=_text(spec["agent_id"], "memory_bridge.agent_id", 240),
        installation_id=_text(
            spec["installation_id"], "memory_bridge.installation_id", 240
        ),
        scope_id=_text(spec["scope_id"], "memory_bridge.scope_id", 240),
        session_id=_text(spec["session_id"], "memory_bridge.session_id", 240),
        capture_timeout_seconds=_integer(
            spec.get("capture_timeout_seconds", 10),
            "memory_bridge.capture_timeout_seconds",
            1,
            300,
        ),
        batch_timeout_seconds=_integer(
            spec.get("batch_timeout_seconds", 60),
            "memory_bridge.batch_timeout_seconds",
            1,
            3600,
        ),
        lease_seconds=_integer(
            spec.get("lease_seconds", 60), "memory_bridge.lease_seconds", 1, 3600
        ),
        max_attempts=_integer(
            spec.get("max_attempts", 5), "memory_bridge.max_attempts", 1, 20
        ),
        retry_backoff_seconds=_integer(
            spec.get("retry_backoff_seconds", 30),
            "memory_bridge.retry_backoff_seconds",
            1,
            3600,
        ),
        max_batch=_integer(
            spec.get("max_batch", 8), "memory_bridge.max_batch", 1, 100
        ),
        permissions=permissions,
        adopt_by=_adoption_field(spec.get("adopt_data_directory"), "by"),
        adopt_reason=_adoption_field(spec.get("adopt_data_directory"), "reason"),
    )


def _adoption_field(value: Any, field: str) -> str | None:
    """Read one field of the optional, explicitly attributed adoption block.

    The block exists only to repair a copied instance whose memory store still
    names the old directory.  It must be attributable, so both ``by`` and
    ``reason`` are required whenever the block is present — a nameless adoption
    is exactly the kind of silent repair this design refuses.
    """

    if value is None:
        return None
    block = _object(value, "memory_bridge.adopt_data_directory", {"by", "reason"}, set())
    text = _text(block[field], f"memory_bridge.adopt_data_directory.{field}", 400)
    if not text.strip():
        raise ConfigError(
            "CONFIG_INVALID",
            f"memory_bridge.adopt_data_directory.{field} must not be blank",
        )
    return text


def _instance_root(value: str) -> Path:
    """Validate the trusted host's instance root as an explicit absolute path."""

    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigError(
            "CONFIG_INVALID", "store.root must be an absolute path"
        )
    if not path.exists() or not path.is_dir():
        raise ConfigError("CONFIG_INVALID", "store.root must be an existing directory")
    if not os.access(path, os.W_OK):
        raise ConfigError("CONFIG_INVALID", "store.root must be writable")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ConfigError("CONFIG_INVALID", "store.root cannot be resolved") from exc
    if os.path.normcase(str(resolved)) != os.path.normcase(str(path)):
        raise ConfigError(
            "CONFIG_INVALID", "store.root must not be reached through a link"
        )
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or _is_junction(candidate):
            raise ConfigError(
                "CONFIG_INVALID", "store.root must not have a linked ancestor"
            )
    return path


def _is_junction(path: Path) -> bool:
    predicate = getattr(path, "is_junction", None)
    if predicate is None:
        return False
    try:
        return bool(predicate())
    except OSError:
        return True


def _budget(value: Any) -> tuple[int, int]:
    budget = _object(
        value,
        "budget",
        {"request_budget_units", "output_reserve_units"},
        {"pcs_context_max_bytes", "pcs_belief_coverage"},
    )
    _integer(budget.get("pcs_context_max_bytes", 65536), "budget.pcs_context_max_bytes", 1, 9007199254740991)
    if budget.get("pcs_belief_coverage", True) is not True:
        raise ConfigError("CONFIG_INVALID", "budget.pcs_belief_coverage cannot be disabled")
    request_units = _integer(
        budget["request_budget_units"], "budget.request_budget_units", 1, 10_000_000
    )
    reserve_units = _integer(
        budget["output_reserve_units"], "budget.output_reserve_units", 0, 10_000_000
    )
    if request_units <= reserve_units + SAFETY_MARGIN_UNITS:
        raise ConfigError(
            "CONFIG_INVALID",
            "budget.request_budget_units must exceed output_reserve_units plus "
            "the safety margin",
        )
    return request_units, reserve_units


def _bind_instance(config_path: Path, namespace: str, root: Path) -> None:
    """Bind one namespace to the canonical instance root, once.

    The registry lives next to the private host config.  A namespace already
    bound to a different root is a configuration error, never a silent
    overwrite; a corrupt registry is refused and left untouched.
    """

    registry_path = config_path.parent / INSTANCES_FILE
    lock_path = config_path.parent / ".instances.lock"
    with _registry_lock(lock_path):
        bindings = _load_registry(registry_path)
        normalized_root = _normalize_root(str(root))
        for other_namespace, other_binding in bindings.items():
            if other_namespace == namespace:
                continue
            if _normalize_root(other_binding["root"]) == normalized_root:
                raise ConfigError(
                    "CONFIG_INVALID",
                    "this instance root is already bound to another namespace",
                )
        existing = bindings.get(namespace)
        if existing is not None:
            if _normalize_root(existing["root"]) != normalized_root:
                raise ConfigError(
                    "CONFIG_INVALID",
                    "this namespace is already bound to a different instance root",
                )
            return
        bindings[namespace] = {
            "root": str(root),
            "bound_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_registry(registry_path, bindings)


def _normalize_root(value: str) -> str:
    return os.path.normcase(os.path.normpath(value))


def _validate_binding(namespace: Any, binding: Any) -> None:
    if not isinstance(namespace, str) or not namespace or "\x00" in namespace:
        raise ConfigError("CONFIG_INVALID", "instances.json has an invalid namespace")
    if not isinstance(binding, Mapping):
        raise ConfigError("CONFIG_INVALID", "instances.json has an invalid binding")
    if set(binding) != {"root", "bound_at"}:
        raise ConfigError(
            "CONFIG_INVALID", "instances.json has an invalid binding shape"
        )
    root = binding["root"]
    bound_at = binding["bound_at"]
    if not isinstance(root, str) or not root or "\x00" in root:
        raise ConfigError("CONFIG_INVALID", "instances.json has an invalid root")
    if not Path(root).is_absolute():
        raise ConfigError("CONFIG_INVALID", "instances.json root must be absolute")
    if not isinstance(bound_at, str) or not bound_at:
        raise ConfigError("CONFIG_INVALID", "instances.json has an invalid bound_at")


def _load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            "CONFIG_INVALID", "instances.json is not readable"
        ) from exc
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ConfigError("CONFIG_INVALID", "instances.json is not JSON") from exc
    if (
        not isinstance(document, dict)
        or document.get("schema") != INSTANCES_SCHEMA
        or not isinstance(document.get("bindings"), dict)
    ):
        raise ConfigError("CONFIG_INVALID", "instances.json is invalid")
    bindings: dict[str, Any] = {}
    roots: dict[str, str] = {}
    for namespace, binding in document["bindings"].items():
        _validate_binding(namespace, binding)
        normalized = _normalize_root(binding["root"])
        other = roots.get(normalized)
        if other is not None and other != namespace:
            raise ConfigError(
                "CONFIG_INVALID",
                "instances.json binds one instance root to multiple namespaces",
            )
        roots[normalized] = namespace
        bindings[namespace] = dict(binding)
    return bindings


def _write_registry(path: Path, bindings: Mapping[str, Any]) -> None:
    document = {"schema": INSTANCES_SCHEMA, "bindings": dict(bindings)}
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=".instances-", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                document,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise ConfigError(
            "CONFIG_INVALID", "instances.json could not be written"
        ) from exc


@contextmanager
def _registry_lock(path: Path):
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _parse_destination(value: Any, base: Path) -> ModelSpec:
    destination = _object(        value,
        "destination",
        {"provider", "model", "model_protocol", "external"},
        {"manifest", "manifest_path", "transport"},
    )
    provider = _text(destination["provider"], "destination.provider", 128)
    model = _text(destination["model"], "destination.model", 128)
    protocol = _text(
        destination["model_protocol"], "destination.model_protocol", 64
    )
    if protocol not in MODEL_PROTOCOLS:
        raise ConfigError(
            "CONFIG_INVALID",
            "destination.model_protocol must be one of "
            + ", ".join(repr(item) for item in MODEL_PROTOCOLS),
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
    # J11: with ``thinking`` enabled the reasoning tokens come out of the same
    # per-call output budget.  A small cap leaves zero tokens for the answer, and
    # a structured proposal then fails as ``finish_reason=length`` with
    # ``malformed`` — after the call was paid for.  Refuse the configuration
    # instead of discovering it at the first proposal.
    if manifest.thinking == "enabled" and (
        manifest.max_output_tokens_per_call < MIN_THINKING_OUTPUT_TOKENS
    ):
        raise ConfigError(
            "CONFIG_INVALID",
            "destination.manifest.max_output_tokens_per_call must be at least "
            f"{MIN_THINKING_OUTPUT_TOKENS} when thinking is enabled "
            f"(got {manifest.max_output_tokens_per_call}); reasoning tokens are "
            "deducted from the same budget and a smaller cap can leave no room "
            "for the answer",
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
            # The scripted transport is a host fixture: accept both protocol
            # families here and let the runtime parser reject a mismatch.
            if action not in (
                "none",
                "query",
                "wait",
                "propose",
                "final",
                "call_tool",
            ):
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



def _seconds(value: Any, field: str) -> float:
    """A bounded wall-clock allowance in seconds (1..300)."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("CONFIG_INVALID", f"{field} must be a number")
    number = float(value)
    if not 1.0 <= number <= 300.0:
        raise ConfigError("CONFIG_INVALID", f"{field} must be between 1 and 300")
    return number

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


def _cognition(
    value: Any, query_plan: BeliefCurrentQuery | None, context_max_bytes: int
) -> CognitionSpec:
    """Parse the RG05 ``cognition`` block; fail closed on an unusable grant."""

    if value is None:
        return CognitionSpec()
    block = _object(
        value,
        "cognition",
        set(),
        {
            "enabled",
            "propose",
            "auto_authorization_id",
            "working_set_limit",
            "pending_limit",
        },
    )
    enabled = block.get("enabled", True)
    if type(enabled) is not bool:
        raise ConfigError("CONFIG_INVALID", "cognition.enabled must be a boolean")
    propose = block.get("propose", False)
    if propose and not enabled:
        raise ConfigError(
            "CONFIG_INVALID", "cognition.propose requires cognition.enabled=true"
        )
    if type(propose) is not bool:
        raise ConfigError("CONFIG_INVALID", "cognition.propose must be a boolean")
    raw_authorization = block.get("auto_authorization_id")
    if raw_authorization is not None:
        if not isinstance(raw_authorization, str) or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            raw_authorization,
        ):
            raise ConfigError(
                "CONFIG_INVALID",
                "cognition.auto_authorization_id must be a lower-case UUID",
            )
        if not propose:
            raise ConfigError(
                "CONFIG_INVALID",
                "cognition.auto_authorization_id requires cognition.propose=true",
            )
    working_set_limit = _integer(
        block.get("working_set_limit", 5), "cognition.working_set_limit", 1, 8
    )
    pending_limit = _integer(
        block.get("pending_limit", 3), "cognition.pending_limit", 0, 3
    )
    if enabled and context_max_bytes < 65536:
        raise ConfigError(
            "CONFIG_INVALID",
            "cognition requires budget.pcs_context_max_bytes >= 65536 "
            "(24,576 remains the no-cognition baseline)",
        )
    if enabled and (query_plan is None or not query_plan.scope):
        raise ConfigError(
            "CONFIG_INVALID",
            "cognition requires consult.query_plan with a non-empty scope",
        )
    if propose:
        if query_plan is None or not query_plan.scope:
            raise ConfigError(
                "CONFIG_INVALID",
                "cognition.propose requires consult.query_plan with a "
                "non-empty scope (the host stamps every proposal with it)",
            )
    return CognitionSpec(
        enabled=enabled,
        propose=propose,
        auto_authorization_id=raw_authorization,
        working_set_limit=working_set_limit,
        pending_limit=pending_limit,
    )


def _sdrpe(value: Any) -> SdrpeSpec:
    """Parse the RG07 ``conversation.sdrpe`` block; absent means off."""

    if value is None:
        return SdrpeSpec()
    block = _object(value, "conversation.sdrpe", set(), {"mode"})
    mode = block.get("mode", "off")
    if not isinstance(mode, str) or mode not in SDRPE_MODES:
        raise ConfigError(
            "CONFIG_INVALID",
            "conversation.sdrpe.mode must be off, on or auto",
        )
    return SdrpeSpec(mode=mode)


#: Hostname shape accepted in ``web.allowed_hosts``: a public DNS name, with an
#: optional leading dot for a suffix entry.  IP literals and single labels are
#: refused at configuration time as well as at fetch time.
_ALLOWED_HOST_PATTERN = re.compile(
    r"\.?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))+\Z"
)
_ENV_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
WEB_CAPTURE_MODES = ("metadata", "evidence")


def _web(value: Any, tool_wall_seconds: int) -> WebSpec:
    """Parse the RG08 ``web`` block; absent means "no network", not "default on".

    The block is validated even when ``enabled`` is false so a typo fails at
    startup instead of at the first turn that tries to use it.  The managed
    settings writer preserves an already-present block on rewrite, so a
    production instance can opt in through the settings page (or by adding the
    block once) without losing it at the next managed start.
    """

    if value is None:
        return WebSpec()
    block = _object(
        value,
        "web",
        set(),
        {
            "enabled",
            "search",
            "fetch",
            "timeout_seconds",
            "allowed_hosts",
            "capture_default",
            "api_key_env",
        },
    )
    enabled = (
        _boolean(block["enabled"], "web.enabled") if "enabled" in block else False
    )
    search = (
        _boolean(block["search"], "web.search") if "search" in block else True
    )
    fetch = _boolean(block["fetch"], "web.fetch") if "fetch" in block else True
    if enabled and not (search or fetch):
        raise ConfigError(
            "CONFIG_INVALID",
            "web.enabled requires web.search=true or web.fetch=true",
        )
    timeout = _integer(
        block.get("timeout_seconds", 20), "web.timeout_seconds", 5, 120
    )
    if timeout > tool_wall_seconds:
        raise ConfigError(
            "CONFIG_INVALID",
            "web.timeout_seconds must not exceed conversation.tool_wall_seconds",
        )
    raw_hosts = block.get("allowed_hosts", [])
    if not isinstance(raw_hosts, list):
        raise ConfigError("CONFIG_INVALID", "web.allowed_hosts must be a list")
    hosts: list[str] = []
    for index, item in enumerate(raw_hosts):
        host = _text(item, f"web.allowed_hosts[{index}]", 253).strip().lower()
        if not _ALLOWED_HOST_PATTERN.fullmatch(host) or _is_ip_literal(host):
            raise ConfigError(
                "CONFIG_INVALID",
                f"web.allowed_hosts[{index}] must be a public DNS name",
            )
        hosts.append(host)
    if len(set(hosts)) != len(hosts):
        raise ConfigError(
            "CONFIG_INVALID", "web.allowed_hosts must not repeat a value"
        )
    capture = _choice(
        block.get("capture_default", "metadata"),
        "web.capture_default",
        WEB_CAPTURE_MODES,
    )
    key_env = block.get("api_key_env", "DEEPSEEK_API_KEY")
    if not isinstance(key_env, str) or not _ENV_NAME_PATTERN.fullmatch(key_env):
        raise ConfigError(
            "CONFIG_INVALID",
            "web.api_key_env must be an environment variable name, not a key",
        )
    return WebSpec(
        enabled=enabled,
        search=search,
        fetch=fetch,
        timeout_seconds=timeout,
        allowed_hosts=tuple(hosts),
        capture_default=capture,
        api_key_env=key_env,
        present=True,
    )


def web_status(config: HostConfig) -> dict[str, Any]:
    """RG08: the non-secret truth about this host's web surface.

    ``enabled`` is the configured switch; ``configured`` is the narrower claim
    that the surface can actually run now (switch on, credential present, at
    least one tool).  The credential itself is never read into the result: only
    the environment variable name and whether it is non-empty leave this
    function.  ``missing`` names the configuration facts still absent, so a
    settings page can explain what to add instead of showing a green switch.
    """

    spec = config.web
    credential_present = bool(os.environ.get(spec.api_key_env, "").strip())
    if config.managed:
        source = "managed"
    elif config.is_lab:
        source = "lab"
    else:
        source = "external"
    missing: list[str] = []
    if not spec.present:
        missing.append("web")
    elif not spec.enabled:
        missing.append("web.enabled")
    if spec.enabled and not credential_present:
        missing.append("web.api_key_env")
    return {
        "enabled": spec.enabled,
        "configured": bool(
            spec.enabled and credential_present and (spec.search or spec.fetch)
        ),
        "declared": spec.present,
        "search": spec.search,
        "fetch": spec.fetch,
        "credential_present": credential_present,
        # A name, never a value; the bridge never echoes the secret.
        "credential_env": spec.api_key_env,
        "allowed_hosts": len(spec.allowed_hosts),
        "host_policy": "allowlist" if spec.allowed_hosts else "any_public_https",
        "source": source,
        "missing": missing,
    }


def memory_recall_status(
    config: HostConfig,
    *,
    adapter_present: bool = True,
    entry_compat: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """RG08 R-B: the non-secret truth about literal automatic recall.

    ``configured`` requires the switch, a usable memory bridge, a live adapter
    and an entry that declares the actions this configuration asks for.  A
    missing bridge or an older entry is reported as a missing fact, never as a
    startup failure: the ordinary chat continues and automatic recall stays
    off.
    """

    spec = config.memory.recall
    bridge = config.memory_bridge
    bridge_configured = bool(
        bridge is not None
        and bridge.enabled
        and MEMORY_BRIDGE_PERMISSION in bridge.permissions
    )
    if config.managed:
        source = "managed"
    elif config.is_lab:
        source = "lab"
    else:
        source = "external"
    missing: list[str] = []
    if not config.memory.present:
        missing.append("memory")
    elif not spec.present:
        missing.append("memory.recall")
    if spec.present and not spec.enabled:
        missing.append("memory.recall.enabled")
    if not bridge_configured:
        missing.append("memory_bridge")
    if not adapter_present:
        missing.append("adapter")

    entry_block: dict[str, Any] | None = None
    if bridge_configured:
        if entry_compat is None:
            assert bridge is not None
            entry_compat = check_entry_compatibility(
                bridge.entry,
                required_actions(
                    recall_enabled=spec.enabled,
                    time_enabled=config.memory.time.enabled,
                ),
            )
        entry_block = {
            "checked": bool(entry_compat.get("checked")),
            "actions": list(entry_compat.get("actions") or ()),
            "required": list(entry_compat.get("required") or ()),
            "missing": list(entry_compat.get("missing") or ()),
            "compatible": bool(entry_compat.get("compatible", True)),
            "reason": entry_compat.get("reason"),
        }
        for action in entry_block["missing"]:
            missing.append(f"entry.{action}")
    entry_compatible = True if entry_block is None else entry_block["compatible"]

    return {
        "enabled": spec.enabled,
        "configured": bool(
            spec.enabled
            and bridge_configured
            and adapter_present
            and entry_compatible
        ),
        "declared": spec.present,
        "bridge_configured": bridge_configured,
        "entry": entry_block,
        "max_items": spec.max_items,
        "budget_units": spec.budget_units,
        "timeout_seconds": spec.timeout_seconds,
        "timezone": config.memory.timezone,
        "time": {
            "enabled": config.memory.time.enabled,
            "declared": config.memory.time.present,
            "scope": "host_rollback",
            "apply": "next_turn",
        },
        "source": source,
        "missing": missing,
        "apply": "next_turn",
    }


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


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
    "MemoryRecallSpec",
    "MemorySpec",
    "WebSpec",
    "destination_id_for",
    "load_config",
    "memory_recall_status",
    "web_status",
]
