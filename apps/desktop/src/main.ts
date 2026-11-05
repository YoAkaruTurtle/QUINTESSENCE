import { AppShell, type PageId } from "./appShell";
import { dashboard, traceSection } from "./dashboard";
import { sessionOverview } from "./sessionOverview";
import { NO_USAGE, usageSummary, type TurnTrace, type UsageLoad } from "./usage";
import { s as shellText } from "./shellText";
import { uiText } from "./locale";
import "./styles.css";
import "./sol.css";
import { instanceSettingsPanel } from "./instanceSettings";
import { interfaceSettings, generationSettings, loadInterfacePreferences } from "./settings";
import { maintenanceSettingsPanel } from "./maintenanceSettings";
import { personaPanel, preferencePanel } from "./adminPanel";
import { webSettingsPanel } from "./webSettings";
import { memoryRecallSettingsPanel } from "./memoryRecallSettings";

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
  personaLabel,
  personaSourceLabel,
  phaseTitle,
  queryStatusLabel,
  reasonLabel,
  shortId,
  sourceCountLabel,
  sourceRows,
  speakerLabel,
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
  reviewWorkState,
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
import { createChatApi } from "./chatApi";
import {
  DisplayPreferenceError,
  EMPTY_DISPLAY_PREFERENCES,
  MAX_TITLE_CHARS,
  archiveLimitReached,
  groupConversations,
  loadDisplayPreferences,
  mergeDisplayTitles,
  setDisplayPreference,
  sortedArchived,
  titleLimitReached,
  validateConversationTitle,
  visibleConversations,
  type DisplayPreferences,
  type SidebarModel,
} from "./conversationDisplay";
import {
  INITIAL_CHAT_STATE,
  canCancel,
  canReadTurnResult,
  canSwitchConversation,
  chatReduce,
  hasPendingMemory,
  memoryStateLabel,
  newestTurnId,
  orderedTurnIds,
  pendingUserMessageVisible,
  requestIdFor,
  turnStatusLabel,
  type ChatMessage,
  type ChatState,
  type ConversationSummary,
  type ConversationTurn,
  type PostprocessTurnStatus,
  type ProcessEvent,
  type TranscriptSearchItem,
  type TranscriptSearchPage,
  type TurnResult,
  type TurnState,
} from "./chat";
import {
  BUBBLE_REVEAL_CLASS,
  BubbleRevealTracker,
  bubbleDelayMs,
  bubbleRevealKey,
  replyBubbles,
  shouldRevealTurn,
} from "./bubbles";
import {
  MAX_EVENT_PAGES,
  MAX_STEP_PAGES,
  MAX_TURN_PROCESS_CACHE,
  classifyProcess,
  commentaryText,
  stepDurationMs,
  toolArguments,
  type LoadState,
  type StepView,
  type TurnStepPage,
} from "./turnProcess";
import { toolStatusLabel } from "./toolStatus";
import {
  memorySearchAvailability,
  mergeMemoryPages,
  normalizeMemoryPage,
  searchQueries,
  type MemorySearchPage,
} from "./knowledgeSearch";
import { knowledgeSearchOverlay } from "./knowledgeSearchView";
import type {
  AppInfo,
  BridgeError,
  ConfirmResult,
  CognitionCandidate,
  CognitionCandidateList,
  ConsultResult,
  HostReply,
  LegacyWorkItem,
  ModelStatus,
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

const chatApi = createChatApi((command, args) => invoke(command, args));

function sessionId(): string {
  return state.host?.session_id ?? "";
}

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

async function bridgePing(): Promise<HostReply> {
  return (await chatApi.bridgePing()) as HostReply;
}

async function conversationCreate(mode: string): Promise<HostReply> {
  return (await chatApi.conversationCreate(sessionId(), mode)) as HostReply;
}

async function conversationList(options: {
  limit?: number | null;
  cursor?: string | null;
} = {}): Promise<HostReply> {
  return (await chatApi.conversationList(
    sessionId(),
    options,
  )) as HostReply;
}

async function conversationGet(
  conversationId: string,
  options: {
    beforeTurnNo?: number | null;
    beforeSeq?: number | null;
    limit?: number | null;
  } = {},
): Promise<HostReply> {
  return (await chatApi.conversationGet(
    sessionId(),
    conversationId,
    options,
  )) as HostReply;
}

async function conversationSend(params: {
  sessionId: string;
  conversationId: string;
  clientRequestId: string;
  text: string;
  confirmedDestinationId: string | null;
}): Promise<HostReply> {
  return (await chatApi.conversationSend(params)) as HostReply;
}

async function turnResult(turnId: string): Promise<HostReply> {
  return (await chatApi.turnResult(sessionId(), turnId)) as HostReply;
}

async function turnEvents(
  turnId: string,
  afterSeq: number,
): Promise<HostReply> {
  return (await chatApi.turnEvents(sessionId(), turnId, {
    afterSeq,
    limit: 100,
  })) as HostReply;
}

async function turnSteps(
  turnId: string,
  afterStepNo = 0,
): Promise<HostReply> {
  return (await chatApi.turnSteps(sessionId(), turnId, {
    afterStepNo,
    limit: 50,
  })) as HostReply;
}

async function turnCancel(turnId: string): Promise<HostReply> {
  return (await chatApi.turnCancel(sessionId(), turnId)) as HostReply;
}

async function turnTrace(turnId: string): Promise<HostReply> {
  return (await chatApi.turnTrace(sessionId(), turnId)) as HostReply;
}

async function conversationUsage(
  conversationId: string,
  turnId: string | null = null,
): Promise<HostReply> {
  return (await chatApi.conversationUsage(
    sessionId(),
    conversationId,
    turnId,
  )) as HostReply;
}

async function searchTranscript(
  query: string,
  before: string | null = null,
): Promise<HostReply> {
  return (await chatApi.conversationSearch(sessionId(), query, {
    limit: 10,
    before,
    includeFull: false,
  })) as HostReply;
}

async function searchMemory(
  query: string,
  cursor: string | null = null,
): Promise<HostReply> {
  return (await chatApi.memorySearch(sessionId(), query, {
    limit: 10,
    cursor,
  })) as HostReply;
}

async function postprocessStatus(
  conversationId: string,
  turnIds: string[] | null = null,
): Promise<HostReply> {
  return (await chatApi.postprocessStatus(
    sessionId(),
    conversationId,
    turnIds,
  )) as HostReply;
}

async function postprocessRetry(turnId: string): Promise<HostReply> {
  return (await chatApi.postprocessRetry(sessionId(), turnId)) as HostReply;
}

interface TurnEventPage {
  events: ProcessEvent[];
  next_seq: number;
  terminal: boolean;
  truncated_from_seq?: number;
}

async function setAiDisplayName(name: string): Promise<HostReply> {
  return (await invoke("set_ai_display_name", { name })) as HostReply;
}

async function modelStatus(): Promise<HostReply> {
  return (await invoke("model_status", {})) as HostReply;
}

async function saveModelApiKey(apiKey: string): Promise<HostReply> {
  return (await invoke("save_model_api_key", { apiKey })) as HostReply;
}

async function deleteModelApiKey(): Promise<HostReply> {
  return (await invoke("delete_model_api_key", {})) as HostReply;
}

async function setModelChoice(model: string): Promise<HostReply> {
  return (await invoke("set_model_choice", { model })) as HostReply;
}

async function testModelConnection(sessionId: string): Promise<HostReply> {
  return (await invoke("test_model_connection", { sessionId })) as HostReply;
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
let chat: ChatState = INITIAL_CHAT_STATE;
// RG08: a bubble animates once; polling re-renders and same-id reloads are quiet.
const bubbleReveal = new BubbleRevealTracker();
let chatPollTimer: number | null = null;
let postprocessLoading = false;
// RG05-F01: bounded status refresh while an idle delivery can still finish.
let postprocessPollTimer: number | null = null;
let postprocessPollAttempts = 0;
const POSTPROCESS_POLL_INTERVAL_MS = 1500;
const POSTPROCESS_POLL_LIMIT = 5;

interface TranscriptSearchUi {
  open: boolean;
  query: string;
  loading: boolean;
  error: string | null;
  page: TranscriptSearchPage | null;
  memoryLoading: boolean;
  memoryError: string | null;
  memoryPage: MemorySearchPage | null;
}

let transcriptSearch: TranscriptSearchUi = {
  open: false,
  query: "",
  loading: false,
  error: null,
  page: null,
  memoryLoading: false,
  memoryError: null,
  memoryPage: null,
};
let pendingChatSend = false;
let creatingConversation = false;
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
  personaChip: HTMLSpanElement;
  settingsButton: HTMLButtonElement;
  main: HTMLElement;
  footer: HTMLElement;
  banner: HTMLElement;
  textarea: HTMLTextAreaElement;
  sendButton: HTMLButtonElement;
  cancelButton: HTMLButtonElement;
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
  transcriptHost: HTMLElement;
}

let shell: Shell;
let appView: AppShell;
let settingsPageMounted = false;
let dashboardKey = "";
let followConversation = true;
let requestedScroll = false;
/**
 * Card C: a saved scroll offset to restore on the next chat render.  Used after
 * a conversation switch loads its messages; restoring while the thread is still
 * empty would be clamped to the top.
 */
let pendingScrollRestore: number | null = null;
/**
 * Card C2: preserve the reader's viewport when an older page is prepended.
 * The new content increases scrollHeight; adding the same delta to scrollTop
 * keeps the message they were reading at the same screen position.
 */
let pendingHistoryAnchor: { top: number; height: number } | null = null;
/** Invalidates an older-page response after a switch or a new request. */
let historyRequestToken = 0;
/** A history prepend is not "new activity" at the bottom. */
let suppressNewActivity = false;
const conversationScroll = new Map<string, number>();
const conversationDrafts = new Map<string, string>();
let renderedConversation: string | null = null;
const processExpansion = new Map<string, boolean>();
let lastActivityKey = "";
/** UI-F04 display-only conversation state; never a business record. */
let displayPreferences: DisplayPreferences = EMPTY_DISPLAY_PREFERENCES;
let displayPreferencesFailed = false;
interface RenameDialog {
  overlay: HTMLElement;
  modal: HTMLElement;
  input: HTMLInputElement;
  error: HTMLElement;
  busy: boolean;
  trigger: HTMLElement | null;
  conversationId: string;
}
let renameDialog: RenameDialog | null = null;

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
    children: [el("span", { class: "status-dot" }), el("span", { text: uiText("连接中") })],
  });
  const destinationChip = el("button", {
    class: "destination-chip",
    text: uiText("目的地未就绪"),
    attrs: { type: "button", id: "destination-chip", title: "查看目的地说明" },
  });
  destinationChip.addEventListener("click", () => openDrawer("destination"));
  const personaChip = el("span", {
    class: "persona-chip is-not-ready",
    text: uiText("人格未就绪"),
    attrs: { id: "persona-chip", title: "人格状态（只读摘要）" },
  });
  const settingsButton = el("button", {
    class: "icon-button settings-button",
    attrs: { type: "button", id: "settings-button", "aria-label": "设置" },
    children: [icon("gear")],
  });

  const topbarRight = el("div", {
    class: "topbar-right",
    children: [hostIndicator, personaChip, destinationChip, settingsButton],
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
  const target = el("strong", { attrs: { id: "composer-target" }, text: uiText("目的地未就绪") });
  const counter = el("span", { class: "counter", attrs: { id: "char-count" }, text: "0 / 8000" });
  const hint = el("span", { class: "composer-hint", attrs: { id: "composer-hint" } });
  const textarea = el("textarea", {
    class: "composer-input",
    attrs: {
      id: "question",
      rows: "2",
      placeholder: "今天想聊些什么？",
      "aria-label": "咨询问题",
      spellcheck: "false",
    },
  });
  const sendButton = el("button", {
    class: "send-button",
    attrs: { type: "button", id: "action-send" },
    children: [
      el("span", { class: "send-label visually-hidden", text: uiText("发送") }),
      icon("arrow"),
    ],
  });
  const sourcesButton = el("button", {
    class: "ghost-button",
    text: uiText("来源与详情"),
    attrs: { type: "button", id: "open-drawer" },
  });
  sourcesButton.addEventListener("click", () => openDrawer("sources"));
  sendButton.addEventListener("click", () => void requestSend());
  const cancelButton = el("button", {
    class: "quiet-button cancel-button hidden",
    attrs: { type: "button", id: "action-cancel", "aria-label": "停止本轮" },
    children: [el("span", { class: "stop-symbol", attrs: { "aria-hidden": "true" } })],
  });
  cancelButton.addEventListener("click", () => void cancelChatTurn());

  const composerTop = el("div", {
    class: "composer-top",
    children: [
      el("span", {
        class: "composer-target",
        children: [el("span", { text: uiText("发送至 ") }), target],
      }),
      el("span", { class: "composer-mode", text: uiText("Consult · 单轮") }),
    ],
  });
  const composerFoot = el("div", {
    class: "composer-foot",
    children: [
      el("div", {
        class: "composer-actions",
        children: [cancelButton, sendButton],
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
    text: uiText("关闭"),
  });
  const drawerTitle = el("h2", {
    class: "drawer-title",
    text: uiText("详情"),
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
  const transcriptHost = el("div", { class: "transcript-host hidden" });
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
    transcriptHost,
  );
  drawerClose.addEventListener("click", () => closeDrawer());
  drawerBackdrop.addEventListener("click", () => closeDrawer());
  document.addEventListener("keydown", onGlobalKeydown);

  shell = {
    hostIndicator,
    destinationChip,
    personaChip,
    settingsButton,
    main,
    footer,
    banner,
    textarea,
    sendButton,
    cancelButton,
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
    transcriptHost,
  };
  appView = new AppShell(root, { brand, header: topbar, main, footer, settings: settingsButton }, {
    route(page: PageId) {
      state = reduce(state, { type: "drawer-close" });
      if (page === "settings") {
        // UI-F02: the settings page shows live instance/conversation state
        // (persona versions, preferences, import directory).  Re-entering it
        // must rebuild the panels instead of showing the first visit's DOM.
        settingsPageMounted = false;
      }
      if (page === "chat") switchWorkspace("consult");
      if (page === "review") switchWorkspace("review");
      if (page === "dashboard") void loadCognitionCandidates();
      render();
    },
    create: () => void createConversation(),
    select: id => void openConversation(id),
    search: openTranscriptSearch,
    notice: showToast,
    follow: () => { followConversation = true; requestedScroll = true; },
    rename: (id, title) => openRenameDialog(id, title),
    archive: id => void archiveConversation(id),
  });

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
  if (transcriptSearch.open) {
    if (event.key === "Escape") { event.preventDefault(); closeTranscriptSearch(); return; }
    if (event.key === "Tab") {
      const items = focusable(shell.transcriptHost), first = items[0], last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
    return;
  }
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

/** The display-title override is the only title layer the UI renders. */
function displayedConversations(): ConversationSummary[] {
  return mergeDisplayTitles(chat.conversations, displayPreferences);
}

function sidebarModel(): SidebarModel<ConversationSummary> {
  const displayed = displayedConversations();
  const visible = visibleConversations(displayed, displayPreferences);
  return {
    groups: groupConversations(visible, new Date()),
    visibleCount: visible.length,
    archivedCount: displayPreferences.archived.length,
    notice: displayPreferencesFailed ? "unavailable" : null,
  };
}

async function loadDisplayPreferencesFromHost(): Promise<void> {
  try {
    displayPreferences = await loadDisplayPreferences(invoke);
    displayPreferencesFailed = false;
  } catch {
    // Keep showing the real host list, and say that archive filtering is off.
    displayPreferences = EMPTY_DISPLAY_PREFERENCES;
    displayPreferencesFailed = true;
  }
  render();
}

/** The host serializes writes; the client still keeps responses in order. */
let displayWriteQueue: Promise<unknown> = Promise.resolve();
function queueDisplayWrite<T>(operation: () => Promise<T>): Promise<T> {
  const next = displayWriteQueue.then(operation, operation);
  displayWriteQueue = next.then(() => undefined, () => undefined);
  return next;
}

function displayFailure(failure: unknown, fallback: string): string {
  if (failure instanceof DisplayPreferenceError && failure.message) {
    return failure.message;
  }
  return fallback;
}

function titleReasonText(reason: "empty" | "tooLong" | "control"): string {
  if (reason === "empty") return shellText("renameEmpty");
  if (reason === "tooLong") return shellText("renameTooLong");
  return shellText("renameControl");
}

function focusConversationRow(conversationId: string): void {
  document
    .querySelector<HTMLElement>(
      `.recent-row[data-conversation-id="${conversationId}"] .recent-select`,
    )
    ?.focus({ preventScroll: true });
}

function closeRenameDialog(restoreFocus: boolean): void {
  const dialog = renameDialog;
  if (!dialog) return;
  renameDialog = null;
  dialog.overlay.remove();
  if (restoreFocus && dialog.trigger && document.contains(dialog.trigger)) {
    dialog.trigger.focus({ preventScroll: true });
  }
}

function openRenameDialog(conversationId: string, currentTitle: string): void {
  closeRenameDialog(false);
  const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const headingId = "rename-dialog-heading";
  const hintId = "rename-dialog-hint";
  const input = el("input", {
    class: "setting-input",
    attrs: {
      type: "text",
      maxlength: String(MAX_TITLE_CHARS),
      autocomplete: "off",
      spellcheck: "false",
      "aria-labelledby": headingId,
      "aria-describedby": hintId,
    },
  }) as HTMLInputElement;
  input.value = currentTitle;
  const error = el("p", { class: "form-error", attrs: { role: "alert" } });
  const hint = el("p", {
    class: "muted",
    text: shellText("renameHint"),
    attrs: { id: hintId },
  });
  const cancel = el("button", { class: "quiet-button", text: shellText("cancel"), attrs: { type: "button" } });
  const save = el("button", { class: "quiet-button", text: shellText("confirm"), attrs: { type: "button" } });
  const restore = el("button", {
    class: "quiet-button",
    text: shellText("restoreDefaultTitle"),
    attrs: { type: "button" },
  });
  restore.hidden = displayPreferences.titles[conversationId] === undefined;
  const modal = el("section", {
    class: "review-modal",
    attrs: { role: "dialog", "aria-modal": "true", "aria-labelledby": headingId },
    children: [
      el("div", {
        class: "review-modal-head",
        children: [el("h2", { text: shellText("renameTitle"), attrs: { id: headingId } })],
      }),
      el("div", { class: "review-modal-body", children: [hint, input, error] }),
      el("div", { class: "review-modal-actions", children: [restore, cancel, save] }),
    ],
  });
  const overlay = el("div", {
    class: "review-overlay",
    attrs: { id: "rename-overlay" },
    children: [modal],
  });
  overlay.addEventListener("pointerdown", event => {
    if (event.target === overlay) closeRenameDialog(true);
  });
  overlay.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeRenameDialog(true);
      return;
    }
    if (event.key !== "Tab") return;
    const nodes = focusable(modal);
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  cancel.onclick = () => closeRenameDialog(true);
  save.onclick = () => void submitRenameDialog("rename");
  restore.onclick = () => void submitRenameDialog("clear_title");
  input.addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      void submitRenameDialog("rename");
    }
  });
  const dialog: RenameDialog = {
    overlay,
    modal,
    input,
    error,
    busy: false,
    trigger,
    conversationId,
  };
  renameDialog = dialog;
  document.body.append(overlay);
  input.focus({ preventScroll: true });
  input.select();
}

async function submitRenameDialog(action: "rename" | "clear_title"): Promise<void> {
  const dialog = renameDialog;
  if (!dialog || dialog.busy) return;
  let value: string | undefined;
  if (action === "rename") {
    const checked = validateConversationTitle(dialog.input.value);
    if (!checked.ok) {
      dialog.error.textContent = titleReasonText(checked.reason);
      dialog.input.focus();
      return;
    }
    if (titleLimitReached(displayPreferences, dialog.conversationId)) {
      dialog.error.textContent = shellText("renameFailed");
      return;
    }
    value = checked.value;
  }
  const controls = Array.from(dialog.modal.querySelectorAll<HTMLButtonElement>("button"));
  dialog.busy = true;
  controls.forEach(control => { control.disabled = true; });
  dialog.input.disabled = true;
  try {
    const next = await queueDisplayWrite(() =>
      setDisplayPreference(invoke, action, dialog.conversationId, value),
    );
    displayPreferences = next;
    displayPreferencesFailed = false;
    closeRenameDialog(false);
    render();
    showToast(shellText("renamed"));
    focusConversationRow(dialog.conversationId);
  } catch (failure) {
    dialog.error.textContent = displayFailure(failure, shellText("renameFailed"));
    dialog.busy = false;
    controls.forEach(control => { control.disabled = false; });
    dialog.input.disabled = false;
    dialog.input.focus();
  }
}

async function archiveConversation(conversationId: string): Promise<void> {
  if (archiveLimitReached(displayPreferences, conversationId)) {
    showToast(shellText("archiveFailed"));
    return;
  }
  const active = chat.activeId === conversationId;
  try {
    displayPreferences = await queueDisplayWrite(() =>
      setDisplayPreference(invoke, "archive", conversationId),
    );
    displayPreferencesFailed = false;
    render();
    showToast(shellText(active ? "archivedActive" : "archivedDone"));
  } catch (failure) {
    showToast(displayFailure(failure, shellText("archiveFailed")));
  }
}

async function restoreArchivedConversation(conversationId: string): Promise<string | null> {
  try {
    displayPreferences = await queueDisplayWrite(() =>
      setDisplayPreference(invoke, "unarchive", conversationId),
    );
    displayPreferencesFailed = false;
    return null;
  } catch (failure) {
    return displayFailure(failure, uiText("恢复失败；未改变显示"));
  }
}

async function restoreAllArchived(): Promise<string | null> {
  try {
    displayPreferences = await queueDisplayWrite(() =>
      setDisplayPreference(invoke, "restore_all"),
    );
    displayPreferencesFailed = false;
    return null;
  } catch (failure) {
    return displayFailure(failure, uiText("恢复失败；未改变显示"));
  }
}

function conversationDisplaySection(): HTMLElement {
  const archived = sortedArchived(displayPreferences);
  const displayed = displayedConversations();
  const byId = new Map(displayed.map(item => [item.conversation_id, item]));
  const status = el("p", { class: "muted", attrs: { role: "status", "aria-live": "polite" } });
  const list = el("div", { class: "archived-list" });
  if (archived.length === 0) {
    list.append(el("p", { class: "muted", text: uiText("没有已归档的会话。") }));
  }
  const remount = () => {
    const scroll = appView.pages.scrollTop;
    settingsPageMounted = false;
    render();
    window.requestAnimationFrame(() => {
      appView.pages.scrollTop = scroll;
    });
  };
  for (const entry of archived) {
    const item = byId.get(entry.conversation_id);
    const title = item?.title ?? `${shellText("conversation")} ${shortId(entry.conversation_id)}`;
    const when = new Date(entry.archived_at_ms).toLocaleString(document.documentElement.lang, {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
    const restore = el("button", {
      class: "quiet-button",
      text: uiText("恢复显示"),
      attrs: { type: "button" },
    });
    restore.onclick = async () => {
      restore.disabled = true;
      status.textContent = uiText("正在恢复…");
      const failure = await restoreArchivedConversation(entry.conversation_id);
      if (failure) {
        status.textContent = failure;
        restore.disabled = false;
        return;
      }
      showToast(uiText("已恢复显示；会话记录未改变。"));
      remount();
    };
    list.append(
      el("div", {
        class: "archived-row",
        children: [
          el("div", {
            class: "archived-meta",
            children: [
              el("strong", { text: title }),
              el("small", { text: `${uiText("归档于")} ${when}` }),
            ],
          }),
          restore,
        ],
      }),
    );
  }
  const all = el("button", {
    class: "quiet-button",
    text: uiText("全部恢复"),
    attrs: { type: "button" },
  });
  all.disabled = archived.length === 0;
  all.onclick = async () => {
    all.disabled = true;
    status.textContent = uiText("正在恢复…");
    const failure = await restoreAllArchived();
    if (failure) {
      status.textContent = failure;
      all.disabled = false;
      return;
    }
    showToast(uiText("已恢复显示；会话记录未改变。"));
    remount();
  };
  return drawerSection(uiText("会话列表与归档"), [
    el("p", {
      class: "muted",
      text: uiText("归档只隐藏列表显示；对话、轮次、消息、记忆与记录都不改变，可随时恢复。"),
    }),
    status,
    list,
    el("div", { class: "setting-row", children: [all] }),
    el("p", {
      class: "muted",
      text: uiText("重命名同样只改变显示标题；宿主会话标题与记录不变。"),
    }),
  ]);
}

function render(): void {
  shell.settingsButton.setAttribute("aria-label", uiText("设置"));
  shell.drawerClose.setAttribute("aria-label", uiText("关闭"));
  shell.drawerClose.textContent = uiText("关闭");
  shell.sourcesButton.textContent = uiText("来源与详情");
  syncTopbar();
  renderMain();
  syncComposer();
  renderDrawer();
  renderReviewOverlay();
  renderTranscriptOverlay();
  const displayed = displayedConversations();
  const overview = sessionOverview(state.host, { ...chat, conversations: displayed }, usageLoad);
  appView.update(overview, sidebarModel(), canSwitchConversation(chat) && !creatingConversation, Boolean(state.host?.host.ready));
  if (appView.page === "settings" && !settingsPageMounted) {
    settingsPageMounted = true;
    appView.settingsPage.replaceChildren(el("div", { class: "page-heading", children: [el("div", { children: [
      el("div", { class: "eyebrow", text: shellText("preferences") }),
      el("h1", { text: shellText("settingsTitle") }),
      el("p", { class: "page-subtitle", text: shellText("settingsSub") }),
    ] })] }));
    renderSettingsTab(appView.settingsPage);
  }
  if (appView.page === "dashboard") {
    const candidates = { configured: Boolean(state.host?.capabilities.cognition?.enabled), loaded: Boolean(state.host?.capabilities.cognition?.enabled) && cognitionView.loaded && !cognitionView.error,
      loading: cognitionView.loading, error: cognitionView.error,
      human: cognitionView.candidates.filter(item => item.requires_human).length,
      other: cognitionView.candidates.filter(item => !item.requires_human).length };
    const key = JSON.stringify([overview, candidates, document.documentElement.lang]);
    if (key !== dashboardKey || appView.pages.firstElementChild?.classList.contains("settings-page")) {
      dashboardKey = key;
      appView.setDashboard(dashboard(overview, candidates, {
        settings: () => appView.navigate("settings"), review: () => appView.navigate("review"),
        details: () => openDrawer("sources"),
        refresh: () => { void refreshHost(); void loadCognitionCandidates(true); if (chat.activeId) { void loadPostprocess(chat.activeId, true); void loadUsage(chat.activeId); } },
      }));
    }
  }
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
    el("span", { class: "status-text", text: uiText(label) }),
  );
  shell.hostIndicator.title = ready
    ? "可信宿主已就绪"
    : reasonLabel(state.hostError?.code ?? info?.host.reason_code ?? null, null);

  const destination = state.destination;
  shell.destinationChip.classList.toggle("is-not-ready", !destination);
  shell.destinationChip.textContent = destination
    ? document.documentElement.lang === "en" ? `${destination.external ? "External" : "Local"} · ${destination.provider} / ${destination.model}` : destinationLabel(destination)
    : uiText("目的地未就绪");
  shell.destinationChip.setAttribute(
    "aria-label",
    `目的地：${destinationLabel(destination)}`,
  );

  const personaReady = info?.persona?.state === "ready";
  shell.personaChip.classList.toggle("is-not-ready", !personaReady);
  shell.personaChip.textContent = uiAssistantName();
  shell.personaChip.title = personaReady
    ? `人格：${info?.persona?.profile_id ?? ""} ${info?.persona?.profile_version ?? ""}\n来源：${personaSourceLabel(info ?? null)}\n当前交谈者：${speakerLabel(info ?? null)}`
    : reasonLabel(
        state.hostError?.code ??
          info?.persona?.reason_code ??
          info?.host.reason_code ??
          null,
        null,
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
  const visible = shell.main.offsetParent !== null;
  const previous = shell.main.scrollTop;
  const forced = pendingScrollRestore;
  const history = pendingHistoryAnchor;
  if (visible && renderedConversation) conversationScroll.set(renderedConversation, forced ?? previous);
  const changed = renderedConversation !== chat.activeId;
  if (changed) bubbleReveal.reset();
  const position = forced ?? (changed ? conversationScroll.get(chat.activeId ?? "") : previous);
  if (visible) followConversation = forced !== null || history !== null ? false : requestedScroll || (changed ? position === undefined : shell.main.scrollHeight - shell.main.clientHeight - previous < 80);
  if (visible) requestedScroll = false;
  const activityKey = `${chat.activeId}:${chat.messages.length}:${chat.eventCursor}`;
  if (changed || followConversation || suppressNewActivity) {
    appView.setNewActivity(false);
    suppressNewActivity = false;
  } else if (activityKey !== lastActivityKey) appView.setNewActivity(true);
  lastActivityKey = activityKey;
  renderedConversation = chat.activeId;
  const renderedFor = chat.activeId;
  window.requestAnimationFrame(() => {
    // A switch may have happened before this frame; the new render owns the
    // scroll position now.
    if (chat.activeId !== renderedFor) {
      return;
    }
    if (shell.main.offsetParent !== null) {
      shell.main.scrollTop = history !== null
        ? history.top + Math.max(0, shell.main.scrollHeight - history.height)
        : forced ?? (followConversation ? shell.main.scrollHeight : (position ?? previous));
    }
    if (history !== null) pendingHistoryAnchor = null;
    if (forced !== null) pendingScrollRestore = null;
  });
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

  if (conversationEnabled()) {
    inner.appendChild(chatWorkspace());
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
    if (followConversation) shell.main.scrollTop = shell.main.scrollHeight;
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
      el("div", { class: "message-who", text: uiText("你") }),
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
          el("span", { text: uiText("正在思考…") }),
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
        el("p", { class: "muted", text: uiText("本次没有可显示的正文；这不作为已回答展示。") }),
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
      el("div", { class: "result-title", text: uiText("等待（暂无完成回答）") }),
      el("p", {
        class: "muted",
        text: uiText("模型这次没有给出完成的回答。这是一次有效结果，不是错误，也不是失败。"),
      }),
      el("p", {
        class: "muted",
        text: uiText("没有创建候选，也没有自动重试或静默重发。"),
      }),
    );
    if (result.reply) {
      bubbleChildren.push(
        el("div", {
          class: "wait-note",
          children: [
            el("span", { class: "wait-label", text: uiText("等待说明") }),
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
      el("summary", { text: uiText("技术详情") }),
      el("code", { text: detail ?? "无阶段标签" }),
      el("p", {
        class: "muted",
        text: uiText("只显示阶段标签；不包含 Prompt、HTTP 正文、模型原始输出或内部修订号。"),
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
        text: uiText("想聊些什么？写下你的问题，我会先由本机可信宿主登记，再据此生成回答。"),
      }),
      el("p", {
        class: "empty-note",
        text: uiText("每次 Consult 都是独立的一轮；刷新或重启后无法恢复此前结果。"),
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
          el("h2", { text: uiText("正在连接可信宿主…") }),
          el("p", { class: "muted", text: uiText("宿主就绪前不会发送任何咨询。") }),
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
    text: uiText("重新连接"),
    attrs: { type: "button", id: "retry-host" },
  });
  button.addEventListener("click", () => void boot());
  return el("section", {
    class: "system-card system-card-warn",
    children: [
      el("div", { class: "system-icon is-warn", children: [icon("alert")] }),
      el("div", {
        children: [
          el("h2", { text: uiText("宿主未就绪") }),
          el("p", { class: "state-lead", text: reasonLabel(code, null) }),
          el("p", { class: "muted", text: detail }),
          el("p", {
            class: "muted",
            text: uiText("发送保持禁用；本窗口不会伪造授权或结果。"),
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
    text: uiText("开始新的提问"),
    attrs: { type: "button" },
  });
  button.addEventListener("click", () => newQuestion());
  return el("section", {
    class: "system-card system-card-danger",
    children: [
      el("div", { class: "system-icon is-danger", children: [icon("alert")] }),
      el("div", {
        children: [
          el("h2", { text: uiText("结果未知") }),
          el("p", {
            class: "state-lead",
            text: uiText("请求可能已经到达宿主，但没有收到完成响应。"),
          }),
          el("p", {
            class: "muted",
            text: uiText("不会自动重发，也不会读取本地缓存冒充正式结果；目前没有权威恢复接口。"),
          }),
          el("p", {
            class: "muted",
            text: uiText("请以一个新的提问重新开始。明确失败与结果未知是两个不同的状态。"),
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
    text: uiText("返回输入"),
    attrs: { type: "button" },
  });
  button.addEventListener("click", () => newQuestion());
  return el("section", {
    class: "system-card system-card-warn",
    children: [
      el("div", { class: "system-icon is-warn", children: [icon("clock")] }),
      el("div", {
        children: [
          el("h2", { text: uiText("已有一次咨询正在处理") }),
          el("p", {
            class: "state-lead",
            text: uiText("第二个请求没有排队，也没有创建新的 interaction、输入或 Attempt。"),
          }),
          el("p", {
            class: "muted",
            text: uiText("本窗口不会自动重发第一个咨询，也不会伪造它的结果。"),
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

interface CognitionViewState {
  loaded: boolean;
  loading: boolean;
  error: string | null;
  candidates: CognitionCandidate[];
  pendingReview: boolean;
  busy: string | null;
  notice: string | null;
}

const cognitionView: CognitionViewState = {
  loaded: false,
  loading: false,
  error: null,
  candidates: [],
  pendingReview: false,
  busy: null,
  notice: null,
};

function cognitionStateLabel(candidate: CognitionCandidate): string {
  if (candidate.requires_human) {
    return candidate.state === "ESCALATED" ? "需你判断" : "待你确认";
  }
  if (candidate.state === "COMMITTED") return "已生效";
  if (candidate.state === "REJECTED") return "未通过";
  if (candidate.state === "DEFERRED") return "证据不足";
  if (candidate.state === "NOT_AUTHORIZED") return "等待授权";
  return "等待授权";
}

async function loadCognitionCandidates(force = false): Promise<void> {
  const caps = state.host?.capabilities.cognition;
  if (!caps?.enabled) {
    cognitionView.loaded = true;
    return;
  }
  if (cognitionView.loading) return;
  if (cognitionView.loaded && !force) return;
  const session = sessionId();
  if (!session) return;
  cognitionView.loading = true;
  cognitionView.error = null;
  try {
    const reply = (await chatApi.cognitionCandidates(session, 20)) as HostReply;
    if (!reply.ok) {
      cognitionView.error = reply.error?.message ?? "读取认知候选失败";
    } else {
      const result = reply.result as CognitionCandidateList;
      cognitionView.candidates = result.candidates ?? [];
      cognitionView.pendingReview = !!result.pending_review;
    }
  } catch (error) {
    cognitionView.error = invokeError(error).message;
  } finally {
    cognitionView.loading = false;
    cognitionView.loaded = true;
    render();
  }
}

async function decideCognitionCandidate(
  candidate: CognitionCandidate,
  decision: "approve" | "reject",
): Promise<void> {
  if (cognitionView.busy) return;
  const session = sessionId();
  if (!session) return;
  cognitionView.busy = candidate.candidate_id;
  cognitionView.notice = null;
  cognitionView.error = null;
  try {
    const reply = (await chatApi.cognitionCandidateDecide(
      session,
      candidate.candidate_id,
      candidate.artifact_version,
      decision,
    )) as HostReply;
    if (!reply.ok) {
      cognitionView.error = reply.error?.message ?? "提交决定失败";
    } else if (decision === "approve") {
      cognitionView.notice = "已提交批准；以实际状态为准。";
    } else {
      cognitionView.notice = "已提交拒绝；以实际状态为准。";
    }
  } catch (error) {
    cognitionView.error = invokeError(error).message;
  } finally {
    cognitionView.busy = null;
    cognitionView.loaded = false;
    await loadCognitionCandidates(true);
  }
}

function cognitionPane(info: AppInfo): HTMLElement | null {
  const caps = info.capabilities.cognition;
  if (!caps?.enabled) return null;
  void loadCognitionCandidates();
  const rows: HTMLElement[] = [];
  if (cognitionView.loading && !cognitionView.loaded) {
    rows.push(el("p", { class: "muted", text: uiText("正在读取认知候选…") }));
  } else if (cognitionView.error) {
    rows.push(callout(cognitionView.error, "danger"));
  } else if (cognitionView.candidates.length === 0) {
    rows.push(
      el("p", {
        class: "review-empty",
        text: caps.propose
          ? "没有待处理的认知候选。"
          : "本实例未开启候选形成（cognition.propose=false）。",
      }),
    );
  } else {
    for (const candidate of cognitionView.candidates) {
      const actions: HTMLElement[] = [];
      if (candidate.requires_human && cognitionView.pendingReview) {
        for (const [decision, label] of [
          ["approve", "批准"],
          ["reject", "拒绝"],
        ] as const) {
          const button = el("button", {
            class: "quiet-button",
            text: cognitionView.busy === candidate.candidate_id ? "提交中…" : label,
            attrs: { type: "button" },
          });
          button.disabled = cognitionView.busy !== null;
          button.addEventListener("click", () =>
            void decideCognitionCandidate(candidate, decision),
          );
          actions.push(button);
        }
      }
      rows.push(
        el("div", {
          class: "review-work-item",
          children: [
            el("div", {
              class: "review-work-main",
              children: [
                el("span", { class: "review-work-event", text: candidate.claim }),
                el("span", {
                  class: "review-work-meta",
                  text: `${cognitionStateLabel(candidate)} · ${
                    candidate.claim_type ?? "未分类"
                  } · ${candidate.created_at}`,
                }),
              ],
            }),
            el("div", { class: "review-work-actions", children: actions }),
          ],
        }),
      );
    }
  }
  const refresh = el("button", {
    class: "quiet-button",
    text: uiText("刷新"),
    attrs: { type: "button" },
  });
  refresh.addEventListener("click", () => {
    cognitionView.loaded = false;
    void loadCognitionCandidates(true);
  });
  return el("aside", {
    class: "review-pane review-cognitionpane",
    attrs: { "aria-label": "认知候选" },
    children: [
      el("div", {
        class: "review-pane-head",
        children: [el("h2", { text: uiText("认知候选") }), refresh],
      }),
      el("p", {
        class: "review-pane-note",
        text: uiText("候选由聊天中的模型建议形成，未生效前不是正式认识；只有待人工的候选可以在这里批准或拒绝。"),
      }),
      cognitionView.notice
        ? callout(cognitionView.notice, "info")
        : null,
      el("div", { class: "review-work-list", children: rows }),
    ],
  });
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
      cognitionPane(info),
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
    text: uiText("刷新宿主状态"),
    attrs: { type: "button", id: "review-refresh-host" },
  });
  refresh.addEventListener("click", () => void refreshHost());
  return el("section", {
    class: "system-card system-card-warn review-unavailable",
    children: [
      el("div", { class: "system-icon is-warn", children: [icon("alert")] }),
      el("div", {
        children: [
          el("h2", { text: uiText("Review 暂不可用") }),
          el("p", {
            class: "state-lead",
            text: reviewReasonLabel(info.workspace.review_reason),
          }),
          el("p", {
            class: "muted",
            text: uiText("审查权限由私有宿主配置；本窗口不能自行赋权，也不能读取数据库补齐。"),
          }),
          el("p", {
            class: "muted",
            text: uiText("Consult 不受影响；你可以继续在 Consult 工作区使用。"),
          }),
          refresh,
        ],
      }),
    ],
  });
}

function reviewHeader(info: AppInfo): HTMLElement {
  const refresh = el("button", { class: "quiet-button", text: uiText("刷新"), attrs: { type: "button", id: "review-refresh" } });
  refresh.onclick = () => void refreshReviewData();
  const english = document.documentElement.lang === "en";
  return el("header", { class: "review-head page-heading", children: [
    el("div", { children: [el("div", { class: "eyebrow", text: shellText("instance") }),
      el("h1", { text: english ? "Decisions, with context." : "待你确认的事项。" }),
      el("p", { class: "page-subtitle", text: english ? "Inspect pending work and review proposals before accepting them." : "查看待审查事项与候选，再决定是否接受。" }),
    ] }), refresh,
  ] });
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
        text: uiText("查询权威结果"),
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
      text: uiText("刷新工作与候选"),
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
              el("strong", { text: uiText("结果未知") }),
              el("p", { text: review.unknown.message }),
              el("p", {
                class: "muted",
                text: uiText("不会自动重发本次审查或决定；只以权威结果恢复。"),
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
    text: uiText("待审查"),
    attrs: {
      type: "button",
      role: "tab",
      "aria-selected": review.tab === "managed" ? "true" : "false",
    },
  });
  managedTab.addEventListener("click", () => applyReview({ type: "tab", tab: "managed" }));
  const legacyTab = el("button", {
    class: `review-tab ${review.tab === "legacy" ? "is-active" : ""}`,
    text: document.documentElement.lang === "en" ? "History · read-only" : "历史只读",
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
      text: uiText("重新加载"),
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
          text: uiText("没有待审查的工作项。新的工作项由宿主在处理事件时创建。"),
        }),
      );
    }
    for (const work of review.managed) {
      body.appendChild(reviewManagedRow(work));
    }
  } else {
    if (review.legacy.length === 0) {
      body.appendChild(
        el("p", { class: "review-empty", text: uiText("没有旧工作项。") }),
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
          el("h2", { text: uiText("工作列表") }),
          el("span", {
            class: "review-visible-count",
            text: document.documentElement.lang === "en" ? `${review.managed.length} pending · ${review.legacy.length} archived` : `${review.managed.length} 待审查 · ${review.legacy.length} 历史`,
          }),
        ],
      }),
      tabs,
      body,
      el("p", {
        class: "review-pane-note",
        text: document.documentElement.lang === "en" ? "Only work available to this instance is shown." : "仅展示当前实例允许查看的事项。",
      }),
    ],
  });
}

function reviewManagedRow(work: ReviewWorkItem): HTMLElement {
  const active = review.selectedWork?.work_id === work.work_id;
  const state = reviewWorkState(work.last_candidate_status, work.blocked);
  const row = el("button", {
    class: `review-work-row ${active ? "is-active" : ""}`,
    attrs: {
      type: "button",
      "data-work-id": work.work_id,
      "data-blocked": work.blocked ? "true" : "false",
      "data-rerun": work.rerun_registered ? "true" : "false",
      "data-candidate-status": work.last_candidate_status ?? "none",
      title: `generation ${work.generation}`,
    },
    children: [
      el("div", {
        class: "review-work-top",
        children: [
          el("span", { class: "review-work-id", text: `工作 ${shortId(work.work_id)}` }),
          reviewBadge(state.label, state.tone),
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
          el("span", { class: "review-empty-symbol", text: "✓", attrs: { "aria-hidden": "true" } }),
          el("h2", { text: review.tab === "managed" && !review.managed.length ? (document.documentElement.lang === "en" ? "Nothing to review right now" : "暂时没有待审查事项") : uiText("选择左侧工作项") }),
          el("p", {
            class: "muted",
            text: document.documentElement.lang === "en" ? "Select an item to inspect its context and proposed changes." : "有待审查事项时，可在这里查看上下文和具体改动。",
          }),
        ],
      }),
    );
  } else if (review.detailLoading) {
    children.push(reviewSkeleton("正在读取候选详情…", 4));
  } else if (review.detailError) {
    const retry = el("button", {
      class: "quiet-button",
      text: uiText("重新读取候选"),
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
          el("h2", { text: uiText("旧工作项只读") }),
          el("p", {
            class: "muted",
            text: uiText("该工作项没有审查控制行（collab_work_control），因此没有候选投影，也不能在本窗口开始审查或批准。"),
          }),
          el("dl", {
            class: "kv",
            children: [
              kv(uiText("工作"), work.work_id),
              kv(uiText("事件"), work.event_id),
              kv(uiText("类型"), work.kind),
              kv(uiText("状态"), work.status),
              kv(uiText("创建"), work.created_at),
            ],
          }),
          el("p", {
            class: "muted",
            text: uiText("缺失的候选/差异字段属于正式能力缺口；本窗口不读数据库补齐。"),
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
          el("p", { class: "eyebrow", text: uiText("审查工作") }),
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
      text: uiText("重新读取候选"),
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
          el("h2", { text: uiText("没有候选") }),
          el("p", {
            class: "muted",
            text: uiText("这个工作项还没有候选。开始审查会调用 C07 生成候选或 WAIT；它不会直接提交 Belief。"),
          }),
        ],
      }),
    );
  } else {
    children.push(
      el("div", {
        class: "review-candidate-head",
        children: [
          el("h2", { text: uiText("候选") }),
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
      text: uiText("开始审查会把工作上下文发送到上方目的地，只生成候选或 WAIT，不提交 Belief。"),
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
        text: uiText("外部目的地：首次或目的地变化后，开始前会再次确认本次发送意图。"),
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
    text: uiText("← 返回候选"),
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
          el("p", { class: "eyebrow", text: uiText("候选差异") }),
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
          el("h2", { text: uiText("候选内容不可读") }),
          el("p", {
            class: "muted",
            text: uiText("候选受隐私或删除屏障保护；本窗口不显示正文，也不提供批准或拒绝。"),
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
          el("h2", { text: uiText("没有候选投影") }),
          el("p", {
            class: "muted",
            text: uiText("宿主没有返回 proposal；本窗口不推断、不读取数据库补字段。"),
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
      kv(uiText("操作"), operationLabel(proposal.operation)),
      kv(uiText("目标"), sourceRefLabel(proposal.target_ref)),
      kv(uiText("撤回理由"), proposal.strength_reason ?? "—"),
      kv(uiText("证据来源"), `${proposal.evidence_links.length} 项`),
    );
  } else {
    rows.push(
      kv(uiText("操作"), operationLabel(proposal.operation)),
      ...(proposal.target_ref
        ? [kv(uiText("目标"), sourceRefLabel(proposal.target_ref))]
        : []),
      kv(uiText("主张"), proposal.claim ?? "—"),
      kv(uiText("类型"), proposal.claim_type ?? "—"),
      kv(uiText("范围 / 适用性"), formatReviewValue("scope", proposal.scope ?? {})),
      kv(uiText("强度"), proposal.strength ?? "—"),
      kv(uiText("有效期"), formatReviewValue("valid_until", proposal.valid_until)),
      kv(uiText("证据来源"), `${proposal.evidence_links.length} 项`),
    );
    if (proposal.strength_reason) {
      rows.push(kv(uiText("强度理由"), proposal.strength_reason));
    }
  }
  return el("section", {
    class: "review-card",
    children: [
      el("h2", { text: uiText("候选内容") }),
      el("dl", { class: "kv", children: rows }),
      el("p", {
        class: "muted",
        text: uiText("只显示正式投影的字段；不显示 Prompt、HTTP 正文、候选原始 body 或内部 revision。"),
      }),
    ],
  });
}

function reviewCurrentBlock(detail: ReviewCandidateDetail): HTMLElement {
  if (detail.current_state === "not_applicable") {
    return el("section", {
      class: "review-card",
      children: [
        el("h2", { text: uiText("当前版本") }),
        el("p", {
          class: "muted",
          text: uiText("新增：没有可比较的当前 Belief。差异表中的“当前”为空，而不是空 Belief。"),
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
        el("h2", { text: uiText("当前版本") }),
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
      el("h2", { text: uiText("当前版本") }),
      el("dl", {
        class: "kv",
        children: [
          kv(uiText("引用"), sourceRefLabel(current.ref)),
          kv(uiText("主张"), current.claim),
          kv(uiText("类型"), current.claim_type),
          kv(uiText("状态"), current.status),
          kv(uiText("强度"), current.strength),
          kv(uiText("范围 / 适用性"), current.applicability),
          kv(uiText("有效期"), current.valid_until ?? "—"),
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
        el("h2", { text: uiText("字段差异") }),
        el("p", {
          class: "muted",
          text: uiText("当前版本不可读或不存在，因此不生成字段差异；这不是“全部变化”。"),
        }),
      ],
    });
  }
  const changedRows = el("div", { class: "review-diff" });
  if (detail.proposal?.operation === "retract_belief") {
    changedRows.appendChild(
      el("p", {
        class: "muted",
        text: uiText("撤回只比较证据来源；Belief 正文、类型、范围与强度不参与比较。"),
      }),
    );
  }
  changedRows.appendChild(
    el("div", {
      class: "review-diff-head",
      children: [
        el("span", { text: uiText("字段") }),
        el("span", {
          text:
            detail.current_state === "not_applicable" ? "当前（新增）" : "当前版本",
        }),
        el("span", { text: uiText("候选版本") }),
        el("span", { text: uiText("变化") }),
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
      el("p", { class: "muted", text: uiText("没有变化字段。") }),
    );
  }
  const block = el("section", {
    class: "review-card",
    children: [
      el("div", {
        class: "review-card-head",
        children: [
          el("h2", { text: uiText("字段差异") }),
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
          el("span", { text: uiText("字段") }),
          el("span", { text: uiText("当前版本") }),
          el("span", { text: uiText("候选版本") }),
          el("span", { text: uiText("变化") }),
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
          el("h2", { text: uiText("来源") }),
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
            text: uiText("来源引用默认折叠；展开查看可读性与精确引用"),
          }),
          detail.sources.length === 0
            ? el("p", { class: "muted", text: uiText("本次没有可披露的来源引用。") })
            : el("div", { class: "review-source-list", children: rows }),
          el("p", {
            class: "muted",
            text: uiText("来源正文只在正式可读时出现在来源抽屉；隐藏来源不显示正文，也不以空值替代。"),
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
  if (review.screen === "works" && !review.selectedWork && !review.selectedLegacy) return el("div", { class: "hidden" });
  const left = el("div", { class: "review-action-left" });
  const right = el("div", { class: "review-action-right" });
  const refresh = el("button", {
    class: "quiet-button",
    text: uiText("刷新"),
    attrs: { type: "button", id: "review-action-refresh" },
  });
  refresh.addEventListener("click", () => void refreshReviewData());
  const back = el("button", {
    class: "quiet-button",
    text: uiText("← 返回工作列表"),
    attrs: { type: "button", id: "review-back-works" },
  });
  back.addEventListener("click", () => backToReviewWorks());

  if (review.screen === "detail") {
    left.append(back, refresh);
    if (review.detail) {
      const sources = el("button", {
        class: "quiet-button",
        text: uiText("来源与详情"),
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
      text: uiText("拒绝…"),
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
      text: uiText("← 返回已暂停的审查"),
      attrs: { type: "button", id: "review-back-paused" },
    });
    backPaused.addEventListener("click", () => applyReview({ type: "reauth-close" }));
    left.appendChild(backPaused);
  } else if (review.screen === "result") {
    left.append(back, refresh);
    const sources = el("button", {
      class: "quiet-button",
      text: uiText("来源与详情"),
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
        text: uiText("已提交只由 C07 ReviewView 的权威回执证明；模型文本不算回执。"),
      }),
    );
  } else if (view.state === "waiting") {
    children.push(reviewReceiptBlock(view));
    children.push(
      el("p", {
        class: "muted",
        text: uiText("WAIT 是正式结果：工作项已完成，没有候选，也不能再次批准。"),
      }),
    );
  } else if (view.state === "rejected") {
    children.push(
      el("dl", {
        class: "kv",
        children: [
          kv(uiText("旧候选"), shortId(view.candidate_id)),
          kv(uiText("状态"), "已拒绝（只读）"),
          kv(uiText("工作"), review.selectedWork ? "已暂停" : "见工作列表"),
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
      text: uiText("重新授权复查"),
      attrs: { type: "button", id: "review-reauth" },
    });
    reauth.addEventListener("click", () => applyReview({ type: "reauth-open" }));
    children.push(reauth);
    children.push(
      el("p", {
        class: "muted",
        text: uiText("本窗口不发放许可；旧候选保持已拒绝，不能复活。"),
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
      text: uiText("刷新工作列表"),
      attrs: { type: "button" },
    });
    refresh.addEventListener("click", () => void refreshReviewData());
    children.push(refresh);
    if (generateEnabled(review, info)) {
      const retry = el("button", {
        class: "primary-button",
        text: uiText("重新开始审查（新候选）"),
        attrs: { type: "button", id: "review-retry" },
      });
      retry.addEventListener("click", () => openReviewGenerate());
      children.push(retry);
    }
    children.push(
      el("p", {
        class: "muted",
        text: uiText("这不是自动重试：只有你再次操作才会生成新的候选；旧候选不会复活。"),
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
        text: uiText("denied 表示审查入口关闭，不是权限不足；没有生成候选。"),
      }),
    );
  } else if (view.state === "incomplete") {
    const id = recoveryInteractionId(review);
    if (id && reviewCaps(info).result) {
      const recover = el("button", {
        class: "quiet-button",
        text: uiText("刷新结果"),
        attrs: { type: "button", id: "review-recover-result" },
      });
      recover.addEventListener("click", () => void performReviewResult(id));
      children.push(recover);
    }
    children.push(
      el("p", {
        class: "muted",
        text: uiText("结论不完整既不是拒绝也不是提交；没有自动重试。"),
      }),
    );
  } else if (view.state === "pending") {
    const candidate = review.candidates.find(
      (item2) => item2.candidate_id === view.candidate_id,
    );
    if (candidate) {
      const open = el("button", {
        class: "quiet-button",
        text: uiText("查看候选"),
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
        el("h3", { text: uiText("回执") }),
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
      el("h3", { text: uiText("回执") }),
      el("dl", {
        class: "kv",
        children: [
          kv(uiText("请求"), shortId(receipt.request_id)),
          kv(uiText("上下文"), shortId(receipt.context_id)),
          kv(uiText("操作"), receiptOperationLabel(receipt.operation)),
          kv(uiText("结果"), receipt.outcome),
          kv(uiText("时间"), receipt.committed_at),
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
        text: uiText("界面不显示 state_revision、proposal_digest、command_digest 或内部授权引用。"),
      }),
    ],
  });
}

function reviewTechnical(detail: string | null): HTMLElement {
  return el("details", {
    class: "technical",
    children: [
      el("summary", { text: uiText("技术详情") }),
      el("code", { text: detail ?? "无阶段标签" }),
      el("p", {
        class: "muted",
        text: uiText("只显示阶段/原因标签；不包含 Prompt、HTTP 正文、模型原始输出或数据库列。"),
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
            el("h3", { text: uiText("旧候选") }),
            el("p", {
              class: "muted",
              text: `${shortId(oldCandidate)} · 已拒绝（只读）`,
            }),
            el("p", {
              class: "muted",
              text: uiText("旧候选不能复活，也不能再次批准。"),
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
            el("h3", { text: uiText("新的复查许可") }),
            el("p", {
              class: "muted",
              text: rerun
                ? "已登记（开始审查时由 C07/C05d 校验有效性）。"
                : "未登记；需要宿主或负责人发放新许可。本窗口不显示许可令牌。",
            }),
            (() => {
              const grant = el("button", {
                class: "quiet-button",
                text: uiText("申请新的复查许可（当前不可用）"),
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
            el("h3", { text: uiText("开始新的审查") }),
            el("p", {
              class: "muted",
              text: uiText("新审查会生成新的审查轮次和候选；旧候选仍保持已拒绝。"),
            }),
          ],
        }),
      ],
    }),
  ];
  const reason = generateBlockReason(review, info);
  const start = el("button", {
    class: "primary-button",
    text: uiText("开始新的审查（新候选）"),
    attrs: { type: "button", id: "review-reauth-start" },
  });
  start.disabled = reason !== null;
  start.title = reason ?? "";
  start.addEventListener("click", () => openReviewGenerate());
  const refresh = el("button", {
    class: "quiet-button",
    text: uiText("刷新工作列表"),
    attrs: { type: "button" },
  });
  refresh.addEventListener("click", () => void refreshReviewData());
  const back = el("button", {
    class: "quiet-button",
    text: uiText("返回已暂停的审查"),
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
    text: uiText("确认并开始审查"),
    attrs: { type: "button", id: "review-confirm-generate" },
  });
  confirm.disabled = review.busy !== null;
  confirm.addEventListener("click", () => void confirmReviewGenerate());
  const cancel = el("button", {
    class: "quiet-button",
    text: uiText("返回检查"),
    attrs: { type: "button", id: "review-cancel-generate" },
  });
  cancel.addEventListener("click", () => closeReviewModal());
  shell.reviewModal.append(
    el("div", {
      class: "review-modal-head",
      children: [
        el("h2", {
          text: uiText("开始审查？"),
          attrs: { id: "review-modal-title" },
        }),
        el("span", { class: "eyebrow", text: uiText("D3 · 外部发送确认") }),
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
          text: uiText("只生成候选或 WAIT，不提交 Belief。生成后仍需要单独批准；批准提交不会再次发送给模型。"),
        }),
        el("p", {
          class: "muted",
          text: uiText("确认只表达本次用户意图，不是授权凭据。"),
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
      kv(uiText("操作"), operationLabel(proposal.operation)),
      kv(uiText("主张"), proposal.claim ?? "—"),
      kv(uiText("类型"), proposal.claim_type ?? "—"),
      kv(uiText("范围"), formatReviewValue("scope", proposal.scope ?? {})),
      kv(uiText("强度"), proposal.strength ?? "—"),
      kv(uiText("来源"), `${proposal.evidence_links.length} 项（详情在审查记录中）`),
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
        el("span", { class: "review-badge is-warn", text: uiText("二次确认") }),
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
          text: uiText("提交前会重新核对 exact candidate/version/Work/current；如果已变化，本窗口不会自动重试。"),
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
  const chatMode = conversationEnabled();
  const blocked =
    creatingConversation ||
    state.phase === "sending" ||
    state.phase === "booting" ||
    state.phase === "not-ready" ||
    !state.host?.host.ready ||
    (chatMode && (chat.sending || chat.busy));
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
        : "今天想聊些什么？";
  shell.textarea.placeholder = uiText(placeholder);
  shell.target.textContent = destinationLabel(state.destination);
  shell.target.classList.toggle("is-not-ready", !state.destination);
  shell.counter.textContent = `${countCharacters(state.question)} / ${maxQuestionChars(state)}`;
  shell.hint.textContent = uiText(
    state.phase === "sending"
      ? "正在等待权威结果；本次无法取消或重发"
      : state.phase === "not-ready" || !state.host?.host.ready
        ? "宿主未就绪；发送已禁用"
        : state.result
          ? "发送新问题会替换当前结果"
          : "Enter 发送 · Shift+Enter 换行");
  const showCancel =
    chatMode && chat.pendingTurnId !== null && processEnabled();
  shell.cancelButton.classList.toggle("hidden", !showCancel);
  shell.cancelButton.disabled = !showCancel || !canCancel(chat);
  shell.cancelButton.setAttribute("aria-label", uiText(chat.cancelRequested ? "停止中…" : "停止"));
  shell.cancelButton.title = uiText(chat.cancelRequested ? "停止中…" : "停止");
  shell.sendButton.classList.toggle("hidden", showCancel);
  shell.sendButton.setAttribute("aria-label", uiText("发送"));
  shell.sendButton.title = uiText("发送");
  const sending = chatMode
    ? chat.sending || chat.pendingTurnId !== null
    : state.phase === "sending";
  const label = shell.sendButton.querySelector(".send-label");
  if (label) {
    label.textContent = uiText(sending ? "处理中" : "发送");
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
    text: uiText("返回检查"),
    attrs: { type: "button", id: "confirm-cancel" },
  });
  const confirm = el("button", {
    class: "confirm-button",
    text: uiText("确认发送"),
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
            el("strong", { text: uiText("本次将发送到外部模型") }),
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
  if (event.key === "Enter" && !event.shiftKey) {
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
  requestedScroll = true;
  followConversation = true;
  if (!sendEnabled(state)) {
    if (!state.host?.host.ready) {
      state = { ...state, notice: "宿主未就绪，发送已禁用。" };
      render();
    }
    shell.textarea.focus({ preventScroll: true });
    return;
  }
  if (needsConfirmation(state)) {
    pendingChatSend = conversationEnabled();
    if (conversationEnabled()) {
      apply({ type: "confirm-open" });
    } else {
      apply({ type: "send-request" });
    }
    shell.banner.querySelector<HTMLButtonElement>("#confirm-send")?.focus();
    return;
  }
  if (conversationEnabled()) {
    await sendChat();
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
  if (conversationEnabled()) {
    apply({ type: "destination-confirmed", destinationId: destination.destination_id });
    pendingChatSend = false;
    await sendChat();
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

function conversationEnabled(): boolean {
  return state.host?.workspace.conversation === true;
}

function stopChatPolling(): void {
  if (chatPollTimer !== null) {
    window.clearTimeout(chatPollTimer);
    chatPollTimer = null;
  }
}

async function ensureChatLoaded(): Promise<void> {
  if (!conversationEnabled() || chat.status === "loading") {
    return;
  }
  if (chat.status === "ready" && chat.conversations.length > 0) {
    return;
  }
  chat = chatReduce(chat, { type: "loading" });
  render();
  const reply = await conversationList();
  if (!reply.ok) {
    const error = replyError(reply);
    chat = chatReduce(chat, {
      type: "error",
      code: error.code,
      message: error.message,
    });
    render();
    return;
  }
  const page = reply.result as { conversations: ConversationSummary[] };
  chat = chatReduce(chat, {
    type: "conversations",
    conversations: page.conversations,
  });
  render();
  if (chat.activeId === null) {
    // Auto-open the newest conversation that is not archived in the list.
    const visible = visibleConversations(
      mergeDisplayTitles(page.conversations, displayPreferences),
      displayPreferences,
    );
    if (visible.length > 0) {
      await openConversation(visible[0].conversation_id);
    }
  }
}

async function openConversation(conversationId: string): Promise<void> {
  if (!conversationEnabled() || !canSwitchConversation(chat)) {
    return;
  }
  const switching = chat.activeId !== conversationId;
  const savedScroll = switching
    ? conversationScroll.get(conversationId)
    : undefined;
  if (switching) {
    // A response for the conversation we are leaving can never be applied.
    historyRequestToken += 1;
    pendingHistoryAnchor = null;
    if (chat.activeId) conversationDrafts.set(`${sessionId()}:${chat.activeId}`, state.question);
    state = { ...state, question: conversationDrafts.get(`${sessionId()}:${conversationId}`) ?? "" };
    chat = chatReduce(chat, { type: "select", conversationId });
    usageLoad = { available: usageAvailable(), loading: usageAvailable(), error: false, busy: false, data: null };
    render();
  }
  const reply = await conversationGet(conversationId);
  // Card C: a newer switch/load owns the screen now.  Never let an older reply
  // overwrite the newly selected conversation.
  if (chat.activeId !== conversationId) {
    return;
  }
  if (!reply.ok) {
    const error = replyError(reply);
    chat = chatReduce(chat, {
      type: "error",
      code: error.code,
      message: error.message,
    });
    render();
    return;
  }
  const page = reply.result as {
    turns: ConversationTurn[];
    messages: ChatMessage[];
    next_before: { turn_no: number; seq: number } | null;
  };
  // A same-conversation refresh (terminal poll) keeps the current thread on
  // screen until the authoritative page arrives; clearing it first would clamp
  // the scroll container to the top while the user is reading history.
  chat = chatReduce(chat, {
    type: "conversation",
    conversationId,
    turns: page.turns,
    messages: page.messages,
    nextBefore: page.next_before,
  });
  if (savedScroll !== undefined) {
    // Restore the old reading position only now that the thread has height;
    // applying it on the empty intermediate view would clamp it to the top.
    pendingScrollRestore = savedScroll;
  }
  render();
  void loadPostprocess(conversationId);
  void loadUsage(conversationId);
}

/**
 * Card C2: load one older page for the active conversation.
 *
 * The control only exists while `nextBefore` is non-null.  A failed read keeps
 * the cursor and shows an explicit retry; a cursor that moved while the request
 * was in flight is dropped instead of merging a page that may leave a gap.
 */
async function loadOlderHistory(): Promise<void> {
  if (!conversationEnabled() || chat.loadingOlder) {
    return;
  }
  const conversationId = chat.activeId;
  const cursor = chat.nextBefore;
  if (conversationId === null || cursor === null) {
    return;
  }
  const token = ++historyRequestToken;
  const anchor = {
    top: shell.main.scrollTop,
    height: shell.main.scrollHeight,
  };
  chat = chatReduce(chat, { type: "older-start", conversationId });
  render();
  let reply: HostReply;
  try {
    reply = await conversationGet(conversationId, {
      beforeTurnNo: cursor.turn_no,
      beforeSeq: cursor.seq,
    });
  } catch (failure) {
    if (token !== historyRequestToken || chat.activeId !== conversationId) {
      return;
    }
    const error = invokeError(failure);
    chat = chatReduce(chat, {
      type: "older-failed",
      conversationId,
      code: error.code,
      message: error.message,
    });
    pendingHistoryAnchor = anchor;
    suppressNewActivity = true;
    render();
    return;
  }
  if (token !== historyRequestToken || chat.activeId !== conversationId) {
    return;
  }
  if (!reply.ok) {
    const error = replyError(reply);
    chat = chatReduce(chat, {
      type: "older-failed",
      conversationId,
      code: error.code,
      message: error.message,
    });
    pendingHistoryAnchor = anchor;
    suppressNewActivity = true;
    render();
    return;
  }
  const page = reply.result as {
    turns: ConversationTurn[];
    messages: ChatMessage[];
    next_before: { turn_no: number; seq: number } | null;
  };
  const current = chat.nextBefore;
  if (
    current === null ||
    current.turn_no !== cursor.turn_no ||
    current.seq !== cursor.seq
  ) {
    // A same-conversation reload replaced the initial window while this page
    // was in flight.  Merging it could skip messages between the windows.
    chat = chatReduce(chat, { type: "older-cancelled", conversationId });
    render();
    return;
  }
  chat = chatReduce(chat, {
    type: "older-page",
    conversationId,
    turns: page.turns,
    messages: page.messages,
    nextBefore: page.next_before,
  });
  pendingHistoryAnchor = anchor;
  suppressNewActivity = true;
  render();
}

async function refreshConversationList(): Promise<void> {
  const session = sessionId();
  const reply = await conversationList();
  if (reply.ok && session === sessionId()) {
    chat = chatReduce(chat, { type: "conversations", conversations: (reply.result as { conversations: ConversationSummary[] }).conversations });
    render();
  }
}

function terminalTurn(turn: ConversationTurn): boolean {
  return !["accepted", "running"].includes(turn.state);
}

function stopPostprocessPoll(): void {
  if (postprocessPollTimer !== null) {
    window.clearTimeout(postprocessPollTimer);
    postprocessPollTimer = null;
  }
}

function schedulePostprocessPoll(conversationId: string): void {
  if (postprocessPollTimer !== null) {
    return;
  }
  if (postprocessPollAttempts >= POSTPROCESS_POLL_LIMIT) {
    return;
  }
  postprocessPollAttempts += 1;
  postprocessPollTimer = window.setTimeout(() => {
    postprocessPollTimer = null;
    if (chat.activeId !== conversationId) {
      return;
    }
    void loadPostprocess(conversationId, true);
  }, POSTPROCESS_POLL_INTERVAL_MS);
}

async function loadPostprocess(
  conversationId: string,
  poll = false,
): Promise<void> {
  if (!poll) {
    stopPostprocessPoll();
    postprocessPollAttempts = 0;
  }
  if (postprocessLoading) {
    return;
  }
  const turnIds = chat.turns
    .filter(terminalTurn)
    .slice(-20)
    .map((turn) => turn.turn_id);
  if (turnIds.length === 0) {
    stopPostprocessPoll();
    return;
  }
  postprocessLoading = true;
  try {
    const reply = await postprocessStatus(conversationId, turnIds);
    if (!reply.ok || chat.activeId !== conversationId) {
      stopPostprocessPoll();
      return;
    }
    const page = reply.result as { turns: PostprocessTurnStatus[] };
    chat = chatReduce(chat, { type: "postprocess", turns: page.turns });
    render();
    if (hasPendingMemory(page.turns)) {
      schedulePostprocessPoll(conversationId);
    } else {
      stopPostprocessPoll();
    }
  } finally {
    postprocessLoading = false;
  }
}

async function retryTurnPostprocess(turnId: string): Promise<void> {
  const reply = await postprocessRetry(turnId);
  if (!reply.ok) {
    showToast(reply.error?.message ?? "记忆交付重试失败");
    return;
  }
  if (chat.activeId) {
    await loadPostprocess(chat.activeId);
  }
}

let transcriptTrigger: HTMLElement | null = null;
let transcriptRenderKey = "";
let transcriptClosing: Animation | null = null;
function openTranscriptSearch(): void {
  transcriptTrigger = document.activeElement as HTMLElement | null;
  transcriptSearch = {
    open: true,
    query: transcriptSearch.query,
    loading: false,
    error: null,
    page: null,
    memoryLoading: false,
    memoryError: null,
    memoryPage: null,
  };
  render();
  window.setTimeout(() => {
    const input = document.querySelector(
      ".transcript-input",
    ) as HTMLInputElement | null;
    input?.focus();
  }, 0);
  // Opening the panel with no keyword shows real recent memory immediately.
  if (!transcriptSearch.query.trim() && memorySearchAvailability(state.host).available) {
    void runMemorySearch(false);
  }
}

function closeTranscriptSearch(): void {
  transcriptSearch = {
    open: false,
    query: transcriptSearch.query,
    loading: false,
    error: null,
    page: null,
    memoryLoading: false,
    memoryError: null,
    memoryPage: null,
  };
  render();
  if (transcriptTrigger?.isConnected) transcriptTrigger.focus();
  else shell.textarea.focus();
}

async function runTranscriptSearch(append: boolean = false): Promise<void> {
  const query = transcriptSearch.query.trim();
  if (!query || transcriptSearch.loading) {
    return;
  }
  const before = append ? transcriptSearch.page?.next_cursor ?? null : null;
  if (append && !before) {
    return;
  }
  transcriptSearch.loading = true;
  transcriptSearch.error = null;
  render();
  const reply = await searchTranscript(query, before);
  transcriptSearch.loading = false;
  if (!reply.ok) {
    transcriptSearch.error = reply.error?.message ?? "旧对话检索失败";
    render();
    return;
  }
  const page = reply.result as TranscriptSearchPage;
  if (append && transcriptSearch.page) {
    transcriptSearch.page = {
      ...page,
      items: [...transcriptSearch.page.items, ...page.items],
    };
  } else {
    transcriptSearch.page = page;
  }
  render();
}

function memorySearchErrorMessage(error: BridgeError | undefined): string {
  switch (error?.code) {
    case "MEMORY_FORBIDDEN":
      return "此实例不允许记忆检索。";
    case "MEMORY_UNAVAILABLE":
      return "记忆存储当前不可用。";
    case "MEMORY_BRIDGE_FORBIDDEN":
      return "此实例未配置可用的记忆桥接。";
    case "MEMORY_CURSOR_STALE":
      return "记忆检索游标已过期，请重新检索。";
    case "HOST_NOT_READY":
      return "宿主尚未就绪，暂不能读取记忆。";
    default:
      return error?.message ?? "记忆检索失败";
  }
}

async function runMemorySearch(append: boolean = false): Promise<void> {
  const availability = memorySearchAvailability(state.host);
  if (!availability.available || transcriptSearch.memoryLoading) {
    return;
  }
  const cursor = append ? transcriptSearch.memoryPage?.next_cursor ?? null : null;
  if (append && !cursor) {
    return;
  }
  const query = transcriptSearch.query.trim();
  transcriptSearch.memoryLoading = true;
  transcriptSearch.memoryError = null;
  if (!append) transcriptSearch.memoryPage = null;
  render();
  const reply = await searchMemory(query, cursor);
  transcriptSearch.memoryLoading = false;
  if (!reply.ok) {
    transcriptSearch.memoryError = memorySearchErrorMessage(reply.error);
    render();
    return;
  }
  try {
    const page = normalizeMemoryPage(reply.result);
    transcriptSearch.memoryPage = mergeMemoryPages(
      append ? transcriptSearch.memoryPage : null,
      page,
    );
  } catch {
    transcriptSearch.memoryError = "记忆检索返回了无法识别的结果。";
  }
  render();
}

/** One submit runs both sources; an empty keyword browses memory only. */
function submitKnowledgeSearch(): void {
  const parsed = searchQueries(transcriptSearch.query);
  if (parsed.transcript === null) {
    transcriptSearch.page = null;
    transcriptSearch.error = null;
  }
  void runMemorySearch(false);
  if (parsed.transcript !== null) {
    void runTranscriptSearch(false);
  }
  render();
}

async function openTranscriptResult(item: TranscriptSearchItem): Promise<void> {
  if (!item.conversation_id) {
    return;
  }
  transcriptSearch.open = false;
  await openConversation(item.conversation_id);
}

async function createConversation(): Promise<void> {
  if (!conversationEnabled() || !canSwitchConversation(chat) || creatingConversation) return;
  creatingConversation = true;
  const priorDraft = state.question;
  if (chat.activeId) conversationDrafts.set(`${sessionId()}:${chat.activeId}`, priorDraft);
  state = { ...state, question: "" };
  render();
  try {
    const reply = await conversationCreate("chat");
    if (!reply.ok) {
      const error = replyError(reply);
      state = { ...state, question: priorDraft };
      chat = chatReduce(chat, { type: "error", code: error.code, message: error.message });
      return;
    }
    const conversation = (reply.result as { conversation: { conversation_id: string } }).conversation;
    chat = chatReduce(chat, { type: "new-conversation", conversationId: conversation.conversation_id });
    render();
    await refreshConversationList();
    await openConversation(conversation.conversation_id);
  } catch (error) {
    state = { ...state, question: priorDraft };
    chat = chatReduce(chat, { type: "error", code: "HOST_UNAVAILABLE", message: invokeError(error).message });
  } finally { creatingConversation = false; render(); }
}

async function sendChat(): Promise<void> {
  if (!conversationEnabled() || chat.sending || chat.busy) {
    return;
  }
  if (!state.question.trim()) {
    return;
  }
  if (chat.activeId === null) {
    const firstQuestion = state.question;
    await createConversation();
    state = { ...state, question: firstQuestion };
    if (chat.activeId === null) {
      return;
    }
  }
  // A new send supersedes the idle memory refresh; its final reopens status.
  stopPostprocessPoll();
  const session = state.host?.session_id ?? "";
  const destinationId = state.destination?.destination_id ?? null;
  const confirmed =
    destinationId !== null && state.confirmedDestinationId === destinationId
      ? destinationId
      : null;
  const requestId = requestIdFor(chat, state.question);
  const conversationId = chat.activeId as string;
  const text = state.question;
  chat = chatReduce(chat, {
    type: "send-start",
    requestId,
    text,
  });
  // The controlled composer clears immediately; the bubble below is a local
  // pending projection until the host returns a formal turn/message.
  apply({ type: "draft", question: "" });
  render();
  let reply: HostReply;
  try {
    reply = await conversationSend({
      sessionId: session,
      conversationId,
      clientRequestId: requestId,
      text,
      confirmedDestinationId: confirmed,
    });
  } catch (failure) {
    // Card C: a lost IPC reply may still hide a committed T1.  Keep the text
    // and request id so the retry is an idempotent replay, not a new turn.
    const error = invokeError(failure);
    chat = chatReduce(chat, {
      type: "send-failed",
      code: error.code,
      message: error.message,
    });
    apply({ type: "draft", question: text });
    render();
    return;
  }
  if (!reply.ok) {
    const error = replyError(reply);
    chat = chatReduce(chat, {
      type: "send-failed",
      code: error.code,
      message: error.message,
    });
    // A refused T1 is not persisted; the local projection is retired but the
    // request id and text stay for an idempotent retry of the same draft.
    apply({ type: "draft", question: text });
    render();
    return;
  }
  const accepted = reply.result as {
    turn_id: string;
    turn_no: number;
    state: TurnState;
    reused: boolean;
  };
  chat = chatReduce(
    chat,
    accepted.reused
      ? {
          type: "send-reused",
          turnId: accepted.turn_id,
          turnNo: accepted.turn_no,
          state: accepted.state,
        }
      : {
          type: "send-accepted",
          turnId: accepted.turn_id,
          turnNo: accepted.turn_no,
          state: accepted.state,
        },
  );
  // Only a committed T1 clears the composer; a refusal keeps the draft.
  apply({ type: "draft", question: "" });
  startChatPolling(accepted.turn_id);
}

function processEnabled(): boolean {
  return Boolean(state.host?.capabilities?.process);
}

async function pollTurnEvents(turnId: string): Promise<boolean> {
  if (!processEnabled() || chat.eventsTurnId !== turnId) {
    return false;
  }
  const reply = await turnEvents(turnId, chat.eventCursor);
  if (!reply.ok) {
    return false;
  }
  const page = reply.result as TurnEventPage;
  chat = chatReduce(chat, {
    type: "events",
    turnId,
    events: page.events,
    nextSeq: page.next_seq,
    terminal: page.terminal,
  });
  return page.terminal;
}

function startChatPolling(turnId: string): void {
  stopChatPolling();
  const tick = async (): Promise<void> => {
    try {
      await pollTurnEvents(turnId);
      const ping = await bridgePing();
      if (!ping.ok) {
        chatPollTimer = window.setTimeout(tick, 600);
        return;
      }
      const busy = Boolean((ping.result as { busy?: boolean }).busy);
      chat = chatReduce(chat, { type: "busy", busy });
      if (busy || !canReadTurnResult(chat)) {
        render();
        chatPollTimer = window.setTimeout(tick, 400);
        return;
      }
      const reply = await turnResult(turnId);
      if (!reply.ok) {
        const error = replyError(reply);
        if (error.code === "BUSY") {
          chat = chatReduce(chat, { type: "turn-busy" });
          render();
          chatPollTimer = window.setTimeout(tick, 400);
          return;
        }
        chat = chatReduce(chat, {
          type: "error",
          code: error.code,
          message: error.message,
        });
        render();
        chatPollTimer = null;
        return;
      }
      const result = reply.result as TurnResult;
      chat = chatReduce(chat, { type: "turn-result", result });
      render();
      if (!result.terminal) {
        chatPollTimer = window.setTimeout(tick, 400);
        return;
      }
      await pollTurnEvents(turnId);
      const keptEvents = chat.events;
      const keptCursor = chat.eventCursor;
      chatPollTimer = null;
      await openConversation(result.conversation_id);
      await refreshConversationList();
      if (keptEvents.length > 0) {
        chat = chatReduce(chat, {
          type: "events",
          turnId,
          events: keptEvents,
          nextSeq: keptCursor,
          terminal: true,
        });
        render();
      }
    } catch (failure) {
      // Card C: a thrown IPC call must not leave the composer disabled and the
      // conversation locked behind a permanent pending turn.  Report it and
      // keep polling; the authoritative turn result resolves it.
      const error = invokeError(failure);
      chat = chatReduce(chat, {
        type: "error",
        code: error.code,
        message: error.message,
      });
      render();
      chatPollTimer = window.setTimeout(tick, 600);
    }
  };
  chatPollTimer = window.setTimeout(tick, 0);
}

async function cancelChatTurn(): Promise<void> {
  const turnId = chat.pendingTurnId;
  if (turnId === null || !canCancel(chat)) {
    return;
  }
  const reply = await turnCancel(turnId);
  if (!reply.ok) {
    const error = replyError(reply);
    chat = chatReduce(chat, {
      type: "cancel-refused",
      code: error.code,
      message: error.message,
    });
    render();
    return;
  }
  chat = chatReduce(chat, { type: "cancel-requested" });
  render();
}

function uiAssistantName(): string {
  const name = aiDisplayName(state.host);
  return document.documentElement.lang === "en" && name === "助手" && state.host?.ui?.source !== "preference" ? "Assistant" : name;
}
function chatBubble(message: ChatMessage, bodyOverride?: string): HTMLElement {
  const assistant = message.kind !== "user";
  const body = message.redacted
    ? "（这段内容已因删除或权限变化不可用）"
    : (bodyOverride ?? message.text ?? "");
  if (message.kind === "commentary") return el("div", { class: "chat-commentary", text: body });
  return el("div", {
    class: `chat-message ${assistant ? "is-assistant" : "is-user"}${
      message.redacted ? " is-redacted" : ""
    }`,
    children: [
      el("div", { class: "chat-role", text: assistant ? uiAssistantName() : uiText("你") }),
      el("div", { class: "chat-text", text: body }),
    ],
  });
}

/** Play the one-shot reveal for a bubble that just appeared. */
function revealBubble(element: HTMLElement, key: string, index: number): void {
  if (!bubbleReveal.claim(key)) {
    return;
  }
  element.classList.add(BUBBLE_REVEAL_CLASS);
  element.style.animationDelay = `${bubbleDelayMs(index)}ms`;
}

/**
 * RG08: render one final/wait message as its explicitly ordered bubbles.
 * A legacy single message is exactly one bubble and keeps today's layout.
 */
function answerBubbles(message: ChatMessage, animate: boolean): HTMLElement[] {
  if (message.redacted) {
    return [chatBubble(message)];
  }
  const parts = replyBubbles(message);
  if (parts.length === 0) {
    return [chatBubble(message)];
  }
  return parts.map((text, index) => {
    const bubble = chatBubble(message, text);
    if (parts.length > 1) {
      bubble.classList.add("is-bubble-split");
      if (index > 0) {
        bubble.classList.add("is-bubble-continuation");
      }
    }
    bubble.setAttribute("data-bubble-index", String(index));
    if (animate) {
      revealBubble(bubble, bubbleRevealKey(message.message_id, index), index);
    }
    return bubble;
  });
}

/**
 * A local bubble for a submit that is not yet a persisted official message.
 *
 * Card C: it has exactly the same role/text structure as the official user
 * bubble (`chatBubble`), so replacing it with the host's message does not
 * change the height or move the content below it.  The send status lives in the
 * single thread status line, not inside the bubble.
 */
function pendingChatBubble(text: string): HTMLElement {
  return el("div", {
    class: "chat-message is-user is-pending",
    attrs: { "data-pending": "true" },
    children: [
      el("div", { class: "chat-role", text: uiText("你") }),
      el("div", { class: "chat-text", text }),
    ],
  });
}

interface TurnEventsEntry {
  events: ProcessEvent[];
  nextSeq: number;
  terminal: boolean;
  truncated: boolean;
}

interface TurnStepsEntry {
  steps: StepView[];
  nextAfter: number | null;
  truncated: boolean;
}

interface ProcessError {
  code: string;
  message: string;
}

/**
 * Card D: one bounded cache per read, keyed by the globally unique turn id.
 *
 * A terminal turn is immutable, so switching conversations never invalidates
 * these entries.  The two reads are cached independently: a failed step read
 * must not discard an already loaded event timeline.
 */
const turnEventsCache = new Map<string, TurnEventsEntry>();
const turnEventsState = new Map<string, LoadState>();
const turnEventsError = new Map<string, ProcessError>();
const turnEventsRequested = new Set<string>();
const turnStepsCache = new Map<string, TurnStepsEntry>();
const turnStepsState = new Map<string, LoadState>();
const turnStepsError = new Map<string, ProcessError>();
const turnStepsRequested = new Set<string>();

/** A bounded insertion-order cache; the oldest turn is evicted first. */
function trimTurnCache<K, V>(
  cache: Map<K, V>,
  forget: (key: K) => void,
): void {
  while (cache.size > MAX_TURN_PROCESS_CACHE) {
    const oldest = cache.keys().next().value as K | undefined;
    if (oldest === undefined) {
      return;
    }
    cache.delete(oldest);
    forget(oldest);
  }
}

/** Never call render() synchronously from inside the render pass. */
function scheduleProcessRender(): void {
  window.setTimeout(() => render(), 0);
}

const traceCache = new Map<string, TurnTrace>();
const traceState = new Map<string, "loading" | "ready" | "busy" | "error">();
const traceRequested = new Set<string>();

let usageLoad: UsageLoad = NO_USAGE;

function usageAvailable(): boolean {
  return Boolean(state.host?.methods?.includes("conversation.usage"));
}

async function loadUsage(conversationId: string, turnId?: string): Promise<void> {
  if (!usageAvailable()) {
    return;
  }
  usageLoad = { available: true, loading: true, error: false, busy: false, data: null };
  render();
  const reply = await conversationUsage(conversationId, turnId ?? null);
  if (chat.activeId !== conversationId) {
    return;
  }
  if (!reply.ok) {
    const busy = reply.error?.code === "BUSY";
    usageLoad = { available: true, loading: false, error: !busy, busy, data: null };
    render();
    return;
  }
  usageLoad = { available: true, loading: false, error: false, busy: false, data: usageSummary(reply.result) };
  render();
}

async function loadTrace(turnId: string): Promise<void> {
  if (traceRequested.has(turnId)) {
    return;
  }
  traceRequested.add(turnId);
  traceState.set(turnId, "loading");
  const reply = await turnTrace(turnId);
  if (!reply.ok) {
    if (reply.error?.code === "BUSY") {
      traceState.set(turnId, "busy");
      traceRequested.delete(turnId);
    } else {
      traceState.set(turnId, "error");
    }
    render();
    return;
  }
  traceCache.set(turnId, reply.result as TurnTrace);
  traceState.set(turnId, "ready");
  render();
}

/**
 * Card D: read every persisted page of one old turn's public events.
 *
 * The live `chat.events` stream is only for the turn this client is currently
 * polling.  A replay therefore reads `turn.events` itself, caches the immutable
 * result and never writes it into the live poll state.
 */
async function ensureTurnEvents(turnId: string): Promise<void> {
  if (!processEnabled()) {
    turnEventsState.set(turnId, "unavailable");
    return;
  }
  const current = turnEventsState.get(turnId);
  if (
    current === "ready" ||
    current === "loading" ||
    current === "busy" ||
    current === "error"
  ) {
    return;
  }
  if (turnEventsRequested.has(turnId)) {
    return;
  }
  turnEventsRequested.add(turnId);
  turnEventsState.set(turnId, "loading");
  turnEventsError.delete(turnId);
  scheduleProcessRender();
  try {
    let after = 0;
    let done = false;
    let terminal = false;
    const byId = new Map<string, ProcessEvent>();
    for (let pageIndex = 0; pageIndex < MAX_EVENT_PAGES; pageIndex += 1) {
      const reply = await turnEvents(turnId, after);
      if (!reply.ok) {
        const error = replyError(reply);
        turnEventsError.set(turnId, error);
        turnEventsState.set(
          turnId,
          error.code === "BUSY" ? "busy" : "error",
        );
        render();
        return;
      }
      const page = reply.result as TurnEventPage;
      terminal = page.terminal;
      for (const event of page.events) {
        byId.set(event.event_id, event);
      }
      const next = page.next_seq;
      if (
        typeof next !== "number" ||
        page.events.length < 100 ||
        next <= after
      ) {
        done = true;
        break;
      }
      after = next;
    }
    const events = [...byId.values()].sort((left, right) => left.seq - right.seq);
    turnEventsCache.delete(turnId);
    turnEventsCache.set(turnId, {
      events,
      nextSeq: after,
      terminal,
      truncated: !done,
    });
    trimTurnCache(turnEventsCache, (key) => {
      turnEventsState.delete(key);
      turnEventsError.delete(key);
      turnEventsRequested.delete(key);
    });
    turnEventsState.set(turnId, "ready");
    turnEventsError.delete(turnId);
    render();
  } catch (failure) {
    const error = invokeError(failure);
    turnEventsError.set(turnId, error);
    turnEventsState.set(turnId, "error");
    render();
  } finally {
    turnEventsRequested.delete(turnId);
  }
}

/** Card D: read every persisted page of one old turn's tool steps. */
async function ensureTurnSteps(turnId: string): Promise<void> {
  if (!processEnabled()) {
    turnStepsState.set(turnId, "unavailable");
    return;
  }
  const current = turnStepsState.get(turnId);
  if (
    current === "ready" ||
    current === "loading" ||
    current === "busy" ||
    current === "error"
  ) {
    return;
  }
  if (turnStepsRequested.has(turnId)) {
    return;
  }
  turnStepsRequested.add(turnId);
  turnStepsState.set(turnId, "loading");
  turnStepsError.delete(turnId);
  scheduleProcessRender();
  try {
    let after = 0;
    let done = false;
    let nextAfter: number | null = null;
    const byStep = new Map<number, StepView>();
    for (let pageIndex = 0; pageIndex < MAX_STEP_PAGES; pageIndex += 1) {
      const reply = await turnSteps(turnId, after);
      if (!reply.ok) {
        const error = replyError(reply);
        turnStepsError.set(turnId, error);
        turnStepsState.set(turnId, error.code === "BUSY" ? "busy" : "error");
        render();
        return;
      }
      const page = reply.result as TurnStepPage;
      for (const step of page.steps) {
        byStep.set(step.step_no, step);
      }
      nextAfter = page.next_after_step_no;
      if (
        page.steps.length < 50 ||
        typeof nextAfter !== "number" ||
        nextAfter <= after
      ) {
        done = true;
        break;
      }
      after = nextAfter;
    }
    const steps = [...byStep.values()].sort(
      (left, right) => left.step_no - right.step_no,
    );
    turnStepsCache.delete(turnId);
    turnStepsCache.set(turnId, {
      steps,
      nextAfter,
      truncated: !done,
    });
    trimTurnCache(turnStepsCache, (key) => {
      turnStepsState.delete(key);
      turnStepsError.delete(key);
      turnStepsRequested.delete(key);
    });
    turnStepsState.set(turnId, "ready");
    turnStepsError.delete(turnId);
    render();
  } catch (failure) {
    const error = invokeError(failure);
    turnStepsError.set(turnId, error);
    turnStepsState.set(turnId, "error");
    render();
  } finally {
    turnStepsRequested.delete(turnId);
  }
}

function retryTurnEvents(turnId: string): void {
  turnEventsState.delete(turnId);
  turnEventsError.delete(turnId);
  void ensureTurnEvents(turnId);
}

function retryTurnSteps(turnId: string): void {
  turnStepsState.delete(turnId);
  turnStepsError.delete(turnId);
  void ensureTurnSteps(turnId);
}

function turnTerminal(
  turn: ConversationTurn | null,
  turnId: string,
): boolean {
  return turn !== null
    ? ["completed", "failed", "cancelled", "interrupted"].includes(turn.state)
    : chat.pendingTurnId !== turnId;
}

/**
 * Load everything an open replay panel needs, exactly once.
 *
 * BUSY and read errors keep their explicit state until the user retries, so a
 * render cannot turn a refused read into a tight request loop.  Every state
 * transition is guarded, which makes this safe to call from the render pass.
 */
function ensureTurnProcess(
  turn: ConversationTurn | null,
  turnId: string,
): void {
  if (!processEnabled()) {
    return;
  }
  const live = turnId === chat.eventsTurnId;
  if (!live && !turnEventsState.has(turnId)) {
    void ensureTurnEvents(turnId);
  }
  const terminal = turnTerminal(turn, turnId);
  if (terminal && !turnStepsState.has(turnId)) {
    void ensureTurnSteps(turnId);
  }
  if (
    terminal &&
    !traceCache.has(turnId) &&
    !traceRequested.has(turnId) &&
    traceState.get(turnId) !== "busy"
  ) {
    void loadTrace(turnId);
  }
}

function processStageName(stage: string | undefined): string {
  return stage === "expression" ? "表达阶段" : "内容阶段";
}

function processEventText(event: ProcessEvent): string | null {
  switch (event.type) {
    case "turn_accepted":
      return "已受理本轮。";
    case "stage_started":
      return `${processStageName(event.stage)}开始。`;
    case "stage_succeeded":
      return `${processStageName(event.stage)}完成。`;
    case "stage_failed":
      return `${processStageName(event.stage)}失败。`;
    case "expression_checked":
      return "表达检查通过。";
    case "message_published":
      return event.kind === "final" ? "回答已发布。" : null;
    case "turn_completed":
      return "本轮完成。";
    case "cancel_requested":
      return "已请求停止；到达检查点后结束。";
    case "late_result_discarded":
      return "停止后返回的模型结果已丢弃，未写入回答。";
    case "turn_failed":
      return `本轮失败${event.reason ? "：" + event.reason : ""}。`;
    case "turn_cancelled":
      return "本轮已停止。";
    case "turn_interrupted":
      return "宿主重启，本轮中断。";
    case "memory_recall":
      switch (event.status) {
        case "ok":
          return "记忆召回：已注入相关历史经历。";
        case "partial":
          return "记忆召回：只注入了部分相关历史经历。";
        case "no_match":
          return "记忆召回：没有相关历史经历。";
        case "incomplete":
          return "记忆召回：扫描未完成，未把结果当作确定无命中。";
        case "timeout":
          return "记忆召回：本轮超时，聊天继续。";
        default:
          return "记忆召回：当前不可用，聊天继续。";
      }
    case "repair_attempt": {
      const stage = processStageName(event.stage);
      return `${event.reason === "format" ? "格式修复" : "反馈重试"}请求 · ${stage}`;
    }
    case "reply_format_rejected":
      return "回复格式错误，正在修复回复。";
    case "plain_text_final_fallback":
      return "回复未按协议返回；已按纯文本最终回复继续。";
    default:
      return null;
  }
}

/** Stable public rejection classes; the event never carries arguments or text. */
const TOOL_REASON_LABELS: Record<string, string> = {
  parse: "解析失败",
  unknown_tool: "未知工具",
  arguments_invalid: "参数无效",
  not_authorized: "无权限",
  execution_failed: "执行失败",
};

function toolReasonLabel(reason: string | undefined): string {
  if (!reason) {
    return "已拒绝";
  }
  return TOOL_REASON_LABELS[reason] ?? reason;
}

function toolHeadText(
  stepNo: number,
  tool: string,
  status: string | undefined,
  durationMs: number | null,
): string {
  const parts = [`${stepNo}. ${tool}`, toolStatusLabel(status)];
  if (durationMs !== null) {
    parts.push(`${durationMs} ms`);
  }
  return parts.join(" · ");
}

function appendProcessGap(
  parent: HTMLElement,
  state: LoadState,
  what: string,
  error: ProcessError | undefined,
  retry: (() => void) | null,
): void {
  let text: string;
  switch (state) {
    case "loading":
      text = `正在读取${what}…`;
      break;
    case "busy":
      text = `另一轮正在处理，${what}暂不可读。`;
      break;
    case "error":
      text = `${what}读取失败：${error?.message ?? "未知错误"}`;
      break;
    case "unavailable":
      text = `此宿主不提供${what}读取（不可用）。`;
      break;
    default:
      return;
  }
  const node = el("div", {
    class: `process-gap is-${state}`,
    text,
    attrs: {
      role: "status",
      "aria-live": state === "loading" ? "polite" : undefined,
    },
  });
  if ((state === "busy" || state === "error") && retry !== null) {
    const button = el("button", {
      class: "ghost-button process-retry",
      text: uiText("重试"),
      attrs: { type: "button" },
    }) as HTMLButtonElement;
    button.addEventListener("click", retry);
    node.appendChild(button);
  }
  parent.appendChild(node);
}

function appendProcessEmpty(
  parent: HTMLElement,
  gap: "not-called" | "inconsistent" | "format-blocked" | "rejected",
): void {
  const text =
    gap === "not-called"
      ? "本轮未调用工具。"
      : gap === "inconsistent"
        ? "本轮公开事件显示有工具计划，但未读到工具步骤。"
        : gap === "rejected"
          ? "本轮工具调用被拒绝或未解析，未执行工具。"
          : "本轮有格式修复，未产生可执行工具步骤。";
  parent.appendChild(
    el("div", { class: `process-gap is-${gap}`, text }),
  );
}

function fillToolArguments(
  target: HTMLElement,
  step: StepView | undefined,
  event: ProcessEvent | undefined,
): void {
  target.replaceChildren();
  const args = toolArguments(step, event);
  if (args === null) {
    target.appendChild(
      el("div", { class: "process-note", text: "参数未公开。" }),
    );
    return;
  }
  target.appendChild(
    el("pre", {
      class: "process-pre",
      text: JSON.stringify(args, null, 2),
    }),
  );
}

function fillToolResult(
  target: HTMLElement,
  step: StepView | undefined,
  event: ProcessEvent | undefined,
): void {
  target.replaceChildren();
  if (step) {
    if (!step.result.available) {
      target.appendChild(
        el("div", {
          class: "process-note",
          text: "结果不可用（未公开或不可读）。",
        }),
      );
      return;
    }
    const excerpt = step.result.excerpt ?? event?.result_excerpt ?? null;
    if (excerpt === null || excerpt === "") {
      target.appendChild(
        el("div", { class: "process-note", text: "没有可展示的结果。" }),
      );
      return;
    }
    target.appendChild(
      el("pre", { class: "process-pre", text: excerpt }),
    );
    if (step.result.truncated) {
      target.appendChild(
        el("div", { class: "process-note", text: "结果已截断。" }),
      );
    }
    return;
  }
  if (event?.result_excerpt) {
    target.appendChild(
      el("pre", { class: "process-pre", text: event.result_excerpt }),
    );
    return;
  }
  if (
    event &&
    ["running", "started", "planned"].includes(event.status ?? "running")
  ) {
    target.appendChild(
      el("div", { class: "process-note", text: "等待结果…" }),
    );
    return;
  }
  target.appendChild(
    el("div", { class: "process-note", text: "结果未公开。" }),
  );
}

interface ToolRow {
  head: HTMLElement;
  result: HTMLElement;
  body: HTMLElement;
  details: HTMLDetailsElement;
}

function renderToolRow(
  parent: HTMLElement,
  stepNo: number,
  step: StepView | undefined,
  event: ProcessEvent | undefined,
  turnId: string,
  rows: Map<number, ToolRow>,
): void {
  const tool = step?.tool_name ?? event?.tool ?? "工具";
  const status =
    step?.status ??
    event?.status ??
    (event?.type === "tool_planned" ? "planned" : undefined);
  const duration = step ? stepDurationMs(step) : event?.duration_ms ?? null;
  const expansionKey = `${turnId}:tool:${stepNo}`;
  const item = el("details", {
    class: "process-step",
    attrs: { "data-step-no": String(stepNo), "data-tool-name": tool },
  }) as HTMLDetailsElement;
  item.open = processExpansion.get(expansionKey) ?? false;
  item.addEventListener("toggle", () =>
    processExpansion.set(expansionKey, item.open),
  );
  const head = el("summary", {
    class: "process-step-head",
    text: toolHeadText(stepNo, tool, status, duration),
  });
  const body = el("div", { class: "process-step-body" });
  body.appendChild(el("div", { class: "process-label", text: uiText("参数") }));
  const argsSlot = el("div", { class: "process-args" });
  fillToolArguments(argsSlot, step, event);
  body.appendChild(argsSlot);
  body.appendChild(
    el("div", {
      class: "process-label",
      text: document.documentElement.lang === "en" ? "Result" : "结果",
    }),
  );
  const resultSlot = el("div", { class: "process-result" });
  fillToolResult(resultSlot, step, event);
  body.appendChild(resultSlot);
  if (step?.error_label) {
    body.appendChild(
      el("div", {
        class: "process-note",
        text: `错误：${step.error_label}`,
      }),
    );
  }
  item.append(head, body);
  parent.appendChild(item);
  rows.set(stepNo, { head, result: resultSlot, body, details: item });
}

function renderProcessEvents(
  parent: HTMLElement,
  events: ProcessEvent[],
  turnId: string,
  stepsByNo: Map<number, StepView>,
  rows: Map<number, ToolRow>,
): Set<number> {
  const used = new Set<number>();
  const commentary = chat.messages.filter(
    (message) =>
      message.turn_id === turnId && message.kind === "commentary",
  );
  for (const event of events) {
    if (event.type === "message_published" && event.kind === "commentary") {
      const text = commentaryText(event, commentary);
      parent.appendChild(
        el("div", {
          class: "process-commentary",
          text: text === null || text === "" ? "（这条说明未公开）" : text,
        }),
      );
      continue;
    }
    if (event.type === "tool_planned") {
      if (typeof event.step_no !== "number") {
        parent.appendChild(
          el("div", {
            class: "process-note",
            text: "一条工具调用没有步骤编号，无法展示细节。",
          }),
        );
        continue;
      }
      const step = stepsByNo.get(event.step_no);
      renderToolRow(parent, event.step_no, step, event, turnId, rows);
      used.add(event.step_no);
      if (event.commentary_omitted) {
        parent.appendChild(
          el("div", {
            class: "process-note",
            text: "工具前的短句被当前配置省略。",
          }),
        );
      }
      continue;
    }
    if (event.type === "tool_started") {
      // Only this event may move a row to "running".  `tool_planned` and a
      // permission refusal stay "planned"/"rejected"; a parse failure has no
      // row at all.
      if (typeof event.step_no === "number") {
        const row = rows.get(event.step_no);
        if (row) {
          const startedStep = stepsByNo.get(event.step_no);
          row.head.textContent = toolHeadText(
            event.step_no,
            event.tool ?? startedStep?.tool_name ?? "工具",
            startedStep?.status ?? "running",
            startedStep ? stepDurationMs(startedStep) : null,
          );
        }
      }
      continue;
    }
    if (event.type === "tool_rejected") {
      const reason = toolReasonLabel(event.reason);
      if (typeof event.step_no !== "number") {
        // A parse failure has no parsed tool name and no step.  It is still a
        // public rejection, never a silent repair.
        parent.appendChild(
          el("div", {
            class: "process-note",
            text: `工具调用未解析：${reason}。本轮未执行任何工具。`,
          }),
        );
        continue;
      }
      used.add(event.step_no);
      const step = stepsByNo.get(event.step_no);
      const row = rows.get(event.step_no);
      const tool = event.tool ?? step?.tool_name ?? "工具";
      if (row) {
        row.head.textContent = toolHeadText(
          event.step_no,
          tool,
          "rejected",
          null,
        );
        row.details.open = true;
        // A rejected call publishes only its name and stable class; its
        // arguments and result body stay out of the process panel.
        row.body.replaceChildren(
          el("div", { class: "process-note", text: `拒绝原因：${reason}` }),
        );
      } else {
        parent.appendChild(
          el("div", {
            class: "process-note",
            text: `${event.step_no}. ${tool} 被拒绝：${reason}。未执行。`,
          }),
        );
      }
      continue;
    }
    if (event.type === "tool_finished") {
      if (typeof event.step_no !== "number") {
        continue;
      }
      const step = stepsByNo.get(event.step_no);
      const row = rows.get(event.step_no);
      if (row && !step) {
        row.head.textContent = toolHeadText(
          event.step_no,
          event.tool ?? "工具",
          event.status,
          event.duration_ms ?? null,
        );
        fillToolResult(row.result, undefined, event);
      } else if (!row) {
        renderToolRow(
          parent,
          event.step_no,
          step,
          event,
          turnId,
          rows,
        );
        used.add(event.step_no);
      }
      if (event.reason) {
        const settled = rows.get(event.step_no);
        if (settled) {
          // A failed call publishes its stable reason and error label only;
          // arguments and the result body stay out of the process panel.
          settled.details.open = true;
          const notes = [
            el("div", {
              class: "process-note",
              text: `失败原因：${toolReasonLabel(event.reason)}`,
            }),
          ];
          if (step?.error_label) {
            notes.push(
              el("div", {
                class: "process-note",
                text: `错误：${step.error_label}`,
              }),
            );
          }
          settled.body.replaceChildren(...notes);
        } else {
          parent.appendChild(
            el("div", {
              class: "process-note",
              text: `失败原因：${toolReasonLabel(event.reason)}`,
            }),
          );
        }
      }
      continue;
    }
    const note = processEventText(event);
    if (note) {
      parent.appendChild(el("div", { class: "process-note", text: note }));
    }
  }
  return used;
}

function renderUnlistedSteps(
  parent: HTMLElement,
  steps: StepView[],
  used: Set<number>,
  turnId: string,
  rows: Map<number, ToolRow>,
): void {
  const missing = steps.filter((step) => !used.has(step.step_no));
  if (missing.length === 0) {
    return;
  }
  parent.appendChild(
    el("div", {
      class: "process-label",
      text: document.documentElement.lang === "en" ? "Tool steps" : "工具步骤",
    }),
  );
  for (const step of missing) {
    renderToolRow(parent, step.step_no, step, undefined, turnId, rows);
    used.add(step.step_no);
  }
}

function hiddenProcessPlaceholder(): HTMLElement {
  return el("span", {
    class: "hidden",
    attrs: { "data-process-placeholder": "true" },
  });
}

function chatProcessPanel(turn: ConversationTurn | null, turnId: string): HTMLElement {
  const live = turnId === chat.eventsTurnId;
  const terminal = turnTerminal(turn, turnId);
  if (!live && !terminal) {
    return hiddenProcessPlaceholder();
  }
  if (!processEnabled()) {
    return turnId === newestTurnId(chat)
      ? el("div", {
          class: "process-gap is-unavailable",
          text: "此宿主不提供过程读取（不可用）。",
        })
      : hiddenProcessPlaceholder();
  }

  const eventsEntry = turnEventsCache.get(turnId);
  const events = live ? chat.events : eventsEntry?.events ?? [];
  const eventsState: LoadState = live
    ? events.length === 0 && !terminal
      ? "loading"
      : "ready"
    : turnEventsState.get(turnId) ?? "idle";
  const stepsEntry = turnStepsCache.get(turnId);
  const steps = stepsEntry?.steps ?? [];
  const stepsState: LoadState = turnStepsState.get(turnId) ?? "idle";
  const view = classifyProcess({
    turn,
    events,
    eventsState,
    steps,
    stepsState,
    processEnabled: true,
    terminal,
  });

  const panel = el("details", { class: "chat-process" }) as HTMLDetailsElement;
  panel.open = processExpansion.get(turnId) ?? false;
  panel.setAttribute("data-process-events", eventsState);
  panel.setAttribute("data-process-steps", stepsState);
  panel.setAttribute("data-process-tools", String(view.toolCount));
  if (view.gap) {
    panel.setAttribute("data-process-gap", view.gap);
  }
  panel.addEventListener("toggle", () => {
    processExpansion.set(turnId, panel.open);
    if (panel.open) {
      ensureTurnProcess(turn, turnId);
    }
  });
  const status = turn ? turn.state : chat.pendingTurnId === turnId ? "running" : null;
  const countKnown =
    eventsState === "ready" || stepsState === "ready";
  const summary = el("summary", {
    class: "chat-process-summary",
    text: `${document.documentElement.lang === "en" ? "View activity" : "查看过程"}${
      countKnown
        ? ` · ${view.toolCount} ${
            document.documentElement.lang === "en" ? "tool calls" : "次工具调用"
          }`
        : ""
    }`,
    attrs: status ? { title: status } : {},
  });
  panel.appendChild(summary);

  const list = el("div", { class: "chat-process-list" });
  const rows = new Map<number, ToolRow>();
  const stepsByNo = new Map(steps.map((step) => [step.step_no, step]));
  const used = new Set<number>();
  if (view.eventsGap) {
    appendProcessGap(
      list,
      view.eventsGap,
      "过程事件",
      turnEventsError.get(turnId),
      () => retryTurnEvents(turnId),
    );
  } else {
    for (const stepNo of renderProcessEvents(
      list,
      events,
      turnId,
      stepsByNo,
      rows,
    )) {
      used.add(stepNo);
    }
  }
  if (stepsState === "ready") {
    renderUnlistedSteps(list, steps, used, turnId, rows);
  }
  if (!live && eventsEntry?.truncated) {
    list.appendChild(
      el("div", {
        class: "process-note",
        text: "过程事件过多，仅显示已读取的前 1000 条。",
      }),
    );
  }
  if (stepsEntry?.truncated) {
    list.appendChild(
      el("div", {
        class: "process-note",
        text: "工具步骤过多，仅显示已读取的前 500 条。",
      }),
    );
  }
  if (view.stepsGap) {
    appendProcessGap(
      list,
      view.stepsGap,
      "工具细节",
      turnStepsError.get(turnId),
      () => retryTurnSteps(turnId),
    );
  }
  if (view.gap) {
    appendProcessEmpty(list, view.gap);
  }
  if (list.childElementCount === 0) {
    list.appendChild(
      el("div", {
        class: "process-note",
        text: terminal
          ? "本轮没有公开的过程事件。"
          : "正在执行，暂未产生工具调用。",
      }),
    );
  }
  panel.appendChild(list);

  const running = turn === null
    ? chat.pendingTurnId === turnId
    : ["accepted", "running"].includes(turn.state);
  if (running) {
    panel.appendChild(traceSection(null, "busy"));
  } else {
    const cached = traceCache.get(turnId) ?? null;
    const detailState = cached !== null
      ? "ready"
      : traceState.get(turnId) === "error"
        ? "error"
        : traceState.get(turnId) === "busy"
          ? "busy"
          : "loading";
    panel.appendChild(traceSection(cached, detailState));
  }
  if (panel.open) {
    ensureTurnProcess(turn, turnId);
  }
  return panel;
}

function postprocessChips(turn: ConversationTurn): HTMLElement {
  const row = el("div", { class: "chat-postprocess" });
  row.appendChild(
    el("span", { class: "postprocess-chip postprocess-saved", text: uiText("对话已保存") }),
  );
  const status = chat.postprocess[turn.turn_id];
  if (!status) {
    return row;
  }
  const memory = status.memory;
  const chip = el("span", {
    class: `postprocess-chip postprocess-memory-${memory.state}`,
    text: uiText(memoryStateLabel(memory.state)),
    attrs: memory.detail ? { title: memory.detail } : {},
  });
  row.appendChild(chip);
  if (memory.state === "pending" || memory.state === "outcome_unknown") {
    const retry = el("button", {
      class: "ghost-button postprocess-retry",
      text: uiText("重试"),
      attrs: { type: "button" },
    }) as HTMLButtonElement;
    retry.addEventListener("click", () => void retryTurnPostprocess(turn.turn_id));
    row.appendChild(retry);
  }
  return row;
}

function renderTranscriptOverlay(): void {
  appView.setBlockingOverlay(transcriptSearch.open);
  if (!transcriptSearch.open) {
    transcriptRenderKey = "";
    const cleanup = () => {
      if (!transcriptSearch.open) { shell.transcriptHost.classList.add("hidden"); shell.transcriptHost.replaceChildren(); }
      const animation = transcriptClosing; transcriptClosing = null; animation?.cancel();
    };
    shell.transcriptHost.inert = true;
    shell.transcriptHost.setAttribute("aria-hidden", "true");
    if (!shell.transcriptHost.childElementCount || window.matchMedia("(prefers-reduced-motion: reduce)").matches) cleanup();
    else if (!transcriptClosing) {
      transcriptClosing = shell.transcriptHost.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 140, easing: "ease-in", fill: "forwards" });
      void transcriptClosing.finished.then(cleanup).catch(() => undefined);
    }
    return;
  }
  transcriptClosing?.cancel(); transcriptClosing = null;
  shell.transcriptHost.inert = false;
  shell.transcriptHost.removeAttribute("aria-hidden");
  shell.transcriptHost.classList.remove("hidden");
  const key = JSON.stringify([
    transcriptSearch.loading,
    transcriptSearch.error,
    transcriptSearch.page,
    transcriptSearch.memoryLoading,
    transcriptSearch.memoryError,
    transcriptSearch.memoryPage,
    memorySearchAvailability(state.host),
    document.documentElement.lang,
  ]);
  if (key === transcriptRenderKey && shell.transcriptHost.childElementCount) return;
  transcriptRenderKey = key;
  const oldInput = shell.transcriptHost.querySelector<HTMLInputElement>(".transcript-input");
  const focused = oldInput === document.activeElement;
  const selection = oldInput?.selectionStart ?? 0;
  shell.transcriptHost.replaceChildren(transcriptSearchOverlay());
  if (focused) {
    const next = shell.transcriptHost.querySelector<HTMLInputElement>(".transcript-input");
    next?.focus(); next?.setSelectionRange(selection, selection);
  }
}

function transcriptSearchOverlay(): HTMLElement {
  return knowledgeSearchOverlay(
    transcriptSearch,
    memorySearchAvailability(state.host),
    document.documentElement.lang,
    {
      onQuery(value: string) {
        transcriptSearch.query = value;
      },
      onSubmit: submitKnowledgeSearch,
      onClose: closeTranscriptSearch,
      onMoreTranscript() {
        void runTranscriptSearch(true);
      },
      onMoreMemory() {
        void runMemorySearch(true);
      },
      onOpenTranscript(item) {
        void openTranscriptResult(item);
      },
    },
  );
}

function chatWorkspace(): HTMLElement {
  const wrapper = el("div", { class: "chat-workspace" });
  const bar = el("div", { class: "chat-bar" });
  const select = el("select", {
    class: "chat-select",
    attrs: { "aria-label": "选择会话" },
  }) as HTMLSelectElement;
  select.disabled = !canSwitchConversation(chat);
  const displayed = displayedConversations();
  const visible = visibleConversations(displayed, displayPreferences);
  const active = displayed.find(item => item.conversation_id === chat.activeId) ?? null;
  const options = [...visible];
  if (active && !visible.some(item => item.conversation_id === active.conversation_id)) {
    // The open conversation stays selectable even while archived.
    options.unshift(active);
  }
  if (options.length === 0) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "尚无会话";
    select.appendChild(option);
  }
  for (const item of options) {
    const option = document.createElement("option");
    option.value = item.conversation_id;
    const archived = !visible.some(entry => entry.conversation_id === item.conversation_id);
    option.textContent = `${item.title ?? "会话"}${archived ? ` · ${shellText("archivedTag")}` : ""} · ${item.turn_count} 轮`;
    if (item.conversation_id === chat.activeId) {
      option.selected = true;
    }
    select.appendChild(option);
  }
  select.addEventListener("change", () => {
    if (select.value) {
      void openConversation(select.value);
    }
  });
  const create = el("button", {
    class: "ghost-button",
    text: uiText("新建会话"),
    attrs: { type: "button" },
  });
  create.disabled = !canSwitchConversation(chat) || !state.host?.host.ready;
  create.addEventListener("click", () => void createConversation());
  const lookup = el("button", {
    class: "ghost-button",
    text: uiText("查找记忆与旧对话"),
    attrs: { type: "button" },
  }) as HTMLButtonElement;
  lookup.disabled = !conversationEnabled() || !state.host?.host.ready;
  lookup.addEventListener("click", openTranscriptSearch);
  bar.append(select, create, lookup);
  if (!canSwitchConversation(chat)) {
    bar.appendChild(
      el("span", { class: "chat-busy", text: uiText("另一轮正在处理，暂不能切换会话") }),
    );
  }

  const thread = el("div", { class: "chat-thread" });
  const pendingVisible = pendingUserMessageVisible(chat);
  const pendingText = pendingVisible ? chat.pendingText : null;
  const pendingTurnId = pendingText !== null ? chat.pendingUserTurnId : null;
  let pendingBubble: HTMLElement | null = null;
  if (pendingText !== null) {
    pendingBubble = pendingChatBubble(pendingText);
    revealBubble(
      pendingBubble,
      `pending:${chat.pendingRequestId ?? pendingText}`,
      0,
    );
  }
  if (chat.status === "loading" || chat.status === "idle") {
    thread.appendChild(
      el("div", { class: "chat-empty", text: uiText("正在读取会话…") }),
    );
    if (chat.status === "idle") {
      window.setTimeout(() => void ensureChatLoaded(), 0);
    }
  } else if (chat.messages.length === 0 && pendingBubble === null) {
    thread.appendChild(
      el("div", {
        class: "chat-empty",
        text: shellText("empty"),
      }),
    );
  }
  const historyControl = chatHistoryControl();
  if (historyControl !== null) {
    thread.appendChild(historyControl);
  }
  const newest = newestTurnId(chat);
  const turnIds = orderedTurnIds(chat);
  const pendingInside =
    pendingTurnId !== null && turnIds.includes(pendingTurnId);
  for (const turnId of turnIds) {
    const turn = chat.turns.find(item => item.turn_id === turnId) ?? null;
    const messages = chat.messages.filter(message => message.turn_id === turnId);
    const group = el("div", { class: "chat-turn-group", attrs: { "data-turn-id": turnId } });
    const animate = shouldRevealTurn(turnId, newest);
    // Card C: the projection occupies the user slot of its own turn, before
    // commentary, process and answers, so confirmation swaps it in place.
    if (pendingBubble !== null && pendingInside && turnId === pendingTurnId) {
      group.append(pendingBubble);
    }
    for (const message of messages.filter(message => message.kind === "user")) {
      const bubble = chatBubble(message);
      if (animate) revealBubble(bubble, bubbleRevealKey(message.message_id, 0), 0);
      group.append(bubble);
    }
    for (const message of messages.filter(message => message.kind === "commentary")) {
      const bubble = chatBubble(message);
      if (animate) revealBubble(bubble, bubbleRevealKey(message.message_id, 0), 0);
      group.append(bubble);
    }
    group.append(chatProcessPanel(turn, turnId));
    const answers = messages.filter(message => message.kind === "final" || message.kind === "wait_question");
    for (const message of answers) group.append(...answerBubbles(message, animate));
    if (turn && !answers.length && !["accepted", "running"].includes(turn.state)) {
      group.append(el("div", { class: "chat-turn-state", text: `${document.documentElement.lang === "en" ? "Turn status" : "本轮状态"}: ${turn.state}${turn.failure_reason ? " · " + turn.failure_reason : ""}` }));
    }
    if (turn && terminalTurn(turn)) group.append(postprocessChips(turn));
    thread.append(group);
  }
  // Only a send that has no turn id yet (the request is in flight) needs a
  // trailing projection group; everything else is placed inside its turn.
  if (pendingBubble !== null && !pendingInside) {
    const group = el("div", {
      class: "chat-turn-group is-pending-turn",
      attrs: { "data-pending": "true" },
    });
    group.append(pendingBubble);
    thread.append(group);
  }
  const status = turnStatusLabel(chat);
  if (status) {
    thread.appendChild(el("div", { class: "chat-status", text: status }));
  }
  if (chat.error) {
    thread.appendChild(
      callout(`会话操作失败：${chat.error.message}（${chat.error.code}）`, "danger"),
    );
  }
  wrapper.append(bar, thread);
  return wrapper;
}

/**
 * Card C2: the explicit "load earlier" entry, or its loading/error/end state.
 *
 * It is omitted for a conversation whose first page already contains every
 * message, so the initial latest-50 behaviour is unchanged.
 */
function chatHistoryControl(): HTMLElement | null {
  const hasCursor = chat.nextBefore !== null;
  if (
    !hasCursor &&
    !chat.loadingOlder &&
    chat.olderError === null &&
    !chat.olderExhausted
  ) {
    return null;
  }
  const state = chat.loadingOlder
    ? "loading"
    : chat.olderError !== null
      ? "error"
      : hasCursor
        ? "more"
        : "exhausted";
  const row = el("div", {
    class: "chat-history",
    attrs: { "data-history-state": state },
  });
  if (chat.loadingOlder) {
    row.append(
      el("span", {
        class: "chat-history-status",
        text: "正在加载更早的消息…",
        attrs: { role: "status" },
      }),
    );
    return row;
  }
  if (chat.olderError !== null) {
    row.append(
      el("span", {
        class: "chat-history-error",
        text: `加载更早失败：${chat.olderError.message}（${chat.olderError.code}）`,
      }),
    );
    const retry = el("button", {
      class: "ghost-button",
      text: "重试",
      attrs: { type: "button", id: "load-older", "data-history-retry": "true" },
    }) as HTMLButtonElement;
    retry.disabled = !hasCursor;
    retry.addEventListener("click", () => void loadOlderHistory());
    row.append(retry);
    return row;
  }
  if (hasCursor) {
    const button = el("button", {
      class: "ghost-button",
      text: "加载更早",
      attrs: { type: "button", id: "load-older" },
    }) as HTMLButtonElement;
    button.addEventListener("click", () => void loadOlderHistory());
    row.append(button);
    return row;
  }
  row.append(
    el("span", {
      class: "chat-history-status",
      text: "已到最早的历史",
      attrs: { role: "status" },
    }),
  );
  return row;
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
      if (conversationEnabled()) {
        void ensureChatLoaded();
      }
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
  if (tab === "settings") { settingsPageMounted = false; appView.navigate("settings"); return; }
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

let mountedSettings = false;
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
    mountedSettings = false;
    return;
  }
  if (state.drawer.tab === "settings" && mountedSettings) return;
  const titles: Record<DrawerTab, string> = {
    sources: "来源与详情",
    audit: "审计与引用",
    destination: "目的地",
    settings: "设置",
  };
  shell.drawerTitle.textContent = uiText(titles[state.drawer.tab]);
  clear(shell.drawerTabs);
  const tabs: Array<[DrawerTab, string]> = [
    ["sources", "来源"],
    ["audit", "审计"],
    ["destination", "目的地"],
  ];
  for (const [tab, label] of tabs) {
    const button = el("button", {
      class: `drawer-tab ${state.drawer.tab === tab ? "is-active" : ""}`,
      text: uiText(label),
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
  mountedSettings = state.drawer.tab === "settings";
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
        text: uiText("本次还没有可展示的来源引用。发起一次咨询后，来源摘要会显示在这里。"),
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
      drawerSection(uiText("来源（只显示正式公共字段）"), [
        list,
        el("p", {
          class: "muted",
          text: uiText("来源按宿主登记的输入、确定性查询产物与查询来源展示；未显示的来源不在这里推断。"),
        }),
      ]),
    );
  }
  shell.drawerBody.append(
    el("div", {
      class: "gap-box",
      children: [
        el("strong", { text: uiText("暂不可用 / 缺口") }),
        el("p", {
          text: uiText("完整 Context 来源清单、Attempt 明细与 Prompt 当前没有正式公共读取接口；本窗口不绕过 Facade 拼装，也不直接查询数据库。"),
        }),
      ],
    }),
  );
}

function renderAuditTab(): void {
  const result = state.result;
  if (!result) {
    shell.drawerBody.append(
      el("p", { class: "muted", text: uiText("本次还没有可审查的交互引用。") }),
    );
  } else {
    shell.drawerBody.append(
      drawerSection(uiText("本次交互"), [
        el("dl", {
          class: "kv",
          children: [
            kv(uiText("交互"), shortId(result.interaction_id)),
            kv(uiText("上下文"), shortId(result.context_id)),
            kv(uiText("模式"), "Consult"),
            kv(uiText("查询"), queryStatusLabel(result.query)),
            kv(uiText("查询策略"), result.query?.policy_version ?? "暂无查询"),
            kv(uiText("尝试"), "暂不可用（缺正式公共读取接口）"),
            kv(uiText("回执"), "尚未生成回执"),
            kv(uiText("目的地"), shortId(result.destination.destination_id)),
          ],
        }),
        el("p", {
          class: "muted",
          text: uiText("只显示可审查的公共字段；不显示 state_revision、内部 omission 计数或原因、Prompt、HTTP 正文或数据库列。"),
        }),
      ]),
    );
  }
  shell.drawerBody.append(
    el("div", {
      class: "gap-box",
      children: [
        el("strong", { text: uiText("缺口说明") }),
        el("p", {
          text: uiText("Consult 的 Attempt 明细与完整 Context 来源在 C12c 之前没有公共读取接口；本窗口将它们标为暂不可用，而不是补字段或读库。"),
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
        text: uiText("本次还没有可展示的候选来源。选择一个候选后，来源摘要会显示在这里。"),
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
            text: uiText("正文不可读或未提供；不显示空值占位。"),
          }),
        );
      }
      list.appendChild(row);
    }
    shell.drawerBody.append(
      drawerSection(uiText("审查来源（只显示正式可读引用）"), [
        el("p", {
          class: "muted",
          text: uiText("Event/Record 只显示引用与可读性；Input/Artifact 正文仅在正式可读时显示。"),
        }),
        list,
        el("p", {
          class: "muted",
          text: uiText("Event/Record 只显示精确引用与可读性，不读取正文。Input/Artifact 的正文仅在 read_experience 正式可读时显示；隐藏来源不显示正文。"),
        }),
      ]),
    );
  }
  shell.drawerBody.append(
    el("div", {
      class: "gap-box",
      children: [
        el("strong", { text: uiText("缺口说明") }),
        el("p", {
          text: uiText("不显示 Prompt、HTTP 正文、候选原始 body、内部 revision 或其他 namespace 的来源。"),
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
      el("p", { class: "muted", text: uiText("本次还没有可审查的 Review 引用。") }),
    );
    return;
  }
  const rows: Node[] = [];
  if (work) {
    rows.push(
      kv(uiText("工作"), work.work_id),
      kv(uiText("事件"), work.event_id),
      kv(uiText("轮次"), String(work.generation)),
      kv(uiText("暂停"), work.blocked ? "是" : "否"),
      kv(uiText("复查许可"), work.rerun_registered ? "已登记（待校验）" : "未登记"),
    );
  }
  if (detail) {
    rows.push(
      kv(uiText("候选"), detail.candidate.candidate_id),
      kv(uiText("候选版本"), String(detail.candidate.artifact_version)),
      kv(uiText("工作轮次"), String(detail.candidate.work_generation)),
      kv(uiText("交互"), detail.candidate.interaction_id),
      kv(uiText("上下文"), detail.candidate.context_id),
      kv(uiText("状态"), candidateStatusLabel(detail.candidate.status)),
      kv(uiText("操作"), operationLabel(detail.proposal?.operation)),
      kv(uiText("结构校验"), detail.structural_validation ?? "—"),
      kv(uiText("可批准提示"), detail.can_approve ? "是（仅内容提示）" : "否"),
      kv(uiText("可拒绝提示"), detail.can_reject ? "是（仅内容提示）" : "否"),
      kv(uiText("资格原因"), detail.eligibility_reason ?? "—"),
      kv(uiText("尝试"), detail.candidate.attempt_id
        ? `${shortId(detail.candidate.attempt_id)} #${detail.candidate.attempt_no ?? "—"}`
        : "暂不可用"),
    );
  }
  if (outcome) {
    rows.push(
      kv(uiText("结果状态"), reviewViewLabel(outcome.state)),
      kv(uiText("模型适配器"), outcome.model_adapter ?? "—"),
      kv(uiText("结果交互"), shortId(outcome.interaction_id)),
    );
    if (outcome.receipt) {
      rows.push(
        kv(uiText("回执请求"), shortId(outcome.receipt.request_id)),
        kv(uiText("回执操作"), receiptOperationLabel(outcome.receipt.operation)),
        kv(uiText("回执结果"), outcome.receipt.outcome),
        kv(uiText("回执时间"), outcome.receipt.committed_at),
      );
    } else if (outcome.state === "committed") {
      rows.push(kv(uiText("回执"), "不可见"));
    }
  }
  const caps = reviewCaps(state.host);
  rows.push(
    kv(uiText("宿主读取权限"), caps.read ? "可用" : "不可用"),
    kv(uiText("宿主开始审查权限"), caps.generate ? "可用" : "不可用"),
    kv(uiText("宿主批准权限"), caps.approve ? "可用" : "不可用"),
    kv(uiText("宿主拒绝权限"), caps.reject ? "可用" : "不可用"),
    kv(uiText("宿主结果权限"), caps.result ? "可用" : "不可用"),
  );
  shell.drawerBody.append(
    drawerSection(uiText("审查与候选引用"), [
      el("dl", { class: "kv", children: rows }),
      el("p", {
        class: "muted",
        text: uiText("只显示公开引用与状态；不显示 Prompt、HTTP 正文、proposal body、内部 revision、Approval/grant 或数据库列。"),
      }),
    ]),
  );
  shell.drawerBody.append(
    el("div", {
      class: "gap-box",
      children: [
        el("strong", { text: uiText("动作边界") }),
        el("p", {
          text: uiText("can_approve/can_reject 只是内容提示；按钮同时要求宿主 review.approve/review.reject 权限、pending 状态、v1 版本与工作轮次一致。最终由 C07 resolve 复验。"),
        }),
      ],
    }),
  );
}

function renderDestinationTab(): void {
  const destination = state.destination;
  if (!destination) {
    shell.drawerBody.append(
      el("p", { class: "muted", text: uiText("宿主尚未提供目的地描述。发送保持禁用。") }),
    );
    return;
  }
  const confirmed = isConfirmed(state);
  shell.drawerBody.append(
    drawerSection(uiText("目的地"), [
      el("dl", {
        class: "kv",
        children: [
          kv(uiText("类型"), destinationKindLabel(destination)),
          kv(uiText("提供方"), destination.provider),
          kv(uiText("模型"), destination.model),
          kv(uiText("标识"), shortId(destination.destination_id)),
          kv(uiText("清单哈希"), shortId(destination.manifest_hash)),
          kv(uiText("策略"), destination.policy_ref),
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
    drawerSection(uiText("发送范围"), [
      el("ul", {
        class: "plain-list",
        children: [
          el("li", { text: uiText("包括：你的问题、宿主登记的输入、当前授权上下文摘要。") }),
          el("li", { text: uiText("不包括：密钥、数据库内部字段、未授权内容、其他 namespace 数据。") }),
          el("li", { text: uiText("目的地由可信宿主配置；本窗口不能修改 provider 或 model。") }),
        ],
      }),
    ]),
    el("p", {
      class: "muted",
      text: uiText("外部目的地在首次使用或目的地变化后需要本会话中的显式确认；本地目的地不弹出确认。C09 发送门仍执行最终授权检查。"),
    }),
  );
}

function renderModelSettings(): HTMLElement {
  const container = el("div", { class: "model-settings" });
  const meta = el("dl", { class: "kv", attrs: { id: "model-meta" } });
  const keyInput = el("input", {
    class: "setting-input",
    attrs: {
      id: "model-api-key",
      "aria-label": "API Key",
      type: "password",
      maxlength: "512",
      placeholder: "sk-…",
      autocomplete: "new-password",
      spellcheck: "false",
    },
  });
  const keyStatus = el("p", {
    class: "setting-status",
    attrs: { id: "model-key-status" },
  });
  const connectionStatus = el("p", {
    class: "setting-status",
    attrs: { id: "model-connection-status" },
  });
  const saveKey = el("button", {
    class: "quiet-button",
    text: uiText("保存 API Key"),
    attrs: { type: "button", id: "save-model-key" },
  });
  const deleteKey = el("button", {
    class: "ghost-button",
    text: uiText("删除 API Key"),
    attrs: { type: "button", id: "delete-model-key" },
  });
  const testConnection = el("button", {
    class: "quiet-button",
    text: uiText("测试连接"),
    attrs: { type: "button", id: "test-model-connection" },
  });
  let status: ModelStatus | null = null;

  const errorText = (reply: HostReply): string => {
    const error = replyError(reply);
    switch (error.code) {
      case "MODEL_KEY_MISSING":
        return "尚未保存 API Key。";
      case "MODEL_KEY_INVALID":
        return "保存的 API Key 格式无效，请重新输入。";
      case "MODEL_AUTH_FAILED":
        return "DeepSeek 鉴权失败：API Key 无效或已被撤销。";
      case "MODEL_RATE_LIMITED":
        return "DeepSeek 限流或配额不足，请稍后再试。";
      case "MODEL_NETWORK":
        return "无法连接 DeepSeek，请检查网络。";
      case "MODEL_TIMEOUT":
        return "DeepSeek 连接超时。";
      case "MODEL_PROVIDER_ERROR":
        return "DeepSeek 服务端错误，请稍后再试。";
      case "MODEL_INVALID_RESPONSE":
        return "DeepSeek 返回了无效响应。";
      case "MODEL_REQUEST_REJECTED":
        return "DeepSeek 拒绝了请求；请确认模型名称和账户状态。";
      case "KEY_STORE_UNAVAILABLE":
      case "KEY_STORE_INVALID":
      case "KEY_STORE_FAILED":
        return "Windows 凭据管理器不可用，无法安全保存 API Key。";
      case "MODEL_CONFIG_EXTERNAL":
        return "当前宿主配置由外部管理，模型选择不能在软件内修改。";
      default:
        return error.message;
    }
  };

  const renderStatus = (value: ModelStatus): void => {
    status = value;
    clear(meta);
    meta.append(
      kv("Provider", value.provider),
      kv(uiText("模型"), value.model),
      kv(uiText("API 地址"), value.base_url),
      kv(uiText("配置来源"), uiText(value.managed ? "软件托管" : "外部宿主配置")),
      kv(uiText("宿主状态"), value.host_ready ? uiText("就绪") : value.reason_code ?? uiText("未就绪")),
    );
    keyStatus.className = "setting-status";
    if (value.key_store_error) {
      keyStatus.textContent = "无法读取 Windows 凭据管理器，API Key 状态未知。";
      keyStatus.classList.add("is-error");
    } else {
      keyStatus.textContent = value.key_saved
        ? "API Key 已保存（不会在界面显示，也不会写入配置文件或 PCS 数据）。"
        : "尚未保存 API Key。";
    }
    if (value.credential_source === "environment") keyStatus.textContent = "正在使用部署环境提供的凭据；未写入实例文件夹。";
    deleteKey.disabled = !value.key_saved;
    testConnection.disabled = !(value.key_available ?? value.key_saved) || Boolean(value.credential_restart_required);
    if (value.credential_restart_required) keyStatus.textContent = "凭据更改已保存；重启应用后生效。";
  };

  const load = async (): Promise<void> => {
    connectionStatus.textContent = "";
    connectionStatus.className = "setting-status";
    try {
      const reply = await modelStatus();
      if (!reply.ok) {
        keyStatus.textContent = errorText(reply);
        keyStatus.classList.add("is-error");
        return;
      }
      renderStatus(reply.result as ModelStatus);
    } catch (error) {
      keyStatus.textContent = invokeError(error).message;
      keyStatus.classList.add("is-error");
    }
  };

  saveKey.addEventListener("click", () => {
    const value = keyInput.value;
    if (!value) {
      keyStatus.textContent = "请输入 API Key。";
      keyStatus.classList.add("is-error");
      return;
    }
    void (async () => {
      keyStatus.textContent = "正在安全保存…";
      keyStatus.className = "setting-status";
      try {
        const reply = await saveModelApiKey(value);
        if (!reply.ok) {
          keyStatus.textContent = errorText(reply);
          keyStatus.classList.add("is-error");
          return;
        }
        keyInput.value = "";
        await refreshHost();
        openDrawer("settings");
        showToast("API Key 已保存到 Windows 凭据管理器；重启应用后生效，当前轮次不受影响");
      } catch (error) {
        keyStatus.textContent = invokeError(error).message;
        keyStatus.classList.add("is-error");
      }
    })();
  });

  deleteKey.addEventListener("click", () => {
    void (async () => {
      keyStatus.textContent = "正在删除…";
      keyStatus.className = "setting-status";
      try {
        const reply = await deleteModelApiKey();
        if (!reply.ok) {
          keyStatus.textContent = errorText(reply);
          keyStatus.classList.add("is-error");
          return;
        }
        keyInput.value = "";
        await refreshHost();
        openDrawer("settings");
        showToast("凭据已删除；当前进程中的旧凭据需重启应用后清除");
      } catch (error) {
        keyStatus.textContent = invokeError(error).message;
        keyStatus.classList.add("is-error");
      }
    })();
  });

  testConnection.addEventListener("click", () => {
    const sessionId = state.host?.session_id;
    if (!sessionId) {
      connectionStatus.textContent = "宿主会话尚未建立。";
      connectionStatus.classList.add("is-error");
      return;
    }
    if (
      !window.confirm(
        "这会向 DeepSeek 发起一次真实 API 请求，可能产生少量费用。继续吗？",
      )
    ) {
      return;
    }
    void (async () => {
      connectionStatus.className = "setting-status";
      connectionStatus.textContent = "正在测试真实 DeepSeek 连接…";
      try {
        const reply = await testModelConnection(sessionId);
        if (!reply.ok) {
          connectionStatus.textContent = `连接失败：${errorText(reply)}`;
          connectionStatus.classList.add("is-error");
          return;
        }
        const result = reply.result as { latency_ms?: number };
        connectionStatus.textContent = `真实连接成功${
          typeof result.latency_ms === "number"
            ? `（${Math.round(result.latency_ms)} ms）`
            : ""
        }。`;
      } catch (error) {
        connectionStatus.textContent = invokeError(error).message;
        connectionStatus.classList.add("is-error");
      }
    })();
  });

  container.append(
    el("p", {
      class: "muted",
      text: uiText("密钥 · 应用范围 · 需要重启。API Key 只保存在 Windows 凭据管理器，不明文回显或进入日志。保存或删除不会打断当前轮次；重启应用后加载凭据。"),
    }),
    meta,
    el("div", { class: "setting-row", children: [keyInput, saveKey, deleteKey] }),
    keyStatus,
    el("div", { class: "setting-row", children: [testConnection] }),
    el("p", {
      class: "muted",
      text: uiText("“API Key 已保存”只表示凭据已保存；只有“测试连接”成功才表示真实 DeepSeek 连接可用。测试连接由你主动发起，可能产生少量费用。"),
    }),
    connectionStatus,
  );
  void load();
  return container;
}

function renderSettingsTab(target: HTMLElement = shell.drawerBody): void {
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
    text: uiText("保存显示名"),
    attrs: { type: "button", id: "save-ai-name" },
  });
  const reset = el("button", {
    class: "ghost-button",
    text: uiText("恢复默认"),
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
    nameInput.value = result.ai_display_name;
    status.textContent = "已保存 · 立即生效";
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

  target.append(
    instanceSettingsPanel(),
    interfaceSettings(() => {
      const scroll = appView.pages.scrollTop;
      mountedSettings = false; settingsPageMounted = false; render();
      window.requestAnimationFrame(() => { appView.pages.scrollTop = scroll; });
    }),
    conversationDisplaySection(),
    generationSettings(async () => { await refreshHost(); }, info),
    maintenanceSettingsPanel(info?.session_id ?? null, async () => { await refreshHost(); }),
    personaPanel({
      api: chatApi,
      info,
      conversationId: chat.activeId,
      refresh: async () => { await refreshHost(); },
      goChat: () => {
        appView.navigate("chat");
        shell.textarea.focus();
      },
    }),
    preferencePanel({
      api: chatApi,
      info,
      conversationId: chat.activeId,
      refresh: async () => { await refreshHost(); },
      goChat: () => {
        appView.navigate("chat");
        shell.textarea.focus();
      },
    }),
    drawerSection(shellText("connection"), [renderModelSettings()]),
    webSettingsPanel(async () => {
      await refreshHost();
    }),
    memoryRecallSettingsPanel(async () => {
      await refreshHost();
    }),
    drawerSection(uiText("对话显示名"), [
      el("p", {
        class: "muted",
        text: uiText("实例范围 · 立即生效。只改变 AI 对话显示名；不改变 PCS 内部身份、来源身份、namespace、授权或 Persona 语义。"),
      }),
      el("div", { class: "setting-row", children: [nameInput, save, reset] }),
      status,
      el("p", {
        class: "muted",
        text: uiText("连接文件夹实例后，显示名和会话显示设置保存在实例文件夹内，迁移时一同保留。未设置时使用宿主配置或中性的“助手”。"),
      }),
    ]),
    drawerSection(uiText("宿主与连接"), [
      el("dl", {
        class: "kv",
        children: [
          kv("namespace", info?.host.namespace ?? "—"),
          kv("host_instance", info?.host.host_instance ?? "—"),
          kv("operator", info?.host.operator ?? "—"),
          kv(uiText("会话"), shortId(info?.session_id)),
          kv(uiText("保留策略"), info?.host.retention ?? "—"),
          kv(uiText("目的地方案"), info?.destination?.policy_ref ?? "—"),
          kv(uiText("宿主状态"), info?.host.ready ? uiText("就绪") : reasonLabel(info?.host.reason_code ?? null, null)),
        ],
      }),
    ]),
    drawerSection(uiText("软件"), [
      el("dl", {
        class: "kv",
        children: [
          kv(uiText("品牌"), "QUINTESSENCE"),
          kv(uiText("版本"), info ? `${info.version} · ${info.protocol}` : "—"),
          kv(uiText("工作区"), "Consult + Review（C12c）"),
          kv("PCS", info ? `${info.pcs.version} · schema ${info.pcs.schema_migrations}` : "—"),
          kv(uiText("外观"), document.documentElement.dataset.theme === "light" ? "日间" : "夜间"),
        ],
      }),
    ]),
    drawerSection(uiText("审查能力（宿主派生，只读）"), [
      el("dl", {
        class: "kv",
        children: [
          kv(uiText("读取"), uiText(reviewCaps(info).read ? "可用" : "不可用")),
          kv(uiText("开始审查"), uiText(reviewCaps(info).generate ? "可用" : "不可用")),
          kv(uiText("批准"), uiText(reviewCaps(info).approve ? "可用" : "不可用")),
          kv(uiText("拒绝"), uiText(reviewCaps(info).reject ? "可用" : "不可用")),
          kv(uiText("结果恢复"), uiText(reviewCaps(info).result ? "可用" : "不可用")),
        ],
      }),
      el("p", {
        class: "muted",
        text: uiText("这些布尔值由可信宿主派生；本窗口不能自行赋权，也不显示 token、grant 或权限表。"),
      }),
    ]),
  );
  if (target !== shell.drawerBody) {
    const extra = el("details", { class: "settings-advanced", children: [el("summary", { text: document.documentElement.lang === "en" ? "Instance details & capabilities" : "实例详情与能力" })] });
    const labels = ["宿主与连接", "软件", "审查能力（宿主派生，只读）"].map(uiText);
    for (const section of Array.from(target.querySelectorAll<HTMLElement>(":scope > .drawer-section"))) {
      if (labels.includes(section.querySelector("h3")?.textContent ?? "")) extra.append(section);
    }
    target.append(extra);
  }

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
void loadInterfacePreferences()
  .catch(() => undefined)
  .then(() => loadDisplayPreferencesFromHost())
  .catch(() => undefined)
  .finally(() => boot());
