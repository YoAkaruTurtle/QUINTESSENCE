/** Human-facing formatting.  All input is untrusted text and is never HTML. */

import type {
  AppInfo,
  ConsultResult,
  Destination,
  QuerySummary,
  TypedRef,
} from "./types";

export const RECOVERY_NOTICE =
  "刷新或重启后无法恢复此前 Consult 的结果；请重新提问。本窗口不会读取本地缓存冒充正式结果。";

export function aiDisplayName(info: AppInfo | null): string {
  if (!info) {
    return "助手";
  }
  const configured = info.ui?.ai_display_name || info.ai_display_name || "助手";
  return configured.trim() || "助手";
}

export function avatarLetter(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) {
    return "AI";
  }
  const first = Array.from(trimmed)[0];
  return first ?? "AI";
}

export function shortId(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  const trimmed = value.replace(/^sha256:/, "");
  if (trimmed.length <= 12) {
    return trimmed;
  }
  return `${trimmed.slice(0, 8)}…`;
}

export function destinationLabel(destination: Destination | null): string {
  if (!destination) {
    return "目的地未就绪";
  }
  const kind = destination.external ? "外部" : "本地";
  return `${kind} · ${destination.provider} / ${destination.model}`;
}

export function destinationKindLabel(destination: Destination | null): string {
  if (!destination) {
    return "—";
  }
  return destination.external ? "外部模型" : "本地模型";
}

export function reasonLabel(code: string | null, detail: string | null): string {
  if (code === "DATA_LOCKED") {
    return "数据实例正在被另一个进程使用";
  }
  if (code === "CONFIG_MISSING") {
    return "未找到私有的宿主配置";
  }
  if (code === "CONFIG_INVALID") {
    return "宿主配置未通过校验";
  }
  if (code === "CONFIG_ENV_MISSING") {
    return "宿主配置引用的环境变量未设置";
  }
  if (code === "MODEL_AUTHORIZATION_MISSING") {
    return "没有匹配当前目的地的模型授权";
  }
  if (code === "MODEL_KEY_MISSING") {
    return "尚未保存模型 API Key";
  }
  if (code === "MODEL_KEY_INVALID") {
    return "保存的模型 API Key 无效";
  }
  if (code === "MODEL_NOT_AUTHORIZED") {
    return "模型调用没有通过 C09 发送门";
  }
  if (code === "MODEL_BUDGET_EXCEEDED") {
    return "模型授权额度已用尽";
  }
  if (code === "MODEL_AUTH_FAILED") {
    return "DeepSeek 鉴权失败，请检查 API Key";
  }
  if (code === "MODEL_RATE_LIMITED") {
    return "DeepSeek 限流或配额不足";
  }
  if (code === "MODEL_NETWORK") {
    return "无法连接 DeepSeek";
  }
  if (code === "MODEL_TIMEOUT") {
    return "DeepSeek 请求超时";
  }
  if (code === "MODEL_PROVIDER_ERROR") {
    return "DeepSeek 服务端错误";
  }
  if (code === "MODEL_INVALID_RESPONSE") {
    return "模型输出未通过协议校验";
  }
  if (code === "PROFILE_MISSING") {
    return "数据实例中没有启用的宿主 Profile";
  }
  if (code === "STORE_UNAVAILABLE") {
    return "数据实例无法打开";
  }
  if (code === "SIDECAR_UNAVAILABLE" || code === "SIDECAR_NOT_FOUND") {
    return "可信宿主未启动";
  }
  if (code === "MODEL_BUDGET_EXCEEDED") {
    return "模型授权额度已用尽";
  }
  if (detail) {
    return detail;
  }
  return code ?? "宿主未就绪";
}

export function detailLabel(detail: string | null): string {
  if (!detail) {
    return "未分类的未完成原因";
  }
  const [phase] = detail.split(":", 1);
  const map: Record<string, string> = {
    model: "模型调用未完成",
    preflight: "宿主能力未就绪",
    capability: "宿主能力未就绪",
    context: "上下文未能建立",
    query: "确定性查询未完成",
    query_artifact: "查询结果写入未完成",
    bind: "上下文绑定未完成",
    current_context: "上下文在完成前已变化",
    format: "模型输出格式未通过校验",
    attempt_evidence: "模型尝试证据缺失",
    attempt_evidence_missing: "模型尝试证据缺失",
    attempt_evidence_mismatch: "模型尝试证据不一致",
    reply_artifact: "回复写入未完成",
    interaction: "交互状态不完整",
    interaction_missing: "交互状态不可用",
    interaction_context_mismatch: "交互与上下文不一致",
    interaction_terminal: "交互已经结束",
    propose: "模型试图提交候选（Consult 不允许）",
    no_reply: "模型没有给出回答",
    second_query: "超出了本次允许的查询次数",
    generation_budget: "超出了本次允许的生成次数",
    query_budget: "超出了本次允许的查询次数",
    query_plan_missing: "模型请求查询，但宿主没有查询计划",
    query_result: "查询结果不可用",
    query_material: "查询材料不可用",
  };
  return map[phase] ?? "未分类的未完成原因";
}

export function materialNotice(query: QuerySummary | null): string | null {
  if (!query) {
    return null;
  }
  const status = query.status;
  const material = query.material_status;
  if (status === "ok" && material === "complete") {
    return null;
  }
  if (status === "ok" && material === "partial") {
    return "材料说明：当前可披露材料可能不完整；本次回答仅基于可披露内容。";
  }
  if (status === "empty" && material === "complete") {
    return "查询说明：本次查询没有可披露的结果。";
  }
  if (status === "empty" && material === "partial") {
    return "查询说明：本次查询没有可披露的结果；当前可披露范围可能不完整。";
  }
  if (status === "unavailable") {
    return "查询说明：本次查询结果不可用。";
  }
  return null;
}

export function queryStatusLabel(query: QuerySummary | null): string {
  if (!query) {
    return "本次没有确定性查询";
  }
  if (query.status === "ok") {
    return query.material_status === "partial"
      ? "查询完成；可披露材料可能不完整"
      : "查询完成";
  }
  if (query.status === "empty") {
    return "查询完成；没有可披露结果";
  }
  if (query.status === "unavailable") {
    return "查询结果不可用";
  }
  return "查询状态不可用";
}

export function sourceCountLabel(result: ConsultResult | null): string {
  if (!result) {
    return "来源：—";
  }
  const query = result.query;
  if (query === null) {
    return "来源：输入 1 · PCS 查询未执行";
  }
  if (query.status === "unavailable") {
    return "来源：输入 1 · PCS 查询结果不可用";
  }
  if (query.status === "empty") {
    return "来源：输入 1 · PCS 查询完成 · 可披露认识 0";
  }
  if (query.status === "ok") {
    const count = query.items.length;
    if (query.material_status === "partial") {
      return `来源：输入 1 · PCS 查询完成 · 可披露认识 ${count} · 材料可能不完整`;
    }
    return `来源：输入 1 · PCS 查询完成 · 可披露认识 ${count}`;
  }
  return "来源：输入 1 · PCS 查询状态不可用";
}

export interface SourceRow {
  key: string;
  kind: string;
  id: string;
  version: string;
  status: string;
  note: string;
}

function refKey(ref: TypedRef): string {
  return [
    ref.ref_type,
    ref.input_id ?? ref.artifact_id ?? ref.record_id ?? ref.event_id ?? "",
    ref.content_version ?? ref.record_version ?? ref.artifact_version ?? "",
  ].join(":");
}

function recordStatus(result: ConsultResult, ref: TypedRef): string {
  const item = result.query?.items.find(
    (candidate) =>
      candidate.ref.ref_type === "record" &&
      candidate.ref.record_id === ref.record_id &&
      candidate.ref.record_version === ref.record_version,
  );
  if (!item) {
    return "—";
  }
  if (item.expired) {
    return "已过期";
  }
  if (item.current) {
    return "当前";
  }
  return "历史";
}

export function sourceRows(result: ConsultResult | null): SourceRow[] {
  if (!result) {
    return [];
  }
  const rows: SourceRow[] = [];
  const inputRef = result.input.input_ref;
  rows.push({
    key: "input",
    kind: "输入",
    id: shortId(inputRef.input_id),
    version: `v${inputRef.content_version ?? 1}`,
    status: "本次登记",
    note: `${result.input.origin_kind} · ${result.input.privacy_class} · ${result.input.retention}`,
  });
  if (result.query_result_ref) {
    rows.push({
      key: "query-result",
      kind: "查询产物",
      id: shortId(result.query_result_ref.artifact_id),
      version: `v${result.query_result_ref.artifact_version ?? 1}`,
      status: "确定性查询",
      note: result.query?.truncated ? "查询结果已截断" : "只读投影",
    });
  }
  const seen = new Set<string>();
  for (const ref of result.query_source_refs) {
    const key = refKey(ref);
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    if (ref.ref_type === "record") {
      rows.push({
        key,
        kind: "记录",
        id: shortId(ref.record_id),
        version: `v${ref.record_version ?? 1}`,
        status: recordStatus(result, ref),
        note: "查询来源",
      });
    } else if (ref.ref_type === "event") {
      rows.push({
        key,
        kind: "事件",
        id: shortId(ref.event_id),
        version: "—",
        status: "—",
        note: "查询来源",
      });
    } else {
      rows.push({
        key,
        kind: "来源",
        id: "—",
        version: "—",
        status: "—",
        note: ref.ref_type,
      });
    }
  }
  if (result.reply_artifact_ref) {
    rows.push({
      key: "reply",
      kind: "回复产物",
      id: shortId(result.reply_artifact_ref.artifact_id),
      version: `v${result.reply_artifact_ref.artifact_version ?? 1}`,
      status: "—",
      note: "模型输出",
    });
  }
  return rows;
}

export function countCharacters(value: string): number {
  return Array.from(value).length;
}

export function phaseTitle(state: string): string {
  const map: Record<string, string> = {
    answered: "已回答",
    waiting: "等待（暂无完成回答）",
    failed: "未完成（failed）",
    incomplete: "未完成（incomplete）",
    stale: "已过期（stale）",
    unknown: "结果未知",
    busy: "已有一次咨询正在处理",
    input: "新的咨询",
    sending: "正在处理你的咨询…",
    "not-ready": "宿主未就绪",
    booting: "正在连接宿主…",
  };
  return map[state] ?? "咨询";
}
