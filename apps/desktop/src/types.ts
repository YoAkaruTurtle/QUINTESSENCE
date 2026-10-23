/** Wire types for the trusted `desktop.v1` host.  Data only; no authority. */

export type ConsultState =
  | "answered"
  | "waiting"
  | "failed"
  | "incomplete"
  | "stale";

export interface Destination {
  destination_id: string;
  provider: string;
  model: string;
  external: boolean;
  kind: "external" | "local";
  confirmation_required: boolean;
  manifest_hash: string;
  policy_ref: string;
}

export interface HostIdentity {
  namespace: string | null;
  host_instance: string | null;
  operator: string | null;
  retention: string | null;
  input_privacy_class: string | null;
  policy_ref: string | null;
  ready: boolean;
  reason_code: string | null;
  reason_detail: string | null;
}

export interface ReviewCapabilities {
  read: boolean;
  generate: boolean;
  approve: boolean;
  reject: boolean;
  result: boolean;
}

export interface AppInfo {
  app: string;
  version: string;
  protocol: string;
  mode: string;
  label: string | null;
  ai_display_name: string | null;
  ui: {
    ai_display_name: string;
    source: "preference" | "host" | "default" | string;
  };
  session_id: string;
  host: HostIdentity;
  destination: Destination | null;
  workspace: {
    consult: boolean;
    review: boolean;
    review_reason: string | null;
  };
  capabilities: {
    consult: boolean;
    confirm_destination: boolean;
    input_registration: boolean;
    review?: ReviewCapabilities;
  };
  limits: {
    frame_bytes: number;
    request_timeout_seconds: number;
    max_question_chars: number;
  };
  methods: string[];
  reserved: string[];
  pcs: {
    package: string;
    version: string;
    schema_migrations: number;
    facade: string;
  };
}

export interface TypedRef {
  ref_type: string;
  namespace?: string;
  input_id?: string;
  artifact_id?: string;
  record_id?: string;
  record_version?: number;
  event_id?: string;
  content_version?: number;
  artifact_version?: number;
}

export interface QueryItemRef {
  ref: TypedRef;
  current: boolean | null;
  expired: boolean | null;
}

export interface QuerySummary {
  kind: string | null;
  status: string | null;
  material_status: string | null;
  truncated: boolean;
  policy_version: string | null;
  items: QueryItemRef[];
}

export interface InputSummary {
  input_ref: TypedRef;
  origin_kind: string;
  privacy_class: string;
  retention: string;
}

export interface ConsultResult {
  interaction_id: string | null;
  context_id: string | null;
  state: ConsultState;
  reply: string | null;
  detail: string | null;
  query: QuerySummary | null;
  query_source_refs: TypedRef[];
  query_result_ref: TypedRef | null;
  reply_artifact_ref: TypedRef | null;
  input: InputSummary;
  destination: Destination;
}

export interface BridgeError {
  code: string;
  message: string;
  result_unknown: boolean;
}

export interface HostReply {
  ok: boolean;
  result?: unknown;
  error?: BridgeError;
}

export interface ConfirmResult {
  confirmed: boolean;
  session_id: string;
  destination_id: string;
  confirmation_required: boolean;
  confirmed_at: string;
}

// --------------------------------------------------------------------------
// review wire types (desktop.v1).  Data only; no authority, no body beyond
// what the Facade already disclosed.
// --------------------------------------------------------------------------

export type ReviewStateName =
  | "structural_only"
  | "pending"
  | "committed"
  | "waiting"
  | "rejected"
  | "stale"
  | "denied"
  | "failed"
  | "incomplete";

export type ReviewCandidateStatus =
  | "pending"
  | "committed"
  | "rejected"
  | "stale"
  | "failed";

export interface ReviewReceipt {
  request_id: string;
  context_id: string;
  operation: string;
  outcome: string;
  committed_at: string;
  record_refs: Array<{ record_id: string; record_version: number }>;
}

export interface ReviewView {
  state: ReviewStateName;
  interaction_id: string | null;
  candidate_id: string | null;
  receipt: ReviewReceipt | null;
  redacted: boolean;
  detail: string | null;
  model_adapter: string | null;
}

export interface ReviewWorkItem {
  work_id: string;
  event_id: string;
  generation: number;
  blocked: boolean;
  rerun_registered: boolean;
}

export interface LegacyWorkItem {
  work_id: string;
  event_id: string;
  kind: string;
  status: string;
  created_at: string;
}

export interface ReviewWorkList {
  managed: ReviewWorkItem[];
  legacy: LegacyWorkItem[];
}

export interface ReviewCandidateSummary {
  candidate_id: string;
  artifact_version: number;
  work_id: string;
  work_generation: number;
  interaction_id: string;
  context_id: string;
  status: ReviewCandidateStatus;
  created_at: string;
  decided_at: string | null;
  attempt_id: string | null;
  attempt_no: number | null;
}

export interface ReviewCandidateList {
  candidates: ReviewCandidateSummary[];
}

export interface ReviewEvidenceLink {
  ref: TypedRef;
  relation: string | null;
}

export interface ReviewProposalView {
  operation: string;
  target_ref: TypedRef | null;
  claim: string | null;
  claim_type: string | null;
  scope: Record<string, unknown> | null;
  evidence_links: ReviewEvidenceLink[];
  strength: string | null;
  strength_reason: string | null;
  valid_until: string | null;
}

export interface ReviewCurrentView {
  ref: TypedRef;
  claim: string;
  claim_type: string;
  status: string;
  strength: string;
  scope: Record<string, unknown>;
  valid_until: string | null;
  applicability: string;
}

export interface ReviewDiffField {
  field: string;
  before: unknown;
  after: unknown;
  changed: boolean;
}

export interface ReviewSourceView {
  ref: TypedRef | null;
  kind: string;
  availability: "available" | "redacted" | "unavailable";
  reason: string | null;
  body: string | null;
  ref_version: number | null;
}

export interface ReviewCandidateDetail {
  candidate: ReviewCandidateSummary;
  proposal: ReviewProposalView | null;
  structural_validation: string | null;
  current: ReviewCurrentView | null;
  current_state: "not_applicable" | "available" | "redacted" | "missing";
  diff: ReviewDiffField[];
  sources: ReviewSourceView[];
  can_approve: boolean;
  can_reject: boolean;
  eligibility_reason: string | null;
  redacted: boolean;
  detail: string | null;
}
