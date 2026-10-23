"""PCS2-008/009 runtime: Context assembly, model ports and one bounded step.

PCS2-008 proved the offline mechanism with FakeModel.  PCS2-009 adds the real
``ModelPort`` boundary, a manifest, a direct HTTP adapter and the persistent
attempt ledger.  The runtime still does one step at a time; it is not a
scheduler.
"""

from __future__ import annotations

from .collab_protocol import (
    COLLAB_ACTIONS,
    COLLAB_MODEL_POLICY_VERSION,
    COLLAB_PROTOCOL_VERSION,
    COLLAB_RENDERER_VERSION,
    MAX_BRIEF_REASON_CHARS,
    MAX_REFERENCES,
    MAX_REPLY_CHARS,
    CollabOutput,
    ConsultSendPolicy,
    build_collab_request_body,
    parse_collab_output,
    render_collab_messages,
    repair_feedback,
)
from .context import (
    CONTEXT_RENDERER,
    CONTEXT_SCHEMA,
    DEFAULT_PERMISSION_VIEW_REF,
    MAX_EPISODES,
    PROPOSAL_CONTRACT,
    ConsultContextEnvelope,
    ConsultQueryMaterial,
    ContextEnvelope,
    ContextService,
    ReadSetEntry,
)
from .fake_model import (
    FAKE_MODEL_ADAPTER_KIND,
    FAKE_MODEL_POLICY_VERSION,
    FAKE_MODEL_REF,
    FakeModel,
)
from .http_model import (
    DIRECT_HTTP_ADAPTER_KIND,
    DIRECT_HTTP_POLICY_VERSION,
    DirectHttpModel,
    ModelCallEvidence,
)
from .manifest import (
    MANIFEST_FIELDS,
    ManifestError,
    ModelManifest,
    build_request_body,
)
from .model_port import (
    ModelAdapterError,
    ModelBudgetExceededError,
    ModelConfigurationError,
    ModelNotAuthorizedError,
    ModelPort,
    ModelResponseError,
    ModelTransportError,
    RunBoundModelPort,
)
from .proposal_generation import parse_proposal
from .step import (
    MODEL_FAILURE_CODE,
    OUTCOME_CHECKED_NO_BELIEF,
    OUTCOME_COMMITTED,
    OUTCOME_FAILED,
    OUTCOME_NO_PENDING_WORK,
    STEP_OUTCOMES,
    CognitionRuntime,
    StepResult,
)

__all__ = [
    "COLLAB_ACTIONS",
    "COLLAB_MODEL_POLICY_VERSION",
    "COLLAB_PROTOCOL_VERSION",
    "COLLAB_RENDERER_VERSION",
    "CONTEXT_RENDERER",
    "CONTEXT_SCHEMA",
    "CognitionRuntime",
    "CollabOutput",
    "ConsultContextEnvelope",
    "ConsultQueryMaterial",
    "ConsultSendPolicy",
    "ContextEnvelope",
    "ContextService",
    "MAX_BRIEF_REASON_CHARS",
    "MAX_REFERENCES",
    "MAX_REPLY_CHARS",
    "DEFAULT_PERMISSION_VIEW_REF",
    "DIRECT_HTTP_ADAPTER_KIND",
    "DIRECT_HTTP_POLICY_VERSION",
    "DirectHttpModel",
    "FAKE_MODEL_ADAPTER_KIND",
    "FAKE_MODEL_POLICY_VERSION",
    "FAKE_MODEL_REF",
    "FakeModel",
    "MANIFEST_FIELDS",
    "MAX_EPISODES",
    "MODEL_FAILURE_CODE",
    "ManifestError",
    "ModelAdapterError",
    "ModelBudgetExceededError",
    "ModelCallEvidence",
    "ModelConfigurationError",
    "ModelManifest",
    "ModelNotAuthorizedError",
    "ModelPort",
    "ModelResponseError",
    "ModelTransportError",
    "OUTCOME_CHECKED_NO_BELIEF",
    "OUTCOME_COMMITTED",
    "OUTCOME_FAILED",
    "OUTCOME_NO_PENDING_WORK",
    "PROPOSAL_CONTRACT",
    "ReadSetEntry",
    "RunBoundModelPort",
    "STEP_OUTCOMES",
    "StepResult",
    "build_collab_request_body",
    "build_request_body",
    "parse_collab_output",
    "parse_proposal",
    "render_collab_messages",
    "repair_feedback",
]
