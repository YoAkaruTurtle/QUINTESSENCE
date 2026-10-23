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
import {
  approveEnabled,
  buildGenerateParams,
  buildResolveParams,
  buildResultParams,
  candidateStatusLabel,
  candidateStatusTone,
  canStartResolve,
  diffFieldLabel,
  eligibilityLabel,
  formatReviewValue,
  generateBlockReason,
  generateEnabled,
  INITIAL_REVIEW_STATE,
  isLegacySelection,
  operationLabel,
  receiptOperationLabel,
  receiptVisible,
  recoveryInteractionId,
  rejectEnabled,
  reviewAvailable,
  reviewCanRecover,
  reviewCaps,
  reviewErrorText,
  reviewReasonLabel,
  reviewReduce,
  reviewViewLabel,
  resolveBlockReason,
  sourceAvailabilityLabel,
  sourceKindLabel,
  sourceReasonLabel,
  sourceRefId,
  sourceRefLabel,
  splitDiff,
  type ReviewAction,
  type ReviewDecision,
  type ReviewState,
} from "./review";
import type {
  AppInfo,
  BridgeError,
  ConfirmResult,
  ConsultResult,
  HostReply,
  LegacyWorkItem,
  ReviewCandidateDetail,
  ReviewCandidateList,
  ReviewCandidateSummary,
  ReviewView,
  ReviewWorkItem,
  ReviewWorkList,
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

async function reviewWorkList(sessionId: string): Promise<HostReply> {
  return (await invoke("review_work_list", { sessionId })) as HostReply;
}

async function reviewCandidates(
  sessionId: string,
  workId: string,
  generation: number | null = null,
): Promise<HostReply> {
  return (await invoke("review_candidates", {
    sessionId,
    workId,
    generation,
  })) as HostReply;
}

async function reviewCandidateDetail(
  sessionId: string,
  candidateId: string,
  artifactVersion: number,
): Promise<HostReply> {
  return (await invoke("review_candidate_detail", {
    sessionId,
    candidateId,
    artifactVersion,
  })) as HostReply;
}

async function reviewGenerate(params: {
  sessionId: string;
  workId: string;
  eventId: string;
  confirmedDestinationId: string | null;
}): Promise<HostReply> {
  return (await invoke("review_generate", params)) as HostReply;
}

async function reviewResolve(params: {
  sessionId: string;
  candidateId: string;
  artifactVersion: number;
  decision: ReviewDecision;
}): Promise<HostReply> {
  return (await invoke("review_resolve", params)) as HostReply;
}

async function reviewResult(params: {
  sessionId: string;
  interactionId: string;
}): Promise<HostReply> {
  return (await invoke("review_result", params)) as HostReply;
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

function isReviewWorkList(value: unknown): value is ReviewWorkList {
  return (
    isRecord(value) &&
    Array.isArray(value.managed) &&
    Array.isArray(value.legacy)
  );
}

function isReviewCandidateList(value: unknown): value is ReviewCandidateList {
  return isRecord(value) && Array.isArray(value.candidates);
}

function isReviewCandidateDetail(value: unknown): value is ReviewCandidateDetail {
  return (
    isRecord(value) &&
    isRecord(value.candidate) &&
    typeof value.candidate.candidate_id === "string" &&
    typeof value.candidate.artifact_version === "number" &&
    typeof value.can_approve === "boolean" &&
    typeof value.can_reject === "boolean" &&
    Array.isArray(value.diff) &&
    Array.isArray(value.sources)
  );
}

function isReviewView(value: unknown): value is ReviewView {
  return (
    isRecord(value) &&
    typeof value.state === "string" &&
    typeof value.redacted === "boolean" &&
    "receipt" in value
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
let review: ReviewState = INITIAL_REVIEW_STATE;
let workspace: "consult" | "review" = "consult";
let composing = false;
let drawerWasOpen = false;
let drawerTrigger: HTMLElement | null = null;
let toastTimer: number | null = null;
let reviewRefreshing = false;
let reviewModalWasOpen = false;

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
  reviewOverlay: HTMLElement;
  reviewModal: HTMLElement;
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
  const reviewModal = el("div", {
    class: "review-modal",
    attrs: {
      id: "review-modal",
      role: "dialog",
      "aria-modal": "true",
      "aria-labelledby": "review-modal-title",
    },
  });
  const reviewOverlay = el("div", {
    class: "review-overlay hidden",
    attrs: { id: "review-overlay" },
    children: [reviewModal],
  });
  reviewOverlay.addEventListener("click", (event) => {
    if (event.target === reviewOverlay) {
      closeReviewModal();
    }
  });

  root.append(
    topbar,
    main,
    footer,
    drawerBackdrop,
    drawer,
    reviewOverlay,
    toast,
    announcer,
  );
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
    reviewOverlay,
    reviewModal,
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
  if (event.key === "Escape" && isReviewModalOpen()) {
    event.preventDefault();
    closeReviewModal();
    return;
  }
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
  renderReviewOverlay();
  root.setAttribute("data-phase", state.phase);
  root.setAttribute("data-workspace", workspace);
  root.setAttribute("data-review-screen", review.screen);
  root.setAttribute("data-review-tab", review.tab);
  root.setAttribute("data-host-ready", state.host?.host.ready ? "true" : "false");
  root.setAttribute(
    "data-confirmed",
    state.destination && isConfirmed(state) ? "true" : "false",
  );
  root.setAttribute("data-destination", state.destination?.destination_id ?? "");
  shell.announcer.textContent =
    workspace === "review"
      ? `Review · ${review.screen}`
      : `Consult · ${phaseTitle(state.phase)}`;
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
  if (next === "review") {
    closeReviewModal();
  }
  render();
  if (next === "consult") {
    window.setTimeout(() => shell.textarea.focus({ preventScroll: true }), 0);
  } else {
    shell.main.focus({ preventScroll: true });
    void ensureReviewLoaded();
  }
}

function renderMain(): void {
  clear(shell.main);
  const conversation = el("div", {
    class: workspace === "review" ? "conversation conversation-review" : "conversation",
  });
  const inner = el("div", {
    class:
      workspace === "review"
        ? "conversation-inner review-inner"
        : "conversation-inner",
  });
  conversation.appendChild(inner);
  shell.main.appendChild(conversation);

  if (workspace === "review") {
    inner.appendChild(reviewWorkspace());
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

// --------------------------------------------------------------------------
// review workspace
// --------------------------------------------------------------------------

function applyReview(action: ReviewAction): void {
  review = reviewReduce(review, action);
  render();
}

function reviewSession(): string {
  return state.host?.session_id ?? "";
}

function reviewInvalid(what: string): BridgeError {
  return {
    code: "HOST_INTERNAL",
    message: `宿主返回了无法识别的${what}`,
    result_unknown: false,
  };
}

async function ensureReviewLoaded(): Promise<void> {
  if (workspace !== "review" || review.loaded || review.loading) {
    return;
  }
  if (!reviewAvailable(state.host)) {
    return;
  }
  await loadReviewWorks();
}

async function loadReviewWorks(): Promise<void> {
  const session = reviewSession();
  if (!session) {
    return;
  }
  applyReview({ type: "works-loading" });
  let reply: HostReply;
  try {
    reply = await reviewWorkList(session);
  } catch (error) {
    applyReview({ type: "works-error", error: invokeError(error) });
    return;
  }
  if (!reply.ok) {
    const error = replyError(reply);
    applyReview({ type: "works-error", error });
    if (
      error.code === "SESSION_MISMATCH" ||
      error.code === "HOST_NOT_READY" ||
      error.code === "DATA_LOCKED"
    ) {
      await refreshHost();
    }
    return;
  }
  if (!isReviewWorkList(reply.result)) {
    applyReview({ type: "works-error", error: reviewInvalid("工作列表") });
    return;
  }
  applyReview({
    type: "works-loaded",
    managed: reply.result.managed,
    legacy: reply.result.legacy,
  });
}

async function loadReviewCandidates(workId: string): Promise<void> {
  const session = reviewSession();
  if (!session) {
    return;
  }
  applyReview({ type: "candidates-loading" });
  let reply: HostReply;
  try {
    reply = await reviewCandidates(session, workId, null);
  } catch (error) {
    if (review.selectedWorkId === workId) {
      applyReview({ type: "candidates-error", error: invokeError(error) });
    }
    return;
  }
  if (review.selectedWorkId !== workId) {
    return;
  }
  if (!reply.ok) {
    applyReview({ type: "candidates-error", error: replyError(reply) });
    return;
  }
  if (!isReviewCandidateList(reply.result)) {
    applyReview({
      type: "candidates-error",
      error: reviewInvalid("候选列表"),
    });
    return;
  }
  applyReview({
    type: "candidates-loaded",
    candidates: reply.result.candidates,
  });
}

async function loadReviewDetail(
  candidate: ReviewCandidateSummary,
): Promise<void> {
  const session = reviewSession();
  if (!session) {
    return;
  }
  applyReview({ type: "detail-loading" });
  let reply: HostReply;
  try {
    reply = await reviewCandidateDetail(
      session,
      candidate.candidate_id,
      candidate.artifact_version,
    );
  } catch (error) {
    if (
      review.selectedCandidateId === candidate.candidate_id &&
      review.selectedVersion === candidate.artifact_version
    ) {
      applyReview({ type: "detail-error", error: invokeError(error) });
    }
    return;
  }
  if (
    review.selectedCandidateId !== candidate.candidate_id ||
    review.selectedVersion !== candidate.artifact_version
  ) {
    return;
  }
  if (!reply.ok) {
    applyReview({ type: "detail-error", error: replyError(reply) });
    return;
  }
  if (!isReviewCandidateDetail(reply.result)) {
    applyReview({ type: "detail-error", error: reviewInvalid("候选详情") });
    return;
  }
  applyReview({ type: "detail-loaded", detail: reply.result });
}

async function refreshReviewData(): Promise<void> {
  if (reviewRefreshing) {
    return;
  }
  reviewRefreshing = true;
  try {
    const work = review.selectedWork;
    const legacy = review.selectedLegacy;
    const candidate =
      review.selectedCandidateId === null
        ? null
        : review.candidates.find(
            (item) => item.candidate_id === review.selectedCandidateId,
          ) ?? null;
    await loadReviewWorks();
    if (work && review.managed.some((item) => item.work_id === work.work_id)) {
      await loadReviewCandidates(work.work_id);
    } else if (
      legacy &&
      review.legacy.some((item) => item.work_id === legacy.work_id)
    ) {
      applyReview({ type: "legacy-selected", work: legacy });
    }
    if (candidate && review.selectedCandidateId === candidate.candidate_id) {
      const refreshed = review.candidates.find(
        (item) =>
          item.candidate_id === candidate.candidate_id &&
          item.artifact_version === candidate.artifact_version,
      );
      if (refreshed) {
        await loadReviewDetail(refreshed);
      }
    }
  } finally {
    reviewRefreshing = false;
    applyReview({ type: "refresh-consumed" });
  }
}

async function afterReviewError(
  error: BridgeError,
  kind: "generate" | "resolve" | "result",
): Promise<void> {
  if (error.result_unknown) {
    const interactionId = review.lastInteractionId;
    if (interactionId && reviewCaps(state.host).result) {
      await performReviewResult(interactionId, true);
    } else {
      await refreshReviewData();
    }
    return;
  }
  if (
    error.code === "REVISION_CONFLICT" ||
    error.code === "CANDIDATE_STALE" ||
    error.code === "CANDIDATE_VERSION_MISMATCH" ||
    error.code === "NOT_FOUND" ||
    error.code === "REDACTED" ||
    error.code === "CANDIDATE_EXISTS" ||
    error.code === "WORK_ALREADY_RESOLVED" ||
    error.code === "UNSUPPORTED_VERSION" ||
    error.code === "WORK_NOT_PENDING" ||
    error.code === "LEGACY_WORK_READ_ONLY" ||
    error.code === "AUTHORIZATION_REQUIRED"
  ) {
    await refreshReviewData();
    if (error.code === "AUTHORIZATION_REQUIRED") {
      await refreshHost();
    }
  }
  if (kind === "generate" && error.code === "CANDIDATE_EXISTS") {
    applyReview({ type: "note", message: reviewErrorText(error) });
  }
}

function selectManagedWork(work: ReviewWorkItem): void {
  applyReview({ type: "work-selected", work });
  void loadReviewCandidates(work.work_id);
}

function selectLegacyWork(work: LegacyWorkItem): void {
  applyReview({ type: "legacy-selected", work });
}

function selectReviewCandidate(candidate: ReviewCandidateSummary): void {
  applyReview({ type: "candidate-selected", candidate });
  void loadReviewDetail(candidate);
}

function backToReviewWorks(): void {
  applyReview({ type: "works-back" });
}

function backToCandidates(): void {
  if (review.outcome) {
    applyReview({ type: "outcome-dismiss" });
    return;
  }
  applyReview({ type: "note", message: null });
  review = {
    ...review,
    screen:
      review.selectedWork || review.selectedLegacy ? "candidates" : "works",
    detail: null,
    detailError: null,
    selectedCandidateId: null,
    selectedVersion: null,
  };
  render();
}

function confirmedDestinationId(): string | null {
  const destination = state.destination;
  if (!destination) {
    return null;
  }
  return state.confirmedDestinationId === destination.destination_id
    ? destination.destination_id
    : null;
}

function openReviewGenerate(): void {
  const reason = generateBlockReason(review, state.host);
  if (reason !== null) {
    applyReview({ type: "note", message: reason });
    return;
  }
  if (needsConfirmation(state)) {
    applyReview({ type: "generate-confirm", open: true });
    return;
  }
  void performReviewGenerate();
}

async function confirmReviewGenerate(): Promise<void> {
  const destination = state.destination;
  const session = reviewSession();
  if (!destination || !session) {
    return;
  }
  let reply: HostReply;
  try {
    reply = await confirmDestination(session, destination.destination_id);
  } catch (error) {
    applyReview({ type: "action-error", error: invokeError(error), kind: "generate" });
    return;
  }
  if (!reply.ok) {
    applyReview({
      type: "action-error",
      error: replyError(reply),
      kind: "generate",
    });
    return;
  }
  state = reduce(state, {
    type: "destination-confirmed",
    destinationId: destination.destination_id,
  });
  render();
  applyReview({ type: "generate-confirm", open: false });
  await performReviewGenerate(destination.destination_id);
}

async function performReviewGenerate(
  confirmed: string | null = confirmedDestinationId(),
): Promise<void> {
  if (!generateEnabled(review, state.host)) {
    return;
  }
  const session = reviewSession();
  if (!session) {
    return;
  }
  const workId = review.selectedWorkId;
  if (!workId) {
    return;
  }
  const params = buildGenerateParams(session, review, confirmed);
  applyReview({ type: "generate-start" });
  let reply: HostReply;
  try {
    reply = await reviewGenerate(params);
  } catch (error) {
    const bridgeError = invokeError(error);
    applyReview({
      type: "action-error",
      error: bridgeError,
      kind: "generate",
    });
    await afterReviewError(bridgeError, "generate");
    return;
  }
  if (!reply.ok) {
    const bridgeError = replyError(reply);
    applyReview({
      type: "action-error",
      error: bridgeError,
      kind: "generate",
    });
    await afterReviewError(bridgeError, "generate");
    return;
  }
  if (!isReviewView(reply.result)) {
    const bridgeError = reviewInvalid("审查结果");
    applyReview({
      type: "action-error",
      error: bridgeError,
      kind: "generate",
    });
    await afterReviewError(bridgeError, "generate");
    return;
  }
  const view = reply.result;
  applyReview({ type: "generate-result", view });
  await loadReviewWorks();
  if (view.state === "pending" && view.candidate_id) {
    const work = review.managed.find((item) => item.work_id === workId);
    if (work) {
      await loadReviewCandidates(workId);
      const candidate = review.candidates.find(
        (item) => item.candidate_id === view.candidate_id,
      );
      if (candidate) {
        selectReviewCandidate(candidate);
      }
    }
  }
  await refreshHost();
}

function openReviewDecision(decision: ReviewDecision): void {
  if (!canStartResolve(review, state.host, decision)) {
    applyReview({
      type: "note",
      message:
        resolveBlockReason(review, state.host, decision) ??
        "当前不能提交这个决定",
    });
    return;
  }
  applyReview({ type: "decision-confirm", decision });
}

async function performReviewDecision(decision: ReviewDecision): Promise<void> {
  if (!review.detail || !canStartResolve(review, state.host, decision)) {
    return;
  }
  const session = reviewSession();
  if (!session) {
    return;
  }
  const params = buildResolveParams(session, review.detail, decision);
  applyReview({ type: "resolve-start", decision });
  let reply: HostReply;
  try {
    reply = await reviewResolve(params);
  } catch (error) {
    const bridgeError = invokeError(error);
    applyReview({
      type: "action-error",
      error: bridgeError,
      kind: "resolve",
    });
    await afterReviewError(bridgeError, "resolve");
    return;
  }
  if (!reply.ok) {
    const bridgeError = replyError(reply);
    applyReview({
      type: "action-error",
      error: bridgeError,
      kind: "resolve",
    });
    await afterReviewError(bridgeError, "resolve");
    return;
  }
  if (!isReviewView(reply.result)) {
    const bridgeError = reviewInvalid("决定结果");
    applyReview({
      type: "action-error",
      error: bridgeError,
      kind: "resolve",
    });
    await afterReviewError(bridgeError, "resolve");
    return;
  }
  const view = reply.result;
  applyReview({ type: "resolve-result", view });
  await loadReviewWorks();
  if (review.selectedWorkId) {
    await loadReviewCandidates(review.selectedWorkId);
    const candidate = review.candidates.find(
      (item) =>
        item.candidate_id === view.candidate_id ||
        (item.candidate_id === review.lastCandidateId &&
          item.artifact_version === review.lastVersion),
    );
    if (candidate) {
      await loadReviewDetail(candidate);
    }
  }
  await refreshHost();
}

async function performReviewResult(
  interactionId: string,
  auto: boolean = false,
): Promise<void> {
  const session = reviewSession();
  if (!session) {
    return;
  }
  applyReview({ type: "result-start" });
  let reply: HostReply;
  try {
    reply = await reviewResult(buildResultParams(session, interactionId));
  } catch (error) {
    const bridgeError = invokeError(error);
    applyReview({ type: "action-error", error: bridgeError, kind: "result" });
    if (!auto) {
      await afterReviewError(bridgeError, "result");
    }
    return;
  }
  if (!reply.ok) {
    const bridgeError = replyError(reply);
    applyReview({ type: "action-error", error: bridgeError, kind: "result" });
    if (!auto) {
      await afterReviewError(bridgeError, "result");
    }
    return;
  }
  if (!isReviewView(reply.result)) {
    const bridgeError = reviewInvalid("结果恢复");
    applyReview({ type: "action-error", error: bridgeError, kind: "result" });
    if (!auto) {
      await afterReviewError(bridgeError, "result");
    }
    return;
  }
  applyReview({ type: "result-result", view: reply.result });
  await refreshReviewData();
}

function reviewWorkspace(): HTMLElement {
  const info = state.host;
  if (!info) {
    return bootingCard();
  }
  if (!info.host.ready) {
    return notReadyCard();
  }
  if (!reviewAvailable(info)) {
    return reviewUnavailableCard(info);
  }
  const section = el("section", {
    class: "review",
    attrs: {
      "data-review-screen": review.screen,
      "data-review-tab": review.tab,
    },
    children: [
      reviewHeader(info),
      reviewCallouts(),
      el("div", {
        class:
          review.screen === "detail" ||
          review.screen === "result" ||
          review.screen === "reauth" ||
          review.selectedLegacy !== null
            ? "review-grid is-detail"
            : "review-grid",
        children: [reviewWorkPane(info), reviewDetailPane(info)],
      }),
      reviewActionBar(info),
    ],
  });
  return section;
}

function reviewUnavailableCard(info: AppInfo): HTMLElement {
  const refresh = el("button", {
    class: "quiet-button",
    text: "刷新宿主状态",
    attrs: { type: "button", id: "review-refresh-host" },
  });
  refresh.addEventListener("click", () => void refreshHost());
  return el("section", {
    class: "system-card system-card-warn review-unavailable",
    children: [
      el("div", { class: "system-icon is-warn", children: [icon("alert")] }),
      el("div", {
        children: [
          el("h2", { text: "Review 暂不可用" }),
          el("p", {
            class: "state-lead",
            text: reviewReasonLabel(info.workspace.review_reason),
          }),
          el("p", {
            class: "muted",
            text: "审查权限由私有宿主配置；本窗口不能自行赋权，也不能读取数据库补齐。",
          }),
          el("p", {
            class: "muted",
            text: "Consult 不受影响；你可以继续在 Consult 工作区使用。",
          }),
          refresh,
        ],
      }),
    ],
  });
}

function reviewHeader(info: AppInfo): HTMLElement {
  const refresh = el("button", {
    class: "quiet-button",
    text: "刷新",
    attrs: { type: "button", id: "review-refresh" },
  });
  refresh.addEventListener("click", () => void refreshReviewData());
  return el("header", {
    class: "review-head",
    children: [
      el("div", {
        children: [
          el("p", { class: "eyebrow", text: "REVIEW · 认知审查" }),
          el("h1", { text: "Review" }),
          el("p", {
            class: "review-meta",
            text: [
              info.host.namespace ?? "—",
              info.host.host_instance ?? "—",
              info.host.operator ?? "—",
            ].join(" · "),
          }),
        ],
      }),
      el("div", { class: "review-head-actions", children: [refresh] }),
    ],
  });
}

function reviewCallouts(): HTMLElement {
  const children: HTMLElement[] = [];
  if (review.unknown) {
    const recover =
      reviewCaps(state.host).result && recoveryInteractionId(review);
    const row = el("div", {
      class: "review-unknown-actions",
    });
    if (recover) {
      const result = el("button", {
        class: "quiet-button",
        text: "查询权威结果",
        attrs: { type: "button", id: "review-recover" },
      });
      result.addEventListener("click", () => {
        const id = recoveryInteractionId(review);
        if (id) {
          void performReviewResult(id);
        }
      });
      row.appendChild(result);
    }
    const refresh = el("button", {
      class: "quiet-button",
      text: "刷新工作与候选",
      attrs: { type: "button" },
    });
    refresh.addEventListener("click", () => void refreshReviewData());
    row.appendChild(refresh);
    children.push(
      el("div", {
        class: "callout callout-danger review-unknown",
        children: [
          icon("alert"),
          el("div", {
            children: [
              el("strong", { text: "结果未知" }),
              el("p", { text: review.unknown.message }),
              el("p", {
                class: "muted",
                text: "不会自动重发本次审查或决定；只以权威结果恢复。",
              }),
              row,
            ],
          }),
        ],
      }),
    );
  }
  if (review.conflict) {
    children.push(callout(review.conflict, "warn"));
  }
  if (review.notice) {
    children.push(callout(review.notice, "info"));
  }
  if (review.error) {
    children.push(callout(reviewErrorText(review.error), "danger"));
  }
  return el("div", { class: "review-callouts", children });
}

function reviewWorkPane(info: AppInfo): HTMLElement {
  const tabs = el("div", {
    class: "review-tabs",
    attrs: { role: "tablist", "aria-label": "审查工作列表" },
  });
  const managedTab = el("button", {
    class: `review-tab ${review.tab === "managed" ? "is-active" : ""}`,
    text: "待审查",
    attrs: {
      type: "button",
      role: "tab",
      "aria-selected": review.tab === "managed" ? "true" : "false",
    },
  });
  managedTab.addEventListener("click", () => applyReview({ type: "tab", tab: "managed" }));
  const legacyTab = el("button", {
    class: `review-tab ${review.tab === "legacy" ? "is-active" : ""}`,
    text: "旧工作项（无控制）",
    attrs: {
      type: "button",
      role: "tab",
      "aria-selected": review.tab === "legacy" ? "true" : "false",
    },
  });
  legacyTab.addEventListener("click", () => applyReview({ type: "tab", tab: "legacy" }));
  tabs.append(managedTab, legacyTab);

  const body = el("div", { class: "review-work-list" });
  if (review.loading && !review.loaded) {
    body.appendChild(reviewSkeleton("正在读取工作列表…", 3));
  } else if (review.error) {
    const retry = el("button", {
      class: "quiet-button",
      text: "重新加载",
      attrs: { type: "button" },
    });
    retry.addEventListener("click", () => void loadReviewWorks());
    body.append(
      callout(reviewErrorText(review.error), "danger"),
      retry,
    );
  } else if (review.tab === "managed") {
    if (review.managed.length === 0) {
      body.appendChild(
        el("p", {
          class: "review-empty",
          text: "没有待审查的工作项。新的工作项由宿主在处理事件时创建。",
        }),
      );
    }
    for (const work of review.managed) {
      body.appendChild(reviewManagedRow(work));
    }
  } else {
    if (review.legacy.length === 0) {
      body.appendChild(
        el("p", { class: "review-empty", text: "没有旧工作项。" }),
      );
    }
    for (const work of review.legacy) {
      body.appendChild(reviewLegacyRow(work));
    }
  }
  return el("aside", {
    class: "review-pane review-workpane",
    attrs: { "aria-label": "审查工作项" },
    children: [
      el("div", {
        class: "review-pane-head",
        children: [
          el("h2", { text: "工作列表" }),
          el("span", {
            class: "review-visible-count",
            text: `${review.managed.length} 待审查 · ${review.legacy.length} 旧项`,
          }),
        ],
      }),
      tabs,
      body,
      el("p", {
        class: "review-pane-note",
        text: "只显示本 namespace 的待处理工作；不显示候选数量或内部 generation。",
      }),
    ],
  });
}

function reviewManagedRow(work: ReviewWorkItem): HTMLElement {
  const active = review.selectedWork?.work_id === work.work_id;
  const row = el("button", {
    class: `review-work-row ${active ? "is-active" : ""}`,
    attrs: {
      type: "button",
      "data-work-id": work.work_id,
      "data-blocked": work.blocked ? "true" : "false",
      "data-rerun": work.rerun_registered ? "true" : "false",
      title: `generation ${work.generation}`,
    },
    children: [
      el("div", {
        class: "review-work-top",
        children: [
          el("span", { class: "review-work-id", text: `工作 ${shortId(work.work_id)}` }),
          reviewBadge(
            work.blocked ? "已暂停" : "待处理",
            work.blocked ? "warn" : "pending",
          ),
        ],
      }),
      el("div", {
        class: "review-work-meta",
        children: [
          el("span", { text: `事件 ${shortId(work.event_id)}` }),
          ...(work.rerun_registered
            ? [
                reviewBadge(
                  "复查许可已登记（待校验）",
                  "neutral",
                ),
              ]
            : []),
        ],
      }),
    ],
  });
  row.addEventListener("click", () => selectManagedWork(work));
  return row;
}

function reviewLegacyRow(work: LegacyWorkItem): HTMLElement {
  const active = review.selectedLegacy?.work_id === work.work_id;
  const row = el("button", {
    class: `review-work-row review-legacy-row ${active ? "is-active" : ""}`,
    attrs: {
      type: "button",
      "data-work-id": work.work_id,
      "data-legacy": "true",
    },
    children: [
      el("div", {
        class: "review-work-top",
        children: [
          el("span", { class: "review-work-id", text: `工作 ${shortId(work.work_id)}` }),
          reviewBadge("只读", "neutral"),
        ],
      }),
      el("div", {
        class: "review-work-meta",
        children: [
          el("span", { text: `事件 ${shortId(work.event_id)}` }),
          el("span", { text: `类型 ${work.kind}` }),
          el("span", { text: work.created_at }),
        ],
      }),
    ],
  });
  row.addEventListener("click", () => selectLegacyWork(work));
  return row;
}

function reviewBadge(label: string, tone: string): HTMLElement {
  return el("span", {
    class: `review-badge is-${tone}`,
    text: label,
  });
}

function reviewSkeleton(label: string, rows: number): HTMLElement {
  const children: HTMLElement[] = [
    el("p", { class: "muted", text: label }),
  ];
  for (let index = 0; index < rows; index += 1) {
    children.push(el("div", { class: "review-skeleton-row" }));
  }
  return el("div", { class: "review-skeleton", children });
}

function reviewDetailPane(info: AppInfo): HTMLElement {
  const children: HTMLElement[] = [];
  if (review.screen === "result" && review.outcome) {
    children.push(reviewOutcomePanel(review.outcome, info));
  } else if (review.screen === "reauth") {
    children.push(reviewReauthPanel(info));
  } else if (review.selectedLegacy) {
    children.push(reviewLegacyPanel(review.selectedLegacy));
  } else if (!review.selectedWork) {
    children.push(
      el("div", {
        class: "review-empty-pane",
        children: [
          el("p", { class: "eyebrow", text: "REVIEW" }),
          el("h2", { text: "选择左侧工作项" }),
          el("p", {
            class: "muted",
            text: "工作项由宿主创建；本窗口只读取正式 Facade 返回的 Work、候选与状态。",
          }),
        ],
      }),
    );
  } else if (review.detailLoading) {
    children.push(reviewSkeleton("正在读取候选详情…", 4));
  } else if (review.detailError) {
    const retry = el("button", {
      class: "quiet-button",
      text: "重新读取候选",
      attrs: { type: "button" },
    });
    retry.addEventListener("click", () => {
      const candidate = review.candidates.find(
        (item) => item.candidate_id === review.selectedCandidateId,
      );
      if (candidate) {
        void loadReviewDetail(candidate);
      }
    });
    children.push(
      callout(reviewErrorText(review.detailError), "danger"),
      retry,
    );
  } else if (review.screen === "detail" && review.detail) {
    children.push(reviewDetailView(review.detail));
  } else {
    children.push(reviewCandidatesPane(review.selectedWork, info));
  }
  return el("section", {
    class: "review-pane review-detailpane",
    attrs: { "aria-label": "审查详情" },
    children,
  });
}

function reviewLegacyPanel(work: LegacyWorkItem): HTMLElement {
  return el("div", {
    class: "review-legacy-panel",
    children: [
      reviewSelectionHeader(`工作 ${shortId(work.work_id)}`, "旧工作项"),
      el("div", {
        class: "review-readonly-card",
        children: [
          el("h2", { text: "旧工作项只读" }),
          el("p", {
            class: "muted",
            text: "该工作项没有审查控制行（collab_work_control），因此没有候选投影，也不能在本窗口开始审查或批准。",
          }),
          el("dl", {
            class: "kv",
            children: [
              kv("工作", work.work_id),
              kv("事件", work.event_id),
              kv("类型", work.kind),
              kv("状态", work.status),
              kv("创建", work.created_at),
            ],
          }),
          el("p", {
            class: "muted",
            text: "缺失的候选/差异字段属于正式能力缺口；本窗口不读数据库补齐。",
          }),
        ],
      }),
    ],
  });
}

function reviewSelectionHeader(title: string, badge: string): HTMLElement {
  return el("div", {
    class: "review-selection-head",
    children: [
      el("div", {
        children: [
          el("p", { class: "eyebrow", text: "审查工作" }),
          el("h2", { text: title }),
        ],
      }),
      reviewBadge(badge, badge === "已暂停" ? "warn" : "neutral"),
    ],
  });
}

function reviewCandidatesPane(
  work: ReviewWorkItem,
  info: AppInfo,
): HTMLElement {
  const reason = generateBlockReason(review, info);
  const children: HTMLElement[] = [];
  children.push(
    reviewSelectionHeader(
      `工作 ${shortId(work.work_id)} · 事件 ${shortId(work.event_id)}`,
      work.blocked ? "已暂停" : "待审查",
    ),
  );
  if (work.blocked) {
    children.push(
      callout(
        work.rerun_registered
          ? "该工作已暂停；宿主已登记复查许可（待校验）。旧候选保持已拒绝，不会复活。"
          : "该工作已暂停；需要宿主发放新的复查许可。本窗口不能自行授权。",
        "warn",
      ),
    );
  }
  if (review.candidatesLoading) {
    children.push(reviewSkeleton("正在读取候选…", 2));
    return el("div", { class: "review-candidates", children });
  }
  if (review.candidatesError) {
    const retry = el("button", {
      class: "quiet-button",
      text: "重新读取候选",
      attrs: { type: "button" },
    });
    retry.addEventListener("click", () => void loadReviewCandidates(work.work_id));
    children.push(callout(reviewErrorText(review.candidatesError), "danger"), retry);
    return el("div", { class: "review-candidates", children });
  }
  if (review.candidates.length === 0) {
    children.push(
      el("div", {
        class: "review-empty-card",
        children: [
          el("h2", { text: "没有候选" }),
          el("p", {
            class: "muted",
            text: "这个工作项还没有候选。开始审查会调用 C07 生成候选或 WAIT；它不会直接提交 Belief。",
          }),
        ],
      }),
    );
  } else {
    children.push(
      el("div", {
        class: "review-candidate-head",
        children: [
          el("h2", { text: "候选" }),
          el("span", {
            class: "muted",
            text: `${review.candidates.length} 项 · 按创建顺序`,
          }),
        ],
      }),
    );
    for (const candidate of review.candidates) {
      children.push(reviewCandidateRow(candidate));
    }
  }
  children.push(reviewGenerateArea(info, reason));
  return el("div", { class: "review-candidates", children });
}

function reviewCandidateRow(candidate: ReviewCandidateSummary): HTMLElement {
  const active = review.selectedCandidateId === candidate.candidate_id;
  const row = el("button", {
    class: `review-candidate-row ${active ? "is-active" : ""}`,
    attrs: {
      type: "button",
      "data-candidate-id": candidate.candidate_id,
      "data-status": candidate.status,
    },
    children: [
      el("div", {
        class: "review-candidate-main",
        children: [
          el("div", {
            class: "review-candidate-title",
            children: [
              el("span", {
                class: "review-candidate-id",
                text: `候选 ${shortId(candidate.candidate_id)} · v${candidate.artifact_version}`,
              }),
              reviewBadge(
                candidateStatusLabel(candidate.status),
                candidateStatusTone(candidate.status),
              ),
            ],
          }),
          el("p", {
            class: "review-candidate-meta",
            text: [
              `交互 ${shortId(candidate.interaction_id)}`,
              `创建 ${candidate.created_at}`,
              candidate.decided_at ? `决定 ${candidate.decided_at}` : "",
              candidate.attempt_id
                ? `尝试 ${shortId(candidate.attempt_id)} #${candidate.attempt_no ?? "—"}`
                : "",
            ]
              .filter(Boolean)
              .join(" · "),
          }),
        ],
      }),
      el("span", { class: "review-chevron", text: "›" }),
    ],
  });
  row.addEventListener("click", () => selectReviewCandidate(candidate));
  return row;
}

function reviewGenerateArea(info: AppInfo, reason: string | null): HTMLElement {
  const action = el("button", {
    class: "primary-button review-generate",
    text:
      reason === null && review.selectedWork?.blocked
        ? "开始新的审查（新候选）"
        : "开始审查",
    attrs: { type: "button", id: "review-generate" },
  });
  action.disabled = reason !== null;
  action.addEventListener("click", () => openReviewGenerate());
  const children: HTMLElement[] = [
    el("p", {
      class: "muted",
      text: "开始审查会把工作上下文发送到上方目的地，只生成候选或 WAIT，不提交 Belief。",
    }),
  ];
  if (reason !== null) {
    children.push(
      el("p", { class: "review-lock-reason", text: reason }),
    );
  }
  if (needsConfirmation(state)) {
    children.push(
      el("p", {
        class: "muted",
        text: "外部目的地：首次或目的地变化后，开始前会再次确认本次发送意图。",
      }),
    );
  }
  children.push(action);
  return el("div", {
    class: "review-generate-area",
    attrs: {
      "data-generate-enabled": reason === null ? "true" : "false",
      "data-generate-reason": reason ?? "",
    },
    children,
  });
}

function reviewDetailView(detail: ReviewCandidateDetail): HTMLElement {
  const back = el("button", {
    class: "quiet-button",
    text: "← 返回候选",
    attrs: { type: "button", id: "review-back-candidates" },
  });
  back.addEventListener("click", () => backToCandidates());
  const sourceButton = el("button", {
    class: "quiet-button",
    text: `来源详情（${detail.sources.length}）`,
    attrs: { type: "button", id: "review-open-sources" },
  });
  sourceButton.addEventListener("click", () => openDrawer("sources"));
  const head = el("div", {
    class: "review-detail-head",
    children: [
      el("div", {
        children: [
          el("p", { class: "eyebrow", text: "候选差异" }),
          el("h2", {
            text: `候选 ${shortId(detail.candidate.candidate_id)} · v${detail.candidate.artifact_version}`,
          }),
          el("p", {
            class: "review-meta",
            text: [
              `工作 ${shortId(detail.candidate.work_id)}`,
              `交互 ${shortId(detail.candidate.interaction_id)}`,
              `结构校验 ${detail.structural_validation ?? "—"}`,
            ].join(" · "),
          }),
        ],
      }),
      el("div", {
        class: "review-detail-actions",
        children: [
          reviewBadge(
            candidateStatusLabel(detail.candidate.status),
            candidateStatusTone(detail.candidate.status),
          ),
          back,
          sourceButton,
        ],
      }),
    ],
  });
  const children: HTMLElement[] = [head];
  if (detail.redacted) {
    children.push(
      el("div", {
        class: "review-readonly-card",
        children: [
          el("h2", { text: "候选内容不可读" }),
          el("p", {
            class: "muted",
            text: "候选受隐私或删除屏障保护；本窗口不显示正文，也不提供批准或拒绝。",
          }),
        ],
      }),
    );
    children.push(reviewLockReason(detail));
    return el("div", { class: "review-detail", children });
  }
  if (!detail.proposal) {
    children.push(
      el("div", {
        class: "review-readonly-card",
        children: [
          el("h2", { text: "没有候选投影" }),
          el("p", {
            class: "muted",
            text: "宿主没有返回 proposal；本窗口不推断、不读取数据库补字段。",
          }),
        ],
      }),
    );
    children.push(reviewLockReason(detail));
    return el("div", { class: "review-detail", children });
  }
  children.push(reviewProposalBlock(detail));
  children.push(reviewCurrentBlock(detail));
  children.push(reviewDiffBlock(detail));
  children.push(reviewSourcesBlock(detail));
  children.push(reviewLockReason(detail));
  return el("div", { class: "review-detail", children });
}

function reviewProposalBlock(detail: ReviewCandidateDetail): HTMLElement {
  const proposal = detail.proposal!;
  const rows: Array<Node> = [];
  if (proposal.operation === "retract_belief") {
    rows.push(
      kv("操作", operationLabel(proposal.operation)),
      kv("目标", sourceRefLabel(proposal.target_ref)),
      kv("撤回理由", proposal.strength_reason ?? "—"),
      kv("证据来源", `${proposal.evidence_links.length} 项`),
    );
  } else {
    rows.push(
      kv("操作", operationLabel(proposal.operation)),
      ...(proposal.target_ref
        ? [kv("目标", sourceRefLabel(proposal.target_ref))]
        : []),
      kv("主张", proposal.claim ?? "—"),
      kv("类型", proposal.claim_type ?? "—"),
      kv("范围 / 适用性", formatReviewValue("scope", proposal.scope ?? {})),
      kv("强度", proposal.strength ?? "—"),
      kv("有效期", formatReviewValue("valid_until", proposal.valid_until)),
      kv("证据来源", `${proposal.evidence_links.length} 项`),
    );
    if (proposal.strength_reason) {
      rows.push(kv("强度理由", proposal.strength_reason));
    }
  }
  return el("section", {
    class: "review-card",
    children: [
      el("h2", { text: "候选内容" }),
      el("dl", { class: "kv", children: rows }),
      el("p", {
        class: "muted",
        text: "只显示正式投影的字段；不显示 Prompt、HTTP 正文、候选原始 body 或内部 revision。",
      }),
    ],
  });
}

function reviewCurrentBlock(detail: ReviewCandidateDetail): HTMLElement {
  if (detail.current_state === "not_applicable") {
    return el("section", {
      class: "review-card",
      children: [
        el("h2", { text: "当前版本" }),
        el("p", {
          class: "muted",
          text: "新增：没有可比较的当前 Belief。差异表中的“当前”为空，而不是空 Belief。",
        }),
      ],
    });
  }
  if (
    detail.current_state === "redacted" ||
    detail.current_state === "missing"
  ) {
    return el("section", {
      class: "review-card",
      children: [
        el("h2", { text: "当前版本" }),
        callout(
          detail.current_state === "redacted"
            ? "当前版本不可读（隐私或删除屏障）；没有用空 Belief 替代。"
            : "当前版本不存在或不可读；没有用空 Belief 替代。",
          "warn",
        ),
      ],
    });
  }
  const current = detail.current!;
  return el("section", {
    class: "review-card",
    children: [
      el("h2", { text: "当前版本" }),
      el("dl", {
        class: "kv",
        children: [
          kv("引用", sourceRefLabel(current.ref)),
          kv("主张", current.claim),
          kv("类型", current.claim_type),
          kv("状态", current.status),
          kv("强度", current.strength),
          kv("范围 / 适用性", current.applicability),
          kv("有效期", current.valid_until ?? "—"),
        ],
      }),
    ],
  });
}

function reviewDiffBlock(detail: ReviewCandidateDetail): HTMLElement {
  const { changed, unchanged } = splitDiff(detail);
  if (detail.current_state === "redacted" || detail.current_state === "missing") {
    return el("section", {
      class: "review-card",
      children: [
        el("h2", { text: "字段差异" }),
        el("p", {
          class: "muted",
          text: "当前版本不可读或不存在，因此不生成字段差异；这不是“全部变化”。",
        }),
      ],
    });
  }
  const changedRows = el("div", { class: "review-diff" });
  if (detail.proposal?.operation === "retract_belief") {
    changedRows.appendChild(
      el("p", {
        class: "muted",
        text: "撤回只比较证据来源；Belief 正文、类型、范围与强度不参与比较。",
      }),
    );
  }
  changedRows.appendChild(
    el("div", {
      class: "review-diff-head",
      children: [
        el("span", { text: "字段" }),
        el("span", {
          text:
            detail.current_state === "not_applicable" ? "当前（新增）" : "当前版本",
        }),
        el("span", { text: "候选版本" }),
        el("span", { text: "变化" }),
      ],
    }),
  );
  for (const row of changed) {
    changedRows.appendChild(
      reviewDiffRow(
        row,
        detail.proposal?.operation === "propose_belief" ? "新增" : undefined,
      ),
    );
  }
  if (changed.length === 0) {
    changedRows.appendChild(
      el("p", { class: "muted", text: "没有变化字段。" }),
    );
  }
  const block = el("section", {
    class: "review-card",
    children: [
      el("div", {
        class: "review-card-head",
        children: [
          el("h2", { text: "字段差异" }),
          reviewBadge(`${changed.length} 项变化`, changed.length ? "warn" : "neutral"),
        ],
      }),
      changedRows,
    ],
  });
  if (unchanged.length > 0) {
    const rows = el("div", { class: "review-diff" });
    rows.appendChild(
      el("div", {
        class: "review-diff-head",
        children: [
          el("span", { text: "字段" }),
          el("span", { text: "当前版本" }),
          el("span", { text: "候选版本" }),
          el("span", { text: "变化" }),
        ],
      }),
    );
    for (const row of unchanged) {
      rows.appendChild(reviewDiffRow(row));
    }
    block.appendChild(
      el("details", {
        class: "review-unchanged",
        children: [
          el("summary", { text: `未变化字段（${unchanged.length}）` }),
          rows,
        ],
      }),
    );
  }
  return block;
}

function reviewDiffRow(
  row: {
    field: string;
    before: unknown;
    after: unknown;
    changed: boolean;
  },
  changedLabel?: string,
): HTMLElement {
  return el("div", {
    class: `review-diff-row ${row.changed ? "is-changed" : ""}`,
    attrs: { "data-field": row.field },
    children: [
      el("span", {
        class: "review-diff-field",
        text: diffFieldLabel(row.field),
      }),
      el("span", {
        class: "review-diff-before",
        text: formatReviewValue(row.field, row.before),
      }),
      el("span", {
        class: "review-diff-after",
        text: formatReviewValue(row.field, row.after),
      }),
      el("span", {
        class: "review-diff-change",
        text: row.changed ? changedLabel ?? "变化" : "未变",
      }),
    ],
  });
}

function reviewSourcesBlock(detail: ReviewCandidateDetail): HTMLElement {
  const readable = detail.sources.filter(
    (source) => source.availability === "available",
  ).length;
  const hidden = detail.sources.filter(
    (source) => source.availability === "redacted",
  ).length;
  const unavailable = detail.sources.length - readable - hidden;
  const rows = detail.sources.map((source) =>
    el("div", {
      class: "review-source-row",
      children: [
        el("span", {
          class: "review-source-kind",
          text: sourceKindLabel(source.kind),
        }),
        el("span", {
          class: "review-source-ref",
          text: sourceRefId(source.ref),
        }),
        el("span", {
          class: `review-source-status is-${source.availability}`,
          text: sourceAvailabilityLabel(source.availability),
        }),
        ...(source.reason
          ? [
              el("span", {
                class: "review-source-reason",
                text: sourceReasonLabel(source.reason),
              }),
            ]
          : []),
      ],
    }),
  );
  return el("section", {
    class: "review-card",
    children: [
      el("div", {
        class: "review-card-head",
        children: [
          el("h2", { text: "来源" }),
          reviewBadge(
            `可读 ${readable} · 隐藏 ${hidden} · 不可用 ${unavailable}`,
            hidden || unavailable ? "warn" : "ok",
          ),
        ],
      }),
      el("details", {
        class: "review-sources",
        children: [
          el("summary", {
            text: "来源引用默认折叠；展开查看可读性与精确引用",
          }),
          detail.sources.length === 0
            ? el("p", { class: "muted", text: "本次没有可披露的来源引用。" })
            : el("div", { class: "review-source-list", children: rows }),
          el("p", {
            class: "muted",
            text: "来源正文只在正式可读时出现在来源抽屉；隐藏来源不显示正文，也不以空值替代。",
          }),
        ],
      }),
    ],
  });
}

function reviewLockReason(detail: ReviewCandidateDetail): HTMLElement {
  const approveReason = resolveBlockReason(review, state.host, "approve");
  const rejectReason = resolveBlockReason(review, state.host, "reject");
  const reasons: string[] = [];
  if (approveReason) {
    reasons.push(`批准不可用：${approveReason}`);
  }
  if (rejectReason) {
    reasons.push(`拒绝不可用：${rejectReason}`);
  }
  if (reasons.length === 0) {
    reasons.push("批准与拒绝会在提交前重新核对 exact candidate/version/Work/current。");
  }
  return el("p", {
    class: "review-lock-reason",
    attrs: {
      "data-approve-enabled": approveReason ? "false" : "true",
      "data-reject-enabled": rejectReason ? "false" : "true",
      "data-eligibility": detail.eligibility_reason ?? "",
    },
    text: reasons.join(" · "),
  });
}

function reviewActionBar(info: AppInfo): HTMLElement {
  const left = el("div", { class: "review-action-left" });
  const right = el("div", { class: "review-action-right" });
  const refresh = el("button", {
    class: "quiet-button",
    text: "刷新",
    attrs: { type: "button", id: "review-action-refresh" },
  });
  refresh.addEventListener("click", () => void refreshReviewData());
  const back = el("button", {
    class: "quiet-button",
    text: "← 返回工作列表",
    attrs: { type: "button", id: "review-back-works" },
  });
  back.addEventListener("click", () => backToReviewWorks());

  if (review.screen === "detail") {
    left.append(back, refresh);
    if (review.detail) {
      const sources = el("button", {
        class: "quiet-button",
        text: "来源与详情",
        attrs: { type: "button", id: "review-action-sources" },
      });
      sources.addEventListener("click", () => openDrawer("sources"));
      left.appendChild(sources);
    }
    const approveReason = resolveBlockReason(review, info, "approve");
    const rejectReason = resolveBlockReason(review, info, "reject");
    const actionReasons = [approveReason, rejectReason].filter(
      (reason): reason is string => Boolean(reason),
    );
    if (actionReasons.length > 0) {
      left.appendChild(
        el("span", {
          class: "review-action-reason",
          text: Array.from(new Set(actionReasons)).join(" · "),
        }),
      );
    }
    const approve = el("button", {
      class: "primary-button review-approve",
      text: review.busy === "resolve" ? "正在提交…" : "批准…",
      attrs: { type: "button", id: "review-approve" },
    });
    approve.disabled = !approveEnabled(review, info);
    approve.title = approveReason ?? "";
    approve.addEventListener("click", () => openReviewDecision("approve"));
    const reject = el("button", {
      class: "danger-button review-reject",
      text: "拒绝…",
      attrs: { type: "button", id: "review-reject" },
    });
    reject.disabled = !rejectEnabled(review, info);
    reject.title = rejectReason ?? "";
    reject.addEventListener("click", () => openReviewDecision("reject"));
    right.append(reject, approve);
  } else if (review.screen === "reauth") {
    left.append(refresh);
    const backPaused = el("button", {
      class: "quiet-button",
      text: "← 返回已暂停的审查",
      attrs: { type: "button", id: "review-back-paused" },
    });
    backPaused.addEventListener("click", () => applyReview({ type: "reauth-close" }));
    left.appendChild(backPaused);
  } else if (review.screen === "result") {
    left.append(back, refresh);
    const sources = el("button", {
      class: "quiet-button",
      text: "来源与详情",
      attrs: { type: "button", id: "review-action-sources" },
    });
    sources.addEventListener("click", () => openDrawer("sources"));
    left.appendChild(sources);
  } else {
    left.append(refresh);
  }
  if (
    (review.screen === "candidates" && review.selectedWork) ||
    review.screen === "reauth"
  ) {
    const reason = generateBlockReason(review, info);
    if (reason) {
      left.appendChild(
        el("span", { class: "review-action-reason", text: reason }),
      );
    }
    const generate = el("button", {
      class: "primary-button",
      text:
        review.selectedWork?.blocked
          ? "开始新的审查（新候选）"
          : "开始审查",
      attrs: { type: "button", id: "review-generate-action" },
    });
    generate.disabled = reason !== null;
    generate.title = reason ?? "";
    generate.addEventListener("click", () => openReviewGenerate());
    right.appendChild(generate);
  }
  const enabled =
    review.busy === null &&
    review.screen === "detail" &&
    review.detail !== null &&
    approveEnabled(review, info);
  const rejectable =
    review.busy === null &&
    review.screen === "detail" &&
    review.detail !== null &&
    rejectEnabled(review, info);
  return el("footer", {
    class: "review-actionbar",
    attrs: {
      "data-approve-enabled": enabled ? "true" : "false",
      "data-reject-enabled": rejectable ? "true" : "false",
    },
    children: [left, right],
  });
}

function reviewOutcomePanel(view: ReviewView, info: AppInfo): HTMLElement {
  const copy: Record<
    string,
    { title: string; lead: string; tone: string }
  > = {
    committed: {
      title: "已提交（committed）",
      lead: "回执只代表本地提交；没有把候选再次发送给模型。",
      tone: "ok",
    },
    rejected: {
      title: "已拒绝 · 已暂停",
      lead: "旧候选不能再次批准；重新考虑需要新的复查许可和新的审查。",
      tone: "warn",
    },
    waiting: {
      title: "已记录：暂不形成 Belief（WAIT）",
      lead: "本次审查没有形成 Belief；已记录为 WAIT 回执，工作项完成。它不是失败，也不能再次批准。",
      tone: "wait",
    },
    denied: {
      title: "审查未启动（denied）",
      lead: "宿主当前未允许启动这次审查，未生成候选。它不是权限不足。",
      tone: "warn",
    },
    failed: {
      title: "审查未完成（failed）",
      lead: "生成或提交流程中断，未提交，也没有形成 Belief。",
      tone: "danger",
    },
    stale: {
      title: "已过期（stale）",
      lead: "工作或候选状态已变化，本次审查未提交，也没有形成 Belief。",
      tone: "warn",
    },
    incomplete: {
      title: "结论不完整（incomplete）",
      lead: "没有取得权威结论。这不等于拒绝，也不等于提交。",
      tone: "warn",
    },
    pending: {
      title: "待决定（pending）",
      lead: "候选存在，但还没有回执；审批仍需要单独确认。",
      tone: "pending",
    },
    structural_only: {
      title: "仅结构校验",
      lead: "没有持久化候选。",
      tone: "neutral",
    },
  };
  const item = copy[view.state] ?? copy.failed;
  const children: HTMLElement[] = [
    el("div", {
      class: "review-outcome-head",
      children: [
        el("div", {
          children: [
            el("p", { class: "eyebrow", text: "REVIEW RESULT" }),
            el("h2", { text: item.title }),
            el("p", { class: "state-lead", text: item.lead }),
          ],
        }),
        reviewBadge(reviewViewLabel(view.state), item.tone),
      ],
    }),
  ];
  if (view.state === "committed") {
    children.push(reviewReceiptBlock(view));
    children.push(
      el("p", {
        class: "muted",
        text: "已提交只由 C07 ReviewView 的权威回执证明；模型文本不算回执。",
      }),
    );
  } else if (view.state === "waiting") {
    children.push(reviewReceiptBlock(view));
    children.push(
      el("p", {
        class: "muted",
        text: "WAIT 是正式结果：工作项已完成，没有候选，也不能再次批准。",
      }),
    );
  } else if (view.state === "rejected") {
    children.push(
      el("dl", {
        class: "kv",
        children: [
          kv("旧候选", shortId(view.candidate_id)),
          kv("状态", "已拒绝（只读）"),
          kv("工作", review.selectedWork ? "已暂停" : "见工作列表"),
          kv(
            "复查许可",
            review.selectedWork?.rerun_registered
              ? "已登记（待校验）"
              : "未登记；需要宿主发放",
          ),
        ],
      }),
    );
    const reauth = el("button", {
      class: "quiet-button",
      text: "重新授权复查",
      attrs: { type: "button", id: "review-reauth" },
    });
    reauth.addEventListener("click", () => applyReview({ type: "reauth-open" }));
    children.push(reauth);
    children.push(
      el("p", {
        class: "muted",
        text: "本窗口不发放许可；旧候选保持已拒绝，不能复活。",
      }),
    );
  } else if (view.state === "failed" || view.state === "stale") {
    children.push(
      el("p", {
        class: "reason-line",
        text: `原因标签：${view.detail ?? "无阶段标签"}`,
      }),
    );
    const refresh = el("button", {
      class: "quiet-button",
      text: "刷新工作列表",
      attrs: { type: "button" },
    });
    refresh.addEventListener("click", () => void refreshReviewData());
    children.push(refresh);
    if (generateEnabled(review, info)) {
      const retry = el("button", {
        class: "primary-button",
        text: "重新开始审查（新候选）",
        attrs: { type: "button", id: "review-retry" },
      });
      retry.addEventListener("click", () => openReviewGenerate());
      children.push(retry);
    }
    children.push(
      el("p", {
        class: "muted",
        text: "这不是自动重试：只有你再次操作才会生成新的候选；旧候选不会复活。",
      }),
    );
  } else if (view.state === "denied") {
    children.push(
      el("p", {
        class: "reason-line",
        text: `原因标签：${view.detail ?? "审查入口关闭"}`,
      }),
    );
    children.push(
      el("p", {
        class: "muted",
        text: "denied 表示审查入口关闭，不是权限不足；没有生成候选。",
      }),
    );
  } else if (view.state === "incomplete") {
    const id = recoveryInteractionId(review);
    if (id && reviewCaps(info).result) {
      const recover = el("button", {
        class: "quiet-button",
        text: "刷新结果",
        attrs: { type: "button", id: "review-recover-result" },
      });
      recover.addEventListener("click", () => void performReviewResult(id));
      children.push(recover);
    }
    children.push(
      el("p", {
        class: "muted",
        text: "结论不完整既不是拒绝也不是提交；没有自动重试。",
      }),
    );
  } else if (view.state === "pending") {
    const candidate = review.candidates.find(
      (item2) => item2.candidate_id === view.candidate_id,
    );
    if (candidate) {
      const open = el("button", {
        class: "quiet-button",
        text: "查看候选",
        attrs: { type: "button" },
      });
      open.addEventListener("click", () => selectReviewCandidate(candidate));
      children.push(open);
    }
  }
  if (view.detail && view.state !== "failed" && view.state !== "stale" && view.state !== "denied") {
    children.push(reviewTechnical(view.detail));
  }
  return el("div", {
    class: `review-outcome review-outcome-${item.tone}`,
    attrs: { "data-review-state": view.state },
    children,
  });
}

function reviewReceiptBlock(view: ReviewView): HTMLElement {
  if (!view.receipt) {
    return el("div", {
      class: "review-receipt is-empty",
      children: [
        el("h3", { text: "回执" }),
        el("p", {
          class: "muted",
          text: view.redacted
            ? "回执不可见（redacted）。不伪造回执，也不改变生命周期状态。"
            : "尚未生成回执；只有权威提交结果才会产生回执。",
        }),
      ],
    });
  }
  const receipt = view.receipt;
  return el("div", {
    class: "review-receipt",
    children: [
      el("h3", { text: "回执" }),
      el("dl", {
        class: "kv",
        children: [
          kv("请求", shortId(receipt.request_id)),
          kv("上下文", shortId(receipt.context_id)),
          kv("操作", receiptOperationLabel(receipt.operation)),
          kv("结果", receipt.outcome),
          kv("时间", receipt.committed_at),
          kv(
            "记录",
            receipt.record_refs.length === 0
              ? "无记录引用"
              : receipt.record_refs
                  .map((ref) => `${shortId(ref.record_id)} · v${ref.record_version}`)
                  .join(" · "),
          ),
        ],
      }),
      el("p", {
        class: "muted",
        text: "界面不显示 state_revision、proposal_digest、command_digest 或内部授权引用。",
      }),
    ],
  });
}

function reviewTechnical(detail: string | null): HTMLElement {
  return el("details", {
    class: "technical",
    children: [
      el("summary", { text: "技术详情" }),
      el("code", { text: detail ?? "无阶段标签" }),
      el("p", {
        class: "muted",
        text: "只显示阶段/原因标签；不包含 Prompt、HTTP 正文、模型原始输出或数据库列。",
      }),
    ],
  });
}

function reviewReauthPanel(info: AppInfo): HTMLElement {
  const work = review.selectedWork;
  const rejected = review.candidates.find(
    (candidate) => candidate.status === "rejected",
  );
  const oldCandidate = review.outcome?.candidate_id ?? rejected?.candidate_id ?? null;
  const rerun = work?.rerun_registered ?? false;
  const children: HTMLElement[] = [
    reviewSelectionHeader("重新授权复查", "已暂停"),
    el("div", {
      class: "review-step",
      children: [
        el("span", { class: "review-step-number", text: "1" }),
        el("div", {
          children: [
            el("h3", { text: "旧候选" }),
            el("p", {
              class: "muted",
              text: `${shortId(oldCandidate)} · 已拒绝（只读）`,
            }),
            el("p", {
              class: "muted",
              text: "旧候选不能复活，也不能再次批准。",
            }),
          ],
        }),
      ],
    }),
    el("div", {
      class: "review-step",
      children: [
        el("span", { class: "review-step-number", text: "2" }),
        el("div", {
          children: [
            el("h3", { text: "新的复查许可" }),
            el("p", {
              class: "muted",
              text: rerun
                ? "已登记（开始审查时由 C07/C05d 校验有效性）。"
                : "未登记；需要宿主或负责人发放新许可。本窗口不显示许可令牌。",
            }),
            (() => {
              const grant = el("button", {
                class: "quiet-button",
                text: "申请新的复查许可（当前不可用）",
                attrs: { type: "button", id: "review-request-grant" },
              });
              grant.disabled = true;
              grant.title = "没有公共发放入口；由宿主/负责人发放";
              return grant;
            })(),
          ],
        }),
      ],
    }),
    el("div", {
      class: "review-step",
      children: [
        el("span", { class: "review-step-number", text: "3" }),
        el("div", {
          children: [
            el("h3", { text: "开始新的审查" }),
            el("p", {
              class: "muted",
              text: "新审查会生成新的审查轮次和候选；旧候选仍保持已拒绝。",
            }),
          ],
        }),
      ],
    }),
  ];
  const reason = generateBlockReason(review, info);
  const start = el("button", {
    class: "primary-button",
    text: "开始新的审查（新候选）",
    attrs: { type: "button", id: "review-reauth-start" },
  });
  start.disabled = reason !== null;
  start.title = reason ?? "";
  start.addEventListener("click", () => openReviewGenerate());
  const refresh = el("button", {
    class: "quiet-button",
    text: "刷新工作列表",
    attrs: { type: "button" },
  });
  refresh.addEventListener("click", () => void refreshReviewData());
  const back = el("button", {
    class: "quiet-button",
    text: "返回已暂停的审查",
    attrs: { type: "button" },
  });
  back.addEventListener("click", () => applyReview({ type: "reauth-close" }));
  children.push(
    el("div", { class: "review-reauth-actions", children: [start, refresh, back] }),
  );
  if (reason) {
    children.push(el("p", { class: "review-lock-reason", text: reason }));
  }
  return el("div", { class: "review-reauth", children });
}

function isReviewModalOpen(): boolean {
  return review.confirmGenerate || review.confirmDecision !== null;
}

function closeReviewModal(): void {
  if (!isReviewModalOpen()) {
    return;
  }
  applyReview({ type: "generate-confirm", open: false });
  applyReview({ type: "decision-confirm", decision: null });
}

function renderReviewOverlay(): void {
  const open = workspace === "review" && isReviewModalOpen();
  shell.reviewOverlay.classList.toggle("hidden", !open);
  if (!open) {
    reviewModalWasOpen = false;
    clear(shell.reviewModal);
    return;
  }
  clear(shell.reviewModal);
  if (review.confirmGenerate) {
    renderGenerateModal();
  } else if (review.confirmDecision) {
    renderDecisionModal(review.confirmDecision);
  }
  if (!reviewModalWasOpen) {
    reviewModalWasOpen = true;
    window.setTimeout(() => {
      const first = shell.reviewModal.querySelector<HTMLButtonElement>(
        "button:not([disabled])",
      );
      first?.focus({ preventScroll: true });
    }, 0);
  }
}

function renderGenerateModal(): void {
  const destination = state.destination;
  const confirm = el("button", {
    class: "primary-button",
    text: "确认并开始审查",
    attrs: { type: "button", id: "review-confirm-generate" },
  });
  confirm.disabled = review.busy !== null;
  confirm.addEventListener("click", () => void confirmReviewGenerate());
  const cancel = el("button", {
    class: "quiet-button",
    text: "返回检查",
    attrs: { type: "button", id: "review-cancel-generate" },
  });
  cancel.addEventListener("click", () => closeReviewModal());
  shell.reviewModal.append(
    el("div", {
      class: "review-modal-head",
      children: [
        el("h2", {
          text: "开始审查？",
          attrs: { id: "review-modal-title" },
        }),
        el("span", { class: "eyebrow", text: "D3 · 外部发送确认" }),
      ],
    }),
    el("div", {
      class: "review-modal-body",
      children: [
        el("p", {
          text: destination
            ? `本次将把工作上下文发送到 ${destination.provider} / ${destination.model}。`
            : "宿主尚未提供目的地。",
        }),
        el("p", {
          class: "muted",
          text: "只生成候选或 WAIT，不提交 Belief。生成后仍需要单独批准；批准提交不会再次发送给模型。",
        }),
        el("p", {
          class: "muted",
          text: "确认只表达本次用户意图，不是授权凭据。",
        }),
      ],
    }),
    el("div", { class: "review-modal-actions", children: [cancel, confirm] }),
  );
}

function renderDecisionModal(decision: ReviewDecision): void {
  const detail = review.detail;
  if (!detail) {
    closeReviewModal();
    return;
  }
  const approve = decision === "approve";
  const confirm = el("button", {
    class: approve ? "primary-button" : "danger-button",
    text: approve ? "批准并提交" : "确认拒绝",
    attrs: { type: "button", id: "review-confirm-decision" },
  });
  confirm.disabled = !canStartResolve(review, state.host, decision);
  confirm.addEventListener("click", () => void performReviewDecision(decision));
  const cancel = el("button", {
    class: "quiet-button",
    text: approve ? "返回检查" : "返回",
    attrs: { type: "button", id: "review-cancel-decision" },
  });
  cancel.addEventListener("click", () => closeReviewModal());
  const proposal = detail.proposal;
  const summary: Node[] = [];
  if (proposal) {
    summary.push(
      kv("操作", operationLabel(proposal.operation)),
      kv("主张", proposal.claim ?? "—"),
      kv("类型", proposal.claim_type ?? "—"),
      kv("范围", formatReviewValue("scope", proposal.scope ?? {})),
      kv("强度", proposal.strength ?? "—"),
      kv("来源", `${proposal.evidence_links.length} 项（详情在审查记录中）`),
    );
  }
  shell.reviewModal.append(
    el("div", {
      class: "review-modal-head",
      children: [
        el("h2", {
          text: approve ? "批准并提交这个候选？" : "拒绝这个候选？",
          attrs: { id: "review-modal-title" },
        }),
        el("span", { class: "review-badge is-warn", text: "二次确认" }),
      ],
    }),
    el("div", {
      class: "review-modal-body",
      children: [
        summary.length > 0 ? el("dl", { class: "kv", children: summary }) : null,
        el("p", {
          text: approve
            ? "这会写入本地 Belief，供之后的咨询使用。批准后不能在这个界面撤回。批准提交不会再次发送给模型。"
            : "拒绝后工作项会暂停，旧候选不能再次批准。重新考虑需要新的复查许可，并会生成新的候选。此决定不能在这个界面撤销。",
        }),
        el("p", {
          class: "muted",
          text: "提交前会重新核对 exact candidate/version/Work/current；如果已变化，本窗口不会自动重试。",
        }),
      ],
    }),
    el("div", { class: "review-modal-actions", children: [cancel, confirm] }),
  );
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
    if (workspace === "review") {
      renderReviewSourcesTab();
    } else {
      renderSourcesTab();
    }
  } else if (state.drawer.tab === "audit") {
    if (workspace === "review") {
      renderReviewAuditTab();
    } else {
      renderAuditTab();
    }
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

function renderReviewSourcesTab(): void {
  const detail = review.detail;
  if (!detail) {
    shell.drawerBody.append(
      el("p", {
        class: "muted",
        text: "本次还没有可展示的候选来源。选择一个候选后，来源摘要会显示在这里。",
      }),
    );
  } else if (detail.redacted) {
    shell.drawerBody.append(
      callout(
        "候选内容不可读；没有可披露的来源，也没有用空值替代。",
        "warn",
      ),
    );
  } else {
    const list = el("div", { class: "source-list" });
    for (const source of detail.sources) {
      const row = el("div", {
        class: "review-source-detail",
        children: [
          el("div", {
            class: "source-row",
            children: [
              el("div", {
                class: "source-main",
                children: [
                  el("span", {
                    class: "source-kind",
                    text: sourceKindLabel(source.kind),
                  }),
                  el("span", {
                    class: "source-id",
                    text: sourceRefId(source.ref),
                  }),
                ],
              }),
              el("span", {
                class: `source-status is-${source.availability}`,
                text: sourceAvailabilityLabel(source.availability),
              }),
              ...(source.reason
                ? [
                    el("span", {
                      class: "source-note",
                      text: sourceReasonLabel(source.reason),
                    }),
                  ]
                : []),
            ],
          }),
        ],
      });
      if (source.availability === "available" && source.body !== null) {
        row.appendChild(
          el("pre", {
            class: "review-source-body",
            text: source.body,
          }),
        );
      } else if (
        source.kind !== "event" &&
        source.kind !== "record"
      ) {
        row.appendChild(
          el("p", {
            class: "muted",
            text: "正文不可读或未提供；不显示空值占位。",
          }),
        );
      }
      list.appendChild(row);
    }
    shell.drawerBody.append(
      drawerSection("审查来源（只显示正式可读引用）", [
        el("p", {
          class: "muted",
          text: "Event/Record 只显示引用与可读性；Input/Artifact 正文仅在正式可读时显示。",
        }),
        list,
        el("p", {
          class: "muted",
          text: "Event/Record 只显示精确引用与可读性，不读取正文。Input/Artifact 的正文仅在 read_experience 正式可读时显示；隐藏来源不显示正文。",
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
          text: "不显示 Prompt、HTTP 正文、候选原始 body、内部 revision 或其他 namespace 的来源。",
        }),
      ],
    }),
  );
}

function renderReviewAuditTab(): void {
  const detail = review.detail;
  const work = review.selectedWork;
  const outcome = review.outcome;
  if (!detail && !outcome && !work) {
    shell.drawerBody.append(
      el("p", { class: "muted", text: "本次还没有可审查的 Review 引用。" }),
    );
    return;
  }
  const rows: Node[] = [];
  if (work) {
    rows.push(
      kv("工作", work.work_id),
      kv("事件", work.event_id),
      kv("轮次", String(work.generation)),
      kv("暂停", work.blocked ? "是" : "否"),
      kv("复查许可", work.rerun_registered ? "已登记（待校验）" : "未登记"),
    );
  }
  if (detail) {
    rows.push(
      kv("候选", detail.candidate.candidate_id),
      kv("候选版本", String(detail.candidate.artifact_version)),
      kv("工作轮次", String(detail.candidate.work_generation)),
      kv("交互", detail.candidate.interaction_id),
      kv("上下文", detail.candidate.context_id),
      kv("状态", candidateStatusLabel(detail.candidate.status)),
      kv("操作", operationLabel(detail.proposal?.operation)),
      kv("结构校验", detail.structural_validation ?? "—"),
      kv("可批准提示", detail.can_approve ? "是（仅内容提示）" : "否"),
      kv("可拒绝提示", detail.can_reject ? "是（仅内容提示）" : "否"),
      kv("资格原因", detail.eligibility_reason ?? "—"),
      kv("尝试", detail.candidate.attempt_id
        ? `${shortId(detail.candidate.attempt_id)} #${detail.candidate.attempt_no ?? "—"}`
        : "暂不可用"),
    );
  }
  if (outcome) {
    rows.push(
      kv("结果状态", reviewViewLabel(outcome.state)),
      kv("模型适配器", outcome.model_adapter ?? "—"),
      kv("结果交互", shortId(outcome.interaction_id)),
    );
    if (outcome.receipt) {
      rows.push(
        kv("回执请求", shortId(outcome.receipt.request_id)),
        kv("回执操作", receiptOperationLabel(outcome.receipt.operation)),
        kv("回执结果", outcome.receipt.outcome),
        kv("回执时间", outcome.receipt.committed_at),
      );
    } else if (outcome.state === "committed") {
      rows.push(kv("回执", "不可见"));
    }
  }
  const caps = reviewCaps(state.host);
  rows.push(
    kv("宿主读取权限", caps.read ? "可用" : "不可用"),
    kv("宿主开始审查权限", caps.generate ? "可用" : "不可用"),
    kv("宿主批准权限", caps.approve ? "可用" : "不可用"),
    kv("宿主拒绝权限", caps.reject ? "可用" : "不可用"),
    kv("宿主结果权限", caps.result ? "可用" : "不可用"),
  );
  shell.drawerBody.append(
    drawerSection("审查与候选引用", [
      el("dl", { class: "kv", children: rows }),
      el("p", {
        class: "muted",
        text: "只显示公开引用与状态；不显示 Prompt、HTTP 正文、proposal body、内部 revision、Approval/grant 或数据库列。",
      }),
    ]),
  );
  shell.drawerBody.append(
    el("div", {
      class: "gap-box",
      children: [
        el("strong", { text: "动作边界" }),
        el("p", {
          text: "can_approve/can_reject 只是内容提示；按钮同时要求宿主 review.approve/review.reject 权限、pending 状态、v1 版本与工作轮次一致。最终由 C07 resolve 复验。",
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
          kv("工作区", "Consult + Review（C12c）"),
          kv("PCS", info ? `${info.pcs.version} · schema ${info.pcs.schema_migrations}` : "—"),
          kv("外观", "深色主题（品牌固定）"),
        ],
      }),
    ]),
    drawerSection("审查能力（宿主派生，只读）", [
      el("dl", {
        class: "kv",
        children: [
          kv("读取", reviewCaps(info).read ? "可用" : "不可用"),
          kv("开始审查", reviewCaps(info).generate ? "可用" : "不可用"),
          kv("批准", reviewCaps(info).approve ? "可用" : "不可用"),
          kv("拒绝", reviewCaps(info).reject ? "可用" : "不可用"),
          kv("结果恢复", reviewCaps(info).result ? "可用" : "不可用"),
        ],
      }),
      el("p", {
        class: "muted",
        text: "这些布尔值由可信宿主派生；本窗口不能自行赋权，也不显示 token、grant 或权限表。",
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
