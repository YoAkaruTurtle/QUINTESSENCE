/**
 * C12c Review workspace state.
 *
 * This module is DOM- and Tauri-free so the exact decision rules can be tested
 * in Node.  It never persists a candidate, a body, a receipt or a permission.
 * The host's `review.*` replies are the only source of truth; `can_approve` and
 * `can_reject` are content hints and never replace the host capability set.
 */

import { shortId } from "./format";
import type {
  AppInfo,
  BridgeError,
  LegacyWorkItem,
  ReviewCandidateDetail,
  ReviewCandidateSummary,
  ReviewCapabilities,
  ReviewDiffField,
  ReviewStateName,
  ReviewView,
  ReviewWorkItem,
  TypedRef,
} from "./types";

export const NO_REVIEW_CAPABILITIES: ReviewCapabilities = {
  read: false,
  generate: false,
  approve: false,
  reject: false,
  result: false,
};

export type ReviewScreen = "works" | "candidates" | "detail" | "result" | "reauth";
export type ReviewTab = "managed" | "legacy";
export type ReviewBusy = "generate" | "resolve" | "result" | null;
export type ReviewDecision = "approve" | "reject";

export interface ReviewUnknown {
  message: string;
  interactionId: string | null;
}

export interface ReviewState {
  loaded: boolean;
  loading: boolean;
  error: BridgeError | null;
  managed: ReviewWorkItem[];
  legacy: LegacyWorkItem[];
  tab: ReviewTab;
  selectedWorkId: string | null;
  selectedWork: ReviewWorkItem | null;
  selectedLegacy: LegacyWorkItem | null;
  screen: ReviewScreen;
  candidatesLoading: boolean;
  candidatesError: BridgeError | null;
  candidates: ReviewCandidateSummary[];
  selectedCandidateId: string | null;
  selectedVersion: number | null;
  detailLoading: boolean;
  detailError: BridgeError | null;
  detail: ReviewCandidateDetail | null;
  busy: ReviewBusy;
  confirmGenerate: boolean;
  confirmDecision: ReviewDecision | null;
  outcome: ReviewView | null;
  notice: string | null;
  conflict: string | null;
  unknown: ReviewUnknown | null;
  lastInteractionId: string | null;
  lastWorkId: string | null;
  lastEventId: string | null;
  lastCandidateId: string | null;
  lastVersion: number | null;
  refreshRequested: boolean;
}

export const INITIAL_REVIEW_STATE: ReviewState = {
  loaded: false,
  loading: false,
  error: null,
  managed: [],
  legacy: [],
  tab: "managed",
  selectedWorkId: null,
  selectedWork: null,
  selectedLegacy: null,
  screen: "works",
  candidatesLoading: false,
  candidatesError: null,
  candidates: [],
  selectedCandidateId: null,
  selectedVersion: null,
  detailLoading: false,
  detailError: null,
  detail: null,
  busy: null,
  confirmGenerate: false,
  confirmDecision: null,
  outcome: null,
  notice: null,
  conflict: null,
  unknown: null,
  lastInteractionId: null,
  lastWorkId: null,
  lastEventId: null,
  lastCandidateId: null,
  lastVersion: null,
  refreshRequested: false,
};

export type ReviewAction =
  | { type: "works-loading" }
  | {
      type: "works-loaded";
      managed: ReviewWorkItem[];
      legacy: LegacyWorkItem[];
    }
  | { type: "works-error"; error: BridgeError }
  | { type: "tab"; tab: ReviewTab }
  | { type: "work-selected"; work: ReviewWorkItem }
  | { type: "legacy-selected"; work: LegacyWorkItem }
  | { type: "works-back" }
  | { type: "candidates-loading" }
  | { type: "candidates-loaded"; candidates: ReviewCandidateSummary[] }
  | { type: "candidates-error"; error: BridgeError }
  | { type: "candidate-selected"; candidate: ReviewCandidateSummary }
  | { type: "detail-loading" }
  | { type: "detail-loaded"; detail: ReviewCandidateDetail }
  | { type: "detail-error"; error: BridgeError }
  | { type: "generate-confirm"; open: boolean }
  | { type: "decision-confirm"; decision: ReviewDecision | null }
  | { type: "generate-start" }
  | { type: "generate-result"; view: ReviewView }
  | { type: "resolve-start"; decision: ReviewDecision }
  | { type: "resolve-result"; view: ReviewView }
  | { type: "result-start" }
  | { type: "result-result"; view: ReviewView }
  | { type: "reauth-open" }
  | { type: "reauth-close" }
  | {
      type: "action-error";
      error: BridgeError;
      kind: "generate" | "resolve" | "result";
    }
  | { type: "refresh-consumed" }
  | { type: "outcome-dismiss" }
  | { type: "note"; message: string | null }
  | { type: "conflict-clear" }
  | { type: "unknown-clear" };

export function reviewCaps(info: AppInfo | null): ReviewCapabilities {
  return info?.capabilities.review ?? NO_REVIEW_CAPABILITIES;
}

export function reviewAvailable(info: AppInfo | null): boolean {
  return Boolean(
    info &&
      info.host.ready &&
      info.workspace.review &&
      reviewCaps(info).read,
  );
}

export function isLegacySelection(state: ReviewState): boolean {
  return state.selectedLegacy !== null;
}

export function selectedWorkId(state: ReviewState): string | null {
  return state.selectedWork?.work_id ?? state.selectedLegacy?.work_id ?? null;
}

export function selectedEventId(state: ReviewState): string | null {
  return state.selectedWork?.event_id ?? state.selectedLegacy?.event_id ?? null;
}

export function hasPendingCandidate(state: ReviewState): boolean {
  return state.candidates.some((candidate) => candidate.status === "pending");
}

export function hasCommittedCandidate(state: ReviewState): boolean {
  return state.candidates.some((candidate) => candidate.status === "committed");
}

export function currentCandidate(state: ReviewState): ReviewCandidateSummary | null {
  if (state.selectedCandidateId === null) {
    return null;
  }
  return (
    state.candidates.find(
      (candidate) => candidate.candidate_id === state.selectedCandidateId,
    ) ?? null
  );
}

export function generateBlockReason(
  state: ReviewState,
  info: AppInfo | null,
): string | null {
  const caps = reviewCaps(info);
  if (!info || !info.host.ready) {
    return "宿主未就绪";
  }
  if (!caps.read) {
    return "缺少审查读取权限";
  }
  if (!caps.generate) {
    return "缺少开始审查的权限";
  }
  if (state.busy !== null) {
    return state.busy === "generate" ? "正在生成候选" : "正在提交决定";
  }
  if (state.candidatesLoading) {
    return "正在读取候选";
  }
  if (state.selectedLegacy !== null) {
    return "旧工作项只读，不产生候选";
  }
  if (!state.selectedWork) {
    return "请先选择工作项";
  }
  if (hasPendingCandidate(state)) {
    return "已有待决定候选；请继续当前审查";
  }
  if (hasCommittedCandidate(state)) {
    return "该工作已有已提交候选；不能重复发起审查";
  }
  if (state.selectedWork.blocked && !state.selectedWork.rerun_registered) {
    return "工作已暂停；需要宿主发放新的复查许可";
  }
  return null;
}

export function generateEnabled(
  state: ReviewState,
  info: AppInfo | null,
): boolean {
  return generateBlockReason(state, info) === null;
}

export function resolveBlockReason(
  state: ReviewState,
  info: AppInfo | null,
  decision: ReviewDecision,
): string | null {
  const caps = reviewCaps(info);
  if (!info || !info.host.ready) {
    return "宿主未就绪";
  }
  if (!caps.read) {
    return "缺少审查读取权限";
  }
  if (decision === "approve" && !caps.approve) {
    return "缺少审批权限";
  }
  if (decision === "reject" && !caps.reject) {
    return "缺少拒绝权限";
  }
  if (state.busy !== null) {
    return state.busy === "generate" ? "正在生成候选" : "正在提交决定";
  }
  const detail = state.detail;
  if (!detail) {
    return "候选详情尚未加载";
  }
  if (detail.redacted) {
    return "候选内容不可读；不能在这里做决定";
  }
  if (detail.candidate.status !== "pending") {
    return "候选已不是待决定状态；请刷新候选";
  }
  if (detail.candidate.artifact_version !== 1) {
    return "此候选版本不在 C12c 支持范围；不能批准或拒绝";
  }
  if (
    state.selectedVersion !== null &&
    state.selectedVersion !== detail.candidate.artifact_version
  ) {
    return "所选候选版本已变化；请刷新候选";
  }
  if (
    state.selectedWork &&
    state.selectedWork.generation !== detail.candidate.work_generation
  ) {
    return "工作轮次已变化；请刷新候选";
  }
  if (detail.eligibility_reason === "stale_generation") {
    return "工作轮次已变化；请刷新候选";
  }
  if (decision === "approve") {
    if (!detail.can_approve) {
      return eligibilityLabel(detail.eligibility_reason, "当前不可批准");
    }
    if (
      detail.current_state === "redacted" ||
      detail.current_state === "missing"
    ) {
      return "当前目标版本不可读或不存在；请刷新权威状态";
    }
  } else if (!detail.can_reject) {
    return eligibilityLabel(detail.eligibility_reason, "当前不可拒绝");
  }
  return null;
}

export function approveEnabled(
  state: ReviewState,
  info: AppInfo | null,
): boolean {
  return resolveBlockReason(state, info, "approve") === null;
}

export function rejectEnabled(
  state: ReviewState,
  info: AppInfo | null,
): boolean {
  return resolveBlockReason(state, info, "reject") === null;
}

export function canStartResolve(
  state: ReviewState,
  info: AppInfo | null,
  decision: ReviewDecision,
): boolean {
  if (state.busy !== null) {
    return false;
  }
  return decision === "approve"
    ? approveEnabled(state, info)
    : rejectEnabled(state, info);
}

export function recoveryInteractionId(state: ReviewState): string | null {
  return (
    state.unknown?.interactionId ??
    state.detail?.candidate.interaction_id ??
    state.outcome?.interaction_id ??
    state.lastInteractionId
  );
}

export function reviewCanRecover(
  state: ReviewState,
  info: AppInfo | null,
): boolean {
  return reviewCaps(info).result && recoveryInteractionId(state) !== null;
}

export function receiptVisible(view: ReviewView | null): boolean {
  return Boolean(view && view.receipt && !view.redacted);
}

export function reviewReduce(state: ReviewState, action: ReviewAction): ReviewState {
  switch (action.type) {
    case "works-loading":
      return { ...state, loading: true, error: null };
    case "works-loaded": {
      const selectedManaged = state.selectedWork
        ? action.managed.find(
            (item) => item.work_id === state.selectedWork!.work_id,
          ) ?? null
        : null;
      const selectedLegacy = state.selectedLegacy
        ? action.legacy.find(
            (item) => item.work_id === state.selectedLegacy!.work_id,
          ) ?? null
        : null;
      const stillSelected =
        (state.selectedWork === null || selectedManaged !== null) &&
        (state.selectedLegacy === null || selectedLegacy !== null);
      return {
        ...state,
        loaded: true,
        loading: false,
        error: null,
        managed: action.managed,
        legacy: action.legacy,
        selectedWork: selectedManaged,
        selectedLegacy: selectedLegacy,
        selectedWorkId:
          selectedManaged?.work_id ?? selectedLegacy?.work_id ?? null,
        screen:
          stillSelected || state.screen === "result" ? state.screen : "works",
        candidates: stillSelected ? state.candidates : [],
        candidatesError: stillSelected ? state.candidatesError : null,
        selectedCandidateId: stillSelected ? state.selectedCandidateId : null,
        selectedVersion: stillSelected ? state.selectedVersion : null,
        detail: stillSelected ? state.detail : null,
        detailError: stillSelected ? state.detailError : null,
      };
    }
    case "works-error":
      return { ...state, loading: false, loaded: true, error: action.error };
    case "tab":
      return {
        ...state,
        tab: action.tab,
        screen: "works",
        selectedWorkId: null,
        selectedWork: null,
        selectedLegacy: null,
        candidates: [],
        candidatesError: null,
        selectedCandidateId: null,
        selectedVersion: null,
        detail: null,
        detailError: null,
        outcome: null,
        conflict: null,
        unknown: null,
        confirmGenerate: false,
        confirmDecision: null,
      };
    case "work-selected":
      return {
        ...state,
        tab: "managed",
        screen: "candidates",
        selectedWorkId: action.work.work_id,
        selectedWork: action.work,
        selectedLegacy: null,
        candidatesLoading: true,
        candidates: [],
        candidatesError: null,
        selectedCandidateId: null,
        selectedVersion: null,
        detail: null,
        detailError: null,
        outcome: null,
        conflict: null,
        unknown: null,
        notice: null,
        confirmGenerate: false,
        confirmDecision: null,
        lastWorkId: action.work.work_id,
        lastEventId: action.work.event_id,
      };
    case "legacy-selected":
      return {
        ...state,
        tab: "legacy",
        screen: "candidates",
        selectedWorkId: action.work.work_id,
        selectedWork: null,
        selectedLegacy: action.work,
        candidatesLoading: false,
        candidates: [],
        candidatesError: null,
        selectedCandidateId: null,
        selectedVersion: null,
        detail: null,
        detailError: null,
        outcome: null,
        conflict: null,
        unknown: null,
        notice: null,
        confirmGenerate: false,
        confirmDecision: null,
        lastWorkId: action.work.work_id,
        lastEventId: action.work.event_id,
      };
    case "works-back":
      return {
        ...state,
        screen: "works",
        selectedWorkId: null,
        selectedWork: null,
        selectedLegacy: null,
        candidates: [],
        candidatesError: null,
        selectedCandidateId: null,
        selectedVersion: null,
        detail: null,
        detailError: null,
        outcome: null,
        conflict: null,
        unknown: null,
        confirmGenerate: false,
        confirmDecision: null,
      };
    case "candidates-loading":
      return { ...state, candidatesLoading: true, candidatesError: null };
    case "candidates-loaded": {
      const selectedStillPresent =
        state.selectedCandidateId !== null &&
        action.candidates.some(
          (candidate) => candidate.candidate_id === state.selectedCandidateId,
        );
      return {
        ...state,
        candidatesLoading: false,
        candidates: action.candidates,
        candidatesError: null,
        selectedCandidateId: selectedStillPresent
          ? state.selectedCandidateId
          : null,
        selectedVersion: selectedStillPresent ? state.selectedVersion : null,
        detail: selectedStillPresent ? state.detail : null,
        detailError: selectedStillPresent ? state.detailError : null,
        detailLoading: selectedStillPresent ? state.detailLoading : false,
      };
    }
    case "candidates-error":
      return {
        ...state,
        candidatesLoading: false,
        candidatesError: action.error,
      };
    case "candidate-selected":
      return {
        ...state,
        screen: "detail",
        selectedCandidateId: action.candidate.candidate_id,
        selectedVersion: action.candidate.artifact_version,
        detailLoading: true,
        detail: null,
        detailError: null,
        outcome: null,
        conflict: null,
        notice: null,
        confirmDecision: null,
        lastCandidateId: action.candidate.candidate_id,
        lastVersion: action.candidate.artifact_version,
        lastInteractionId: action.candidate.interaction_id,
      };
    case "detail-loading":
      return { ...state, detailLoading: true, detailError: null };
    case "detail-loaded":
      return {
        ...state,
        detailLoading: false,
        detail: action.detail,
        detailError: null,
        selectedCandidateId: action.detail.candidate.candidate_id,
        selectedVersion: action.detail.candidate.artifact_version,
        lastCandidateId: action.detail.candidate.candidate_id,
        lastVersion: action.detail.candidate.artifact_version,
        lastInteractionId: action.detail.candidate.interaction_id,
      };
    case "detail-error":
      return {
        ...state,
        detailLoading: false,
        detailError: action.error,
      };
    case "generate-confirm":
      return { ...state, confirmGenerate: action.open };
    case "decision-confirm":
      return { ...state, confirmDecision: action.decision };
    case "generate-start":
      return {
        ...state,
        busy: "generate",
        confirmGenerate: false,
        error: null,
        notice: null,
        conflict: null,
        outcome: null,
      };
    case "generate-result": {
      const pending = action.view.state === "pending";
      return {
        ...state,
        busy: null,
        outcome: action.view,
        notice: pending ? "候选已生成" : state.notice,
        screen: pending ? "candidates" : "result",
        lastInteractionId: action.view.interaction_id ?? state.lastInteractionId,
        lastCandidateId: action.view.candidate_id ?? state.lastCandidateId,
        unknown: null,
        confirmGenerate: false,
      };
    }
    case "resolve-start":
      return {
        ...state,
        busy: "resolve",
        confirmDecision: null,
        error: null,
        notice: null,
        conflict: null,
      };
    case "resolve-result":
      return {
        ...state,
        busy: null,
        outcome: action.view,
        screen: "result",
        lastInteractionId: action.view.interaction_id ?? state.lastInteractionId,
        lastCandidateId: action.view.candidate_id ?? state.lastCandidateId,
        unknown: null,
        conflict: null,
      };
    case "result-start":
      return { ...state, busy: "result", error: null };
    case "result-result":
      return {
        ...state,
        busy: null,
        outcome: action.view,
        screen: "result",
        lastInteractionId: action.view.interaction_id ?? state.lastInteractionId,
        unknown: null,
        notice: "已按权威结果恢复",
      };
    case "reauth-open":
      return {
        ...state,
        screen: "reauth",
        confirmGenerate: false,
        confirmDecision: null,
        conflict: null,
      };
    case "reauth-close":
      return {
        ...state,
        screen: state.outcome ? "result" : "candidates",
      };
    case "action-error":
      return reviewErrorState(state, action.error, action.kind);
    case "refresh-consumed":
      return { ...state, refreshRequested: false };
    case "outcome-dismiss":
      return {
        ...state,
        outcome: null,
        screen: state.selectedWork || state.selectedLegacy ? "candidates" : "works",
        confirmGenerate: false,
        confirmDecision: null,
      };
    case "note":
      return { ...state, notice: action.message };
    case "conflict-clear":
      return { ...state, conflict: null };
    case "unknown-clear":
      return { ...state, unknown: null };
    default:
      return state;
  }
}

function reviewErrorState(
  state: ReviewState,
  error: BridgeError,
  kind: "generate" | "resolve" | "result",
): ReviewState {
  const base: ReviewState = { ...state, busy: null };
  if (error.result_unknown) {
    return {
      ...base,
      unknown: {
        message: reviewErrorText(error),
        interactionId: state.lastInteractionId,
      },
      refreshRequested: true,
      confirmGenerate: false,
      confirmDecision: null,
    };
  }
  if (
    error.code === "REVISION_CONFLICT" ||
    error.code === "CANDIDATE_STALE" ||
    error.code === "CANDIDATE_VERSION_MISMATCH"
  ) {
    return {
      ...base,
      conflict: reviewErrorText(error),
      refreshRequested: true,
      screen: state.detail ? "detail" : "candidates",
      confirmGenerate: false,
      confirmDecision: null,
    };
  }
  if (error.code === "AUTHORIZATION_REQUIRED") {
    return {
      ...base,
      notice: reviewErrorText(error),
      refreshRequested: true,
      confirmGenerate: false,
      confirmDecision: null,
    };
  }
  if (
    error.code === "CANDIDATE_EXISTS" ||
    error.code === "WORK_ALREADY_RESOLVED" ||
    error.code === "LEGACY_WORK_READ_ONLY" ||
    error.code === "WORK_NOT_PENDING" ||
    error.code === "UNSUPPORTED_VERSION"
  ) {
    return {
      ...base,
      notice: reviewErrorText(error),
      refreshRequested: true,
      confirmGenerate: false,
      confirmDecision: null,
    };
  }
  if (error.code === "NOT_FOUND" || error.code === "REDACTED") {
    return {
      ...base,
      notice: reviewErrorText(error),
      refreshRequested: true,
      confirmGenerate: false,
      confirmDecision: null,
    };
  }
  if (error.code === "BUSY") {
    return {
      ...base,
      notice: "已有一次审查操作正在处理；没有排队，也没有重复提交。",
      confirmGenerate: false,
      confirmDecision: null,
    };
  }
  return {
    ...base,
    error,
    notice: kind === "generate" ? "审查没有开始" : "决定没有提交",
    confirmGenerate: false,
    confirmDecision: null,
  };
}

export function reviewErrorText(error: BridgeError): string {
  switch (error.code) {
    case "REVISION_CONFLICT":
      return "目标版本已变化，需要重新获取权威状态；没有自动重试。";
    case "CANDIDATE_STALE":
      return "候选状态已变化，需要刷新候选；没有提交决定。";
    case "CANDIDATE_VERSION_MISMATCH":
      return "候选版本已变化或不存在，需要刷新候选；没有提交决定。";
    case "AUTHORIZATION_REQUIRED":
      return "缺少审批权限或许可；没有读取或提交任何内容。";
    case "MODEL_KEY_MISSING":
      return "尚未保存 API Key；请先在设置中配置 DeepSeek。";
    case "MODEL_KEY_INVALID":
      return "保存的 API Key 格式无效；请重新保存。";
    case "MODEL_NOT_AUTHORIZED":
    case "MODEL_BUDGET_EXCEEDED":
      return "模型调用没有通过 C09 授权或额度检查；没有提交。";
    case "MODEL_AUTH_FAILED":
      return "DeepSeek 鉴权失败；请检查 API Key。";
    case "MODEL_RATE_LIMITED":
      return "DeepSeek 限流或配额不足；请稍后再试。";
    case "MODEL_NETWORK":
      return "无法连接 DeepSeek；没有自动重试。";
    case "MODEL_TIMEOUT":
      return "DeepSeek 请求超时；结果未知，请刷新权威状态。";
    case "MODEL_PROVIDER_ERROR":
      return "DeepSeek 服务端错误；没有提交。";
    case "MODEL_INVALID_RESPONSE":
      return "模型输出未通过协议校验；没有提交。";
    case "MODEL_REQUEST_REJECTED":
      return "DeepSeek 拒绝了请求；请检查模型或账户状态。";
    case "CANDIDATE_EXISTS":
      return "已有待决定候选；请继续当前审查，不重复生成。";
    case "WORK_ALREADY_RESOLVED":
      return "该工作已有已提交候选；不能重复发起审查。";
    case "LEGACY_WORK_READ_ONLY":
      return "旧工作项只读，不产生候选。";
    case "WORK_NOT_PENDING":
      return "该工作项已不是待处理状态；没有提交决定。";
    case "UNSUPPORTED_VERSION":
      return "此候选版本不在 C12c 支持范围，不能批准或拒绝。";
    case "NOT_FOUND":
      return "未找到对应的审查记录；请刷新工作或候选列表。";
    case "REDACTED":
      return "该内容受删除或隐私屏障保护；没有显示正文。";
    case "DATA_LOCKED":
    case "HOST_NOT_READY":
    case "SIDECAR_UNAVAILABLE":
      return error.message || "可信宿主未就绪。";
    default:
      if (error.result_unknown) {
        return "结果未知；没有自动重发。请刷新权威状态或查询结果。";
      }
      return error.message || "本次审查操作没有完成。";
  }
}

export function reviewViewLabel(state: ReviewStateName): string {
  const labels: Record<ReviewStateName, string> = {
    structural_only: "仅结构校验",
    pending: "待决定",
    committed: "已提交",
    waiting: "已记录 WAIT",
    rejected: "已拒绝 · 已暂停",
    stale: "已过期",
    denied: "审查未启动",
    failed: "审查未完成",
    incomplete: "结论不完整",
  };
  return labels[state];
}

export function candidateStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    pending: "待决定",
    committed: "已提交",
    rejected: "已拒绝",
    stale: "已过期",
    failed: "未完成",
  };
  return labels[status] ?? status;
}

export function candidateStatusTone(status: string): string {
  if (status === "committed") {
    return "ok";
  }
  if (status === "rejected" || status === "failed") {
    return "danger";
  }
  if (status === "stale") {
    return "warn";
  }
  return "pending";
}

export function eligibilityLabel(
  reason: string | null,
  fallback: string,
): string {
  const labels: Record<string, string> = {
    candidate_redacted: "候选内容不可读",
    terminal: "候选已是终态",
    stale_generation: "工作轮次已变化",
    unsupported_version: "此候选版本不在支持范围",
    unsupported_operation: "此操作不支持批准",
    current_redacted: "当前版本不可读",
    current_missing: "当前版本不存在",
    target_moved: "目标版本已变化",
    current_terminal: "当前版本已终结",
    source_redacted: "有来源不可读",
    source_unavailable: "有来源不可用",
  };
  return reason ? labels[reason] ?? fallback : fallback;
}

export function diffFieldLabel(field: string): string {
  const labels: Record<string, string> = {
    claim: "主张",
    claim_type: "类型",
    scope: "范围 / 适用性",
    evidence_links: "来源引用",
    strength: "强度",
    valid_until: "有效期",
  };
  return labels[field] ?? field;
}

export function formatReviewValue(field: string, value: unknown): string {
  if (value === null || value === undefined) {
    return "—";
  }
  if (field === "scope") {
    if (typeof value !== "object" || Array.isArray(value)) {
      return "—";
    }
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) {
      return "general";
    }
    return entries.map(([key, item]) => `${key}=${String(item)}`).join("; ");
  }
  if (field === "evidence_links") {
    if (!Array.isArray(value)) {
      return "—";
    }
    if (value.length === 0) {
      return "0 项";
    }
    return `${value.length} 项`;
  }
  if (field === "valid_until") {
    return typeof value === "string" ? value : "—";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return "—";
}

export function splitDiff(detail: ReviewCandidateDetail | null): {
  changed: ReviewDiffField[];
  unchanged: ReviewDiffField[];
} {
  if (!detail) {
    return { changed: [], unchanged: [] };
  }
  return {
    changed: detail.diff.filter((row) => row.changed),
    unchanged: detail.diff.filter((row) => !row.changed),
  };
}

export function sourceAvailabilityLabel(availability: string): string {
  if (availability === "available") {
    return "可读";
  }
  if (availability === "redacted") {
    return "已隐藏";
  }
  return "不可用";
}

export function sourceReasonLabel(reason: string | null): string {
  const labels: Record<string, string> = {
    deleted: "已删除",
    privacy: "隐私限制",
    not_found: "不存在",
    unavailable: "不可用",
    redacted: "已隐藏",
  };
  return reason ? labels[reason] ?? reason : "—";
}

export function sourceKindLabel(kind: string): string {
  const labels: Record<string, string> = {
    event: "事件",
    record: "记录",
    collab_input: "登记输入",
    collab_artifact: "协作产物",
  };
  return labels[kind] ?? kind;
}

export function sourceRefLabel(ref: TypedRef | null): string {
  if (!ref) {
    return "引用不可用";
  }
  const kind = ref.ref_type;
  const id = sourceRefId(ref);
  const labels: Record<string, string> = {
    event: "事件",
    record: "记录",
    collab_input: "输入",
    collab_artifact: "产物",
  };
  return id === "引用不可用" ? id : `${labels[kind] ?? kind} ${id}`;
}

export function sourceRefId(ref: TypedRef | null): string {
  if (!ref) {
    return "引用不可用";
  }
  const kind = ref.ref_type;
  if (kind === "event") {
    return shortId(ref.event_id ?? "");
  }
  if (kind === "record") {
    return `${shortId(ref.record_id ?? "")} · v${String(
      ref.record_version ?? "—",
    )}`;
  }
  if (kind === "collab_input") {
    return `${shortId(ref.input_id ?? "")} · v${String(
      ref.content_version ?? "—",
    )}`;
  }
  if (kind === "collab_artifact") {
    return `${shortId(ref.artifact_id ?? "")} · v${String(
      ref.artifact_version ?? "—",
    )}`;
  }
  return "引用不可用";
}

export function operationLabel(operation: string | null | undefined): string {
  const labels: Record<string, string> = {
    propose_belief: "新增 Belief",
    revise_belief: "修订 Belief",
    retract_belief: "撤回 Belief",
    wait: "WAIT（暂不形成 Belief）",
  };
  return operation ? labels[operation] ?? operation : "未知操作";
}

export function reviewReasonLabel(reason: string | null | undefined): string {
  const labels: Record<string, string> = {
    REVIEW_NOT_CONFIGURED: "宿主未配置审查权限",
    REVIEW_READ_PERMISSION_MISSING: "缺少审查读取权限",
    REVIEW_PURPOSE_MISSING: "宿主未声明 review 用途",
    HOST_NOT_READY: "可信宿主未就绪",
  };
  return reason ? labels[reason] ?? reason : "审查不可用";
}

export function buildGenerateParams(
  sessionId: string,
  state: ReviewState,
  confirmedDestinationId: string | null,
): {
  sessionId: string;
  workId: string;
  eventId: string;
  confirmedDestinationId: string | null;
} {
  return {
    sessionId,
    workId: selectedWorkId(state) ?? "",
    eventId: selectedEventId(state) ?? "",
    confirmedDestinationId,
  };
}

export function buildResolveParams(
  sessionId: string,
  detail: ReviewCandidateDetail,
  decision: ReviewDecision,
): {
  sessionId: string;
  candidateId: string;
  artifactVersion: number;
  decision: ReviewDecision;
} {
  return {
    sessionId,
    candidateId: detail.candidate.candidate_id,
    artifactVersion: detail.candidate.artifact_version,
    decision,
  };
}

export function buildResultParams(
  sessionId: string,
  interactionId: string,
): { sessionId: string; interactionId: string } {
  return { sessionId, interactionId };
}

export function receiptOperationLabel(operation: string | null | undefined): string {
  return operationLabel(operation);
}
