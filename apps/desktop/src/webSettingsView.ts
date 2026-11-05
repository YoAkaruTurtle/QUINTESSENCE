/**
 * RG08: pure rendering rules for the web settings section.
 *
 * The panel must never call the surface "available" because a switch is on:
 * only the host's `configured` claim (switch on, credential present, at least
 * one tool) may say that.  Missing configuration is named explicitly, and no
 * credential value, host path or complete configuration is rendered here.
 */
import type { WebCapabilities } from "./types";

export type WebLang = "zh-CN" | "en";

/** The command DTO: host capability plus the two facts only the Rust host knows. */
export interface WebStatus extends WebCapabilities {
  managed?: boolean;
  editable?: boolean;
  sidecar_available?: boolean;
  credential_restart_required?: boolean;
  host_ready?: boolean;
  reason_code?: string | null;
  apply?: string;
}

export type WebState =
  | "available"
  | "enabled_no_credential"
  | "disabled"
  | "unconfigured"
  | "unavailable";

export interface WebRow {
  label: string;
  value: string;
}

export interface WebPanelModel {
  state: WebState;
  headline: string;
  rows: WebRow[];
  notes: string[];
  /** Null when the configuration is not writable by this window. */
  toggleLabel: string | null;
  editable: boolean;
  editableNote: string;
}

const copy = {
  title: ["联网", "Web access"],
  intro: [
    "搜索与抓取是宿主提供的联网工具。这里的开关直接改写宿主配置；下一轮生效，不打断正在执行的轮次。",
    "Search and fetch are host-provided web tools. This switch rewrites the host configuration; it applies from the next turn and never interrupts a running one.",
  ],
  headline: {
    available: ["可用", "Available"],
    enabled_no_credential: ["已开启 · 缺少凭据", "On · credential missing"],
    disabled: ["已关闭", "Disabled"],
    unconfigured: ["未配置", "Not configured"],
    unavailable: ["状态不可读", "Status unavailable"],
  },
  source: ["来源", "Source"],
  search: ["搜索", "Search"],
  fetch: ["抓取", "Fetch"],
  credential: ["凭据", "Credential"],
  hosts: ["允许主机", "Allowed hosts"],
  scope: ["应用范围", "Scope"],
  sourceLabels: {
    managed: ["软件托管", "Application managed"],
    external: ["外部宿主配置", "External host config"],
    lab: ["Lab 实验配置", "Lab configuration"],
  },
  on: ["已启用", "Enabled"],
  off: ["已停用", "Disabled"],
  unknown: ["—", "—"],
  credentialPresent: [
    "已提供（环境变量 {env}，未验证）",
    "Present (env {env}, unverified)",
  ],
  credentialMissing: ["未提供（环境变量 {env}）", "Missing (env {env})"],
  credentialUnknown: ["未知", "Unknown"],
  allowlist: ["{n} 个", "{n} allowlisted"],
  anyHost: ["不限（仅允许公网 HTTPS）", "Unrestricted (public HTTPS only)"],
  nextTurn: ["下一轮生效；当前轮次保持原配置", "Next turn; running turns keep their configuration"],
  notes: {
    available: [
      "凭据已提供不等于连接已验证；每次实际调用仍受宿主出口策略约束。",
      "A present credential is not a verified connection; each call still obeys the host egress policy.",
    ],
    disabled: [
      "联网已关闭：模型不会看到 web.search / web.fetch，宿主也不会主动联网。",
      "Web access is off: the model never sees web.search / web.fetch and the host makes no network call on its own.",
    ],
    unconfigured: [
      "宿主配置中没有 web 块；联网默认关闭。",
      "The host configuration has no web block; web access is off by default.",
    ],
    missingCredential: [
      "环境变量 {env} 未设置：实际搜索/抓取会返回“未配置”，不会伪造结果。",
      "Environment variable {env} is unset: a real search/fetch would return not-configured rather than invent a result.",
    ],
    managedCredential: [
      "该凭据由应用在启动时注入。先在“连接”中保存 API Key，然后重启应用。",
      "The application injects this credential at startup. Save an API key under Connection, then restart the application.",
    ],
    restartPending: [
      "API Key 已保存；重启应用后联网工具才能读取它。",
      "The API key is saved; restart the application before web tools can read it.",
    ],
    unavailable: [
      "无法读取宿主状态；这里不显示推测的联网状态。",
      "The host status could not be read; no guessed web state is shown.",
    ],
    externalWritable: [
      "修改会原子写回该宿主配置文件，只改动 web.enabled，其余字段保留。",
      "The change is written atomically into the host config; only web.enabled changes and every other field is kept.",
    ],
    labWritable: [
      "Lab 实验配置：修改会原子写回该文件，只改动 web.enabled；它不改变生产默认。",
      "Lab configuration: the change atomically rewrites only web.enabled; it does not change production defaults.",
    ],
    managedWritable: [
      "修改会写入应用设置并重建托管宿主配置；web 块中的其他字段（允许主机、超时、抓取方式）保留。",
      "The change is stored in application settings and the managed host config is regenerated; other web fields (allowlist, timeout, capture mode) are kept.",
    ],
    notWritable: [
      "当前宿主配置不可写：请由文件所有者修改配置中的 web 块（enabled、api_key_env）后重启应用。",
      "The host configuration is not writable here. Have its owner edit the web block (enabled, api_key_env), then restart the application.",
    ],
    noSource: [
      "宿主配置不可读或尚未建立，无法在此启停。",
      "The host configuration is unreadable or missing, so it cannot be toggled here.",
    ],
  },
} as const;

function text(pair: readonly [string, string], lang: WebLang): string {
  return pair[lang === "en" ? 1 : 0];
}

function format(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (_, key: string) =>
    key in values ? String(values[key]) : `{${key}}`,
  );
}

export function webState(status: WebStatus | null | undefined): WebState {
  if (!status || status.sidecar_available === false) return "unavailable";
  if (!status.source) return "unavailable";
  if (status.configured) return "available";
  if (status.enabled && !status.credential_present) return "enabled_no_credential";
  if (!status.declared) return "unconfigured";
  return "disabled";
}

export function webHeadline(state: WebState, lang: WebLang): string {
  return text(copy.headline[state], lang);
}

export function webSourceLabel(
  source: WebStatus["source"],
  lang: WebLang,
): string {
  if (!source) return text(copy.unknown, lang);
  return text(copy.sourceLabels[source], lang);
}

function hostPolicy(status: WebStatus, lang: WebLang): string {
  if (status.host_policy === "allowlist") {
    return format(text(copy.allowlist, lang), { n: status.allowed_hosts });
  }
  return text(copy.anyHost, lang);
}

function credential(status: WebStatus, lang: WebLang): string {
  const env = status.credential_env ?? "";
  if (!env) return text(copy.credentialUnknown, lang);
  return format(
    text(
      status.credential_present ? copy.credentialPresent : copy.credentialMissing,
      lang,
    ),
    { env },
  );
}

export function webPanelModel(
  status: WebStatus | null | undefined,
  lang: WebLang,
): WebPanelModel {
  const state = webState(status);
  const rows: WebRow[] = [];
  const notes: string[] = [];
  if (!status || state === "unavailable") {
    notes.push(text(copy.notes.unavailable, lang));
    return {
      state,
      headline: webHeadline(state, lang),
      rows,
      notes,
      toggleLabel: null,
      editable: false,
      editableNote: text(copy.notes.noSource, lang),
    };
  }
  rows.push({
    label: text(copy.source, lang),
    value: webSourceLabel(status.source, lang),
  });
  if (status.declared) {
    rows.push({
      label: text(copy.search, lang),
      value: text(status.search ? copy.on : copy.off, lang),
    });
    rows.push({
      label: text(copy.fetch, lang),
      value: text(status.fetch ? copy.on : copy.off, lang),
    });
  }
  rows.push({
    label: text(copy.credential, lang),
    value: credential(status, lang),
  });
  if (status.declared) {
    rows.push({
      label: text(copy.hosts, lang),
      value: hostPolicy(status, lang),
    });
  }
  rows.push({
    label: text(copy.scope, lang),
    value: text(copy.nextTurn, lang),
  });

  if (state === "available") notes.push(text(copy.notes.available, lang));
  if (state === "disabled") notes.push(text(copy.notes.disabled, lang));
  if (state === "unconfigured") {
    notes.push(text(copy.notes.unconfigured, lang));
  }
  if (state === "enabled_no_credential") {
    notes.push(
      format(text(copy.notes.missingCredential, lang), {
        env: status.credential_env ?? "",
      }),
    );
    if (status.managed || status.source === "managed") {
      notes.push(text(copy.notes.managedCredential, lang));
    }
  }
  if (status.credential_restart_required) {
    notes.push(text(copy.notes.restartPending, lang));
  }

  const editable = status.editable === true;
  let editableNote: string;
  if (!editable) {
    editableNote = text(copy.notes.notWritable, lang);
  } else if (status.managed || status.source === "managed") {
    editableNote = text(copy.notes.managedWritable, lang);
  } else if (status.source === "lab") {
    editableNote = text(copy.notes.labWritable, lang);
  } else {
    editableNote = text(copy.notes.externalWritable, lang);
  }
  const toggleLabel = editable
    ? status.enabled
      ? text(["关闭联网", "Turn web access off"], lang)
      : text(["启用联网", "Turn web access on"], lang)
    : null;
  return {
    state,
    headline: webHeadline(state, lang),
    rows,
    notes,
    toggleLabel,
    editable,
    editableNote,
  };
}

export function webSectionTitle(lang: WebLang): string {
  return text(copy.title, lang);
}

export function webIntro(lang: WebLang): string {
  return text(copy.intro, lang);
}

export function webLang(lang: string): WebLang {
  return lang === "en" ? "en" : "zh-CN";
}
