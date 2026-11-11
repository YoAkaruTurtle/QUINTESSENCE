import { invoke } from "@tauri-apps/api/core";
import { el } from "./dom";
import type { HostReply } from "./types";

interface InstanceStatus {
  root: string | null;
  instance_id: string | null;
  connected: boolean;
  reason: string | null;
  portable: boolean;
}

const en = (): boolean => document.documentElement.lang === "en";
const tr = (zh: string, english: string): string => en() ? english : zh;

function message(reply: HostReply): string {
  return reply.error?.message ?? tr(
    "实例连接未完成，请检查所选文件夹后重试。",
    "Instance connection did not complete. Check the selected folder and try again.",
  );
}

export function instanceSettingsPanel(): HTMLElement {
  const path = el("input", { class: "setting-input", attrs: {
    id: "instance-folder",
    type: "text",
    placeholder: tr("选择空文件夹或已有实例文件夹", "Choose an empty folder or an existing instance folder"),
    "aria-label": tr("实例文件夹", "Instance folder"),
    autocomplete: "off",
  } });
  const choose = el("button", { class: "quiet-button", text: tr("选择文件夹", "Choose folder"), attrs: { type: "button", id: "choose-instance-folder" } });
  const connect = el("button", { class: "quiet-button", text: tr("连接此文件夹", "Connect this folder"), attrs: { type: "button", id: "connect-instance" } });
  const status = el("p", { class: "setting-status", attrs: { id: "instance-connection-status", role: "status", "aria-live": "polite" } });
  const identity = el("p", { class: "muted", attrs: { id: "instance-identity" } });
  const operator = el("input", { class: "setting-input", attrs: {
    type: "text",
    placeholder: tr("操作者名称", "Operator name"),
    "aria-label": tr("旧实例接入操作者", "Legacy instance operator"),
    maxlength: "128",
  } });
  const reason = el("input", { class: "setting-input", attrs: {
    type: "text",
    placeholder: tr("说明你核对了什么、为什么确定组件此前未建立", "Explain what you checked and why you are sure the component was never created"),
    "aria-label": tr("旧实例接入原因", "Legacy instance reason"),
    maxlength: "512",
  } });
  const recoveryConfirm = el("input", { attrs: { type: "checkbox", id: "legacy-adoption-confirm" } });
  const recovery = el("div", { children: [
    el("p", { text: tr(
      "旧实例没有恢复安全记录。登记新基线会保留现有内容，但无法证明接入前的删除历史。若记录可能意外丢失，请先找回原记录。已有的部分安全记录不能被此操作覆盖。",
      "The legacy instance has no recovery safety record. Registering a new baseline keeps existing content but cannot prove the deletion history before adoption. If records may have been lost unexpectedly, recover them first. Existing partial safety records cannot be overwritten by this action.",
    ) }),
    el("label", { children: [recoveryConfirm, el("span", { text: tr(
      "我接受接入前的删除历史不可证明，从当前状态开始启用保护。",
      "I accept that deletion history before adoption cannot be proven, and protections start from the current state.",
    ) })] }),
  ] });
  const componentConfirm = el("input", { attrs: { type: "checkbox", id: "never-created-component-confirm" } });
  const componentLabel = el("span");
  const component = el("div", { children: [
    el("p", { text: tr(
      "没有投递记录不代表组件从未建立。如果组件曾经存在、可能意外丢失，或者你不能确认，请停止接入并找回数据或初始化记录；不能用空组件替代。",
      "No delivery record does not mean the component never existed. If it existed, may have been lost, or you are unsure, stop and recover the data or initialization record; do not substitute an empty component.",
    ) }),
    el("label", { children: [componentConfirm, componentLabel] }),
  ] });
  const register = el("button", { class: "quiet-button", text: tr("按上述声明登记并继续", "Register as declared and continue"), attrs: { type: "button", id: "register-legacy-instance" } });
  const registration = el("div", { class: "legacy-instance-adoption", children: [recovery, component, operator, reason, register] });
  registration.hidden = true;
  recovery.hidden = true;
  component.hidden = true;
  const content = el("section", { class: "drawer-section instance-settings", attrs: { "aria-label": tr("实例连接", "Instance connection") }, children: [
    el("h3", { text: tr("实例文件夹", "Instance folder") }),
    el("p", { class: "muted", text: tr(
      "一个文件夹保存一个独立实例。选择空文件夹会自动创建；选择已有实例会恢复原有内容。连接后会自动重新加载。",
      "One folder holds one independent instance. An empty folder is created automatically; an existing instance restores its content. The window reloads after connecting.",
    ) }),
    path, el("div", { class: "setting-row", children: [choose, connect] }), status, identity, registration,
    el("p", { class: "muted", text: tr(
      "没有自定义人格也可以直接聊天。已有数据损坏或关键记录丢失时会停止连接，保留原文件并说明原因。",
      "You can chat without a custom persona. If existing data is damaged or a critical record is missing, connection stops, the original files stay in place, and the reason is shown.",
    ) }),
  ] });
  let busy = false;
  let needsRecovery = false;
  let missing: Array<"pcs" | "memory"> = [];
  const resetRegistration = (): void => {
    needsRecovery = false; missing = []; registration.hidden = true; recovery.hidden = true; component.hidden = true;
    recoveryConfirm.checked = false; componentConfirm.checked = false;
  };
  const setBusy = (value: boolean): void => { busy = value; choose.disabled = value; connect.disabled = value; path.disabled = value; register.disabled = value; };
  const submit = async (): Promise<void> => {
    if (busy) return;
    const root = path.value.trim();
    if (!root) { status.textContent = tr("请先选择实例文件夹。", "Choose an instance folder first."); return; }
    if ((needsRecovery || missing.length) && (!operator.value.trim() || !reason.value.trim() || (needsRecovery && !recoveryConfirm.checked) || (missing.length && !componentConfirm.checked))) {
      status.textContent = tr("请核对历史，填写操作者和原因，并明确勾选所需声明；不确定时不要继续。", "Review the history, enter an operator and reason, and confirm the required declarations. Do not continue if unsure."); return;
    }
    const attribution = { by: operator.value.trim(), reason: reason.value.trim(), confirm: true };
    setBusy(true); status.classList.remove("is-error"); status.textContent = tr("正在识别并检查实例，原有内容会保留…", "Inspecting the instance; existing content is preserved…");
    const overlay = el("div", { class: "instance-connecting-overlay", attrs: { role: "dialog", "aria-modal": "true", "aria-label": tr("正在连接实例", "Connecting instance"), tabindex: "-1" }, children: [el("p", { text: tr("正在连接实例，请稍候…", "Connecting to the instance…") })] });
    document.body.append(overlay); overlay.focus();
    try {
      const reply = await invoke<HostReply>("connect_instance", {
        root,
        legacyAdoption: needsRecovery ? attribution : null,
        componentDeclaration: missing.length ? { ...attribution, components: missing } : null,
      });
      if (!reply.ok) {
        status.textContent = message(reply); status.classList.add("is-error");
        const code = reply.error?.code;
        if (code === "INSTANCE_RECOVERY_REQUIRED") needsRecovery = true;
        else if (code === "INSTANCE_MEMORY_HISTORY_UNKNOWN") missing = ["memory"];
        else if (code === "INSTANCE_PCS_HISTORY_UNKNOWN") missing = ["pcs"];
        else resetRegistration();
        registration.hidden = !needsRecovery && !missing.length;
        recovery.hidden = !needsRecovery;
        component.hidden = !missing.length;
        componentLabel.textContent = tr(
          `我已核对并确认 ${missing[0] === "pcs" ? "PCS（会话、人格和 Cognition）" : "Memory（Scope Recall）"} 此前从未建立；这不是丢失后的重建。`,
          `I checked and confirm that ${missing[0] === "pcs" ? "PCS (conversations, persona and Cognition)" : "Memory (Scope Recall)"} was never created; this is not a rebuild after loss.`,
        );
        overlay.remove(); setBusy(false); return;
      }
      const result = reply.result as { restart_required?: boolean };
      status.textContent = result.restart_required
        ? tr("实例已准备完成，正在重新加载…", "Instance prepared; reloading…")
        : tr("已连接当前实例。", "Connected to the current instance.");
      if (!result.restart_required) { overlay.remove(); setBusy(false); resetRegistration(); }
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : tr("连接未完成，请重新检查。", "Connection did not complete. Check again.");
      status.classList.add("is-error"); overlay.remove(); setBusy(false);
    }
  };
  choose.onclick = () => { void (async () => {
    if (busy) return;
    setBusy(true);
    try {
      const reply = await invoke<HostReply>("choose_instance_folder");
      if (!reply.ok) { status.textContent = message(reply); status.classList.add("is-error"); return; }
      const result = reply.result as { root: string | null };
      if (result.root) { path.value = result.root; resetRegistration(); setBusy(false); await submit(); }
    } catch { status.textContent = tr("无法打开文件夹选择窗口，也可以直接输入路径。", "Could not open the folder picker; you can type the path directly."); }
    finally { if (!document.querySelector(".instance-connecting-overlay")) setBusy(false); }
  })(); };
  register.onclick = () => { void submit(); };
  path.oninput = resetRegistration;
  connect.onclick = () => { void submit(); };
  path.onkeydown = event => { if (event.key === "Enter") void submit(); };
  void invoke<HostReply>("instance_status").then(reply => {
    if (!reply.ok || busy) return;
    const value = reply.result as InstanceStatus;
    path.value = value.root ?? "";
    status.textContent = value.connected ? tr("已连接", "Connected") : value.reason ?? tr("尚未连接实例。请选择一个文件夹。", "No instance connected. Choose a folder.");
    if (!value.connected && value.reason) status.classList.add("is-error");
    identity.textContent = value.instance_id
      ? `${tr("实例身份：", "Instance identity: ")}${value.instance_id}`
      : tr("空文件夹将获得独立身份，已有实例将保留原身份。", "An empty folder gets a new identity; an existing instance keeps its original identity.");
  }).catch(() => { if (!busy) status.textContent = tr("请选择实例文件夹以开始。", "Choose an instance folder to begin."); });
  return content;
}
