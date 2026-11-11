/** Settings UI uses only fixed trusted-host commands. No filesystem or secrets. */
import { invoke } from "@tauri-apps/api/core";
import { el } from "./dom";
import { ChoiceControl } from "./choice";
import type { AppInfo, HostReply } from "./types";
import { applyAppearance, contrastRatio, DEFAULT_APPEARANCE, fontStack, foreground, importDisplayImage, type Appearance } from "./appearance";
import { openAvatarPreview } from "./avatarPreview";

const messages = {
  "zh-CN": {
    appearance: "界面偏好", language: "界面语言", theme: "主题", light: "日间", dark: "夜间", font: "字体", fontSystem: "系统无衬线", fontHumanist: "人文黑体", fontSerif: "宋体阅读", fontPreview: "字体预览 · 从一句话开始。 0123", lightAccent: "日间主题色", lightBackground: "日间背景色", wallpaper: "聊天背景", assistantAvatar: "助手头像", myAvatar: "我的头像", chooseImage: "选择图片", restoreDefault: "恢复默认", restoreColors: "恢复默认配色", viewAvatar: "查看头像", effective: "当前实际：模型 / SDRPE", persisted: "以下为已保存值",
    immediate: "应用范围 · 立即生效；不改变对话内容", save: "保存", saved: "已保存", unsaved: "未保存", contrastLow: "当前日间配色对比度较低，正文可能不易阅读。", saving: "正在保存；生成配置会等待当前轮次结束…", unknown: "保存结果未知；请重新读取当前值，不要盲目重试。",
    generation: "生成设置", next: "当前实例 · 下一轮生效；正在执行的轮次保持原配置",
    model: "模型", tokens: "每次请求最大输出 Token", fixed: "当前协议：temperature 固定为 0；不支持 top-p 调节。输出同时受预算与授权上限约束。", thinking: "当前宿主使用思考模式；temperature 不参与请求，暂不提供 top-p 调节。输出同时受预算与授权上限约束。",
    external: "模型与输出上限由外部宿主管理；当前仅可保存 SDRPE。", auto: "历史配置为 auto，尚未实现，实际为 off。请选择 off 或 on。",
    pending: "已保存；尚未确认应用。请刷新状态，勿重复提交。", failed: "保存失败；请检查宿主状态和输入值。", protocol: "注意：当前宿主使用 collab.v1，SDRPE 实际为 off；on 仅在支持的双阶段协议中生效。",
  },
  en: {
    appearance: "Interface preferences", language: "Interface language", theme: "Theme", light: "Light", dark: "Dark", font: "Font", fontSystem: "System sans", fontHumanist: "Humanist sans", fontSerif: "Serif reading", fontPreview: "Font preview · Start with a message. 0123", lightAccent: "Light accent", lightBackground: "Light background", wallpaper: "Chat background", assistantAvatar: "Assistant avatar", myAvatar: "My avatar", chooseImage: "Choose image", restoreDefault: "Use default", restoreColors: "Reset colors", viewAvatar: "View avatar", effective: "Currently active: model / SDRPE", persisted: "Saved values below",
    immediate: "Application · Immediate; conversation content stays unchanged", save: "Save", saved: "Saved", unsaved: "Unsaved", contrastLow: "The current light palette has low contrast; body text may be hard to read.", saving: "Saving; generation changes wait for the current turn…", unknown: "Save outcome unknown. Reload current values before retrying.",
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
let savedAppearance: Appearance = { ...DEFAULT_APPEARANCE };
const t = (key: keyof typeof messages.en): string => messages[language][key];
function applyPreferences(value: { language: Language; theme: string; appearance?: Appearance }): void {
  language = value.language;
  theme = value.theme;
  document.documentElement.lang = language;
  document.documentElement.dataset.theme = theme;
  savedAppearance = { ...DEFAULT_APPEARANCE, ...value.appearance };
  applyAppearance(savedAppearance, theme);
}
export async function loadInterfacePreferences(): Promise<void> {
  const reply = await invoke<HostReply>("ui_preferences");
  if (reply.ok) applyPreferences(reply.result as { language: Language; theme: string; appearance?: Appearance });
}

/** Quick day/night switch used by the topbar. Keeps the saved appearance intact. */
export async function toggleThemePreference(): Promise<{ ok: boolean; error?: string }> {
  const next = theme === "light" ? "dark" : "light";
  try {
    const reply = await invoke<HostReply>("save_ui_preferences", {
      language,
      theme: next,
      appearance: savedAppearance,
    });
    if (!reply.ok) {
      return { ok: false, error: reply.error?.message ?? "保存主题失败" };
    }
    applyPreferences(reply.result as { language: Language; theme: string; appearance?: Appearance });
    preferenceSaved = true;
    return { ok: true };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "保存主题失败" };
  }
}
function row(label: string, input: HTMLElement): HTMLElement {
  input.setAttribute("aria-label", label);
  const field = el("div", { class: "settings-field", children: [el("span", { class: "settings-label", text: label }), input] });
  return field;
}
function select(values: [string, string][], value: string, onChange?: (value: string) => void): ChoiceControl {
  return new ChoiceControl(values, value, onChange);
}
function section(title: string): HTMLElement {
  return el("section", { class: "drawer-section", children: [el("h3", { text: title })] });
}
function status(): HTMLParagraphElement { return el("p", { attrs: { role: "status", "aria-live": "polite" } }); }
export function interfaceSettings(refresh: () => void): HTMLElement {
  const box = section(t("appearance"));
  const feedback = status();
  const markDirty = (): void => {
    preferenceSaved = false;
    feedback.textContent = t("unsaved");
  };
  if (preferenceSaved) feedback.textContent = t("saved");
  const lang = select([["zh-CN", "简体中文"], ["en", "English"]], language, markDirty);
  const appearance = select([["light", t("light")], ["dark", t("dark")]], theme, markDirty);
  const draft = { ...savedAppearance };
  let importsInProgress = 0;
  const save = el("button", { class: "quiet-button", text: t("save"), attrs: { type: "button" } });
  save.onclick = async () => {
    save.disabled = true;
    feedback.textContent = t("saving");
    try {
      const reply = await invoke<HostReply>("save_ui_preferences", { language: lang.value, theme: appearance.value, appearance: draft });
      if (!reply.ok) throw new Error();
      applyPreferences(reply.result as { language: Language; theme: string; appearance?: Appearance });
      preferenceSaved = true;
      refresh();
    } catch { feedback.textContent = t("failed"); } finally { save.disabled = false; }
  };
  const fontPreview = el("p", { class: "appearance-font-preview", text: t("fontPreview") });
  const updateFontPreview = (): void => { fontPreview.style.fontFamily = fontStack(draft.font); };
  const font = select([
    ["system", t("fontSystem")],
    ["humanist", t("fontHumanist")],
    ["serif", t("fontSerif")],
  ], draft.font ?? "system", value => {
    draft.font = value;
    markDirty();
    updateFontPreview();
  });
  updateFontPreview();
  box.append(row(t("language"), lang.element), row(t("theme"), appearance.element), row(t("font"), font.element), fontPreview);
  const colors = el("div", { class: "appearance-colors" });
  const contrast = el("p", { class: "appearance-warning", attrs: { role: "status", "aria-live": "polite" } });
  const updateContrast = (): void => {
    const accent = draft.light_accent ?? "#a82e40";
    const background = draft.light_background ?? "#f5f4f1";
    const accentRatio = contrastRatio(foreground(accent), accent) ?? 0;
    const backgroundRatio = contrastRatio(foreground(background), background) ?? 0;
    const low = accentRatio < 4.5 || backgroundRatio < 4.5;
    contrast.hidden = !low;
    contrast.textContent = low ? t("contrastLow") : "";
  };
  for (const [key, label, fallback] of [
    ["light_accent", t("lightAccent"), "#a82e40"],
    ["light_background", t("lightBackground"), "#f5f4f1"],
  ] as const) {
    const input = el("input", { attrs: { type: "color" } });
    input.value = draft[key] ?? fallback;
    input.oninput = () => { draft[key] = input.value; markDirty(); updateContrast(); };
    colors.append(row(label, input));
  }
  updateContrast();
  box.append(colors, contrast);
  const images = el("div", { class: "appearance-images" });
  for (const [key, label] of [
    ["wallpaper", t("wallpaper")], ["assistant_avatar", t("assistantAvatar")], ["user_avatar", t("myAvatar")],
  ] as const) {
    const input = el("input", { attrs: { type: "file", accept: "image/png,image/jpeg,image/webp" } });
    input.className = "visually-hidden";
    input.setAttribute("aria-label", label);
    const choose = el("button", { class: "quiet-button", text: t("chooseImage"), attrs: { type: "button" } });
    choose.onclick = () => input.click();
    const preview = el("img", { class: `appearance-preview ${key === "wallpaper" ? "is-wallpaper" : ""}`, attrs: { alt: label } });
    const update = () => { preview.hidden = !draft[key]; if (draft[key]) preview.src = draft[key]!; else preview.removeAttribute("src"); };
    update();
    if (key !== "wallpaper") {
      const showPreview = () => openAvatarPreview({
        kind: key === "assistant_avatar" ? "assistant" : "user",
        label,
        letter: key === "assistant_avatar" ? "Q" : document.documentElement.lang === "en" ? "You" : "你",
        image: draft[key],
        trigger: preview,
      });
      preview.classList.add("appearance-preview-clickable");
      preview.tabIndex = 0;
      preview.setAttribute("role", "button");
      preview.setAttribute("aria-label", t("viewAvatar"));
      preview.onclick = showPreview;
      preview.onkeydown = event => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          showPreview();
        }
      };
    }
    input.onchange = async () => {
      const file = input.files?.[0];
      if (!file) return;
      importsInProgress++; input.disabled = true; choose.disabled = true; remove.disabled = true; save.disabled = true;
      try { draft[key] = await importDisplayImage(file, key !== "wallpaper"); update(); markDirty(); }
      catch (error) { feedback.textContent = error instanceof Error ? error.message : "无法载入图片"; }
      finally { importsInProgress--; input.disabled = false; choose.disabled = false; remove.disabled = false; save.disabled = importsInProgress > 0; }
    };
    const remove = el("button", { class: "quiet-button", text: t("restoreDefault"), attrs: { type: "button" } });
    remove.onclick = () => { draft[key] = null; input.value = ""; update(); markDirty(); };
    images.append(el("div", { class: "appearance-image-field", children: [el("h4", { text: label }), preview, input,
      el("div", { class: "appearance-actions", children: [choose, remove] }),
    ] }));
  }
  box.append(images);
  const resetColors = el("button", { class: "quiet-button", text: t("restoreColors"), attrs: { type: "button" } });
  resetColors.onclick = () => {
    draft.light_accent = null; draft.light_background = null;
    const inputs = box.querySelectorAll<HTMLInputElement>('input[type="color"]');
    inputs[0].value = "#a82e40"; inputs[1].value = "#f5f4f1";
    markDirty();
    updateContrast();
  };
  box.append(el("div", { class: "appearance-actions", children: [resetColors, save] }), feedback);
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
  const offer = info?.capabilities.model_renewal;
  if (info?.destination && offer) {
    const destination = info.destination;
    const chinese = language === "zh-CN";
    const renew = el("button", {
      class: "quiet-button",
      text: chinese ? `续领 ${offer.max_calls} 次模型调用` : `Authorize ${offer.max_calls} more model calls`,
      attrs: { type: "button" },
    });
    renew.onclick = async () => {
      const target = `${destination.provider} / ${destination.model}`;
      const prompt = chinese
        ? `为当前实例的 ${target} 再授权 ${offer.max_calls} 次调用？每次输出及运行时长上限沿用上一份授权。`
        : `Authorize ${offer.max_calls} more calls to ${target} for this instance? The previous output and run-time limits apply.`;
      if (!window.confirm(prompt)) return;
      renew.disabled = true;
      try {
        const reply = await invoke<HostReply>("renew_model_authorization", {
          sessionId: info.session_id,
          destinationId: destination.destination_id,
          authorizationId: offer.authorization_id,
        });
        if (!reply.ok) {
          feedback.textContent = reply.error?.message ?? t("failed");
          return;
        }
        feedback.textContent = chinese ? "新的模型授权已生效" : "Model authorization renewed";
        renew.remove();
      } catch {
        feedback.textContent = t("unknown");
      } finally {
        await refresh();
      }
    };
    box.append(renew);
  }
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
