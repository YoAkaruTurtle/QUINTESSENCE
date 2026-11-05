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

function message(reply: HostReply): string {
  return reply.error?.message ?? "实例连接未完成，请检查所选文件夹后重试。";
}

export function instanceSettingsPanel(): HTMLElement {
  const path = el("input", { class: "setting-input", attrs: { id: "instance-folder", type: "text", placeholder: "选择空文件夹或已有实例文件夹", "aria-label": "实例文件夹", autocomplete: "off" } });
  const choose = el("button", { class: "quiet-button", text: "选择文件夹", attrs: { type: "button", id: "choose-instance-folder" } });
  const connect = el("button", { class: "quiet-button", text: "连接此文件夹", attrs: { type: "button", id: "connect-instance" } });
  const status = el("p", { class: "setting-status", attrs: { id: "instance-connection-status", role: "status", "aria-live": "polite" } });
  const identity = el("p", { class: "muted", attrs: { id: "instance-identity" } });
  const operator = el("input", { class: "setting-input", attrs: { type: "text", placeholder: "操作者名称", "aria-label": "旧实例接入操作者", maxlength: "128" } });
  const reason = el("input", { class: "setting-input", attrs: { type: "text", placeholder: "说明你核对了什么、为什么确定组件此前未建立", "aria-label": "旧实例接入原因", maxlength: "512" } });
  const recoveryConfirm = el("input", { attrs: { type: "checkbox", id: "legacy-adoption-confirm" } });
  const recovery = el("div", { children: [
    el("p", { text: "旧实例没有恢复安全记录。登记新基线会保留现有内容，但无法证明接入前的删除历史。若记录可能意外丢失，请先找回原记录。已有的部分安全记录不能被此操作覆盖。" }),
    el("label", { children: [recoveryConfirm, el("span", { text: "我接受接入前的删除历史不可证明，从当前状态开始启用保护。" })] }),
  ] });
  const componentConfirm = el("input", { attrs: { type: "checkbox", id: "never-created-component-confirm" } });
  const componentLabel = el("span");
  const component = el("div", { children: [
    el("p", { text: "没有投递记录不代表组件从未建立。如果组件曾经存在、可能意外丢失，或者你不能确认，请停止接入并找回数据或初始化记录；不能用空组件替代。" }),
    el("label", { children: [componentConfirm, componentLabel] }),
  ] });
  const register = el("button", { class: "quiet-button", text: "按上述声明登记并继续", attrs: { type: "button", id: "register-legacy-instance" } });
  const registration = el("div", { class: "legacy-instance-adoption", children: [recovery, component, operator, reason, register] });
  registration.hidden = true;
  recovery.hidden = true;
  component.hidden = true;
  const content = el("section", { class: "drawer-section instance-settings", attrs: { "aria-label": "实例连接" }, children: [
    el("h3", { text: "实例文件夹" }),
    el("p", { class: "muted", text: "一个文件夹保存一个独立实例。选择空文件夹会自动创建；选择已有实例会恢复原有内容。连接后会自动重新加载。" }),
    path, el("div", { class: "setting-row", children: [choose, connect] }), status, identity, registration,
    el("p", { class: "muted", text: "没有自定义人格也可以直接聊天。已有数据损坏或关键记录丢失时会停止连接，保留原文件并说明原因。" }),
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
    if (!root) { status.textContent = "请先选择实例文件夹。"; return; }
    if ((needsRecovery || missing.length) && (!operator.value.trim() || !reason.value.trim() || (needsRecovery && !recoveryConfirm.checked) || (missing.length && !componentConfirm.checked))) {
      status.textContent = "请核对历史，填写操作者和原因，并明确勾选所需声明；不确定时不要继续。"; return;
    }
    const attribution = { by: operator.value.trim(), reason: reason.value.trim(), confirm: true };
    setBusy(true); status.classList.remove("is-error"); status.textContent = "正在识别并检查实例，原有内容会保留…";
    const overlay = el("div", { class: "instance-connecting-overlay", attrs: { role: "dialog", "aria-modal": "true", "aria-label": "正在连接实例", tabindex: "-1" }, children: [el("p", { text: "正在连接实例，请稍候…" })] });
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
        componentLabel.textContent = `我已核对并确认 ${missing[0] === "pcs" ? "PCS（会话、人格和 Cognition）" : "Memory（Scope Recall）"} 此前从未建立；这不是丢失后的重建。`;
        overlay.remove(); setBusy(false); return;
      }
      const result = reply.result as { restart_required?: boolean };
      status.textContent = result.restart_required ? "实例已准备完成，正在重新加载…" : "已连接当前实例。";
      if (!result.restart_required) { overlay.remove(); setBusy(false); resetRegistration(); }
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : "连接未完成，请重新检查。";
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
    } catch { status.textContent = "无法打开文件夹选择窗口，也可以直接输入路径。"; }
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
    status.textContent = value.connected ? "已连接" : value.reason ?? "尚未连接实例。请选择一个文件夹。";
    if (!value.connected && value.reason) status.classList.add("is-error");
    identity.textContent = value.instance_id ? `实例身份：${value.instance_id}` : "空文件夹将获得独立身份，已有实例将保留原身份。";
  }).catch(() => { if (!busy) status.textContent = "请选择实例文件夹以开始。"; });
  return content;
}
