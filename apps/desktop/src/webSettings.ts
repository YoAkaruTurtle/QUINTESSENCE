/**
 * RG08: the settings section for the real web switch.
 *
 * The section renders only what `web_settings` returned.  Its button is shown
 * only when the host said the configuration is writable, and clicking it calls
 * the real writer (`save_web_settings`) instead of changing a local boolean.
 * Nothing here reads an API key, a file path or the complete configuration.
 */
import { invoke } from "@tauri-apps/api/core";
import { clear, el } from "./dom";
import type { HostReply } from "./types";
import {
  webIntro,
  webLang,
  webPanelModel,
  webSectionTitle,
  type WebPanelModel,
  type WebStatus,
} from "./webSettingsView";

interface SaveWebResult {
  saved: boolean;
  applied: boolean;
  application?: HostReply;
  status?: WebStatus;
}

function kv(label: string, value: string): DocumentFragment {
  const fragment = document.createDocumentFragment();
  fragment.appendChild(el("dt", { text: label }));
  fragment.appendChild(el("dd", { text: value }));
  return fragment;
}

export function webSettingsPanel(refresh: () => Promise<void>): HTMLElement {
  const lang = webLang(document.documentElement.lang);
  const box = el("section", { class: "drawer-section" });
  box.append(
    el("h3", { text: webSectionTitle(lang) }),
    el("p", { class: "muted", text: webIntro(lang) }),
  );
  const body = el("div", { class: "web-settings-body" });
  const controls = el("div", { class: "setting-row", attrs: { id: "web-toggle-row" } });
  const feedback = el("p", {
    class: "setting-status",
    attrs: { id: "web-settings-status", role: "status", "aria-live": "polite" },
  });
  box.append(body, controls, feedback);
  let busy = false;

  const render = (status: WebStatus | null): WebPanelModel => {
    const model = webPanelModel(status, lang);
    clear(body);
    const meta = el("dl", { class: "kv", attrs: { id: "web-meta" } });
    meta.append(kv(lang === "en" ? "Status" : "状态", model.headline));
    for (const row of model.rows) {
      meta.append(kv(row.label, row.value));
    }
    body.append(meta);
    for (const note of model.notes) {
      body.append(el("p", { class: "muted", text: note }));
    }
    body.append(el("p", { class: "muted", text: model.editableNote }));
    clear(controls);
    if (model.toggleLabel !== null && status) {
      const button = el("button", {
        class: "quiet-button",
        text: model.toggleLabel,
        attrs: {
          type: "button",
          id: "web-toggle",
          "aria-pressed": status.enabled ? "true" : "false",
        },
      });
      button.disabled = busy;
      button.addEventListener("click", () => {
        if (busy) return;
        void save(!status.enabled);
      });
      controls.append(button);
    }
    return model;
  };

  const save = async (enabled: boolean): Promise<void> => {
    busy = true;
    clear(controls);
    const pending = el("button", {
      class: "quiet-button",
      text: lang === "en" ? "Saving…" : "正在保存…",
      attrs: { type: "button", disabled: true },
    });
    controls.append(pending);
    feedback.className = "setting-status";
    feedback.textContent = lang === "en" ? "Saving…" : "正在保存…";
    try {
      const reply = await invoke<HostReply>("save_web_settings", { enabled });
      if (!reply.ok) {
        feedback.textContent = reply.error?.message ?? (lang === "en" ? "Could not save." : "保存失败。");
        feedback.classList.add("is-error");
        await load();
        return;
      }
      const result = reply.result as SaveWebResult;
      await refresh();
      render(result.status ?? null);
      const applied = result.applied === true;
      feedback.className = "setting-status";
      if (!applied) {
        feedback.classList.add("is-error");
        const code = result.application?.error?.code ?? "HOST_NOT_READY";
        feedback.textContent =
          lang === "en"
            ? `Saved, but the host has not applied it yet (${code}). Restart the application after the current turn.`
            : `已保存，但宿主尚未应用（${code}）。请等当前轮次结束后重启应用或再次保存。`;
      } else {
        feedback.textContent = lang === "en" ? "Saved · next turn" : "已保存 · 下一轮生效";
      }
    } catch (error) {
      feedback.textContent = error instanceof Error ? error.message : String(error);
      feedback.classList.add("is-error");
      await load();
    } finally {
      busy = false;
      const toggle = controls.querySelector<HTMLButtonElement>("#web-toggle");
      if (toggle) toggle.disabled = false;
    }
  };

  const load = async (): Promise<void> => {
    try {
      const reply = await invoke<HostReply>("web_settings");
      if (!reply.ok) {
        feedback.textContent = reply.error?.message ?? (lang === "en" ? "Could not read the web status." : "无法读取联网状态。");
        feedback.classList.add("is-error");
        render(null);
        return;
      }
      render(reply.result as WebStatus);
    } catch (error) {
      feedback.textContent = error instanceof Error ? error.message : String(error);
      feedback.classList.add("is-error");
      render(null);
    }
  };

  void load();
  return box;
}
