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

export interface PersonaSpeaker {
  identity: "owner" | "known_other" | "unknown" | string;
  source?: string;
  display_name?: string;
}

export interface PersonaInfo {
  state: "ready" | "not_ready" | string;
  source: "expected" | "active" | "neutral_provisioned" | null;
  profile_id: string | null;
  profile_version: string | null;
  record_id?: string | null;
  record_version?: number | null;
  asset_hash?: string | null;
  expected: { profile_id: string; profile_version?: string } | null;
  matches_expected?: boolean | null;
  speaker: PersonaSpeaker | null;
  reason_code?: string | null;
}

/** RG08: the host-reported web surface, without a credential value. */
export interface WebCapabilities {
  enabled: boolean;
  configured: boolean;
  declared: boolean;
  search: boolean;
  fetch: boolean;
  credential_present: boolean;
  credential_env: string | null;
  allowed_hosts: number;
  host_policy: "allowlist" | "any_public_https" | null;
  source: "managed" | "external" | "lab" | null;
  missing: string[];
}

export interface AppInfo {
  app: string;
  version: string;
  protocol: string;
  mode: string;
  label: string | null;
  ai_display_name: string | null;
  persona?: PersonaInfo;
  ui: {
    ai_display_name: string;
    source: "preference" | "host" | "default" | string;
  };
  session_id: string;
  host: HostIdentity;
  destination: Destination | null;
  workspace: {
    consult: boolean;
    conversation?: boolean;
    review: boolean;
    review_reason: string | null;
  };
  capabilities: {
    consult: boolean;
    conversation?: boolean;
    confirm_destination: boolean;
    input_registration: boolean;
    process?: boolean;
    cancel?: boolean;
    review?: ReviewCapabilities;
    memory?: { configured: boolean; search?: boolean };
    cognition?: CognitionCapabilities;
    persona?: PersonaCapabilities;
    preferences?: PreferenceCapabilities;
    web?: WebCapabilities;
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

export interface CognitionCapabilities {
  enabled: boolean;
  configured: boolean;
  propose: boolean;
  pending_review: boolean;
  auto_authorization: boolean;
  working_set_limit: number;
  pending_limit: number;
}

export interface CognitionCandidate {
  candidate_id: string;
  artifact_version: number;
  claim: string;
  claim_type: string | null;
  target?: unknown;
  created_at: string;
  state: string;
  requires_human: boolean;
  impact: string | null;
  reason_code: string | null;
  turn_id: string | null;
}

export interface CognitionCandidateList {
  candidates: CognitionCandidate[];
  pending_review: boolean;
}

export interface HostReply {
  ok: boolean;
  result?: unknown;
  error?: BridgeError;
}

export interface ModelStatus {
  provider: string;
  model: string;
  base_url: string;
  managed: boolean;
  model_choices: string[];
  key_saved: boolean;
  key_available?: boolean;
  credential_source?: "environment" | "credential_manager" | "none";
  credential_restart_required?: boolean;
  key_store_error: string | null;
  sidecar_available: boolean;
  host_ready: boolean;
  reason_code: string | null;
  reason_detail: string | null;
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
  last_candidate_status: string | null;
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

// --------------------------------------------------------------------------
// UI-F02 persona & preference management (desktop.v1).  Data only.
// --------------------------------------------------------------------------

export interface PersonaCapabilities {
  read: boolean;
  publish: boolean;
  activate: boolean;
  read_body?: boolean;
  import?: boolean;
}

export interface PreferenceCapabilities {
  read: boolean;
  revoke: boolean;
}

export interface ProfileVersionSummary {
  record_version: number;
  profile_version: string | null;
  schema_version: string | null;
  recorded_at: string;
  record_status: string;
  is_active_version: boolean;
  review_event_id: string | null;
}

export interface ProfileStyleSummary {
  language: string | null;
  tone: string | null;
  verbosity_by_context: { casual: string | null; technical: string | null };
}

export interface ProfileCurrentSummary {
  record_version: number;
  profile_version: string | null;
  schema_version: string | null;
  record_status: string;
  recorded_at: string;
  asset_hash: string | null;
  renderer_compatibility: string | null;
  style_summary: ProfileStyleSummary | null;
  principles_count: number;
  examples_count: number;
}

export interface ProfileSummary {
  profile_id: string;
  record_id: string;
  is_active: boolean;
  current: ProfileCurrentSummary;
  versions: ProfileVersionSummary[];
  versions_total: number;
  versions_truncated: boolean;
}

export interface ActiveProfileRef {
  record_id: string;
  record_version: number;
  profile_id: string | null;
  degraded: boolean;
}

export interface ProfileList {
  active: ActiveProfileRef | null;
  profiles: ProfileSummary[];
  truncated: boolean;
}

export interface ProfileBody {
  record_id: string;
  record_version: number;
  profile_id: string | null;
  profile_version: string | null;
  schema_version: string | null;
  body: Record<string, unknown>;
  body_sha256: string;
  size_bytes: number;
}

export interface ImportFile {
  name: string;
  size_bytes: number;
  modified_at: string;
}

export interface ImportList {
  available: boolean;
  reason: string | null;
  dir_label: string;
  files: ImportFile[];
}

export interface ImportSummary {
  import_id: string;
  profile_id: string;
  profile_version: string | null;
  schema_version: string;
  asset_hash: string | null;
  renderer_compatibility: string | null;
  style_summary: ProfileStyleSummary | null;
  principles_count: number;
  examples_count: number;
  body_sha256: string;
  size_bytes: number;
  expires_at: string;
  warnings: string[];
}

export interface PersonaPublishResult {
  profile_id: string;
  record_id: string;
  record_version: number;
  review_event_id: string | null;
  review_event_bound?: boolean;
  review_body_sha256: string;
  active_changed: boolean;
  reused: boolean;
}

export interface PreferenceSource {
  kind: "user_statement" | "unknown" | string;
  input_id: string | null;
}

export interface PreferenceRecordRef {
  record_id: string;
  record_version: number;
  recorded_at: string;
}

export interface ConfirmedPreference {
  dimension: string;
  value: string;
  label: string;
  claim: string | null;
  strength: string | null;
  scope: Record<string, unknown>;
  source: PreferenceSource;
  record: PreferenceRecordRef;
}

export interface TentativePreference {
  dimension: string;
  value: string;
  label: string;
  status: string;
  scope: Record<string, unknown>;
  source: PreferenceSource;
  record: PreferenceRecordRef;
}

export interface InactivePreference {
  kind: "confirmed" | "tentative";
  state: string;
  status: string | null;
  dimension: string | null;
  value: string | null;
  label: string | null;
  reason: string | null;
  scope: Record<string, unknown>;
  record: PreferenceRecordRef;
  superseded_by: { record_id: string; record_version: number } | null;
}

export interface PreferenceList {
  confirmed: ConfirmedPreference[];
  tentative: TentativePreference[];
  inactive: InactivePreference[];
  conversation: { conversation_id: string; title: string | null } | null;
  expression_recording: { enabled: boolean; reason: string | null };
  confirmed_recording: { enabled: boolean; reason: string | null };
  truncated: boolean;
}
