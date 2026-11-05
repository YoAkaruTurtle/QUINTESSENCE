"""RG08: the immutable, per-turn policy bundle.

One turn resolves exactly one bundle at its start; content and expression both
receive that same object.  The bundle carries the texts it will actually send,
so a digest can never disagree with what the model sees.

Sources are strictly separated:

* ``shared_text`` / ``content_text`` / ``host_expression_text`` -- PCS generic
  policy, identical for every persona;
* ``persona`` -- the current persona's expression policy asset (optional).

The persona asset is loaded from a store record and never from source.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domains.persona.policy import (
    PersonaPolicyAsset,
    resolve_persona_policy,
)
from ..foundation.errors import InvalidInput
from ..foundation.jsonutil import sha256_hex
from ..runtime.collab_protocol import SHARED_STYLE_POLICY
from .content_task import CONTENT_TASK_POLICY
from .expression_host_policy import HOST_EXPRESSION_STAGE_POLICY
from .sdrpe import SDRPE_POLICY_VERSION


@dataclass(frozen=True)
class PolicyBundle:
    """Every policy text one turn will send, frozen at turn start."""

    version: str
    shared_text: str
    content_text: str
    host_expression_text: str
    persona: PersonaPolicyAsset | None

    @property
    def expression_text(self) -> str:
        """The exact expression policy text this turn sends.

        A present persona policy is concatenated *before* the generic host
        rules; an absent/invalid one leaves the host rules alone.  The empty
        host case still yields the persona text with no trailing blank line.
        """

        if self.persona is not None and self.persona.state == "present":
            if self.host_expression_text:
                return self.persona.text + "\n\n" + self.host_expression_text
            return self.persona.text
        return self.host_expression_text

    @property
    def shared_sha256(self) -> str:
        return sha256_hex(self.shared_text)

    @property
    def content_sha256(self) -> str:
        return sha256_hex(self.content_text)

    @property
    def host_expression_sha256(self) -> str:
        return sha256_hex(self.host_expression_text)

    @property
    def persona_sha256(self) -> str | None:
        if self.persona is not None and self.persona.state == "present":
            return self.persona.sha256
        return None

    @property
    def expression_sha256(self) -> str:
        return sha256_hex(self.expression_text)

    @property
    def digest(self) -> str:
        """Stable digest over the version and the four text digests."""

        parts = [
            self.version,
            self.shared_sha256,
            self.content_sha256,
            self.host_expression_sha256,
            self.persona_sha256 or "",
        ]
        return sha256_hex("\x1f".join(parts))


def current_bundle() -> PolicyBundle:
    """The generic-only bundle (no persona asset requested)."""

    return PolicyBundle(
        version=SDRPE_POLICY_VERSION,
        shared_text=SHARED_STYLE_POLICY,
        content_text=CONTENT_TASK_POLICY,
        host_expression_text=HOST_EXPRESSION_STAGE_POLICY,
        persona=None,
    )


def resolve_bundle(
    records: Any,
    namespace: str,
    profile_ref: Any,
    *,
    host_is_lab: bool,
    include_persona: bool,
) -> PolicyBundle:
    """Resolve one turn's bundle.

    ``include_persona=False`` (collab.v1, or SDRPE off) never reads the
    expression asset: those paths must not gain a database dependency they do
    not need.
    """

    if not isinstance(include_persona, bool) or not isinstance(host_is_lab, bool):
        raise InvalidInput("include_persona and host_is_lab must be booleans")
    persona: PersonaPolicyAsset | None = None
    if include_persona:
        persona = resolve_persona_policy(
            records, namespace, profile_ref, host_is_lab=host_is_lab
        )
    return PolicyBundle(
        version=SDRPE_POLICY_VERSION,
        shared_text=SHARED_STYLE_POLICY,
        content_text=CONTENT_TASK_POLICY,
        host_expression_text=HOST_EXPRESSION_STAGE_POLICY,
        persona=persona,
    )


__all__ = [
    "PolicyBundle",
    "current_bundle",
    "resolve_bundle",
]
