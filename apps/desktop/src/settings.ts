/** Settings UI uses only fixed trusted-host commands. No filesystem or secrets. */
import { invoke } from "@tauri-apps/api/core";
import { el } from "./dom";
import { ChoiceControl } from "./choice";
import type { AppInfo, HostReply } from "./types";

const messages = {
  "zh-CN": {
    appearance: "界面偏好", language: "界面语言", theme: "主题", light: "日间", dark: "夜间", effective: "当前实际：模型 / SDRPE", persisted: "以下为已保存值",
    immediate: "应用范围 · 立即生效；不改变对话内容", save: "保存", saved: "已保存", saving: "正在保存；生成配置会等待当前轮次结束…", unknown: "保存结果未知；请重新读取当前值，不要盲目重试。",
    generation: "生成设置", next: "当前实例 · 下一轮生效；正在执行的轮次保持原配置",
    model: "模型", tokens: "每次请求最大输出 Token", fixed: "当前协议：temperature 固定为 0；不支持 top-p 调节。输出同时受预算与授权上限约束。", thinking: "当前宿主使用思考模式；temperature 不参与请求，暂不提供 top-p 调节。输出同时受预算与授权上限约束。",
    external: "模型与输出上限由外部宿主管理；当前仅可保存 SDRPE。", auto: "历史配置为 auto，尚未实现，实际为 off。请选择 off 或 on。",
    pending: "已保存；尚未确认应用。请刷新状态，勿重复提交。", failed: "保存失败；请检查宿主状态和输入值。", protocol: "注意：当前宿主使用 collab.v1，SDRPE 实际为 off；on 仅在支持的双阶段协议中生效。",
  },
  en: {
    appearance: "Interface preferences", language: "Interface language", theme: "Theme", light: "Light", dark: "Dark", effective: "Currently active: model / SDRPE", persisted: "Saved values below",
    immediate: "Application · Immediate; conversation content stays unchanged", save: "Save", saved: "Saved", saving: "Saving; generation changes wait for the current turn…", unknown: "Save outcome unknown. Reload current values before retrying.",
    generation: "Generation settings", next: "Current instance · Next turn; running turns keep their configuration",
    model: "Model", tokens: "Maximum output tokens per request", fixed: "Current protocol: temperature is fixed at 0; top-p is unavailable. Budget and authorization limits also apply.", thinking: "This host uses thinking mode. Temperature is not sent; top-p adjustment is unavailable. Budget and authorization limits still apply.",
    external: "The external host manages model and output limits. Only SDRPE can be saved here.", auto: "Legacy auto mode is unavailable; effective mode is off. Choose off or on.",
    pending: "Saved; application is not confirmed. Refresh status before retrying.", failed: "Could not save. Check the host status and values.", protocol: "This host uses collab.v1: effective SDRPE is off. On requires a supported two-stage protocol.",
  },
} as const;
type Language = keyof typeof messages;
let language: Language = "zh-CN";
let theme = "dark";
let preferenceSaved = false;
const t = (key: keyof typeof messages.en): string => messages[language][key];
function applyPreferences(value: { language: Language; theme: string }): void {
  language = value.language;
  theme = value.theme;
  document.documentElement.lang = language;
  document.documentElement.dataset.theme = theme;
}
export async function loadInterfacePreferences(): Promise<void> {
  const reply = await invoke<HostReply>("ui_preferences");
  if (reply.ok) applyPreferences(reply.result as { language: Language; theme: string });
}
function row(label: string, input: HTMLElement): HTMLElement {
  input.setAttribute("aria-label", label);
  const field = el("div", { class: "settings-field", children: [el("span", { class: "settings-label", text: label }), input] });
  return field;
}
function select(values: [string, string][], value: string): ChoiceControl { return new ChoiceControl(values, value); }
function section(title: string): HTMLElement {
  return el("section", { class: "drawer-section", children: [el("h3", { text: title })] });
}
function status(): HTMLParagraphElement { return el("p", { attrs: { role: "status", "aria-live": "polite" } }); }
export function interfaceSettings(refresh: () => void): HTMLElement {
  const box = section(t("appearance"));
  const lang = select([["zh-CN", "简体中文"], ["en", "English"]], language);
  const appearance = select([["light", t("light")], ["dark", t("dark")]], theme);
  const feedback = status();
  if (preferenceSaved) feedback.textContent = t("saved");
  const save = el("button", { class: "quiet-button", text: t("save"), attrs: { type: "button" } });
  save.onclick = async () => {
    save.disabled = true;
    try {
      const reply = await invoke<HostReply>("save_ui_preferences", { language: lang.value, theme: appearance.value });
      if (!reply.ok) throw new Error();
      applyPreferences(reply.result as { language: Language; theme: string });
      preferenceSaved = true;
      refresh();
    } catch { feedback.textContent = t("failed"); } finally { save.disabled = false; }
  };
  box.append(row(t("language"), lang.element), row(t("theme"), appearance.element), el("p", { text: t("immediate") }), save, feedback);
  return box;
}
interface GenerationSettings { managed: boolean; model: string; model_choices: string[]; sdrpe_mode: string; max_output_tokens: number; thinking?: string }
export function generationSettings(refresh: () => Promise<void>, info?: AppInfo | null): HTMLElement {
  const box = section(t("generation"));
  const feedback = status();
  box.append(el("p", { text: t("next") }), feedback);
  const actual = el("p");
  const showEffective = (host?: AppInfo | null): void => {
    const expression = (host?.capabilities as { expression?: { effective_mode: string } } | undefined)?.expression;
    actual.textContent = `${t("effective")}: ${host?.destination?.model ?? "—"} / ${expression?.effective_mode ?? "—"}`;
  };
  showEffective(info);
  box.append(actual, el("p", { text: t("persisted") }));
  const expression = (info?.capabilities as { expression?: { resolved_reason: string | null } } | undefined)?.expression;
  if (expression?.resolved_reason === "protocol_v1") box.append(el("p", { text: t("protocol") }));
  void (async () => {
    try {
      const reply = await invoke<HostReply>("generation_settings");
      if (!reply.ok) throw new Error();
      const current = reply.result as GenerationSettings;
      const model = select((current.managed ? current.model_choices : [current.model]).map(v => [v, v]), current.model);
      model.disabled = !current.managed;
      const mode = select([["off", "off"], ["on", "on"]], current.sdrpe_mode);
      if (current.sdrpe_mode === "auto") { mode.selectedIndex = -1; box.append(el("p", { text: t("auto") })); }
      const tokens = el("input", { class: "setting-input", attrs: { type: "number", min: "1", max: "393216", step: "1" } });
      tokens.value = String(current.max_output_tokens);
      tokens.disabled = !current.managed;
      const save = el("button", { class: "quiet-button", text: t("save"), attrs: { type: "button" } });
      save.onclick = async () => {
        if (!tokens.checkValidity() || !["off", "on"].includes(mode.value)) { tokens.reportValidity(); feedback.textContent = t("failed"); return; }
        save.disabled = true; feedback.textContent = t("saving");
        model.disabled = true; mode.disabled = true; tokens.disabled = true;
        try {
          const result = await invoke<HostReply>("save_generation_settings", { model: model.value, sdrpeMode: mode.value, maxOutputTokens: Number(tokens.value) });
          if (!result.ok) { feedback.textContent = result.error?.message ?? t("failed"); return; }
          const outcome = result.result as { saved: boolean; application: HostReply };
          feedback.textContent = outcome.application.ok ? t("saved") : t("pending");
          await refresh();
          const host = await invoke<HostReply>("app_info").catch(() => null);
          if (host?.ok) showEffective(host.result as AppInfo);
        } catch { feedback.textContent = t("unknown"); } finally {
          save.disabled = false; mode.disabled = false;
          model.disabled = !current.managed; tokens.disabled = !current.managed;
        }
      };
      box.append(row(t("model"), model.element), row("SDRPE", mode.element), row(t("tokens"), tokens), el("p", { text: t(current.thinking === "enabled" ? "thinking" : "fixed") }));
      if (!current.managed) box.append(el("p", { text: t("external") }));
      box.append(save);
    } catch { feedback.textContent = t("failed"); }
  })();
  return box;
}
