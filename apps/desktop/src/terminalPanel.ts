/**
 * UI-F03: the runtime-log panel.
 *
 * The v0.2 bottom panel keeps its header, resize handle and session summary.
 * This component owns only the log body: fixed source/level filters, search,
 * pause/resume, copy and a coverage line that names what is really captured.
 * It never renders a synthetic row and never claims adapter stderr coverage.
 */
import { clear, el } from "./dom";
import {
  LOG_LEVEL_ORDER,
  LOG_POLL_MS,
  LOG_SEARCH_MAX,
  LOG_SOURCE_ORDER,
  LogReadError,
  adapterNotice,
  coverageLines,
  copyText,
  emptyNotice,
  errorNotice,
  isFiltered,
  levelLabel,
  logLang,
  logMessage,
  logTime,
  readLogs,
  sourceCollected,
  sourceLabel,
  statusText,
  type LogCoverage,
  type LogEntry,
  type LogLevelId,
  type LogSelection,
  type LogSourceId,
  type LogStatus,
} from "./terminalLog";

type InvokeFn = (command: string, args: Record<string, unknown>) => Promise<unknown>;

export class TerminalPanel {
  readonly root = el("div", { class: "terminal-content" });
  private toolbar = el("div", { class: "terminal-toolbar", attrs: { role: "toolbar" } });
  private sourceSelect = el("select", { class: "log-select" });
  private levelSelect = el("select", { class: "log-select" });
  private search = el("input", {
    class: "log-search",
    attrs: {
      type: "search",
      maxlength: String(LOG_SEARCH_MAX),
      autocomplete: "off",
      spellcheck: "false",
    },
  });
  private pauseButton = el("button", { class: "log-tool", attrs: { type: "button" } });
  private copyButton = el("button", { class: "log-tool", attrs: { type: "button" } });
  private status = el("span", { class: "log-status", attrs: { role: "status" } });
  private sourceText = el("span", { class: "log-field-label" });
  private levelText = el("span", { class: "log-field-label" });
  private list = el("ol", {
    class: "log-list",
    attrs: { role: "log", "aria-live": "off" },
  });
  private notice = el("div", { class: "log-notice hidden" });
  private retry = el("button", { class: "quiet-button log-retry", attrs: { type: "button" } });
  private body = el("div", { class: "log-body", children: [this.list, this.notice] });
  private coverage = el("p", { class: "log-coverage" });

  private selection: LogSelection = { source: "all", level: "all", query: "" };
  private coverageData: LogCoverage | null = null;
  private entries: LogEntry[] = [];
  private rendered: LogEntry[] = [];
  private openState = false;
  private paused = false;
  private inFlight = false;
  private errorCode: string | null = null;
  private statusKind: LogStatus = "idle";
  private timer: number | null = null;
  private searchTimer: number | null = null;

  constructor(
    private invoke: InvokeFn,
    private notify: (text: string) => void,
  ) {
    const sourceField = el("label", {
      class: "log-field",
      children: [this.sourceText, this.sourceSelect],
    });
    const levelField = el("label", {
      class: "log-field",
      children: [this.levelText, this.levelSelect],
    });
    this.toolbar.append(
      sourceField,
      levelField,
      this.search,
      this.pauseButton,
      this.copyButton,
      this.status,
    );
    this.root.append(this.toolbar, this.body, this.coverage);
    this.buildOptions();
    this.sourceSelect.onchange = () => {
      this.selection.source = this.sourceSelect.value as LogSourceId;
      void this.refresh();
    };
    this.levelSelect.onchange = () => {
      this.selection.level = this.levelSelect.value as LogLevelId;
      void this.refresh();
    };
    this.search.oninput = () => {
      if (this.searchTimer !== null) window.clearTimeout(this.searchTimer);
      this.searchTimer = window.setTimeout(() => {
        this.searchTimer = null;
        this.selection.query = this.search.value;
        void this.refresh();
      }, 250);
    };
    this.pauseButton.onclick = () => this.togglePause();
    this.copyButton.onclick = () => void this.copy();
    this.retry.onclick = () => void this.refresh();
    document.addEventListener("visibilitychange", () => this.onVisibility());
    this.applyLabels();
    this.render();
  }

  /** Called when the bottom panel opens. */
  open(): void {
    this.openState = true;
    this.paused = false;
    this.stopTimer();
    this.applyLabels();
    void this.loop();
  }

  /** Called when the bottom panel closes or the shell is torn down. */
  close(): void {
    this.openState = false;
    this.stopTimer();
  }

  applyLabels(): void {
    const lang = logLang();
    const sourceText = lang === "en" ? "Source" : "来源";
    const levelText = lang === "en" ? "Level" : "级别";
    this.sourceText.textContent = sourceText;
    this.levelText.textContent = levelText;
    this.sourceSelect.setAttribute("aria-label", sourceText);
    this.levelSelect.setAttribute("aria-label", levelText);
    this.search.setAttribute("aria-label", lang === "en" ? "Search logs" : "搜索日志");
    this.search.placeholder = lang === "en" ? "Search logs" : "搜索日志";
    this.list.setAttribute("aria-label", lang === "en" ? "Runtime logs" : "运行日志");
    this.retry.textContent = lang === "en" ? "Retry" : "重试";
    this.copyButton.textContent = lang === "en" ? "Copy" : "复制";
    this.copyButton.setAttribute("aria-label", lang === "en" ? "Copy visible log lines" : "复制可见日志行");
    this.buildOptions();
    this.render();
  }

  private buildOptions(): void {
    const lang = logLang();
    const sourceValue = this.selection.source;
    clear(this.sourceSelect);
    for (const id of LOG_SOURCE_ORDER) {
      const option = el("option", { text: sourceLabel(id, lang) });
      option.value = id;
      option.selected = id === sourceValue;
      this.sourceSelect.append(option);
    }
    const levelValue = this.selection.level;
    clear(this.levelSelect);
    for (const id of LOG_LEVEL_ORDER) {
      const option = el("option", { text: levelLabel(id, lang) });
      option.value = id;
      option.selected = id === levelValue;
      this.levelSelect.append(option);
    }
  }

  private async loop(): Promise<void> {
    this.stopTimer();
    if (!this.openState || this.paused) return;
    await this.refresh();
    this.schedule();
  }

  private schedule(): void {
    if (this.timer !== null || !this.openState || this.paused) return;
    this.timer = window.setTimeout(() => {
      this.timer = null;
      void this.loop();
    }, LOG_POLL_MS);
  }

  private stopTimer(): void {
    if (this.timer !== null) {
      window.clearTimeout(this.timer);
      this.timer = null;
    }
  }

  private async refresh(): Promise<void> {
    if (!this.openState || this.inFlight) return;
    this.inFlight = true;
    const selection: LogSelection = { ...this.selection };
    if (!this.coverageData) {
      this.statusKind = "loading";
      this.renderStatus();
    }
    try {
      const result = await readLogs(this.invoke, selection);
      if (!this.openState || !sameSelection(selection, this.selection)) return;
      this.coverageData = result.coverage;
      this.entries = result.entries;
      this.errorCode = null;
      this.statusKind = this.paused ? "paused" : "live";
      this.render();
    } catch (error) {
      if (!this.openState) return;
      this.errorCode = error instanceof LogReadError ? error.code : "LOG_UNAVAILABLE";
      this.entries = [];
      this.coverageData = null;
      this.statusKind = "error";
      this.render();
    } finally {
      this.inFlight = false;
    }
  }

  private togglePause(): void {
    this.paused = !this.paused;
    if (this.paused) {
      this.stopTimer();
      this.statusKind = "paused";
    } else {
      this.statusKind = this.coverageData ? "live" : "loading";
      void this.loop();
    }
    this.applyLabels();
  }

  private onVisibility(): void {
    if (document.visibilityState === "hidden") {
      this.stopTimer();
      return;
    }
    if (this.openState && !this.paused) void this.loop();
  }

  private render(): void {
    this.renderStatus();
    this.renderCoverage();
    this.renderNotice();
    this.renderList();
  }

  private renderStatus(): void {
    this.status.textContent = statusText(logLang(), this.statusKind, this.entries.length);
    this.status.dataset.state = this.statusKind;
    const lang = logLang();
    this.pauseButton.textContent = this.paused
      ? lang === "en" ? "Resume" : "恢复滚动"
      : lang === "en" ? "Pause" : "暂停滚动";
    this.pauseButton.setAttribute("aria-pressed", String(this.paused));
    this.pauseButton.setAttribute(
      "aria-label",
      this.paused
        ? lang === "en" ? "Resume log scrolling" : "恢复日志滚动"
        : lang === "en" ? "Pause log scrolling" : "暂停日志滚动",
    );
  }

  private renderCoverage(): void {
    const lang = logLang();
    if (this.errorCode) {
      this.coverage.textContent =
        lang === "en" ? "Coverage: unknown (read failed)" : "覆盖：读取失败，未知";
      return;
    }
    this.coverage.textContent = this.coverageData
      ? coverageLines(this.coverageData, lang).join("  ·  ")
      : lang === "en"
        ? "Coverage is reported by the host after the first read."
        : "覆盖范围由宿主在首次读取后给出。";
  }

  private adapterBlocked(): boolean {
    return (
      this.selection.source === "adapter" &&
      sourceCollected(this.coverageData, "adapter") === false
    );
  }

  private renderNotice(): void {
    const lang = logLang();
    clear(this.notice);
    this.notice.classList.add("hidden");
    let text: string | null = null;
    if (this.errorCode) {
      text = errorNotice(lang, this.errorCode);
    } else if (this.adapterBlocked()) {
      text = adapterNotice(this.coverageData, lang);
    } else if (this.coverageData && this.entries.length === 0) {
      text = emptyNotice(lang, isFiltered(this.selection));
    } else if (!this.coverageData && this.statusKind === "loading") {
      text = lang === "en" ? "Reading host logs…" : "正在读取宿主日志…";
    }
    if (!text) return;
    this.notice.append(el("strong", { text }));
    if (this.errorCode) this.notice.append(this.retry);
    this.notice.classList.remove("hidden");
  }

  private renderList(): void {
    if (this.errorCode || this.adapterBlocked() || this.entries.length === 0) {
      // Keep no stale rows behind an "unavailable / not collected / empty"
      // notice: the list and the notice never claim different things.
      if (this.rendered.length > 0 || this.list.childElementCount > 0) {
        clear(this.list);
        this.rendered = [];
      }
      this.list.classList.add("hidden");
      return;
    }
    this.list.classList.remove("hidden");
    const nearBottom =
      this.body.scrollHeight - this.body.scrollTop - this.body.clientHeight < 8;
    const firstRender = this.rendered.length === 0;
    const prefix =
      this.rendered.length <= this.entries.length &&
      this.rendered.every((entry, index) => entry.seq === this.entries[index].seq);
    if (prefix && this.rendered.length > 0) {
      const fragment = document.createDocumentFragment();
      for (const entry of this.entries.slice(this.rendered.length)) {
        fragment.append(this.renderRow(entry));
      }
      this.list.append(fragment);
    } else {
      clear(this.list);
      const fragment = document.createDocumentFragment();
      for (const entry of this.entries) fragment.append(this.renderRow(entry));
      this.list.append(fragment);
    }
    this.rendered = this.entries.slice();
    if (!this.paused && (nearBottom || firstRender)) {
      this.body.scrollTop = this.body.scrollHeight;
    }
  }

  private renderRow(entry: LogEntry): HTMLElement {
    const row = el("li", {
      class: "log-line",
      attrs: { "data-level": entry.level, "data-source": entry.source },
    });
    row.append(
      el("span", { class: "log-time", text: logTime(entry) }),
      el("span", { class: "log-source", text: entry.source }),
      el("span", { class: "log-level", text: entry.level }),
      el("span", { class: "log-message", text: logMessage(entry) }),
    );
    return row;
  }

  private async copy(): Promise<void> {
    const lang = logLang();
    const text = copyText(this.entries);
    if (!text) {
      this.notify(lang === "en" ? "No visible log lines to copy" : "没有可复制的日志行");
      return;
    }
    let copied = false;
    try {
      await navigator.clipboard.writeText(text);
      copied = true;
    } catch {
      copied = this.fallbackCopy(text);
    }
    if (!copied) {
      this.notify(lang === "en" ? "Copy failed" : "复制失败");
      return;
    }
    this.notify(
      lang === "en"
        ? `Copied ${this.entries.length} log lines`
        : `已复制 ${this.entries.length} 行日志`,
    );
  }

  private fallbackCopy(text: string): boolean {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.left = "-9999px";
    document.body.append(area);
    area.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch {
      copied = false;
    }
    area.remove();
    return copied;
  }
}

function sameSelection(left: LogSelection, right: LogSelection): boolean {
  return (
    left.source === right.source &&
    left.level === right.level &&
    left.query.trim() === right.query.trim()
  );
}
