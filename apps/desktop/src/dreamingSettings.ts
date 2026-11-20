import { invoke } from "@tauri-apps/api/core";
import { el } from "./dom";
import { dreamingDetails, dreamingLabel, dreamingStatus, type DreamingStatus } from "./dreamingStatus";
import type { AppInfo, HostReply } from "./types";

type DestinationBasis = { sessionId: string; destinationId: string; target: string; external: boolean };

function destinationBasis(info: AppInfo | null): DestinationBasis | null {
  if (!info?.destination) return null;
  const { destination_id, provider, model, external } = info.destination;
  return {
    sessionId: info.session_id,
    destinationId: destination_id,
    target: [provider, model].filter(Boolean).join(" / "),
    external,
  };
}

export function dreamingSettingsPanel(initialInfo: AppInfo | null, visible: () => boolean): HTMLElement {
  const language = document.documentElement.lang === "en" ? "en" : "zh-CN";
  const theme = document.documentElement.dataset.theme === "light" ? "light" : "dark";
  const t = (zh: string, en: string): string => language === "en" ? en : zh;
  const box = el("section", { class: "drawer-section" });
  const feedback = el("p", { class: "setting-status", attrs: { role: "status" } });
  const stateLine = el("p", { class: "muted" });
  const reasonLine = el("p", { class: "muted" });
  const toggle = el("button", { class: "quiet-button", attrs: { type: "button", disabled: true } });
  const journalsToggle = el("button", { class: "quiet-button", attrs: { type: "button", disabled: true } });
  const showOrb = el("button", {
    class: "ghost-button",
    text: t("显示悬浮球", "Show Dreaming orb"),
    attrs: { type: "button", disabled: true },
  });
  let status = dreamingStatus(null, language, theme);
  let basis = destinationBasis(initialInfo);
  let busy = false;
  let pending: Promise<DreamingStatus> | null = null;

  const render = (): void => {
    const label = dreamingLabel(status);
    if (stateLine.textContent !== label) stateLine.textContent = label;
    const reason = dreamingDetails(status).join(" ");
    if (reasonLine.textContent !== reason) reasonLine.textContent = reason;
    reasonLine.hidden = !reason;
    const toggleText = status.enabled
      ? t("关闭空闲思考", "Disable idle reflection")
      : t("授权空闲思考", "Authorize idle reflection");
    if (toggle.textContent !== toggleText) toggle.textContent = toggleText;
    toggle.disabled = busy || status.state === "disconnected" || (!status.can_configure && !status.enabled);
    const journalsText = status.journals_enabled
      ? t("暂停日历日记", "Pause calendar journals")
      : t("授权日历日记", "Authorize calendar journals");
    if (journalsToggle.textContent !== journalsText) journalsToggle.textContent = journalsText;
    journalsToggle.disabled = busy || status.state === "disconnected"
      || (!status.can_configure_journals && !status.journals_enabled);
    showOrb.disabled = busy || !status.can_show_orb;
  };

  const report = (message: string, error = false): void => {
    feedback.textContent = message;
    feedback.classList.toggle("is-error", error);
  };

  const read = (): Promise<DreamingStatus> => {
    if (pending) return pending;
    pending = (async () => {
      try {
        const reply = await invoke<HostReply>("dreaming_status");
        status = dreamingStatus(reply.ok ? reply.result : null, language, theme);
      } catch {
        status = dreamingStatus(null, language, theme);
      } finally {
        pending = null;
        render();
      }
      return status;
    })();
    return pending;
  };

  const poll = async (): Promise<void> => {
    if (!box.isConnected || !visible()) return;
    if (!busy) await read();
    if (box.isConnected && visible()) window.setTimeout(poll, 1_500);
  };

  const save = async (journals: boolean): Promise<void> => {
    if (busy) return;
    const enabledFor = (value: DreamingStatus): boolean => journals ? value.journals_enabled : value.enabled;
    const enabled = !enabledFor(status);
    busy = true;
    render();
    report("");
    try {
      const observed = await read();
      if (enabledFor(observed) === enabled) {
        report(t("授权状态已变化，请核对后重试。", "Authorization changed. Check the current state."), true);
        return;
      }
      const canConfigure = journals ? observed.can_configure_journals : observed.can_configure;
      if (observed.state === "disconnected" || (enabled && !canConfigure)) {
        report(t("当前实例无法更改此项设置。", "This setting is unavailable for this instance."), true);
        return;
      }

      const reply = await invoke<HostReply>("app_info");
      const current = reply.ok ? destinationBasis(reply.result as AppInfo) : null;
      if (!current) {
        report(t("宿主连接不可用。", "The host is disconnected."), true);
        return;
      }
      if (basis && (basis.sessionId !== current.sessionId || basis.destinationId !== current.destinationId)) {
        basis = current;
        report(t("宿主或模型已变化。请重新确认。", "The host or model changed. Confirm again."), true);
        return;
      }
      basis = current;

      const calendarNotice = t(
        "授权此实例用 " + current.target + " 写日记、周记、月记和年记？"
          + "模型会读取获准的人格和经历，并可查询相关记忆和认知记录。日记保存在当前实例。"
          + (current.external ? "相关文本会发往当前模型服务，调用可能产生费用。" : ""),
        "Authorize journals for this instance through " + current.target + "? The model can read authorized persona and experiences and look up relevant memory and cognition. Journals are stored in this instance."
          + (current.external ? " Related text will be sent to the current model service and calls may incur charges." : ""),
      );
      if (enabled && !window.confirm(journals ? calendarNotice : t(
        "授权此实例向当前宿主的 " + current.target + " 发起空闲思考请求？请求可能携带已授权的近期经历或复查材料。"
          + (current.external ? "外部模型调用可能产生费用。" : "")
          + "\n\n空闲满 15 分钟后可发起请求，两次至少间隔 30 分钟。滚动 24 小时最多 4 次，30 天内合计最多 120 次。每次输出上限与当前聊天设置一致。前台任务优先。",
        "Authorize this instance to send idle reflection requests to " + current.target + "? Requests may include authorized recent experiences or review material."
          + (current.external ? " External model calls may incur charges." : "")
          + "\n\nRequests require 15 minutes of idle time and run at least 30 minutes apart. This grant allows at most 4 calls in any 24 hours and 120 total within 30 days. The output limit per call matches your current chat settings. Foreground work takes priority.",
      ))) return;

      const saved = await invoke<HostReply>("save_dreaming_enabled", {
        sessionId: current.sessionId,
        destinationId: current.destinationId,
        enabled,
        journals,
      });
      if (!saved.ok) {
        report(saved.error?.message ?? t("保存未完成。", "The change could not be saved."), true);
        return;
      }
      const confirmed = await read();
      if (confirmed.state === "disconnected" || enabledFor(confirmed) !== enabled) {
        report(t("宿主尚未确认新状态，请刷新后核对。", "The host has not confirmed the new state. Refresh to check."), true);
        return;
      }
      if (journals) {
        report(enabled ? t("日历日记已获授权。", "Calendar journals authorized.")
          : t("已暂停写日历日记。", "Calendar journal writing paused."));
        return;
      }
      report(enabled ? t("空闲思考已获授权。", "Idle reflection authorized.")
        : t("已撤销空闲思考调用授权。近期经历仍可在本地整理。", "Idle model authorization revoked. Local continuity may continue."));
    } catch {
      report(t("保存结果未知，请核对当前状态。", "The result is unknown. Check the current state."), true);
    } finally {
      busy = false;
      render();
    }
  };

  toggle.addEventListener("click", () => void save(false));
  journalsToggle.addEventListener("click", () => void save(true));
  showOrb.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    render();
    report("");
    try {
      const reply = await invoke<HostReply>("open_dreaming_orb");
      if (!reply.ok) {
        report(reply.error?.message ?? t("无法显示悬浮球。", "The Dreaming orb is unavailable."), true);
      }
    } catch {
      report(t("无法显示悬浮球。", "The Dreaming orb is unavailable."), true);
    } finally {
      busy = false;
      render();
    }
  });

  box.append(
    el("h3", { text: t("空闲思考", "Idle reflection") }),
    el("p", { class: "muted", text: t(
      "宿主空闲时可整理近期经历。授权后，模型可以在空闲时提出下一步；提议留在当前实例中等待后续处理。",
      "The host can organize recent experiences while idle. With authorization, the model can suggest a next step for later review in this instance.",
    ) }),
    stateLine,
    reasonLine,
    el("div", { class: "setting-row", children: [toggle, journalsToggle, showOrb] }),
    feedback,
  );
  render();
  window.setTimeout(poll, 0);
  return box;
}
