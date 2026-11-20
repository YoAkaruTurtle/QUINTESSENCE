type Language = "zh-CN" | "en";
type Theme = "light" | "dark";

const labels = {
  waiting: ["待机", "Idle"],
  shallow: ["浅睡", "Shallow"],
  deep: ["深睡", "Deep"],
  thinking: ["思考中", "Thinking"],
  foreground: ["前台优先", "Yielding"],
  disabled: ["未开启", "Off"],
  paused: ["已暂停", "Paused"],
  failed: ["本次失败", "Failed"],
  discarded: ["结果已舍弃", "Discarded"],
  unavailable: ["不可用", "Unavailable"],
  disconnected: ["未连接", "Disconnected"],
} as const;

type DreamingState = keyof typeof labels;

const reasons = {
  maintenance_pending: ["有维护任务待处理。", "Maintenance work is pending."],
  wake_budget: ["正在等待下次可用唤醒时段。", "Waiting for the next wake window."],
  transport_draining: ["正在收尾上次请求。", "The previous request is settling."],
  model_not_authorized: ["空闲模型调用未获授权。", "Idle model calls have no active authorization."],
  purpose_not_allowed: ["当前宿主不允许此用途。", "This use is unavailable under the current host policy."],
  calls_exhausted: ["空闲思考调用额度已用完。", "The idle reflection call budget is exhausted."],
  model_key_missing: ["宿主模型凭据不可用。", "The host's model credential is unavailable."],
} as const;

const semanticReasons = {
  embedding_unavailable: ["语义嵌入不可用，可按关键词检索。", "Embeddings unavailable. Keyword search is available."],
  scope_recall_unavailable: ["语义召回不可用，可按关键词检索。", "Semantic recall unavailable. Keyword search is available."],
  vector_unavailable: ["向量召回不可用，可按关键词检索。", "Vector recall unavailable. Keyword search is available."],
} as const;

export interface DreamingStatus {
  state: DreamingState;
  reason: keyof typeof reasons | null;
  semantic_reason: keyof typeof semanticReasons | null;
  enabled: boolean;
  can_configure: boolean;
  journals_enabled: boolean;
  can_configure_journals: boolean;
  can_show_orb: boolean;
  language: Language;
  theme: Theme;
}

export function dreamingStatus(
  value: unknown,
  fallbackLanguage: Language,
  fallbackTheme: Theme,
): DreamingStatus {
  const status = value as Partial<DreamingStatus> | null | undefined;
  if (!status || typeof status.state !== "string" || !Object.hasOwn(labels, status.state)) {
    return {
      state: "disconnected", reason: null, semantic_reason: null, enabled: false,
      can_configure: false, journals_enabled: false, can_configure_journals: false,
      can_show_orb: false, language: fallbackLanguage, theme: fallbackTheme,
    };
  }
  return {
    state: status.state,
    reason: typeof status.reason === "string" && Object.hasOwn(reasons, status.reason) ? status.reason : null,
    semantic_reason: typeof status.semantic_reason === "string" && Object.hasOwn(semanticReasons, status.semantic_reason)
      ? status.semantic_reason : null,
    enabled: status.enabled === true,
    can_configure: status.can_configure === true,
    journals_enabled: status.journals_enabled === true,
    can_configure_journals: status.can_configure_journals === true,
    can_show_orb: status.can_show_orb === true,
    language: status.language === "en" || status.language === "zh-CN" ? status.language : fallbackLanguage,
    theme: status.theme === "light" || status.theme === "dark" ? status.theme : fallbackTheme,
  };
}

export function dreamingLabel(status: DreamingStatus): string {
  return labels[status.state][status.language === "en" ? 1 : 0];
}

export function dreamingDetails(status: DreamingStatus): string[] {
  const language = status.language === "en" ? 1 : 0;
  const details = [];
  if (status.reason) details.push(reasons[status.reason][language]);
  if (status.semantic_reason) details.push(semanticReasons[status.semantic_reason][language]);
  return details;
}
