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
    review_reason: string;
  };
  capabilities: {
    consult: boolean;
    confirm_destination: boolean;
    input_registration: boolean;
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
