/**
 * UI-F02: pure view helpers for the persona and preference settings panels.
 *
 * The rules the review fixed live here, not in the DOM code:
 *
 * * a version is only "active" when the host's active pointer says so
 *   (`is_active` / `is_active_version`), never because `record_status` says
 *   `active` (every published version stores that);
 * * a preference is only "confirmed" inside the `confirmed` group; tentative
 *   values say "this conversation only, not remembered long-term" and inactive
 *   values say why they no longer apply;
 * * a missing capability is reported as unavailable, not simulated.
 *
 * Nothing in this module invents a row.  It maps wire data to display strings.
 */
import type {
  ActiveProfileRef,
  ImportFile,
  ImportList,
  ImportSummary,
  InactivePreference,
  PersonaCapabilities,
  PreferenceList,
  ProfileBody,
  ProfileList,
  ProfileSummary,
  ProfileVersionSummary,
} from "./types";

export type AdminLang = "zh-CN" | "en";

const copy = {
  personaTitle: ["人格管理", "Persona management"],
  personaHelp: [
    "默认只显示摘要。发布新版本会写入一条真实审核事件；发布不会自动激活。",
    "Summaries only by default. Publishing writes a real review event and never activates the version.",
  ],
  preferencesTitle: ["表达偏好", "Expression preferences"],
  preferencesHelp: [
    "只影响回答的长度、格式和语气；不改变工具、权限、隐私或任何外部行动。",
    "They only affect answer length, format and tone; never tools, permissions, privacy or external actions.",
  ],
  refresh: ["刷新", "Refresh"],
  loading: ["正在读取…", "Loading…"],
  unavailable: ["当前实例暂不提供", "Unavailable on this instance"],
  loadFailed: ["读取失败；没有显示推测内容。", "Could not load; nothing is guessed."],
  active: ["当前激活", "Active"],
  published: ["已发布", "Published"],
  currentVersion: ["当前版本", "Current version"],
  versions: ["版本", "Versions"],
  versionsTotal: ["共 {n} 个版本", "{n} version(s)"],
  truncated: ["列表已截断；只显示最新部分。", "List truncated; newest entries only."],
  principles: ["原则", "principles"],
  examples: ["示例", "examples"],
  schema: ["schema", "schema"],
  activeVersion: ["激活中", "Active version"],
  activate: ["激活此版本", "Activate this version"],
  activateConfirm: [
    "激活该人格版本？下一轮生效；正在执行的轮次保持原人格。",
    "Activate this persona version? It applies from the next turn; a running turn keeps its persona.",
  ],
  viewBody: ["查看完整正文", "Open full body"],
  bodyWarning: [
    "正文只在本次点击后显示，不写入普通日志或遥测。",
    "The body is shown only after this click; it is not written to logs or telemetry.",
  ],
  bodyDigest: ["正文摘要", "Body digest"],
  hideBody: ["收起正文", "Hide body"],
  noActive: ["没有激活的人格", "No active persona"],
  degraded: ["激活指针不可读", "Active pointer unreadable"],
  importTitle: ["导入已有配置", "Import an existing configuration"],
  importHelp: [
    "把人格 JSON 放到当前实例的 persona-import/ 目录；这里只列出文件名和修改时间，正文不会进入本窗口。",
    "Put a persona JSON into this instance's persona-import/ directory. Only file names and times are listed; the body stays in the host.",
  ],
  importDirMissing: [
    "实例目录下没有 persona-import/。先创建该目录并放入 JSON。",
    "The instance has no persona-import/ directory. Create it and add a JSON file.",
  ],
  importEmpty: ["目录里没有可导入的 JSON 文件。", "No importable JSON file in the directory."],
  importPreview: ["预览", "Preview"],
  importPublish: ["审核通过并发布", "Review and publish"],
  importPublishConfirm: [
    "确认这是要发布的人格正文？发布会写入真实审核事件并新增一个版本，不会激活。",
    "Publish this exact body? This writes a real review event and adds a version; it does not activate.",
  ],
  importReused: ["正文与当前最新版本相同，未新增版本。", "Same body as the newest version; no new version was added."],
  publishedNew: ["已发布新版本 r{v}。", "Published a new version r{v}."],
  importWarn: ["文件里的 review_ref 已由宿主替换为本次审核事件。", "The review_ref in the file was replaced by this review event."],
  digestLabel: ["摘要", "Digest"],
  sizeLabel: ["大小", "Size"],
  expires: ["草稿有效期", "Draft expires"],
  confirmedState: ["已确认 · 实例范围", "Confirmed · instance scope"],
  tentativeState: ["暂定 · 仅本会话，未长期记住", "Tentative · this conversation only, not remembered"],
  revokedState: ["已撤销", "Revoked"],
  supersededState: ["已被确认值取代", "Superseded by a confirmed value"],
  retractedState: ["已撤销（确认记录）", "Retracted (confirmed record)"],
  evidenceDeletedState: ["证据已删除，未生效", "Evidence deleted; not in effect"],
  sourceDeletedState: ["来源已删除，未生效", "Source deleted; not in effect"],
  unsupportedState: ["未生效", "Not in effect"],
  scope: ["作用范围", "Scope"],
  source: ["来源", "Source"],
  sourceUser: ["你在对话中的直接陈述", "Your direct statement in chat"],
  sourceUnknown: ["来源不可用", "Source unavailable"],
  thisConversation: ["仅本会话", "This conversation only"],
  instanceScope: ["实例范围", "Instance scope"],
  revoke: ["撤销", "Revoke"],
  revokeConfirm: [
    "撤销这条暂定偏好？之后不再用于回答；记录会保留为已撤销。",
    "Revoke this tentative preference? It stops applying; the record stays as revoked.",
  ],
  revokedOk: ["已撤销；下一次回答起不再使用。", "Revoked; it no longer applies from the next answer."],
  correctInChat: ["去对话中更正/撤销", "Correct or revoke in chat"],
  correctHelp: [
    "已确认的偏好会进入长期认知，设置页不提供直接改写或撤销按钮。请在对话里明确说明要更正或撤销哪一条；宿主会走既有的授权流程。",
    "A confirmed preference is long-term cognition, so this page does not edit or revoke it directly. State the correction in chat; the host uses its existing authorization path.",
  ],
  noConversation: [
    "历史暂定记录按会话保存；打开会话可查看或撤销。这些记录不参与新的回答。",
    "Legacy tentative records are per conversation. Open a conversation to view or revoke them; they are not used in new answers.",
  ],
  proposeDisabled: [
    "当前实例未开启认知提议（cognition.propose=false）；已有的已确认偏好仍可查看。不会记录新的暂定偏好。",
    "This instance does not enable cognition proposals (cognition.propose=false). Existing confirmed preferences remain readable; no new tentative preferences are recorded.",
  ],
  cognitionDisabled: [
    "认知功能未开启；已有偏好记录仍可查看，不会自动形成新的认知偏好。",
    "Cognition is disabled. Existing preference records remain readable; new cognition preferences are not formed automatically.",
  ],
  recordingDisabled: [
    "本实例不会记录新的暂定表达偏好（该能力已停用）。",
    "This instance does not record new tentative expression preferences (the mechanism is retired).",
  ],
  noneConfirmed: ["没有已确认的表达偏好。", "No confirmed expression preferences."],
  noneTentative: ["本会话没有暂定偏好。", "No tentative preference in this conversation."],
  noneInactive: ["没有失效记录。", "No inactive records."],
  noManagement: [
    "宿主未开启人格管理（app.persona_control）。这里只显示摘要，不提供发布、导入或正文。",
    "The host has not enabled persona management (app.persona_control). Only the summary is shown.",
  ],
  version: ["版本", "Version"],
  confirmHelp: ["发布前请在预览里核对该正文。", "Review the preview before publishing."],
  busy: ["本轮运行中，结束后可读。", "A turn is running; readable when it settles."],
  errorPERSONACONTROLDISABLED: ["宿主未开启人格管理。", "Persona management is disabled on this host."],
  errorIMPORTUNAVAILABLE: ["实例没有 persona-import/ 目录。", "The instance has no persona-import/ directory."],
  errorIMPORTINVALID: ["导入文件不符合人格正文格式。", "The imported file is not a valid profile body."],
  errorPREFERENCENOTACTIVE: ["该偏好已不是暂定状态，不能撤销。", "The preference is no longer tentative."],
  errorCONFIRMATIONREQUIRED: ["需要显式确认。", "Explicit confirmation is required."],
  errorBUSY: ["本轮运行中，请结束后再试。", "A turn is running; try again when it settles."],
  errorNOTFOUND: ["找不到该记录，可能已被删除或过期。", "Not found; it may be deleted or expired."],
  errorREDACTED: ["该记录已被删除。", "The record was deleted."],
  errorSESSIONMISMATCH: ["宿主会话已重建，请刷新。", "The host session changed; refresh."],
  errorHOSTNOTREADY: ["宿主未就绪。", "The host is not ready."],
  errorINVALIDPARAMS: ["请求参数不被接受。", "The request was not accepted."],
  errorSOURCEUNTRUSTED: ["审核事件与正文不匹配。", "The review event does not match the body."],
  errorDATALOCKED: ["另一个进程拥有该实例。", "Another process owns this instance."],
  statusLabel: ["状态", "Status"],
  readyState: ["就绪", "Ready"],
  notReadyState: ["未就绪", "Not ready"],
  currentPersona: ["当前人格", "Current persona"],
  sourceLabel: ["来源", "Source"],
  assetLabel: ["资产哈希", "Asset hash"],
  speakerLabel: ["当前交谈者", "Current speaker"],
  expectedLabel: ["启动契约", "Startup contract"],
  matchesLabel: ["与启动契约一致", "Matches contract"],
  yes: ["是", "Yes"],
  no: ["否", "No"],
  notConfigured: ["未配置", "Not configured"],
  sourceExpected: ["启动契约指定", "Declared by startup contract"],
  sourceActive: ["当前激活版本", "Active version"],
  sourceNeutral: ["中性默认（宿主初始化）", "Neutral default (host provisioned)"],
  confirmedGroup: ["已确认", "Confirmed"],
  tentativeGroup: ["暂定（未长期记住）", "Tentative (not remembered)"],
  inactiveGroup: ["已失效", "Inactive"],
  activatedOk: ["已激活；下一轮生效。", "Activated; applies from the next turn."],
} as const;

export type AdminTextKey = keyof typeof copy;

export function t(lang: AdminLang, key: AdminTextKey): string {
  return copy[key][lang === "en" ? 1 : 0];
}

export function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) {
    return "—";
  }
  if (value < 1024) {
    return `${Math.round(value)} B`;
  }
  const kib = value / 1024;
  return kib < 1024 ? `${kib.toFixed(1)} KiB` : `${(kib / 1024).toFixed(1)} MiB`;
}

export function formatDigest(digest: string | null | undefined): string {
  if (!digest) {
    return "—";
  }
  return digest.length > 16 ? `${digest.slice(0, 10)}…${digest.slice(-6)}` : digest;
}

export function formatTime(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  return value.replace("T", " ").replace(/(\.\d+)?Z$/, "Z");
}

export interface VersionRow {
  key: string;
  recordVersion: number;
  profileVersion: string | null;
  schema: string | null;
  recordedAt: string;
  isActiveVersion: boolean;
  reviewEventId: string | null;
  label: string;
  meta: string;
}

export interface ProfileRow {
  key: string;
  profileId: string;
  isActive: boolean;
  recordVersion: number;
  profileVersion: string | null;
  schema: string | null;
  principles: number;
  examples: number;
  assetHash: string | null;
  versions: VersionRow[];
  versionsTotal: number;
  truncated: boolean;
}

function versionRow(
  version: ProfileVersionSummary,
  lang: AdminLang,
): VersionRow {
  const label = `r${version.record_version} · v${version.profile_version ?? "—"}`;
  const meta = [
    `${t(lang, "schema")} ${version.schema_version ?? "—"}`,
    formatTime(version.recorded_at),
    version.is_active_version ? t(lang, "activeVersion") : "",
  ]
    .filter((item) => item)
    .join(" · ");
  return {
    key: `v${version.record_version}`,
    recordVersion: version.record_version,
    profileVersion: version.profile_version,
    schema: version.schema_version,
    recordedAt: version.recorded_at,
    isActiveVersion: version.is_active_version === true,
    reviewEventId: version.review_event_id,
    label,
    meta,
  };
}

function profileRow(profile: ProfileSummary, lang: AdminLang): ProfileRow {
  return {
    key: profile.record_id,
    profileId: profile.profile_id,
    isActive: profile.is_active === true,
    recordVersion: profile.current.record_version,
    profileVersion: profile.current.profile_version,
    schema: profile.current.schema_version,
    principles: profile.current.principles_count,
    examples: profile.current.examples_count,
    assetHash: profile.current.asset_hash,
    versions: profile.versions.map((item) => versionRow(item, lang)),
    versionsTotal: profile.versions_total,
    truncated: profile.versions_truncated === true,
  };
}

export function profileRows(list: ProfileList | null, lang: AdminLang): ProfileRow[] {
  if (!list || !Array.isArray(list.profiles)) {
    return [];
  }
  return list.profiles.map((profile) => profileRow(profile, lang));
}

export interface ActiveSummary {
  profileId: string;
  recordVersion: number;
  degraded: boolean;
  label: string;
}

export function activeSummary(
  active: ActiveProfileRef | null,
  lang: AdminLang,
): ActiveSummary | null {
  if (!active) {
    return null;
  }
  return {
    profileId: active.profile_id ?? "—",
    recordVersion: active.record_version,
    degraded: active.degraded === true,
    label: active.degraded
      ? t(lang, "degraded")
      : `${active.profile_id ?? "—"} · r${active.record_version}`,
  };
}

export function profileSummaryLine(row: ProfileRow, lang: AdminLang): string {
  return [
    `r${row.recordVersion}`,
    `v${row.profileVersion ?? "—"}`,
    `${row.principles} ${t(lang, "principles")}`,
    `${row.examples} ${t(lang, "examples")}`,
  ].join(" · ");
}

export function canReadBody(caps: PersonaCapabilities | undefined): boolean {
  return caps?.read_body === true;
}

export function canImport(caps: PersonaCapabilities | undefined): boolean {
  return caps?.import === true;
}

export function canPublish(caps: PersonaCapabilities | undefined): boolean {
  return caps?.publish === true;
}

export function personaCapabilityNote(lang: AdminLang): string {
  return t(lang, "noManagement");
}

export function canActivate(caps: PersonaCapabilities | undefined): boolean {
  return caps?.activate === true;
}

export interface ImportRow {
  key: string;
  name: string;
  size: string;
  modifiedAt: string;
}

export function importRows(list: ImportList | null, lang: AdminLang): ImportRow[] {
  if (!list || !Array.isArray(list.files)) {
    return [];
  }
  return list.files.map((file: ImportFile) => ({
    key: file.name,
    name: file.name,
    size: formatBytes(file.size_bytes),
    modifiedAt: formatTime(file.modified_at),
  }));
}

export function importAvailability(
  list: ImportList | null,
  lang: AdminLang,
): { available: boolean; message: string; rows: ImportRow[] } {
  if (!list) {
    return { available: false, message: t(lang, "loading"), rows: [] };
  }
  if (!list.available) {
    return { available: false, message: t(lang, "importDirMissing"), rows: [] };
  }
  const rows = importRows(list, lang);
  return {
    available: true,
    message: rows.length === 0 ? t(lang, "importEmpty") : "",
    rows,
  };
}

export function importSummaryRows(
  summary: ImportSummary,
  lang: AdminLang,
): Array<{ label: string; value: string }> {
  return [
    { label: "profile_id", value: summary.profile_id },
    { label: t(lang, "version"), value: summary.profile_version ?? "—" },
    { label: t(lang, "principles"), value: String(summary.principles_count) },
    { label: t(lang, "examples"), value: String(summary.examples_count) },
    { label: t(lang, "digestLabel"), value: formatDigest(summary.body_sha256) },
    { label: t(lang, "sizeLabel"), value: formatBytes(summary.size_bytes) },
  ];
}

export function bodyText(body: ProfileBody): string {
  try {
    return JSON.stringify(body.body, null, 2);
  } catch {
    return "";
  }
}

export function scopeLabel(scope: Record<string, unknown> | null, lang: AdminLang): string {
  if (!scope || Object.keys(scope).length === 0) {
    return t(lang, "instanceScope");
  }
  if (Object.keys(scope).length === 1 && typeof scope.session_id === "string") {
    return t(lang, "thisConversation");
  }
  return Object.entries(scope)
    .map(([key, value]) => `${key}=${String(value)}`)
    .join(", ");
}

export interface PreferenceRow {
  key: string;
  kind: "confirmed" | "tentative" | "inactive";
  title: string;
  detail: string;
  stateLabel: string;
  dimension: string;
  canRevoke: boolean;
  scope: string;
  sourceLabel: string;
  recordLabel: string;
}

export interface PreferenceSections {
  confirmed: PreferenceRow[];
  tentative: PreferenceRow[];
  inactive: PreferenceRow[];
  notes: string[];
}

function recordLabel(record: { record_id: string; record_version: number }): string {
  return `r${record.record_version} · ${record.record_id.slice(0, 8)}`;
}

export function inactiveStateLabel(state: string, lang: AdminLang): string {
  switch (state) {
    case "revoked":
      return t(lang, "revokedState");
    case "superseded":
      return t(lang, "supersededState");
    case "retracted":
      return t(lang, "retractedState");
    case "evidence_deleted":
      return t(lang, "evidenceDeletedState");
    case "source_deleted":
      return t(lang, "sourceDeletedState");
    default:
      return t(lang, "unsupportedState");
  }
}

function sourceLabel(source: { kind: string } | null, lang: AdminLang): string {
  return source && source.kind === "user_statement"
    ? t(lang, "sourceUser")
    : t(lang, "sourceUnknown");
}

export function preferenceSections(
  list: PreferenceList | null,
  lang: AdminLang,
): PreferenceSections {
  if (!list) {
    return { confirmed: [], tentative: [], inactive: [], notes: [] };
  }
  const confirmed = (list.confirmed ?? []).map((item) => ({
    key: `c-${item.record.record_id}-${item.record.record_version}`,
    kind: "confirmed" as const,
    title: item.label,
    detail: item.claim ?? "",
    stateLabel: t(lang, "confirmedState"),
    dimension: item.dimension,
    canRevoke: false,
    scope: scopeLabel(item.scope, lang),
    sourceLabel: sourceLabel(item.source, lang),
    recordLabel: recordLabel(item.record),
  }));
  const tentative = (list.tentative ?? []).map((item) => ({
    key: `t-${item.record.record_id}-${item.record.record_version}`,
    kind: "tentative" as const,
    title: item.label,
    detail: "",
    stateLabel: t(lang, "tentativeState"),
    dimension: item.dimension,
    canRevoke: true,
    scope: scopeLabel(item.scope, lang),
    sourceLabel: sourceLabel(item.source, lang),
    recordLabel: recordLabel(item.record),
  }));
  const inactive = (list.inactive ?? []).map(
    (item: InactivePreference) => ({
      key: `i-${item.kind}-${item.record.record_id}-${item.record.record_version}`,
      kind: "inactive" as const,
      title: item.label ?? item.dimension ?? "—",
      detail: item.reason ?? "",
      stateLabel: inactiveStateLabel(item.state, lang),
      dimension: item.dimension ?? "",
      canRevoke: false,
      scope: scopeLabel(item.scope, lang),
      sourceLabel: t(lang, "sourceUnknown"),
      recordLabel: recordLabel(item.record),
    }),
  );
  const notes: string[] = [];
  if (!list.expression_recording?.enabled) {
    notes.push(t(lang, "recordingDisabled"));
  }
  if (!list.confirmed_recording?.enabled) {
    notes.push(t(lang, list.confirmed_recording?.reason === "COGNITION_DISABLED" ? "cognitionDisabled" : "proposeDisabled"));
  }
  if (!list.conversation) {
    notes.push(t(lang, "noConversation"));
  }
  if (list.truncated) {
    notes.push(t(lang, "truncated"));
  }
  return { confirmed, tentative, inactive, notes };
}

export function errorLabel(
  code: string | null | undefined,
  lang: AdminLang,
): string | null {
  if (!code) {
    return null;
  }
  const key = `error${code.replace(/[^A-Z]/g, "")}` as AdminTextKey;
  return key in copy ? t(lang, key) : null;
}
