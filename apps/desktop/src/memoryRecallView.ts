/**
 * RG08 R-B: DOM-free rendering rules for the automatic-recall settings.
 *
 * The panel must not call recall "configured" because a switch is on.  It shows
 * the host's own configured/bridge facts, the bounded values, and an explicit
 * next-turn/restart note.  No path, credential, upstream ref or memory content
 * is rendered here.
 */

export type RecallLang = "zh-CN" | "en";

export interface MemoryRecallStatus {
  enabled: boolean;
  configured: boolean;
  declared: boolean;
  bridge_configured: boolean;
  max_items: number;
  budget_units: number;
  timeout_seconds: number;
  timezone: string;
  source: "managed" | "external" | "lab" | null;
  missing: string[];
  apply?: string;
  managed?: boolean;
  editable?: boolean;
  sidecar_available?: boolean;
  host_ready?: boolean;
}

export interface RecallRow {
  label: string;
  value: string;
}

export interface RecallPanelModel {
  headline: string;
  rows: RecallRow[];
  notes: string[];
  editable: boolean;
  editableNote: string;
  values: {
    enabled: boolean;
    max_items: number;
    budget_units: number;
    timeout_ms: number;
    timezone: string;
  };
}

export const TIMEZONES = ["Asia/Shanghai", "UTC"] as const;

const copy = {
  title: ["记忆自动召回", "Automatic memory recall"],
  intro: [
    "在每轮聊天开始时，用当前问题对过去的经历做一次有界字面检索；命中少量相关经历作为历史材料进入本轮 Context。默认关闭。",
    "At the start of a chat turn, run one bounded literal lookup over past experience and place a few relevant items into this turn's Context as historical material. Off by default.",
  ],
  headline: {
    available: ["已开启", "On"],
    disabled: ["已关闭", "Off"],
    unavailable: ["不可用", "Unavailable"],
  },
  source: ["来源", "Source"],
  bridge: ["记忆桥", "Memory bridge"],
  maxItems: ["最多条数", "Max items"],
  budget: ["预算 (PCS U)", "Budget (PCS U)"],
  timeout: ["整轮超时 (ms)", "Turn timeout (ms)"],
  timezone: ["时区", "Time zone"],
  status: ["状态", "Status"],
  sourceLabels: {
    managed: ["软件托管", "Application managed"],
    external: ["外部宿主配置", "External host config"],
    lab: ["Lab 实验配置", "Lab configuration"],
  },
  on: ["已启用", "Enabled"],
  off: ["已停用", "Disabled"],
  configured: ["可用", "Ready"],
  notConfigured: ["未就绪", "Not ready"],
  nextTurn: [
    "下一轮生效；当前轮次保持原配置。不需要重启。",
    "Next turn; running turns keep their configuration. No restart needed.",
  ],
  notes: {
    disabled: [
      "自动召回已关闭；显式 memory.search 仍可用。",
      "Automatic recall is off; explicit memory.search remains available.",
    ],
    noBridge: [
      "记忆桥未配置或未授权：自动召回不会运行，聊天照常。",
      "The memory bridge is not configured or authorized: automatic recall stays off and chat continues.",
    ],
    notWritable: [
      "当前宿主配置不可写：请由文件所有者修改 memory.recall 后重启应用。",
      "This host configuration is not writable. Have its owner edit memory.recall, then restart the application.",
    ],
    writable: [
      "保存只改 memory 块中的召回/时区字段，其他字段保留。",
      "Saving changes only the recall/time-zone fields in the memory block; every other field is kept.",
    ],
  },
} as const;

function pick(pair: readonly [string, string], lang: RecallLang): string {
  return pair[lang === "en" ? 1 : 0];
}

export function recallLang(value: string): RecallLang {
  return value === "en" ? "en" : "zh-CN";
}

export function recallSectionTitle(lang: RecallLang): string {
  return pick(copy.title, lang);
}

export function recallIntro(lang: RecallLang): string {
  return pick(copy.intro, lang);
}

export function recallPanelModel(
  status: MemoryRecallStatus | null | undefined,
  lang: RecallLang,
): RecallPanelModel {
  const fallback = {
    enabled: false,
    max_items: 4,
    budget_units: 2048,
    timeout_ms: 2000,
    timezone: "Asia/Shanghai",
  };
  if (!status) {
    return {
      headline: pick(copy.headline.unavailable, lang),
      rows: [],
      notes: [pick(copy.notes.notWritable, lang)],
      editable: false,
      editableNote: pick(copy.notes.notWritable, lang),
      values: fallback,
    };
  }
  const editable = status.editable === true;
  const values = {
    enabled: status.enabled === true,
    max_items: status.max_items,
    budget_units: status.budget_units,
    timeout_ms: Math.round(status.timeout_seconds * 1000),
    timezone: status.timezone,
  };
  const headline = !status.bridge_configured
    ? pick(copy.headline.unavailable, lang)
    : status.enabled
      ? pick(copy.headline.available, lang)
      : pick(copy.headline.disabled, lang);
  const rows: RecallRow[] = [
    {
      label: pick(copy.source, lang),
      value: status.source
        ? pick(copy.sourceLabels[status.source], lang)
        : "—",
    },
    {
      label: pick(copy.bridge, lang),
      value: status.bridge_configured
        ? pick(copy.configured, lang)
        : pick(copy.notConfigured, lang),
    },
    { label: pick(copy.maxItems, lang), value: String(status.max_items) },
    { label: pick(copy.budget, lang), value: String(status.budget_units) },
    {
      label: pick(copy.timeout, lang),
      value: `${Math.round(status.timeout_seconds * 1000)} ms`,
    },
    { label: pick(copy.timezone, lang), value: status.timezone },
    {
      label: pick(copy.status, lang),
      value: status.enabled ? pick(copy.on, lang) : pick(copy.off, lang),
    },
  ];
  const notes: string[] = [];
  if (!status.bridge_configured) notes.push(pick(copy.notes.noBridge, lang));
  else if (!status.enabled) notes.push(pick(copy.notes.disabled, lang));
  notes.push(pick(copy.nextTurn, lang));
  if (editable) notes.push(pick(copy.notes.writable, lang));
  return {
    headline,
    rows,
    notes,
    editable,
    editableNote: editable
      ? pick(copy.notes.writable, lang)
      : pick(copy.notes.notWritable, lang),
    values,
  };
}
