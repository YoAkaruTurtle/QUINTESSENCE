/**
 * RG08 R-B: the settings panel for bounded literal automatic recall.
 *
 * All rendering rules live in `memoryRecallView.ts`; this module only builds
 * DOM, calls the real Rust read/write commands and shows the host's answer.
 * It never shows a path, credential, upstream ref or memory content.
 */
import { invoke } from "@tauri-apps/api/core";
import { clear, el } from "./dom";
import type { HostReply } from "./types";
import {
  TIMEZONES,
  recallIntro,
  recallLang,
  recallPanelModel,
  recallSectionTitle,
  type MemoryRecallStatus,
  type RecallPanelModel,
} from "./memoryRecallView";

interface SaveRecallResult {
  saved: boolean;
  applied: boolean;
  application?: HostReply;
  status?: MemoryRecallStatus;
}

function kv(label: string, value: string): DocumentFragment {
  const fragment = document.createDocumentFragment();
  fragment.appendChild(el("dt", { text: label }));
  fragment.appendChild(el("dd", { text: value }));
  return fragment;
}

export function memoryRecallSettingsPanel(refresh: () => Promise<void>): HTMLElement {
  const lang = recallLang(document.documentElement.lang);
  const box = el("section", { class: "drawer-section" });
  box.append(
    el("h3", { text: recallSectionTitle(lang) }),
    el("p", { class: "muted", text: recallIntro(lang) }),
  );
  const body = el("div", { class: "memory-recall-body" });
  const controls = el("div", {
    class: "setting-row",
    attrs: { id: "memory-recall-controls" },
  });
  const feedback = el("p", {
    class: "setting-status",
    attrs: {
      id: "memory-recall-status",
      role: "status",
      "aria-live": "polite",
    },
  });
  box.append(body, controls, feedback);
  let busy = false;
  let current: MemoryRecallStatus | null = null;

  const numberInput = (
    id: string,
    label: string,
    value: number,
    min: number,
    max: number,
    step: number,
  ): HTMLElement => {
    const input = el("input", {
      class: "setting-input",
      attrs: {
        id,
        type: "number",
        min: String(min),
        max: String(max),
        step: String(step),
        value: String(value),
      },
    }) as HTMLInputElement;
    input.disabled = busy;
    return el("label", {
      class: "setting-row",
      children: [el("span", { text: label }), input],
    });
  };

  const render = (status: MemoryRecallStatus | null): RecallPanelModel => {
    const model = recallPanelModel(status, lang);
    clear(body);
    const meta = el("dl", { class: "kv", attrs: { id: "memory-recall-meta" } });
    for (const row of model.rows) meta.append(kv(row.label, row.value));
    body.append(meta);
    for (const note of model.notes) {
      body.append(el("p", { class: "muted", text: note }));
    }
    clear(controls);
    if (!model.editable) {
      controls.append(el("p", { class: "muted", text: model.editableNote }));
      return model;
    }
    const toggle = el("button", {
      class: "quiet-button",
      text: model.values.enabled
        ? lang === "en"
          ? "Turn automatic recall off"
          : "关闭自动召回"
        : lang === "en"
          ? "Turn automatic recall on"
          : "启用自动召回",
      attrs: {
        type: "button",
        id: "memory-recall-toggle",
        "aria-pressed": model.values.enabled ? "true" : "false",
      },
    }) as HTMLButtonElement;
    toggle.disabled = busy;
    toggle.addEventListener("click", () => {
      if (busy || !current) return;
      current = { ...current, enabled: !current.enabled };
      render(current);
    });
    controls.append(
      toggle,
      numberInput(
        "memory-recall-max-items",
        "最多条数",
        model.values.max_items,
        1,
        4,
        1,
      ),
      numberInput(
        "memory-recall-budget",
        "预算 (PCS U)",
        model.values.budget_units,
        64,
        2048,
        64,
      ),
      numberInput(
        "memory-recall-timeout",
        "整轮超时 (ms)",
        model.values.timeout_ms,
        250,
        2000,
        50,
      ),
    );
    const select = el("select", {
      class: "setting-input",
      attrs: { id: "memory-recall-timezone" },
      children: TIMEZONES.map((zone) =>
        el("option", {
          text: zone,
          attrs: {
            value: zone,
            ...(model.values.timezone === zone ? { selected: "selected" } : {}),
          },
        }),
      ),
    }) as HTMLSelectElement;
    select.disabled = busy;
    controls.append(
      el("label", {
        class: "setting-row",
        children: [el("span", { text: "时区" }), select],
      }),
    );
    const save = el("button", {
      class: "quiet-button",
      text: lang === "en" ? "Save" : "保存",
      attrs: { type: "button", id: "memory-recall-save" },
    }) as HTMLButtonElement;
    save.disabled = busy;
    save.addEventListener("click", () => {
      if (busy) return;
      const maxItems = Number(
        (controls.querySelector("#memory-recall-max-items") as HTMLInputElement | null)
          ?.value ?? model.values.max_items,
      );
      const budget = Number(
        (controls.querySelector("#memory-recall-budget") as HTMLInputElement | null)
          ?.value ?? model.values.budget_units,
      );
      const timeout = Number(
        (controls.querySelector("#memory-recall-timeout") as HTMLInputElement | null)
          ?.value ?? model.values.timeout_ms,
      );
      void saveValues(
        (controls.querySelector("#memory-recall-toggle") as HTMLButtonElement | null)
          ?.getAttribute("aria-pressed") === "true",
        maxItems,
        budget,
        timeout,
        (controls.querySelector("#memory-recall-timezone") as HTMLSelectElement | null)
          ?.value ?? model.values.timezone,
      );
    });
    controls.append(save);
    return model;
  };

  const saveValues = async (
    enabled: boolean,
    maxItems: number,
    budget: number,
    timeout: number,
    timezone: string,
  ): Promise<void> => {
    busy = true;
    feedback.className = "setting-status";
    feedback.textContent = lang === "en" ? "Saving…" : "正在保存…";
    try {
      const reply = await invoke<HostReply>("save_memory_recall_settings", {
        enabled,
        maxItems,
        budgetUnits: budget,
        timeoutMs: timeout,
        timezone,
      });
      if (!reply.ok) {
        feedback.textContent =
          reply.error?.message ??
          (lang === "en" ? "Could not save." : "保存失败。");
        feedback.classList.add("is-error");
        await load();
        return;
      }
      const result = reply.result as SaveRecallResult;
      await refresh();
      current = result.status ?? null;
      render(current);
      if (result.applied !== true) {
        const code = result.application?.error?.code ?? "HOST_NOT_READY";
        feedback.classList.add("is-error");
        feedback.textContent =
          lang === "en"
            ? `Saved, but the host has not applied it yet (${code}).`
            : `已保存，但宿主尚未应用（${code}）。`;
      } else {
        feedback.textContent =
          lang === "en" ? "Saved · next turn" : "已保存 · 下一轮生效";
      }
    } catch (error) {
      feedback.textContent = error instanceof Error ? error.message : String(error);
      feedback.classList.add("is-error");
      await load();
    } finally {
      busy = false;
    }
  };

  const load = async (): Promise<void> => {
    try {
      const reply = await invoke<HostReply>("memory_recall_settings");
      if (!reply.ok) {
        feedback.textContent =
          reply.error?.message ??
          (lang === "en" ? "Could not read recall status." : "无法读取召回状态。");
        feedback.classList.add("is-error");
        current = null;
        render(null);
        return;
      }
      current = reply.result as MemoryRecallStatus;
      render(current);
    } catch (error) {
      feedback.textContent = error instanceof Error ? error.message : String(error);
      feedback.classList.add("is-error");
      current = null;
      render(null);
    }
  };

  void load();
  return box;
}
