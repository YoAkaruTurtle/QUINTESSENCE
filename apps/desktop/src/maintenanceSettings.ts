/**
 * RG08 DELIVERY: the minimal in-app maintenance surface.
 *
 * It exposes only the existing controlled service: the auto-maintenance switch,
 * the current service state, and Run once / Continue, Pause and Cancel for the
 * task the service names.  No scheduler, worker or cognition record is created
 * here; the host owns every write.
 */
import { invoke } from "@tauri-apps/api/core";
import { el } from "./dom";
import type { HostReply } from "./types";

interface MaintenanceService {
  enabled?: boolean;
  running?: boolean;
  cancelling?: boolean;
  draining?: boolean;
  paused_foreground?: boolean;
  active?: { task_id?: string | null; turn_id?: string | null } | null;
  last?: { task_id?: string | null; stop_reason?: string | null; state?: string | null } | null;
}

interface MaintenanceStatus {
  service?: MaintenanceService;
  jobs?: unknown[];
}

const zh = (): boolean => document.documentElement.lang !== "en";
const text = (zhText: string, enText: string): string => (zh() ? zhText : enText);

function statusLine(service: MaintenanceService): string {
  if (service.running) {
    if (service.cancelling) {
      return text("正在让路：当前维护会在前台优先处停止。", "Yielding: the current maintenance attempt is stopping for the foreground.");
    }
    if (service.draining) {
      return text("正在结束旧的维护请求；新的维护会等待它排空。", "The previous maintenance request is draining; new work waits for it.");
    }
    return text("正在维护：前台消息到达时会让路。", "Maintenance is running; a foreground send makes it yield.");
  }
  if (service.draining) {
    return text("旧的维护请求仍未结束；前台不受影响。", "The previous maintenance request is still draining; the foreground is not blocked.");
  }
  if (service.paused_foreground) {
    return text("维护已暂停（前台优先），可继续维护。", "Maintenance is paused for the foreground. Continue to resume one bounded page.");
  }
  if (service.enabled) {
    return text("自动维护已启用，当前空闲。", "Automatic maintenance is enabled and idle.");
  }
  return text("自动维护已关闭。", "Automatic maintenance is off.");
}

export function maintenanceSettingsPanel(
  sessionId: string | null,
  refresh: () => Promise<void>,
): HTMLElement {
  const box = el("section", { class: "drawer-section" });
  const feedback = el("p", { class: "setting-status", attrs: { role: "status", "aria-live": "polite" } });
  const toggle = el("input", { attrs: { type: "checkbox", id: "maintenance-enabled" } }) as HTMLInputElement;
  const toggleLabel = el("label", { class: "setting-row", children: [
    toggle,
    el("span", { text: text("自动维护", "Automatic maintenance") }),
  ] });
  const run = el("button", { class: "quiet-button", text: text("运行一次 / 继续维护", "Run once / Continue"), attrs: { type: "button" } }) as HTMLButtonElement;
  const pause = el("button", { class: "ghost-button", text: text("暂停当前维护", "Pause current"), attrs: { type: "button" } }) as HTMLButtonElement;
  const cancel = el("button", { class: "ghost-button", text: text("取消已暂停任务", "Cancel paused task"), attrs: { type: "button" } }) as HTMLButtonElement;
  const details = el("dl", { class: "kv" });
  let service: MaintenanceService = {};
  let busy = false;

  const errorText = (reply: HostReply): string =>
    reply.error?.message ?? text("维护服务不可用。", "The maintenance service is unavailable.");

  const render = (): void => {
    toggle.checked = Boolean(service.enabled);
    toggle.disabled = !sessionId || busy;
    details.replaceChildren(
      el("dt", { text: text("状态", "State") }),
      el("dd", { text: statusLine(service) }),
      el("dt", { text: text("最近停止原因", "Last stop") }),
      el("dd", { text: service.last?.stop_reason ?? "—" }),
      el("dt", { text: text("任务", "Task") }),
      el("dd", { text: service.active?.task_id ?? service.last?.task_id ?? "—" }),
    );
    run.disabled = !sessionId || busy;
    pause.disabled = !sessionId || busy || !service.running || !service.active?.task_id;
    cancel.disabled = !sessionId || busy || !service.paused_foreground || !service.last?.task_id;
  };

  const load = async (): Promise<void> => {
    if (!sessionId) {
      feedback.textContent = text("宿主会话尚未建立。", "The host session is not ready.");
      return;
    }
    try {
      const reply = await invoke<HostReply>("maintenance_status", { sessionId });
      if (!reply.ok) {
        feedback.textContent = errorText(reply);
        feedback.classList.add("is-error");
        return;
      }
      const result = reply.result as MaintenanceStatus;
      service = result.service ?? {};
      feedback.textContent = "";
      feedback.classList.remove("is-error");
      render();
    } catch (error) {
      feedback.textContent = error instanceof Error ? error.message : text("读取维护状态失败。", "Could not read maintenance status.");
      feedback.classList.add("is-error");
    }
  };

  const saveToggle = async (): Promise<void> => {
    if (!sessionId || busy) return;
    const enabled = toggle.checked;
    busy = true;
    render();
    try {
      const reply = await invoke<HostReply>("save_maintenance_enabled", {
        sessionId,
        enabled,
      });
      if (!reply.ok) {
        feedback.textContent = errorText(reply);
        feedback.classList.add("is-error");
        return;
      }
      const result = reply.result as { saved?: boolean; application?: HostReply };
      const message = result.application?.ok
        ? text("已保存；下一轮起生效。", "Saved; applies from the next turn.")
        : text("已保存；应用确认待刷新。", "Saved; application is not confirmed yet.");
      await refresh();
      await load();
      feedback.textContent = message;
      feedback.classList.toggle("is-error", !result.application?.ok);
    } catch (error) {
      feedback.textContent = error instanceof Error ? error.message : text("保存失败。", "Could not save.");
      feedback.classList.add("is-error");
    } finally {
      busy = false;
      render();
    }
  };

  const invokeRun = async (): Promise<void> => {
    if (!sessionId || busy) return;
    busy = true;
    render();
    feedback.textContent = text("正在运行一次有界维护…", "Running one bounded maintenance slice…");
    feedback.classList.remove("is-error");
    try {
      const taskId = service.paused_foreground ? service.last?.task_id ?? null : null;
      const reply = await invoke<HostReply>("maintenance_run", { sessionId, taskId });
      if (!reply.ok) {
        feedback.textContent = errorText(reply);
        feedback.classList.add("is-error");
        return;
      }
      const result = reply.result as { status?: string; state?: string; stop_reason?: string };
      const message = text(
        `已运行：${result.status ?? "ran"} / ${result.state ?? "—"} / ${result.stop_reason ?? "—"}`,
        `Ran: ${result.status ?? "ran"} / ${result.state ?? "—"} / ${result.stop_reason ?? "—"}`,
      );
      await load();
      feedback.textContent = message;
    } catch (error) {
      feedback.textContent = error instanceof Error ? error.message : text("运行失败。", "Run failed.");
      feedback.classList.add("is-error");
    } finally {
      busy = false;
      render();
    }
  };

  const invokeControl = async (action: "yield" | "cancel"): Promise<void> => {
    if (!sessionId || busy) return;
    const taskId = action === "yield" ? service.active?.task_id : service.last?.task_id;
    if (!taskId) return;
    if (action === "cancel" && !window.confirm(text(
      "取消这个已暂停的维护任务？审计记录会保留，不会提交任何候选。",
      "Cancel this paused maintenance task? The audit record remains and no candidate is committed.",
    ))) return;
    busy = true;
    render();
    try {
      const reply = await invoke<HostReply>("maintenance_control", { sessionId, taskId, action });
      if (!reply.ok) {
        feedback.textContent = errorText(reply);
        feedback.classList.add("is-error");
        return;
      }
      const message = action === "yield"
        ? text("已请求让路。", "Yield requested.")
        : text("已取消暂停任务。", "Paused task cancelled.");
      await load();
      feedback.textContent = message;
    } catch (error) {
      feedback.textContent = error instanceof Error ? error.message : text("维护控制失败。", "Maintenance control failed.");
      feedback.classList.add("is-error");
    } finally {
      busy = false;
      render();
    }
  };

  toggle.addEventListener("change", () => void saveToggle());
  run.addEventListener("click", () => void invokeRun());
  pause.addEventListener("click", () => void invokeControl("yield"));
  cancel.addEventListener("click", () => void invokeControl("cancel"));

  box.append(
    el("h3", { text: text("自动维护", "Automatic maintenance") }),
    el("p", {
      class: "muted",
      text: text(
        "自动维护只处理已有认知材料；它不会新建认知形成专项。前台消息到达时，维护会让路；恢复只跑一个有界页面。",
        "Automatic maintenance only reviews existing material; it does not add a cognition-formation checkpoint. A foreground send makes it yield; resume runs one bounded page.",
      ),
    }),
    toggleLabel,
    details,
    el("div", { class: "setting-row", children: [run, pause, cancel] }),
    feedback,
  );
  void load();
  return box;
}
