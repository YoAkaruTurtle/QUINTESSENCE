"""PCS2-009 ModelPort boundary and runtime-local adapter errors.

The protocol is deliberately narrower than a provider SDK: a port receives the
exact rendered Context text, the exact reference list the Context made
available, and an optional format-repair hint.  It returns one JSON text.  It
must not add a hidden system prompt or examples, and it must never parse the
proposal into a domain object itself — ``parse_proposal`` and the PCS2-008
validators remain the authority.

``MODEL_FAILURE`` is not a ``pcs.v2`` error code.  These exceptions are
runtime-local; the step maps every one of them to that label, as the handoff
card requires, and no new public error code is introduced.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable


class ModelAdapterError(Exception):
    """Base class for a model adapter refusing or failing a call.

    Messages are public-safe: no API key, no Authorization header and no raw
    provider response body may appear here.
    """

    error_label = "MODEL_FAILURE"


class ModelConfigurationError(ModelAdapterError):
    """The manifest or the local adapter wiring is not usable."""

    error_label = "CONFIGURATION_ERROR"


class ModelNotAuthorizedError(ModelAdapterError):
    """No active grant covers this provider/model/manifest, or no key is set.

    ``reason`` is an optional machine token (``authorization_expired``,
    ``authorization_mismatch``, ``purpose_not_allowed``, ...).  It only refines
    the diagnosis; it never changes whether the call is refused.
    """

    error_label = "NOT_AUTHORIZED"

    def __init__(self, message: str = "", *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class ModelBudgetExceededError(ModelAdapterError):
    """The call would exceed the authorization's call or wall-time budget.

    ``reason`` distinguishes the three budgets a caller can hit — call count,
    injected Context material, and the whole request — because the recovery
    differs for each (re-authorise vs. shrink the Context vs. shrink the
    request).  It is diagnosis only; the refusal itself is unchanged.
    """

    error_label = "BUDGET_EXCEEDED"

    def __init__(self, message: str = "", *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class ModelTransportError(ModelAdapterError):
    """The network exchange did not produce a usable HTTP 200 response."""

    error_label = "TRANSPORT_ERROR"

    def __init__(self, message: str = "", *, label: str | None = None) -> None:
        super().__init__(message)
        self.error_label = label or type(self).error_label


class ModelResponseError(ModelAdapterError):
    """HTTP 200 arrived, but the response envelope is not a usable proposal."""

    error_label = "RESPONSE_ERROR"


class ModelCancelled(ModelAdapterError):
    """The host abandoned this model wait on purpose.

    This is not a timeout and not a provider rate limit: the local worker was
    asked to yield to the foreground.  The transport may still be running, so a
    cancelled call is recorded as a local cancellation, never as success.
    """

    error_label = "CANCELLED"


@runtime_checkable
class ModelPort(Protocol):
    """The only model surface the cognition runtime knows about."""

    adapter_kind: str
    model_ref: str
    policy_version: str

    def propose(
        self,
        *,
        rendered_input: str,
        references: Sequence[Mapping[str, Any]],
        feedback: str | None = None,
    ) -> str:
        """Return exactly one proposal JSON text (or raise)."""
        ...


@runtime_checkable
class RunBoundModelPort(ModelPort, Protocol):
    """Optional extension used by adapters that need the persisted Context.

    ``propose`` itself keeps the frozen three-argument signature.  The runtime
    binds a Context for the duration of one call so a real adapter can record
    ``context_id`` / ``run_id`` without re-reading or guessing them, then clears
    the binding.  This is not part of the proposal contract handed to a model.
    """

    def preflight(self, *, namespace: str) -> None:
        """Fail before a Context is built / any socket is opened."""
        ...

    def bind_run(self, *, namespace: str, context: Any) -> Any:
        """Return a context manager that binds the persisted Context."""
        ...


__all__ = [
    "ModelAdapterError",
    "ModelBudgetExceededError",
    "ModelCancelled",
    "ModelConfigurationError",
    "ModelNotAuthorizedError",
    "ModelPort",
    "ModelResponseError",
    "ModelTransportError",
    "RunBoundModelPort",
]
