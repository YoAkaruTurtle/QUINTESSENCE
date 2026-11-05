/**
 * RG08: the single sidebar search panel.
 *
 * One query box, two clearly separated result groups: Scope Recall memory
 * first, saved conversation transcript second.  Memory rows are not buttons:
 * nothing in this panel can open, edit or promote a memory.  Transcript rows
 * keep their existing navigation.  All values enter through `textContent`.
 */

import { el } from "./dom";
import type { TranscriptSearchItem, TranscriptSearchPage } from "./chat";
import {
  formatMemoryTime,
  memoryCitation,
  memoryKindLabel,
  memoryPrimaryTime,
  memoryScanText,
  memorySourceLine,
  memoryStateLabel,
  memoryUnavailableText,
  type MemoryAvailability,
  type MemorySearchItem,
  type MemorySearchPage,
} from "./knowledgeSearch";

export interface KnowledgeSearchState {
  open: boolean;
  query: string;
  loading: boolean;
  error: string | null;
  page: TranscriptSearchPage | null;
  memoryLoading: boolean;
  memoryError: string | null;
  memoryPage: MemorySearchPage | null;
}

export interface KnowledgeSearchCallbacks {
  onQuery(value: string): void;
  onSubmit(): void;
  onClose(): void;
  onMoreTranscript(): void;
  onMoreMemory(): void;
  onOpenTranscript(item: TranscriptSearchItem): void;
}

function memoryItemView(
  value: MemorySearchItem,
  lang: string,
): HTMLElement {
  const row = el("div", { class: "knowledge-item" });
  const meta = el("div", { class: "knowledge-meta" });
  meta.append(
    el("span", { class: "knowledge-chip knowledge-chip-memory", text: lang === "en" ? "Memory" : "记忆" }),
    el("span", { class: "knowledge-chip", text: memoryKindLabel(value.kind, lang) }),
    el("span", { class: "knowledge-chip knowledge-chip-state", text: memoryStateLabel(value.state, lang) }),
    el("span", {
      class: "knowledge-time",
      text: formatMemoryTime(memoryPrimaryTime(value), lang),
    }),
  );
  row.appendChild(meta);
  row.appendChild(el("div", { class: "knowledge-snippet", text: value.content }));
  if (value.content_truncated) {
    row.appendChild(
      el("div", { class: "knowledge-truncated", text: lang === "en" ? "snippet clipped" : "片段已截断" }),
    );
  }
  const citation = el("div", { class: "knowledge-ref", text: `${lang === "en" ? "ref" : "引用"} ${memoryCitation(value)}${value.scope_id ? ` · ${lang === "en" ? "scope" : "范围"} ${value.scope_id}` : ""}` });
  citation.title = `${value.ref}@${value.revision}`;
  row.appendChild(citation);
  row.appendChild(el("div", { class: "knowledge-source", text: `${lang === "en" ? "source" : "来源"} ${memorySourceLine(value, lang)}` }));
  return row;
}

function transcriptItemView(
  item: TranscriptSearchItem,
  lang: string,
  callbacks: KnowledgeSearchCallbacks,
): HTMLButtonElement {
  const row = el("button", {
    class: "transcript-result knowledge-transcript-result",
    attrs: { type: "button" },
  }) as HTMLButtonElement;
  const meta = el("div", { class: "knowledge-meta" });
  meta.append(
    el("span", { class: "knowledge-chip knowledge-chip-transcript", text: lang === "en" ? "Transcript" : "旧对话" }),
    el("span", {
      class: "knowledge-time",
      text: new Date(item.created_at).toLocaleString(),
    }),
    el("span", { class: "knowledge-time", text: item.role === "user" ? (lang === "en" ? "you" : "你") : (lang === "en" ? "assistant" : "助手") }),
    el("span", {
      class: "knowledge-time",
      text:
        item.source === "legacy"
          ? lang === "en"
            ? "legacy single turn (read-only)"
            : "旧单轮记录（只读）"
          : lang === "en"
            ? `conversation ${(item.conversation_id ?? "").slice(0, 8)} · turn ${item.turn_no}`
            : `会话 ${(item.conversation_id ?? "").slice(0, 8)} · 第 ${item.turn_no} 轮`,
    }),
  );
  row.appendChild(meta);
  row.appendChild(el("div", { class: "transcript-snippet", text: item.text }));
  if (item.truncated) {
    row.appendChild(el("div", { class: "transcript-truncated", text: lang === "en" ? "snippet clipped" : "片段已截断" }));
  }
  if (item.conversation_id) {
    row.addEventListener("click", () => callbacks.onOpenTranscript(item));
  } else {
    row.disabled = true;
  }
  return row;
}

function sectionHead(
  title: string,
  badge: string,
  count: number | null,
): HTMLElement {
  const head = el("div", { class: "knowledge-section-head" });
  head.append(
    el("strong", { class: "knowledge-section-title", text: title }),
    el("span", { class: "knowledge-chip knowledge-chip-source", text: badge }),
  );
  if (count !== null) {
    head.appendChild(
      el("span", { class: "knowledge-count", text: `${count}` }),
    );
  }
  return head;
}

function memorySection(
  state: KnowledgeSearchState,
  availability: MemoryAvailability,
  lang: string,
  callbacks: KnowledgeSearchCallbacks,
): HTMLElement {
  const section = el("section", { class: "knowledge-section", attrs: { "aria-label": lang === "en" ? "Memory results" : "记忆结果" } });
  const page = state.memoryPage;
  section.appendChild(sectionHead(lang === "en" ? "Memory" : "记忆", "Scope Recall", page ? page.items.length : null));
  if (!availability.available) {
    section.appendChild(
      el("div", {
        class: "knowledge-note is-unavailable",
        text: memoryUnavailableText(availability.reason, lang),
        attrs: { "data-memory-unavailable": availability.reason },
      }),
    );
    return section;
  }
  if (state.memoryLoading) {
    section.appendChild(
      el("div", { class: "knowledge-empty", text: lang === "en" ? "Reading memory…" : "正在读取记忆…" }),
    );
  } else if (state.memoryError) {
    section.appendChild(
      el("div", { class: "knowledge-error", text: state.memoryError }),
    );
  } else if (page) {
    if (page.items.length === 0) {
      section.appendChild(
        el("div", {
          class: "knowledge-empty",
          text: state.query.trim()
            ? lang === "en"
              ? "No memory matched this keyword."
              : "没有找到匹配的记忆。"
            : lang === "en"
              ? "This instance has no memory to browse yet."
              : "此实例还没有可浏览的记忆。",
        }),
      );
    }
    for (const value of page.items) {
      section.appendChild(memoryItemView(value, lang));
    }
    section.appendChild(el("div", { class: "knowledge-scan", text: memoryScanText(page, lang) }));
    if (page.next_cursor) {
      const more = el("button", {
        class: "ghost-button",
        text: lang === "en" ? "More memory" : "更多记忆",
        attrs: { type: "button" },
      }) as HTMLButtonElement;
      more.addEventListener("click", callbacks.onMoreMemory);
      section.appendChild(more);
    }
  } else {
    section.appendChild(
      el("div", {
        class: "knowledge-empty",
        text: lang === "en" ? "Search or leave the box empty to browse recent memory." : "输入关键词，或留空浏览最近的记忆。",
      }),
    );
  }
  return section;
}

function transcriptSection(
  state: KnowledgeSearchState,
  lang: string,
  callbacks: KnowledgeSearchCallbacks,
): HTMLElement {
  const section = el("section", { class: "knowledge-section", attrs: { "aria-label": lang === "en" ? "Conversation results" : "旧对话结果" } });
  const page = state.page;
  section.appendChild(sectionHead(lang === "en" ? "Conversations" : "旧对话", "conversation.search", page ? page.items.length : null));
  if (!state.query.trim()) {
    section.appendChild(
      el("div", {
        class: "knowledge-note",
        text: lang === "en" ? "Type a word to also search saved conversations." : "输入关键词可同时检索已保存的旧对话。",
      }),
    );
    return section;
  }
  if (state.loading) {
    section.appendChild(
      el("div", { class: "knowledge-empty", text: lang === "en" ? "Reading the conversation archive…" : "正在读取对话档案…" }),
    );
  } else if (state.error) {
    section.appendChild(el("div", { class: "knowledge-error", text: state.error }));
  } else if (page) {
    if (page.items.length === 0) {
      section.appendChild(
        el("div", {
          class: "knowledge-empty",
          text: lang === "en"
            ? "No saved conversation matched. It may never have been said, or it is outside this instance."
            : "没有找到匹配的对话记录。可能尚未说过，或不在本实例已保存的记录里。",
        }),
      );
    }
    for (const item of page.items) {
      section.appendChild(transcriptItemView(item, lang, callbacks));
    }
    section.appendChild(
      el("div", {
        class: "knowledge-scan",
        text: lang === "en"
          ? `Scanned ${page.scanned} · filtered ${page.filtered}${page.next_cursor ? " · truncated, use a narrower word" : ""}`
          : `已扫描 ${page.scanned} 条 · 过滤 ${page.filtered} 条${page.next_cursor ? " · 结果已截断，可换更具体的关键词" : ""}`,
      }),
    );
    if (page.next_cursor) {
      const more = el("button", {
        class: "ghost-button",
        text: lang === "en" ? "More conversations" : "更多旧对话",
        attrs: { type: "button" },
      }) as HTMLButtonElement;
      more.addEventListener("click", callbacks.onMoreTranscript);
      section.appendChild(more);
    }
  } else {
    section.appendChild(
      el("div", { class: "knowledge-empty", text: lang === "en" ? "Press search to read saved conversations." : "点击检索以读取已保存的旧对话。" }),
    );
  }
  return section;
}

export function knowledgeSearchOverlay(
  state: KnowledgeSearchState,
  availability: MemoryAvailability,
  lang: string,
  callbacks: KnowledgeSearchCallbacks,
): HTMLElement {
  const overlay = el("div", { class: "transcript-overlay knowledge-overlay" });
  const panel = el("div", {
    class: "transcript-panel knowledge-panel",
    attrs: {
      role: "dialog",
      "aria-modal": "true",
      "aria-labelledby": "knowledge-title",
      "data-memory-count": state.memoryPage ? state.memoryPage.items.length : -1,
      "data-memory-available": availability.available ? "true" : "false",
      "data-transcript-count": state.page ? state.page.items.length : -1,
    },
  });
  const head = el("div", { class: "transcript-head" });
  head.append(
    el("strong", {
      text: lang === "en" ? "Search memory & conversations" : "查找记忆与旧对话",
      attrs: { id: "knowledge-title" },
    }),
  );
  const close = el("button", {
    class: "ghost-button",
    text: lang === "en" ? "Close" : "关闭",
    attrs: { type: "button" },
  }) as HTMLButtonElement;
  close.addEventListener("click", callbacks.onClose);
  head.appendChild(close);

  const note = el("div", {
    class: "transcript-note",
    text: lang === "en"
      ? "Memory comes from this instance's Scope Recall store; conversations come from saved transcripts. Results are read-only and do not change the current context."
      : "记忆来自本实例的 Scope Recall 存储，旧对话来自已保存的会话档案。两者只读，不会改变当前上下文。",
  });

  const input = el("input", {
    class: "transcript-input knowledge-input",
    attrs: {
      type: "search",
      placeholder: lang === "en" ? "Enter a word, or leave empty to browse memory…" : "输入你记得的词；留空可浏览最近的记忆…",
      "aria-label": lang === "en" ? "Memory and conversation search" : "记忆与旧对话关键词",
      maxlength: "256",
    },
  }) as HTMLInputElement;
  input.value = state.query;
  input.addEventListener("input", () => callbacks.onQuery(input.value));
  input.addEventListener("keydown", (event: KeyboardEvent) => {
    if (event.key === "Enter") {
      event.preventDefault();
      callbacks.onSubmit();
    }
  });
  const busy = state.loading || state.memoryLoading;
  const run = el("button", {
    class: "primary-button",
    text: lang === "en"
      ? busy
        ? "Searching…"
        : state.query.trim()
          ? "Search"
          : "Browse memory"
      : busy
        ? "检索中…"
        : state.query.trim()
          ? "检索"
          : "浏览记忆",
    attrs: { type: "button" },
  }) as HTMLButtonElement;
  run.disabled = busy;
  run.addEventListener("click", callbacks.onSubmit);
  const form = el("div", { class: "transcript-form" });
  form.append(input, run);

  const results = el("div", { class: "transcript-results knowledge-results" });
  results.append(
    memorySection(state, availability, lang, callbacks),
    transcriptSection(state, lang, callbacks),
  );
  panel.append(head, note, form, results);
  overlay.appendChild(panel);
  overlay.addEventListener("click", (event: MouseEvent) => {
    if (event.target === overlay) callbacks.onClose();
  });
  return overlay;
}
