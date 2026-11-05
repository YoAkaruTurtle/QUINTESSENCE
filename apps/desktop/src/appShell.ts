import { invoke } from "@tauri-apps/api/core";
import { clear, el, focusable } from "./dom";
import { metricsSummary } from "./dashboard";
import { s, type ShellTextKey } from "./shellText";
import { TerminalPanel } from "./terminalPanel";
import type { HostReply } from "./types";
import type { ConversationSummary } from "./chat";
import type { SessionOverview } from "./sessionOverview";
import type { ConversationGroupKey, SidebarModel } from "./conversationDisplay";

export type PageId = "chat" | "dashboard" | "review" | "settings";
const GROUP_LABEL: Record<ConversationGroupKey, ShellTextKey> = {
  today: "groupToday",
  yesterday: "groupYesterday",
  week: "groupWeek",
  older: "groupOlder",
  unknown: "groupUnknown",
};
const navigation: { id: PageId | "terminal"; label: ShellTextKey; path: string }[] = [
  { id: "chat", label: "chat", path: "M4 4h16v12H9l-5 4zM8 8h8M8 12h5" },
  { id: "dashboard", label: "dashboard", path: "M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z" },
  { id: "terminal", label: "terminal", path: "m5 6 6 6-6 6M13 18h6" },
  { id: "review", label: "review", path: "M8 4h8v3H8zM6 6H4v15h16V6h-2M8 13l3 3 5-6" },
];
function glyph(path: string): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("aria-hidden", "true");
  const line = document.createElementNS(svg.namespaceURI, "path");
  line.setAttribute("d", path); line.setAttribute("fill", "none"); line.setAttribute("stroke", "currentColor"); line.setAttribute("stroke-width", "1.5"); line.setAttribute("stroke-linejoin", "round"); line.setAttribute("stroke-linecap", "round");
  svg.append(line); return svg;
}
type Actions = { route(page: PageId): void; create(): void; select(id: string): void; search(): void; notice(text: string): void; follow(): void; rename(id: string, title: string): void; archive(id: string): void };
export class AppShell {
  page: PageId = "chat";
  readonly pages = el("section", { class: "app-page-host hidden", attrs: { "aria-label": s("dashboard") } });
  readonly settingsPage = el("div", { class: "sol-page settings-page" });
  private stage = el("div", { class: "app-stage" });
  private business: HTMLElement;
  private sidebar = el("aside", { class: "sol-sidebar", attrs: { "aria-label": s("menu") } });
  private nav = el("nav", { class: "sol-nav" });
  private recent = el("div", { class: "recent-conversations" });
  private recentTitle = el("div", { class: "eyebrow" });
  private shade = el("button", { class: "sidebar-shade", attrs: { type: "button", tabindex: "-1" } });
  private menu = el("button", { class: "mobile-menu", attrs: { type: "button" }, children: [glyph("M4 6h16M4 12h16M4 18h16")] });
  private collapse = el("button", { class: "sidebar-collapse", attrs: { type: "button" }, children: [glyph("M8 5v14M16 7l-5 5 5 5")] });
  private mobileClose = el("button", { class: "sidebar-mobile-close", attrs: { type: "button" }, children: [glyph("m6 6 12 12M6 18 18 6")] });
  private create = el("button", { class: "new-conversation", attrs: { type: "button" } });
  private search = el("button", { class: "sidebar-search", attrs: { type: "button" } });
  private settings: HTMLButtonElement;
  private caption = el("div", { class: "app-caption" });
  private panel = el("section", { class: "bottom-panel", attrs: { "aria-label": s("terminal") } });
  private summary = el("div");
  private terminal: TerminalPanel;
  private panelTitle = el("strong");
  private readonlyMark = el("span", { class: "readonly-mark" });
  private panelClose = el("button", { class: "panel-close", attrs: { type: "button" }, children: [glyph("m6 6 12 12M6 18 18 6")] });
  private resize = el("div", { class: "panel-resize", attrs: { role: "separator", tabindex: "0", "aria-orientation": "horizontal", "aria-valuemin": "240" } });
  private terminalOpen = false;
  private collapsed = false;
  private mobileOpen = false;
  private blockingOverlay = false;
  private panelHeight = 320;
  private heightBeforeTerminal: number | null = null;
  private labelKey = "";
  private recentKey = "";
  private activeId: string | null = null;
  private newActivity = el("button", { class: "new-activity hidden", attrs: { type: "button" } });
  private pageScroll = new Map<PageId, number>();
  private canSwitch = false;
  private contextMenu = el("div", {
    class: "conversation-menu",
    attrs: { role: "menu", "aria-label": s("conversationActions"), hidden: true },
  });
  private contextTrigger: HTMLElement | null = null;
  private contextConversationId: string | null = null;
  private contextOpen = false;
  constructor(private root: HTMLElement, parts: { brand: HTMLElement; header: HTMLElement; main: HTMLElement; footer: HTMLElement; settings: HTMLButtonElement }, private actions: Actions) {
    this.business = parts.main;
    this.settings = parts.settings;
    root.classList.add("sol-shell"); root.dataset.page = "chat";
    const brandRow = el("div", { class: "sidebar-brand-row", children: [parts.brand, this.mobileClose] });
    this.sidebar.append(brandRow, this.create, this.nav, this.search, this.recentTitle, this.recent,
      el("div", { class: "sidebar-bottom", children: [this.settings, this.collapse] }));
    parts.header.prepend(this.menu, this.caption);
    const panelHead = el("div", { class: "terminal-head", children: [el("div", { children: [this.panelTitle, this.readonlyMark] }), this.panelClose] });
    this.terminal = new TerminalPanel(invoke, text => this.actions.notice(text));
    this.panel.append(this.resize, this.summary, panelHead, this.terminal.root);
    const content = el("div", { class: "workspace-content", children: [parts.main, this.pages] });
    this.stage.append(parts.header, content, this.newActivity, parts.footer, this.panel);
    this.newActivity.onclick = () => { parts.main.scrollTop = parts.main.scrollHeight; this.setNewActivity(false); this.actions.follow(); };
    root.prepend(this.sidebar, this.shade, this.stage);
    document.body.append(this.contextMenu);
    document.addEventListener("pointerdown", event => {
      if (!this.contextOpen) return;
      const target = event.target as Node;
      if (this.contextMenu.contains(target) || this.contextTrigger?.contains(target)) return;
      this.closeContextMenu(false);
    }, true);
    document.addEventListener("scroll", () => {
      if (this.contextOpen) this.closeContextMenu(false);
    }, true);
    this.contextMenu.addEventListener("keydown", event => {
      if (event.key === "Escape" || event.key === "Tab") {
        event.preventDefault();
        this.closeContextMenu(true);
        return;
      }
      const items = Array.from(this.contextMenu.querySelectorAll<HTMLElement>('[role="menuitem"]'));
      if (!items.length) return;
      if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const current = items.indexOf(document.activeElement as HTMLElement);
      const next = event.key === "Home" ? 0
        : event.key === "End" ? items.length - 1
        : (current + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      items[next].focus();
    });
    this.menu.onclick = () => this.toggleMobile(true);
    this.shade.onclick = this.mobileClose.onclick = () => this.toggleMobile(false);
    this.collapse.onclick = () => { this.collapsed = !this.collapsed; this.applyLayout(); void this.saveLayout(); };
    this.create.onclick = () => { this.navigate("chat"); this.actions.create(); };
    this.search.onclick = () => { this.navigate("chat"); this.actions.search(); };
    this.settings.onclick = () => this.navigate("settings");
    this.panelClose.onclick = () => {
      this.terminalOpen = false;
      this.closeTerminal();
      this.applyLayout();
      this.refreshLabels();
    };
    this.resize.onkeydown = event => {
      if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
      event.preventDefault(); this.heightBeforeTerminal = null;
      const next = event.key === "Home" ? 240
        : event.key === "End" ? Math.max(240, window.innerHeight * .65)
        : this.panelHeight + (event.key === "ArrowUp" ? 20 : -20);
      this.panelHeight = Math.min(1600, Math.max(240, next));
      this.applyLayout(); void this.saveLayout();
    };
    this.resize.onpointerdown = event => {
      this.resize.setPointerCapture(event.pointerId);
      this.heightBeforeTerminal = null;
      const start = event.clientY, height = this.panelHeight;
      this.resize.onpointermove = move => {
        this.panelHeight = Math.min(1600, Math.max(240, height + start - move.clientY));
        this.applyLayout();
      };
      this.resize.onpointerup = () => { this.resize.onpointermove = null; void this.saveLayout(); };
      this.resize.onpointercancel = () => { this.resize.onpointermove = null; };
    };
    document.addEventListener("keydown", event => {
      if (!this.mobileOpen) return;
      if (event.key === "Escape") { event.preventDefault(); this.toggleMobile(false); }
      if (event.key === "Tab") {
        const items = focusable(this.sidebar), first = items[0], last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    });
    window.addEventListener("resize", () => { if (window.innerWidth > 650 && this.mobileOpen) this.toggleMobile(false); if (this.contextOpen) this.closeContextMenu(false); this.applyLayout(); });
    this.refreshLabels(); this.applyLayout();
    void invoke<HostReply>("ui_preferences").then(reply => {
      if (!reply.ok) return;
      const layout = reply.result as { sidebar_collapsed?: boolean; panel_height?: number };
      this.collapsed = layout.sidebar_collapsed ?? false;
      this.panelHeight = layout.panel_height ?? 320;
      if (this.terminalOpen && this.heightBeforeTerminal !== null) {
        // The preference reply can land after the log panel was opened; keep
        // the temporary log height and remember the real restore value.
        this.heightBeforeTerminal = this.panelHeight;
        const viewport = Math.max(window.innerHeight, 560);
        this.panelHeight = Math.max(
          this.panelHeight,
          Math.min(440, Math.max(240, viewport * .65)),
        );
      }
      this.applyLayout();
    }).catch(() => undefined);
  }
  navigate(page: PageId): void {
    const previous = this.page;
    this.pageScroll.set(this.page, this.pages.scrollTop);
    this.page = page; this.root.dataset.page = page;
    this.toggleMobile(false); this.labelKey = ""; this.refreshLabels();
    this.pages.classList.toggle("hidden", page === "chat" || page === "review");
    this.pages.setAttribute("aria-label", s(page));
    if (page === "settings" && this.settingsPage.parentElement !== this.pages) { clear(this.pages); this.pages.append(this.settingsPage); }
    this.actions.route(page);
    if (page !== previous && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      (page === "chat" || page === "review" ? this.business : this.pages).animate(
        [{ opacity: 0, transform: "translateY(8px)" }, { opacity: 1, transform: "none" }],
        { duration: 230, easing: "cubic-bezier(.2,.75,.25,1)" },
      );
    }
    window.requestAnimationFrame(() => { this.pages.scrollTop = this.pageScroll.get(page) ?? 0; });
  }
  setDashboard(content: HTMLElement): void { if (this.page === "dashboard") this.pages.replaceChildren(content); }
  setNewActivity(show: boolean): void { this.newActivity.classList.toggle("hidden", !show); }
  setBlockingOverlay(open: boolean): void {
    this.blockingOverlay = open;
    this.stage.inert = open || this.mobileOpen;
    this.sidebar.inert = open;
  }
  private toggleMobile(open: boolean): void {
    this.mobileOpen = open; this.root.classList.toggle("sidebar-open", open); this.stage.inert = open || this.blockingOverlay;
    if (open) this.mobileClose.focus(); else if (document.activeElement && this.sidebar.contains(document.activeElement) && window.innerWidth <= 650) this.menu.focus();
  }
  /**
   * A log body needs room to be readable; the v0.2 panel default (320px) is
   * mostly consumed by the selected-conversation summary.  Opening the log
   * temporarily grows the panel to 440px (still within the 65% cap) and closing
   * restores the previous height.  A manual resize drops the restore bound.
   */
  private openTerminal(): void {
    this.heightBeforeTerminal = this.panelHeight;
    const viewport = Math.max(window.innerHeight, 560);
    const target = Math.min(440, Math.max(240, viewport * .65));
    if (this.panelHeight < target) {
      this.panelHeight = target;
      this.applyLayout();
    }
    this.terminal.open();
  }
  private closeTerminal(): void {
    this.terminal.close();
    if (this.heightBeforeTerminal !== null) {
      this.panelHeight = this.heightBeforeTerminal;
      this.heightBeforeTerminal = null;
      this.applyLayout();
    }
  }
  private applyLayout(): void {
    this.root.classList.toggle("terminal-open", this.terminalOpen);
    this.root.classList.toggle("sidebar-collapsed", this.collapsed);
    this.panel.classList.toggle("hidden", !this.terminalOpen);
    // Clamp only the rendered height: a transient 0/small `innerHeight` during
    // window setup must not destroy the user's stored panel height.
    const viewport = Math.max(window.innerHeight, 240);
    const max = Math.max(240, viewport * .65);
    const shown = Math.min(Math.max(this.panelHeight, 240), max);
    this.panel.style.height = `${shown}px`;
    this.resize.setAttribute("aria-valuenow", String(Math.round(shown)));
    this.resize.setAttribute("aria-valuemax", String(Math.round(max)));
    this.collapse.setAttribute("aria-expanded", String(!this.collapsed));
  }
  private async saveLayout(): Promise<void> {
    try {
      // The temporary log-opening height is never persisted; a manual resize
      // clears the restore bound and is saved as the real preference.
      const height = Math.round(this.heightBeforeTerminal ?? this.panelHeight);
      const reply = await invoke<HostReply>("save_layout_preferences", { sidebarCollapsed: this.collapsed, panelHeight: height });
      if (!reply.ok) this.actions.notice(reply.error?.message ?? s("loadError"));
    } catch { this.actions.notice(s("loadError")); }
  }
  private refreshLabels(): void {
    this.menu.setAttribute("aria-label", s("menu")); this.mobileClose.setAttribute("aria-label", s("close")); this.shade.setAttribute("aria-label", s("close"));
    this.collapse.setAttribute("aria-label", s(this.collapsed ? "expand" : "collapse")); this.collapse.title = s(this.collapsed ? "expand" : "collapse");
    this.create.replaceChildren(glyph("M12 5v14M5 12h14"), el("span", { text: s("newChat") })); this.create.title = s("newChat"); this.create.setAttribute("aria-label", s("newChat"));
    this.search.replaceChildren(glyph("M15 15l5 5M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0"), el("span", { text: s("search") })); this.search.title = s("search"); this.search.setAttribute("aria-label", s("search"));
    this.recentTitle.textContent = s("recent");
    this.newActivity.textContent = `${s("jump")} ↓`;
    this.settings.replaceChildren(glyph("M12 3v3M12 18v3M3 12h3M18 12h3M6 6l2 2M16 16l2 2M6 18l2-2M16 8l2-2M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0"), el("span", { text: s("settings") }));
    this.settings.className = `sidebar-settings ${this.page === "settings" ? "is-active" : ""}`; this.settings.title = s("settings");
    clear(this.nav);
    for (const item of navigation) {
      const active = item.id === "terminal" ? this.terminalOpen : item.id === this.page;
      const button = el("button", { class: active ? "is-active" : "", attrs: { type: "button", title: s(item.label), "aria-label": s(item.label) }, children: [glyph(item.path), el("span", { text: s(item.label) })] });
      if (item.id === "terminal") button.setAttribute("aria-expanded", String(this.terminalOpen)); else if (active) button.setAttribute("aria-current", "page");
      button.onclick = () => {
        if (item.id === "terminal") {
          this.terminalOpen = !this.terminalOpen;
          if (this.terminalOpen) this.openTerminal(); else this.closeTerminal();
          this.applyLayout();
          this.refreshLabels();
          this.toggleMobile(false);
        } else this.navigate(item.id);
      };
      this.nav.append(button);
    }
    this.caption.replaceChildren(el("span", { class: "caption-label", text: s(this.page) }));
    this.panelTitle.textContent = s("logTitle"); this.panelClose.setAttribute("aria-label", s("close")); this.resize.setAttribute("aria-label", s("resize"));
    this.readonlyMark.textContent = s("readonly"); this.panel.setAttribute("aria-label", s("terminal"));
    this.terminal.applyLabels();
  }
  update(model: SessionOverview, list: SidebarModel<ConversationSummary>, canSwitch: boolean, ready: boolean): void {
    const key = `${document.documentElement.lang}:${this.page}:${this.collapsed}`;
    if (key !== this.labelKey) { this.labelKey = key; this.refreshLabels(); }
    this.activeId = model.id; this.canSwitch = canSwitch;
    this.create.disabled = !canSwitch || !ready; this.search.disabled = !ready;
    const recentKey = JSON.stringify([
      list.groups.map(group => [group.key, group.items.map(item => [item.conversation_id, item.title, item.turn_count])]),
      model.id, canSwitch, document.documentElement.lang, list.archivedCount, list.notice,
    ]);
    if (recentKey !== this.recentKey) {
      this.recentKey = recentKey;
      this.closeContextMenu(false);
      clear(this.recent);
      for (const group of list.groups) {
        const section = el("div", { class: "recent-group", attrs: { role: "group", "aria-label": s(GROUP_LABEL[group.key]) } });
        section.append(el("div", { class: "recent-group-title", text: s(GROUP_LABEL[group.key]), attrs: { "aria-hidden": "true" } }));
        for (const item of group.items) section.append(this.recentRow(item));
        this.recent.append(section);
      }
      if (list.notice) this.recent.append(el("p", { class: "sidebar-empty", text: s("displayPrefsUnavailable") }));
      if (!list.visibleCount) this.recent.append(el("p", { class: "sidebar-empty", text: s(list.archivedCount > 0 ? "allArchived" : "noHistory") }));
    }
    this.summary.replaceChildren(metricsSummary(model));
  }
  private recentRow(item: ConversationSummary): HTMLElement {
    const date = new Date(item.created_at).toLocaleString(document.documentElement.lang, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
    const title = item.title ?? `${s("conversation")} · ${date}`;
    const selected = item.conversation_id === this.activeId;
    const row = el("div", { class: "recent-row", attrs: { "data-conversation-id": item.conversation_id } });
    const select = el("button", {
      class: selected ? "recent-select is-selected" : "recent-select",
      attrs: { type: "button", title, "aria-current": selected ? "true" : undefined },
      children: [el("span", { text: title }), el("small", { text: String(item.turn_count) })],
    });
    select.disabled = !this.canSwitch;
    select.onclick = () => { this.navigate("chat"); this.actions.select(item.conversation_id); };
    select.onkeydown = event => {
      if (event.key !== "ContextMenu" && !(event.shiftKey && event.key === "F10")) return;
      event.preventDefault();
      if (this.canSwitch) this.openContextMenu(item, select);
    };
    const more = el("button", {
      class: "recent-more",
      text: "⋯",
      attrs: {
        type: "button",
        title: s("conversationActions"),
        "aria-label": `${s("conversationActions")} · ${title}`,
        "aria-haspopup": "menu",
        "aria-expanded": "false",
      },
    });
    more.disabled = !this.canSwitch;
    more.onclick = event => { event.stopPropagation(); if (this.canSwitch) this.openContextMenu(item, more); };
    row.oncontextmenu = event => {
      event.preventDefault();
      if (this.canSwitch) this.openContextMenu(item, select, event.clientX, event.clientY);
    };
    row.append(select, more);
    return row;
  }
  private openContextMenu(item: ConversationSummary, trigger: HTMLElement, x?: number, y?: number): void {
    this.closeContextMenu(false);
    clear(this.contextMenu);
    this.contextTrigger = trigger;
    trigger.setAttribute("aria-expanded", "true");
    const rename = el("button", { class: "conversation-menu-item", text: s("renameConversation"), attrs: { type: "button", role: "menuitem", tabindex: "-1" } });
    rename.onclick = () => {
      const id = item.conversation_id;
      const title = item.title ?? "";
      this.closeContextMenu(false);
      this.actions.rename(id, title);
    };
    const archive = el("button", { class: "conversation-menu-item", text: s("archiveConversation"), attrs: { type: "button", role: "menuitem", tabindex: "-1" } });
    archive.onclick = () => {
      const id = item.conversation_id;
      this.closeContextMenu(false);
      this.actions.archive(id);
    };
    this.contextMenu.append(rename, archive);
    this.contextMenu.hidden = false;
    this.contextMenu.style.left = "0px";
    this.contextMenu.style.top = "0px";
    const rect = trigger.getBoundingClientRect();
    const size = this.contextMenu.getBoundingClientRect();
    const left = Math.max(8, Math.min(x !== undefined ? x : rect.right - size.width, window.innerWidth - size.width - 8));
    const top = Math.max(8, Math.min(y !== undefined ? y : rect.bottom + 4, window.innerHeight - size.height - 8));
    this.contextMenu.style.left = `${left}px`;
    this.contextMenu.style.top = `${top}px`;
    this.contextOpen = true;
    rename.focus({ preventScroll: true });
  }
  private closeContextMenu(restoreFocus: boolean): void {
    if (!this.contextOpen) return;
    this.contextOpen = false;
    const trigger = this.contextTrigger;
    this.contextTrigger = null;
    this.contextMenu.hidden = true;
    clear(this.contextMenu);
    if (trigger) {
      trigger.setAttribute("aria-expanded", "false");
      if (restoreFocus && document.contains(trigger)) trigger.focus({ preventScroll: true });
    }
  }
}
