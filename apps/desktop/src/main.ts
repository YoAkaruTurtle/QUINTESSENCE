import "./styles.css";

import { invoke } from "@tauri-apps/api/core";

import { clear, el, focusable, icon } from "./dom";
import {
  aiDisplayName,
  avatarLetter,
  countCharacters,
  destinationKindLabel,
  destinationLabel,
  detailLabel,
  materialNotice,
  phaseTitle,
  queryStatusLabel,
  reasonLabel,
  shortId,
  sourceCountLabel,
  sourceRows,
} from "./format";
import {
  buildConsultParams,
  INITIAL_STATE,
  isConfirmed,
  maxQuestionChars,
  needsConfirmation,
  reduce,
  sendEnabled,
  type DrawerTab,
  type Phase,
  type UiAction,
  type UiState,
} from "./state";
import type {
  AppInfo,
  BridgeError,
  ConfirmResult,
  ConsultResult,
  HostReply,
} from "./types";

// --------------------------------------------------------------------------
// bridge
// --------------------------------------------------------------------------

async function appInfo(): Promise<HostReply> {
  return (await invoke("app_info", {})) as HostReply;
}

async function confirmDestination(
  sessionId: string,
  destinationId: string,
): Promise<HostReply> {
  return (await invoke("confirm_destination", {
    sessionId,
    destinationId,
  })) as HostReply;
}

async function consultInteract(params: {
  sessionId: string;
  question: string;
  confirmedDestinationId: string | null;
}): Promise<HostReply> {
  return (await invoke("consult_interact", params)) as HostReply;
}

async function setAiDisplayName(name: string): Promise<HostReply> {
  return (await invoke("set_ai_display_name", { name })) as HostReply;
}

function invokeError(error: unknown): BridgeError {
  return {
    code: "HOST_INTERNAL",
    message: error instanceof Error ? error.message : "桌面命令调用失败",
    result_unknown: false,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isAppInfo(value: unknown): value is AppInfo {
  return (
    isRecord(value) &&
    typeof value.session_id === "string" &&
    isRecord(value.host) &&
    typeof value.host.ready === "boolean"
  );
}

function isConsultResult(value: unknown): value is ConsultResult {
  if (!isRecord(value)) {
    return false;
  }
  const state = value.state;
  return (
    (state === "answered" ||
      state === "waiting" ||
      state === "failed" ||
      state === "incomplete" ||
      state === "stale") &&
    isRecord(value.input) &&
    isRecord(value.destination) &&
    typeof value.destination.destination_id === "string"
  );
}

function replyError(reply: HostReply): BridgeError {
  return (
    reply.error ?? {
      code: "HOST_INTERNAL",
      message: "宿主没有返回错误说明",
      result_unknown: false,
    }
  );
}

// --------------------------------------------------------------------------
// controller state
// --------------------------------------------------------------------------

let state: UiState = INITIAL_STATE;
let workspace: "consult" | "review" = "consult";
let composing = false;
let drawerWasOpen = false;
let drawerTrigger: HTMLElement | null = null;
let toastTimer: number | null = null;

const root = document.getElementById("app") as HTMLElement;

interface Shell {
  hostIndicator: HTMLSpanElement;
  destinationChip: HTMLButtonElement;
  settingsButton: HTMLButtonElement;
  main: HTMLElement;
  footer: HTMLElement;
  banner: HTMLElement;
  textarea: HTMLTextAreaElement;
  sendButton: HTMLButtonElement;
  counter: HTMLSpanElement;
  target: HTMLElement;
  hint: HTMLElement;
  sourcesButton: HTMLButtonElement;
  drawer: HTMLElement;
  drawerBackdrop: HTMLElement;
  drawerBody: HTMLElement;
  drawerTabs: HTMLElement;
  drawerClose: HTMLButtonElement;
  drawerTitle: HTMLElement;
  toast: HTMLElement;
  announcer: HTMLElement;
  consultTab: HTMLButtonElement;
  reviewTab: HTMLButtonElement;
}

let shell: Shell;

function buildShell(): void {
  clear(root);
  root.className = "app-shell";
  root.setAttribute("data-phase", state.phase);
  root.setAttribute("data-workspace", workspace);

  const brand = el("div", {
    class: "brand",
    children: [brandMark("sm"), el("span", { class: "brand-name", text: "QUINTESSENCE" })],
  });
  const consultTab = el("button", {
    class: "workspace-tab is-active",
    text: "Consult",
    attrs: { type: "button", role: "tab", "aria-selected": "true" },
  });
  const reviewTab = el("button", {
    class: "workspace-tab",
    text: "Review",
    attrs: { type: "button", role: "tab", "aria-selected": "false" },
  });
  consultTab.addEventListener("click", () => switchWorkspace("consult"));
  reviewTab.addEventListener("click", () => switchWorkspace("review"));
  const tabs = el("nav", {
    class: "workspace-tabs",
    attrs: { role: "tablist", "aria-label": "工作区" },
    children: [consultTab, reviewTab],
  });

  const hostIndicator = el("span", {
    class: "host-indicator is-booting",
    attrs: { id: "host-indicator" },
    children: [el("span", { class: "status-dot" }), el("span", { text: "连接中" })],
  });
  const destinationChip = el("button", {
    class: "destination-chip",
    text: "目的地未就绪",
    attrs: { type: "button", id: "destination-chip", title: "查看目的地说明" },
  });
  destinationChip.addEventListener("click", () => openDrawer("destination"));
  const settingsButton = el("button", {
    class: "icon-button settings-button",
    attrs: { type: "button", id: "settings-button", "aria-label": "设置" },
    children: [icon("gear")],
  });
  settingsButton.addEventListener("click", () => openDrawer("settings"));
  const topbarRight = el("div", {
    class: "topbar-right",
    children: [hostIndicator, destinationChip, settingsButton],
  });

  const topbar = el("header", {
    class: "topbar",
    children: [brand, tabs, topbarRight],
  });

  const main = el("main", {
    class: "main",
    attrs: { id: "main", tabindex: "-1" },
  });

  const banner = el("div", {
    class: "confirm-banner hidden",
    attrs: { id: "confirm-banner" },
  });
  const target = el("strong", { attrs: { id: "composer-target" }, text: "目的地未就绪" });
  const counter = el("span", { class: "counter", attrs: { id: "char-count" }, text: "0 / 8000" });
  const hint = el("span", { class: "composer-hint", attrs: { id: "composer-hint" } });
  const textarea = el("textarea", {
    class: "composer-input",
    attrs: {
      id: "question",
      rows: "2",
      placeholder: "写下你想咨询的问题…",
      "aria-label": "咨询问题",
      spellcheck: "false",
    },
  });
  const sendButton = el("button", {
    class: "send-button",
    attrs: { type: "button", id: "action-send" },
    children: [
      el("span", { class: "send-label", text: "发送" }),
      icon("arrow"),
    ],
  });
  const sourcesButton = el("button", {
    class: "ghost-button",
    text: "来源与详情",
    attrs: { type: "button", id: "open-drawer" },
  });
  sourcesButton.addEventListener("click", () => openDrawer("sources"));
  sendButton.addEventListener("click", () => void requestSend());

  const composerTop = el("div", {
    class: "composer-top",
    children: [
      el("span", {
        class: "composer-target",
        children: [el("span", { text: "发送至 " }), target],
      }),
      el("span", { class: "composer-mode", text: "Consult · 单轮" }),
    ],
  });
  const composerFoot = el("div", {
    class: "composer-foot",
    children: [
      el("div", {
        class: "composer-meta",
        children: [counter, el("span", { class: "meta-sep", text: "·" }), hint],
      }),
      el("div", {
        class: "composer-actions",
        children: [sourcesButton, sendButton],
      }),
    ],
  });
  const composer = el("div", {
    class: "composer",
    children: [composerTop, textarea, composerFoot],
  });
  const footer = el("footer", {
    class: "composer-shell",
    attrs: { id: "composer-shell" },
    children: [banner, composer],
  });

  textarea.addEventListener("input", onQuestionInput);
  textarea.addEventListener("compositionstart", () => {
    composing = true;
  });
  textarea.addEventListener("compositionend", () => {
    composing = false;
  });
  textarea.addEventListener("keydown", onQuestionKeydown);

  const drawerBackdrop = el("div", {
    class: "drawer-backdrop hidden",
    attrs: { id: "drawer-backdrop" },
  });
  const drawerClose = el("button", {
    class: "icon-button drawer-close",
    attrs: { type: "button", id: "drawer-close", "aria-label": "关闭详情" },
    text: "关闭",
  });
  const drawerTitle = el("h2", {
    class: "drawer-title",
    text: "详情",
    attrs: { id: "drawer-title" },
  });
  const drawerHead = el("div", {
    class: "drawer-head",
    children: [drawerTitle, drawerClose],
  });
  const drawerTabs = el("div", {
    class: "drawer-tabs",
    attrs: { role: "tablist", "aria-label": "详情分类" },
  });
  const drawerBody = el("div", {
    class: "drawer-body",
    attrs: { id: "drawer-body" },
  });
  const drawer = el("aside", {
    class: "drawer hidden",
    attrs: {
      id: "drawer",
      role: "dialog",
      "aria-modal": "true",
      "aria-labelledby": "drawer-title",
    },
    children: [drawerHead, drawerTabs, drawerBody],
  });
  const toast = el("div", {
    class: "toast hidden",
    attrs: { id: "toast", role: "status" },
  });
  const announcer = el("div", {
    class: "visually-hidden",
    attrs: { id: "status-announcer", role: "status", "aria-live": "polite" },
  });

  root.append(topbar, main, footer, drawerBackdrop, drawer, toast, announcer);
  drawerClose.addEventListener("click", () => closeDrawer());
  drawerBackdrop.addEventListener("click", () => closeDrawer());
  document.addEventListener("keydown", onGlobalKeydown);

  shell = {
    hostIndicator,
    destinationChip,
    settingsButton,
    main,
    footer,
    banner,
    textarea,
    sendButton,
    counter,
    target,
    hint,
    sourcesButton,
    drawer,
    drawerBackdrop,
    drawerBody,
    drawerTabs,
    drawerClose,
    drawerTitle,
    toast,
    announcer,
    consultTab,
    reviewTab,
  };
}

function brandMark(size: "sm" | "lg"): HTMLElement {
  const wrapper = el("span", { class: `brand-mark brand-mark-${size}` });
  const image = el("img", {
    class: "brand-image",
    attrs: { src: "/brand.png", alt: "", draggable: "false" },
  });
  image.addEventListener("error", () => {
    image.remove();
    wrapper.classList.add("is-fallback");
    wrapper.textContent = "Q";
  });
  wrapper.appendChild(image);
  return wrapper;
}

function onGlobalKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape" && state.drawer.open) {
    event.preventDefault();
    closeDrawer();
    return;
  }
  if (state.drawer.open && event.key === "Tab") {
    const nodes = focusable(shell.drawer);
    if (nodes.length === 0) {
      return;
    }
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
}

// --------------------------------------------------------------------------
// rendering
// --------------------------------------------------------------------------

function apply(action: UiAction): void {
  state = reduce(state, action);
  render();
}

function render(): void {
  syncTopbar();
  renderMain();
  syncComposer();
  renderDrawer();
  root.setAttribute("data-phase", state.phase);
  root.setAttribute("data-workspace", workspace);
  root.setAttribute("data-host-ready", state.host?.host.ready ? "true" : "false");
  root.setAttribute(
    "data-confirmed",
    state.destination && isConfirmed(state) ? "true" : "false",
  );
  root.setAttribute("data-destination", state.destination?.destination_id ?? "");
  shell.announcer.textContent = `${workspace === "review" ? "Review" : "Consult"} · ${phaseTitle(state.phase)}`;
}

function syncTopbar(): void {
  const info = state.host;
  const ready = !!info?.host.ready;
  shell.hostIndicator.className = `host-indicator ${
    ready ? "is-ready" : state.phase === "booting" ? "is-booting" : "is-not-ready"
  }`;
  const label = ready
    ? "已连接"
    : state.phase === "booting"
      ? "连接中"
      : "未连接";
  clear(shell.hostIndicator);
  shell.hostIndicator.append(
    el("span", { class: "status-dot" }),
    el("span", { class: "status-text", text: label }),
  );
  shell.hostIndicator.title = ready
    ? "可信宿主已就绪"
    : reasonLabel(state.hostError?.code ?? info?.host.reason_code ?? null, null);

  const destination = state.destination;
  shell.destinationChip.classList.toggle("is-not-ready", !destination);
  shell.destinationChip.textContent = destination
    ? destinationLabel(destination)
    : "目的地未就绪";
  shell.destinationChip.setAttribute(
    "aria-label",
    `目的地：${destinationLabel(destination)}`,
  );

  shell.consultTab.classList.toggle("is-active", workspace === "consult");
  shell.reviewTab.classList.toggle("is-active", workspace === "review");
  shell.consultTab.setAttribute(
    "aria-selected",
    workspace === "consult" ? "true" : "false",
  );
  shell.reviewTab.setAttribute(
    "aria-selected",
    workspace === "review" ? "true" : "false",
  );
  document.title =
    workspace === "review" ? "QUINTESSENCE · Review" : "QUINTESSENCE · Consult";
}

function switchWorkspace(next: "consult" | "review"): void {
  if (workspace === next) {
    return;
  }
  workspace = next;
  state = reduce(state, { type: "drawer-close" });
  render();
  if (next === "consult") {
    window.setTimeout(() => shell.textarea.focus({ preventScroll: true }), 0);
  } else {
    shell.main.focus({ preventScroll: true });
  }
}

function renderMain(): void {
  clear(shell.main);
  const conversation = el("div", { class: "conversation" });
  const inner = el("div", { class: "conversation-inner" });
  conversation.appendChild(inner);
  shell.main.appendChild(conversation);

  if (workspace === "review") {
    inner.appendChild(reviewPlaceholder());
    return;
  }

  if (state.notice) {
    inner.appendChild(callout(state.notice, "info"));
  }
  if (
    state.error &&
    state.phase !== "unknown" &&
    state.phase !== "busy" &&
    state.phase !== "not-ready"
  ) {
    inner.appendChild(callout(errorText(state.error), "danger"));
  }

  if (state.phase === "booting") {
    inner.appendChild(bootingCard());
    return;
  }
  if (state.phase === "not-ready") {
    inner.appendChild(notReadyCard());
    return;
  }

  if (state.lastQuestion) {
    inner.appendChild(userMessage(state.lastQuestion));
  }

  if (state.phase === "sending") {
    inner.appendChild(thinkingMessage());
    scrollToBottom();
    return;
  }
  if (state.phase === "unknown") {
    inner.appendChild(unknownCard());
    return;
  }
  if (state.phase === "busy") {
    inner.appendChild(busyCard());
    return;
  }
  if (state.result) {
    inner.appendChild(resultMessage(state.result));
    scrollToBottom();
    return;
  }
  inner.appendChild(emptyState());
}

function scrollToBottom(): void {
  window.requestAnimationFrame(() => {
    shell.main.scrollTop = shell.main.scrollHeight;
  });
}

function callout(text: string, tone: "info" | "warn" | "danger"): HTMLElement {
  return el("div", {
    class: `callout callout-${tone}`,
    children: [icon(tone === "info" ? "info" : "alert"), el("span", { text })],
  });
}

function errorText(error: BridgeError): string {
  if (error.code === "INPUT_REGISTRATION_FAILED") {
    return "输入没有登记成功；本次没有创建咨询。请修改问题后重试。";
  }
  if (error.code === "CONFIRMATION_REQUIRED") {
    return "需要先确认当前外部目的地。";
  }
  if (error.code === "DESTINATION_MISMATCH") {
    return "确认的目的地与当前目的地不一致；请重新确认。";
  }
  if (error.code === "SESSION_MISMATCH") {
    return "宿主会话已重建；请重新确认目的地并重新提问。";
  }
  if (error.code === "HOST_NOT_READY" || error.code === "DATA_LOCKED") {
    return reasonLabel(error.code, null);
  }
  return error.message || "本次操作没有完成。";
}

function userMessage(question: string): HTMLElement {
  return el("article", {
    class: "message message-user",
    children: [
      el("div", { class: "bubble", text: question }),
      el("div", { class: "message-who", text: "你" }),
    ],
  });
}

function aiHeader(name: string, badge?: HTMLElement): HTMLElement {
  return el("header", {
    class: "message-head",
    children: [
      el("span", { class: "ai-avatar", text: avatarLetter(name), attrs: { "aria-hidden": "true" } }),
      el("span", { class: "ai-name", text: name }),
      el("span", { class: "ai-role", text: "AI" }),
      ...(badge ? [badge] : []),
    ],
  });
}

function thinkingMessage(): HTMLElement {
  const name = aiDisplayName(state.host);
  return el("article", {
    class: "message message-ai",
    children: [
      aiHeader(name),
      el("div", {
        class: "bubble bubble-thinking",
        children: [
          el("span", { class: "thinking-dots", children: [
            el("span"), el("span"), el("span"),
          ] }),
          el("span", { text: "正在思考…" }),
        ],
      }),
    ],
  });
}

function resultMessage(result: ConsultResult): HTMLElement {
  const name = aiDisplayName(state.host);
  const badge = statePill(result.state);
  const bubbleChildren: HTMLElement[] = [];
  const actions: HTMLElement[] = [];

  if (result.state === "answered") {
    if (result.reply) {
      bubbleChildren.push(
        el("div", { class: "reply-text", attrs: { id: "reply-text", tabindex: "0" }, text: result.reply }),
      );
    } else {
      bubbleChildren.push(
        el("p", { class: "muted", text: "本次没有可显示的正文；这不作为已回答展示。" }),
      );
    }
    const notice = materialNotice(result.query);
    if (notice) {
      bubbleChildren.push(callout(notice, "warn"));
    }
    if (result.reply) {
      actions.push(messageAction("copy", "复制回答", () => void copyReply()));
    }
  } else if (result.state === "waiting") {
    bubbleChildren.push(
      el("div", { class: "result-title", text: "等待（暂无完成回答）" }),
      el("p", {
        class: "muted",
        text: "模型这次没有给出完成的回答。这是一次有效结果，不是错误，也不是失败。",
      }),
      el("p", {
        class: "muted",
        text: "没有创建候选，也没有自动重试或静默重发。",
      }),
    );
    if (result.reply) {
      bubbleChildren.push(
        el("div", {
          class: "wait-note",
          children: [
            el("span", { class: "wait-label", text: "等待说明" }),
            el("span", { text: result.reply }),
          ],
        }),
      );
    }
  } else {
    const copy: Record<string, { title: string; lead: string }> = {
      failed: {
        title: "未完成（failed）",
        lead: "本次咨询没有完成，没有生成回答。",
      },
      incomplete: {
        title: "未完成（incomplete）",
        lead: "本次咨询尚未完成，没有可展示的回答。这不等于失败，也不等于已有结论。",
      },
      stale: {
        title: "已过期（stale）",
        lead: "本次咨询依据的上下文已变化，结果未采用；不会用旧结果冒充当前结果。",
      },
    };
    const text = copy[result.state] ?? copy.failed;
    bubbleChildren.push(
      el("div", { class: "result-title", text: text.title }),
      el("p", { class: "muted", text: text.lead }),
      el("p", { class: "reason-line", text: `原因：${detailLabel(result.detail)}` }),
      technicalDetails(result.detail),
    );
  }

  actions.push(messageAction("new-question", "新提问", () => newQuestion()));
  actions.push(messageAction("sources", "来源与详情", () => openDrawer("sources")));

  const sourceCount = sourceCountLabel(result);
  const footer = el("div", {
    class: "message-footer",
    children: [
      el("div", {
        class: "source-line",
        children: [
          el("span", { class: "source-count", text: sourceCount }),
          el("span", { class: "source-state", text: queryStatusLabel(result.query) }),
        ],
      }),
      el("div", { class: "message-actions", children: actions }),
    ],
  });

  return el("article", {
    class: "message message-ai",
    children: [
      aiHeader(name, badge),
      el("div", { class: `bubble bubble-result state-${result.state}`, children: bubbleChildren }),
      footer,
    ],
  });
}

function messageAction(
  action: "copy" | "new-question" | "sources",
  label: string,
  handler: () => void,
): HTMLButtonElement {
  const button = el("button", {
    class: `message-action action-${action}`,
    text: label,
    attrs: { type: "button" },
  });
  button.addEventListener("click", handler);
  return button;
}

function technicalDetails(detail: string | null): HTMLElement {
  return el("details", {
    class: "technical",
    children: [
      el("summary", { text: "技术详情" }),
      el("code", { text: detail ?? "无阶段标签" }),
      el("p", {
        class: "muted",
        text: "只显示阶段标签；不包含 Prompt、HTTP 正文、模型原始输出或内部修订号。",
      }),
    ],
  });
}

function statePill(phase: Phase): HTMLElement {
  const map: Record<string, { label: string; tone: string }> = {
    answered: { label: "已回答", tone: "ok" },
    waiting: { label: "等待", tone: "wait" },
    failed: { label: "failed", tone: "danger" },
    incomplete: { label: "incomplete", tone: "warn" },
    stale: { label: "stale", tone: "warn" },
    unknown: { label: "结果未知", tone: "danger" },
    busy: { label: "处理中", tone: "warn" },
  };
  const item = map[phase] ?? { label: phase, tone: "neutral" };
  return el("span", { class: `state-pill state-${item.tone}`, text: item.label });
}

function emptyState(): HTMLElement {
  const name = aiDisplayName(state.host);
  return el("section", {
    class: "empty-state",
    children: [
      brandMark("lg"),
      el("p", { class: "eyebrow", text: "QUINTESSENCE · CONSULT" }),
      el("h1", { text: `你好，我是${name}。` }),
      el("p", {
        class: "empty-lead",
        text: "想聊些什么？写下你的问题，我会先由本机可信宿主登记，再据此生成回答。",
      }),
      el("p", {
        class: "empty-note",
        text: "每次 Consult 都是独立的一轮；刷新或重启后无法恢复此前结果。",
      }),
    ],
  });
}

function bootingCard(): HTMLElement {
  return el("section", {
    class: "system-card",
    children: [
      el("div", { class: "system-icon is-working", children: [spinner()] }),
      el("div", {
        children: [
          el("h2", { text: "正在连接可信宿主…" }),
          el("p", { class: "muted", text: "宿主就绪前不会发送任何咨询。" }),
        ],
      }),
    ],
  });
}

function notReadyCard(): HTMLElement {
  const error = state.hostError ?? state.error;
  const code = error?.code ?? state.host?.host.reason_code ?? null;
  const detail =
    error?.message ??
    state.host?.host.reason_detail ??
    "可信宿主没有报告更多信息。";
  const button = el("button", {
    class: "quiet-button",
    text: "重新连接",
    attrs: { type: "button", id: "retry-host" },
  });
  button.addEventListener("click", () => void boot());
  return el("section", {
    class: "system-card system-card-warn",
    children: [
      el("div", { class: "system-icon is-warn", children: [icon("alert")] }),
      el("div", {
        children: [
          el("h2", { text: "宿主未就绪" }),
          el("p", { class: "state-lead", text: reasonLabel(code, null) }),
          el("p", { class: "muted", text: detail }),
          el("p", {
            class: "muted",
            text: "发送保持禁用；本窗口不会伪造授权或结果。",
          }),
          button,
        ],
      }),
    ],
  });
}

function unknownCard(): HTMLElement {
  const button = el("button", {
    class: "quiet-button",
    text: "开始新的提问",
    attrs: { type: "button" },
  });
  button.addEventListener("click", () => newQuestion());
  return el("section", {
    class: "system-card system-card-danger",
    children: [
      el("div", { class: "system-icon is-danger", children: [icon("alert")] }),
      el("div", {
        children: [
          el("h2", { text: "结果未知" }),
          el("p", {
            class: "state-lead",
            text: "请求可能已经到达宿主，但没有收到完成响应。",
          }),
          el("p", {
            class: "muted",
            text: "不会自动重发，也不会读取本地缓存冒充正式结果；目前没有权威恢复接口。",
          }),
          el("p", {
            class: "muted",
            text: "请以一个新的提问重新开始。明确失败与结果未知是两个不同的状态。",
          }),
          button,
        ],
      }),
    ],
  });
}

function busyCard(): HTMLElement {
  const button = el("button", {
    class: "quiet-button",
    text: "返回输入",
    attrs: { type: "button" },
  });
  button.addEventListener("click", () => newQuestion());
  return el("section", {
    class: "system-card system-card-warn",
    children: [
      el("div", { class: "system-icon is-warn", children: [icon("clock")] }),
      el("div", {
        children: [
          el("h2", { text: "已有一次咨询正在处理" }),
          el("p", {
            class: "state-lead",
            text: "第二个请求没有排队，也没有创建新的 interaction、输入或 Attempt。",
          }),
          el("p", {
            class: "muted",
            text: "本窗口不会自动重发第一个咨询，也不会伪造它的结果。",
          }),
          button,
        ],
      }),
    ],
  });
}

function reviewPlaceholder(): HTMLElement {
  const back = el("button", {
    class: "quiet-button",
    text: "返回 Consult",
    attrs: { type: "button" },
  });
  back.addEventListener("click", () => switchWorkspace("consult"));
  return el("section", {
    class: "review-placeholder",
    children: [
      el("p", { class: "eyebrow", text: "REVIEW · 认知审查" }),
      el("h1", { text: "Review 尚未实现（C12c）" }),
      el("p", {
        class: "state-lead",
        text: "C12b 只实现 Consult。这里没有可执行候选、批准或拒绝。",
      }),
      el("ul", {
        class: "plain-list",
        children: [
          el("li", { text: "没有候选枚举或候选差异投影。" }),
          el("li", { text: "没有批准、拒绝或提交入口。" }),
          el("li", { text: "没有从 Consult 回答直达 Review 的按钮（D7=B）。" }),
        ],
      }),
      el("p", {
        class: "muted",
        text: "Review 的正式实现、候选投影与权限发放在 C12c；本卡不提前实现，也不伪造占位动作。",
      }),
      back,
    ],
  });
}

// --------------------------------------------------------------------------
// composer and confirmation
// --------------------------------------------------------------------------

function syncComposer(): void {
  const isReview = workspace === "review";
  shell.footer.classList.toggle("hidden", isReview);
  if (isReview) {
    clear(shell.banner);
    shell.banner.classList.add("hidden");
    return;
  }
  const blocked =
    state.phase === "sending" ||
    state.phase === "booting" ||
    state.phase === "not-ready" ||
    !state.host?.host.ready;
  shell.textarea.disabled = blocked;
  shell.sendButton.disabled = blocked || !sendEnabled(state);
  if (shell.textarea.value !== state.question) {
    shell.textarea.value = state.question;
  }
  const placeholder =
    state.phase === "sending"
      ? "正在处理本次咨询…"
      : state.phase === "not-ready"
        ? "宿主未就绪，暂时无法发送"
        : state.result
          ? "继续输入将开始新的独立咨询…"
          : "写下你想咨询的问题…";
  shell.textarea.placeholder = placeholder;
  shell.target.textContent = destinationLabel(state.destination);
  shell.target.classList.toggle("is-not-ready", !state.destination);
  shell.counter.textContent = `${countCharacters(state.question)} / ${maxQuestionChars(state)}`;
  shell.hint.textContent =
    state.phase === "sending"
      ? "正在等待权威结果；本次无法取消或重发"
      : state.phase === "not-ready" || !state.host?.host.ready
        ? "宿主未就绪；发送已禁用"
        : state.result
          ? "发送新问题会替换当前结果"
          : "Enter 换行 · Ctrl+Enter 发送";
  const label = shell.sendButton.querySelector(".send-label");
  if (label) {
    label.textContent = state.phase === "sending" ? "处理中" : "发送";
  }
  shell.sendButton.classList.toggle("is-sending", state.phase === "sending");
  autoSizeTextarea();
  renderBanner();
}

function autoSizeTextarea(): void {
  shell.textarea.style.height = "auto";
  shell.textarea.style.height = `${Math.min(shell.textarea.scrollHeight, 168)}px`;
}

function renderBanner(): void {
  clear(shell.banner);
  const show =
    state.confirmPending && needsConfirmation(state) && state.destination !== null;
  shell.banner.classList.toggle("hidden", !show);
  if (!show) {
    return;
  }
  const destination = state.destination!;
  const cancel = el("button", {
    class: "quiet-button",
    text: "返回检查",
    attrs: { type: "button", id: "confirm-cancel" },
  });
  const confirm = el("button", {
    class: "confirm-button",
    text: "确认发送",
    attrs: { type: "button", id: "confirm-send" },
  });
  cancel.addEventListener("click", () => apply({ type: "confirm-cancel" }));
  confirm.addEventListener("click", () => void confirmAndSend());
  shell.banner.append(
    el("div", {
      class: "confirm-copy",
      children: [
        icon("alert"),
        el("div", {
          children: [
            el("strong", { text: "本次将发送到外部模型" }),
            el("p", {
              text: `${destination.provider} / ${destination.model}。确认只代表本次用户意图，不是授权凭据。`,
            }),
          ],
        }),
      ],
    }),
    el("div", { class: "confirm-actions", children: [cancel, confirm] }),
  );
}

function onQuestionInput(event: Event): void {
  const textarea = event.target as HTMLTextAreaElement;
  state = reduce(state, { type: "draft", question: textarea.value });
  shell.counter.textContent = `${countCharacters(state.question)} / ${maxQuestionChars(state)}`;
  shell.sendButton.disabled = !sendEnabled(state);
  autoSizeTextarea();
}

function onQuestionKeydown(event: KeyboardEvent): void {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    if (composing || event.isComposing) {
      return;
    }
    event.preventDefault();
    void requestSend();
  }
}

// --------------------------------------------------------------------------
// actions
// --------------------------------------------------------------------------

async function requestSend(): Promise<void> {
  if (!sendEnabled(state)) {
    if (!state.host?.host.ready) {
      state = { ...state, notice: "宿主未就绪，发送已禁用。" };
      render();
    }
    shell.textarea.focus({ preventScroll: true });
    return;
  }
  if (needsConfirmation(state)) {
    apply({ type: "send-request" });
    shell.banner.querySelector<HTMLButtonElement>("#confirm-send")?.focus();
    return;
  }
  apply({ type: "send-request" });
  await performConsult();
}

async function confirmAndSend(): Promise<void> {
  const destination = state.destination;
  const sessionId = state.host?.session_id;
  if (!destination || !sessionId) {
    return;
  }
  let reply: HostReply;
  try {
    reply = await confirmDestination(sessionId, destination.destination_id);
  } catch (error) {
    apply({ type: "error", error: invokeError(error) });
    return;
  }
  if (!reply.ok) {
    apply({ type: "error", error: replyError(reply) });
    return;
  }
  const result = reply.result as ConfirmResult;
  if (!isRecord(result) || result.confirmed !== true) {
    apply({
      type: "error",
      error: {
        code: "CONFIRMATION_REQUIRED",
        message: "宿主没有确认当前目的地",
        result_unknown: false,
      },
    });
    return;
  }
  apply({ type: "confirm-accepted" });
  await performConsult();
}

async function performConsult(): Promise<void> {
  let reply: HostReply;
  try {
    reply = await consultInteract(buildConsultParams(state));
  } catch (error) {
    apply({ type: "error", error: invokeError(error) });
    return;
  }
  if (!reply.ok) {
    apply({ type: "error", error: replyError(reply) });
    await refreshHost();
    return;
  }
  if (!isConsultResult(reply.result)) {
    apply({
      type: "error",
      error: {
        code: "HOST_INTERNAL",
        message: "宿主返回了无法识别的咨询结果",
        result_unknown: false,
      },
    });
    return;
  }
  apply({ type: "result", result: reply.result });
  shell.textarea.focus({ preventScroll: true });
  await refreshHost();
}

function newQuestion(): void {
  apply({ type: "new-question" });
  shell.textarea.focus({ preventScroll: true });
}

async function copyReply(): Promise<void> {
  const reply = state.result?.reply;
  if (!reply) {
    return;
  }
  try {
    await navigator.clipboard.writeText(reply);
    showToast("回答已复制");
  } catch {
    const helper = el("textarea", { class: "visually-hidden" });
    helper.value = reply;
    document.body.appendChild(helper);
    helper.select();
    try {
      document.execCommand("copy");
      showToast("回答已复制");
    } catch {
      showToast("复制失败；请手动选择回答文本");
    }
    document.body.removeChild(helper);
  }
}

// --------------------------------------------------------------------------
// boot / host refresh
// --------------------------------------------------------------------------

async function boot(): Promise<void> {
  state = reduce(state, { type: "retry-host" });
  render();
  for (let attempt = 0; attempt < 24; attempt += 1) {
    let reply: HostReply;
    try {
      reply = await appInfo();
    } catch (error) {
      if (attempt < 23) {
        await sleep(250);
        continue;
      }
      apply({ type: "host-error", error: invokeError(error) });
      return;
    }
    if (reply.ok && isAppInfo(reply.result)) {
      apply({ type: "host-info", info: reply.result });
      shell.textarea.focus({ preventScroll: true });
      return;
    }
    const error = replyError(reply);
    if (error.code === "SIDECAR_UNAVAILABLE" && attempt < 23) {
      await sleep(250);
      continue;
    }
    apply({ type: "host-error", error });
    return;
  }
}

async function refreshHost(): Promise<void> {
  try {
    const reply = await appInfo();
    if (reply.ok && isAppInfo(reply.result)) {
      const previous = state.destination?.destination_id ?? null;
      const next = reply.result.destination?.destination_id ?? null;
      state = reduce(state, { type: "host-info", info: reply.result });
      if (previous && next && previous !== next) {
        state = reduce(state, { type: "destination-changed" });
      }
      render();
      return;
    }
    if (!reply.ok) {
      state = reduce(state, { type: "host-error", error: replyError(reply) });
      render();
    }
  } catch {
    // Never replace an authoritative result with a local guess.
  }
}

// --------------------------------------------------------------------------
// drawer
// --------------------------------------------------------------------------

function openDrawer(tab: DrawerTab): void {
  if (!state.drawer.open) {
    drawerTrigger = document.activeElement as HTMLElement | null;
  }
  state = reduce(state, { type: "drawer-open", tab });
  renderDrawer();
}

function closeDrawer(): void {
  state = reduce(state, { type: "drawer-close" });
  renderDrawer();
  if (drawerTrigger && document.contains(drawerTrigger)) {
    drawerTrigger.focus({ preventScroll: true });
  }
  drawerTrigger = null;
}

function renderDrawer(): void {
  const open = state.drawer.open;
  shell.drawer.classList.toggle("hidden", !open);
  shell.drawerBackdrop.classList.toggle("hidden", !open);
  document.body.classList.toggle("drawer-open", open);
  if (open && !drawerWasOpen) {
    shell.drawerClose.focus({ preventScroll: true });
  }
  drawerWasOpen = open;
  if (!open) {
    return;
  }
  const titles: Record<DrawerTab, string> = {
    sources: "来源与详情",
    audit: "审计与引用",
    destination: "目的地",
    settings: "设置",
  };
  shell.drawerTitle.textContent = titles[state.drawer.tab];
  clear(shell.drawerTabs);
  const tabs: Array<[DrawerTab, string]> = [
    ["sources", "来源"],
    ["audit", "审计"],
    ["destination", "目的地"],
    ["settings", "设置"],
  ];
  for (const [tab, label] of tabs) {
    const button = el("button", {
      class: `drawer-tab ${state.drawer.tab === tab ? "is-active" : ""}`,
      text: label,
      attrs: {
        type: "button",
        role: "tab",
        "aria-selected": state.drawer.tab === tab ? "true" : "false",
      },
    });
    button.addEventListener("click", () => {
      state = reduce(state, { type: "drawer-tab", tab });
      renderDrawer();
    });
    shell.drawerTabs.appendChild(button);
  }
  clear(shell.drawerBody);
  if (state.drawer.tab === "sources") {
    renderSourcesTab();
  } else if (state.drawer.tab === "audit") {
    renderAuditTab();
  } else if (state.drawer.tab === "destination") {
    renderDestinationTab();
  } else {
    renderSettingsTab();
  }
}

function drawerSection(title: string, children: HTMLElement[]): HTMLElement {
  return el("section", {
    class: "drawer-section",
    children: [el("h3", { text: title }), ...children],
  });
}

function renderSourcesTab(): void {
  const result = state.result;
  if (!result) {
    shell.drawerBody.append(
      el("p", {
        class: "muted",
        text: "本次还没有可展示的来源引用。发起一次咨询后，来源摘要会显示在这里。",
      }),
    );
  } else {
    const rows = sourceRows(result);
    const list = el("div", { class: "source-list" });
    for (const row of rows) {
      list.appendChild(
        el("div", {
          class: "source-row",
          children: [
            el("div", {
              class: "source-main",
              children: [
                el("span", { class: "source-kind", text: row.kind }),
                el("span", { class: "source-id", text: row.id }),
              ],
            }),
            el("span", { class: "source-version", text: row.version }),
            el("span", {
              class: `source-status ${statusClass(row.status)}`,
              text: row.status,
            }),
            el("span", { class: "source-note", text: row.note }),
          ],
        }),
      );
    }
    shell.drawerBody.append(
      drawerSection("来源（只显示正式公共字段）", [
        list,
        el("p", {
          class: "muted",
          text: "来源按宿主登记的输入、确定性查询产物与查询来源展示；未显示的来源不在这里推断。",
        }),
      ]),
    );
  }
  shell.drawerBody.append(
    el("div", {
      class: "gap-box",
      children: [
        el("strong", { text: "暂不可用 / 缺口" }),
        el("p", {
          text: "完整 Context 来源清单、Attempt 明细与 Prompt 当前没有正式公共读取接口；本窗口不绕过 Facade 拼装，也不直接查询数据库。",
        }),
      ],
    }),
  );
}

function renderAuditTab(): void {
  const result = state.result;
  if (!result) {
    shell.drawerBody.append(
      el("p", { class: "muted", text: "本次还没有可审查的交互引用。" }),
    );
  } else {
    shell.drawerBody.append(
      drawerSection("本次交互", [
        el("dl", {
          class: "kv",
          children: [
            kv("交互", shortId(result.interaction_id)),
            kv("上下文", shortId(result.context_id)),
            kv("模式", "Consult"),
            kv("查询", queryStatusLabel(result.query)),
            kv("查询策略", result.query?.policy_version ?? "暂无查询"),
            kv("尝试", "暂不可用（缺正式公共读取接口）"),
            kv("回执", "尚未生成回执"),
            kv("目的地", shortId(result.destination.destination_id)),
          ],
        }),
        el("p", {
          class: "muted",
          text: "只显示可审查的公共字段；不显示 state_revision、内部 omission 计数或原因、Prompt、HTTP 正文或数据库列。",
        }),
      ]),
    );
  }
  shell.drawerBody.append(
    el("div", {
      class: "gap-box",
      children: [
        el("strong", { text: "缺口说明" }),
        el("p", {
          text: "Consult 的 Attempt 明细与完整 Context 来源在 C12c 之前没有公共读取接口；本窗口将它们标为暂不可用，而不是补字段或读库。",
        }),
      ],
    }),
  );
}

function renderDestinationTab(): void {
  const destination = state.destination;
  if (!destination) {
    shell.drawerBody.append(
      el("p", { class: "muted", text: "宿主尚未提供目的地描述。发送保持禁用。" }),
    );
    return;
  }
  const confirmed = isConfirmed(state);
  shell.drawerBody.append(
    drawerSection("目的地", [
      el("dl", {
        class: "kv",
        children: [
          kv("类型", destinationKindLabel(destination)),
          kv("提供方", destination.provider),
          kv("模型", destination.model),
          kv("标识", shortId(destination.destination_id)),
          kv("清单哈希", shortId(destination.manifest_hash)),
          kv("策略", destination.policy_ref),
          kv(
            "本会话确认",
            destination.confirmation_required
              ? confirmed
                ? "已确认（宿主记录）"
                : "未确认"
              : "本地目的地不需要确认",
          ),
        ],
      }),
    ]),
    drawerSection("发送范围", [
      el("ul", {
        class: "plain-list",
        children: [
          el("li", { text: "包括：你的问题、宿主登记的输入、当前授权上下文摘要。" }),
          el("li", { text: "不包括：密钥、数据库内部字段、未授权内容、其他 namespace 数据。" }),
          el("li", { text: "目的地由可信宿主配置；本窗口不能修改 provider 或 model。" }),
        ],
      }),
    ]),
    el("p", {
      class: "muted",
      text: "外部目的地在首次使用或目的地变化后需要本会话中的显式确认；本地目的地不弹出确认。C09 发送门仍执行最终授权检查。",
    }),
  );
}

function renderSettingsTab(): void {
  const info = state.host;
  const name = aiDisplayName(info);
  const nameInput = el("input", {
    class: "setting-input",
    attrs: {
      id: "ai-name",
      type: "text",
      maxlength: "40",
      placeholder: "助手",
      autocomplete: "off",
    },
  });
  nameInput.value = name;
  const status = el("p", { class: "setting-status", attrs: { id: "ai-name-status" } });
  const save = el("button", {
    class: "quiet-button",
    text: "保存显示名",
    attrs: { type: "button", id: "save-ai-name" },
  });
  const reset = el("button", {
    class: "ghost-button",
    text: "恢复默认",
    attrs: { type: "button", id: "reset-ai-name" },
  });
  const submit = async (value: string): Promise<void> => {
    status.textContent = "";
    status.className = "setting-status";
    let reply: HostReply;
    try {
      reply = await setAiDisplayName(value);
    } catch (error) {
      status.textContent = invokeError(error).message;
      status.classList.add("is-error");
      return;
    }
    if (!reply.ok) {
      status.textContent = replyError(reply).message;
      status.classList.add("is-error");
      return;
    }
    const result = reply.result as { ai_display_name: string; source: string };
    if (state.host) {
      state = {
        ...state,
        host: {
          ...state.host,
          ui: {
            ai_display_name: result.ai_display_name,
            source: result.source,
          },
        },
      };
    }
    render();
    showToast(`对话显示名已更新为 ${result.ai_display_name}`);
  };
  save.addEventListener("click", () => void submit(nameInput.value));
  reset.addEventListener("click", () => void submit(""));
  nameInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      void submit(nameInput.value);
    }
  });

  shell.drawerBody.append(
    drawerSection("对话显示名", [
      el("p", {
        class: "muted",
        text: "只改变本窗口的 AI 对话显示名；不改变 PCS 内部身份、来源身份、namespace、授权或 Persona 语义。",
      }),
      el("div", { class: "setting-row", children: [nameInput, save, reset] }),
      status,
      el("p", {
        class: "muted",
        text: "显示名保存在应用配置目录，不在仓库、安装包或 PCS 数据实例中。未设置时使用宿主配置或中性的“助手”。",
      }),
    ]),
    drawerSection("宿主与连接", [
      el("dl", {
        class: "kv",
        children: [
          kv("namespace", info?.host.namespace ?? "—"),
          kv("host_instance", info?.host.host_instance ?? "—"),
          kv("operator", info?.host.operator ?? "—"),
          kv("会话", shortId(info?.session_id)),
          kv("保留策略", info?.host.retention ?? "—"),
          kv("目的地方案", info?.destination?.policy_ref ?? "—"),
          kv("宿主状态", info?.host.ready ? "就绪" : reasonLabel(info?.host.reason_code ?? null, null)),
        ],
      }),
    ]),
    drawerSection("软件", [
      el("dl", {
        class: "kv",
        children: [
          kv("品牌", "QUINTESSENCE"),
          kv("版本", info ? `${info.version} · ${info.protocol}` : "—"),
          kv("工作区", "Consult（C12b）"),
          kv("PCS", info ? `${info.pcs.version} · schema ${info.pcs.schema_migrations}` : "—"),
          kv("外观", "深色主题（品牌固定）"),
        ],
      }),
    ]),
  );
}

function statusClass(status: string): string {
  if (status === "当前" || status === "本次登记") {
    return "is-current";
  }
  if (status === "已过期") {
    return "is-expired";
  }
  if (status === "历史") {
    return "is-history";
  }
  return "";
}

function kv(label: string, value: string): DocumentFragment {
  const fragment = document.createDocumentFragment();
  fragment.appendChild(el("dt", { text: label }));
  fragment.appendChild(el("dd", { text: value }));
  return fragment;
}

// --------------------------------------------------------------------------
// widgets
// --------------------------------------------------------------------------

function spinner(): HTMLElement {
  return el("span", { class: "spinner", attrs: { "aria-hidden": "true" } });
}

function showToast(message: string): void {
  shell.toast.textContent = message;
  shell.toast.classList.remove("hidden");
  if (toastTimer !== null) {
    window.clearTimeout(toastTimer);
  }
  toastTimer = window.setTimeout(() => {
    shell.toast.classList.add("hidden");
    toastTimer = null;
  }, 2600);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

// --------------------------------------------------------------------------
// start
// --------------------------------------------------------------------------

buildShell();
void boot();
