/**
 * UI-F02: the persona and expression-preference settings panels.
 *
 * The panels are render-only: they call the trusted host, show exactly what it
 * returned, and never synthesize a profile, version, approval or preference.
 * Capability and capability-absence both come from `app.info`.
 */
import { clear, el } from "./dom";
import type {
  AppInfo,
  HostReply,
  ImportList,
  ImportSummary,
  PersonaPublishResult,
  PreferenceList,
  ProfileBody,
  ProfileList,
} from "./types";
import {
  activeSummary,
  bodyText,
  canActivate,
  canImport,
  canPublish,
  canReadBody,
  errorLabel,
  formatBytes,
  formatDigest,
  importAvailability,
  importSummaryRows,
  personaCapabilityNote,
  preferenceSections,
  profileRows,
  profileSummaryLine,
  t,
  type AdminLang,
  type ProfileRow,
} from "./adminView";
import type { PreferenceRow } from "./adminView";

export interface AdminApi {
  profileList(sessionId: string): Promise<unknown>;
  profileActivate(
    sessionId: string,
    profileId: string,
    recordVersion: number,
  ): Promise<unknown>;
  profileReadBody(
    sessionId: string,
    recordId: string,
    recordVersion: number,
  ): Promise<unknown>;
  profileImportList(sessionId: string): Promise<unknown>;
  profileImportPreview(sessionId: string, name: string): Promise<unknown>;
  profilePublishReviewed(
    sessionId: string,
    importId: string,
    profileId: string | null,
    confirm: boolean,
  ): Promise<unknown>;
  preferenceList(sessionId: string, conversationId?: string | null): Promise<unknown>;
  preferenceRevoke(
    sessionId: string,
    conversationId: string,
    dimension: string,
    reason?: string | null,
  ): Promise<unknown>;
}

export interface AdminContext {
  api: AdminApi;
  info: AppInfo | null;
  conversationId: string | null;
  refresh: () => Promise<void>;
  goChat: () => void;
}

function lang(): AdminLang {
  return document.documentElement.lang === "en" ? "en" : "zh-CN";
}

function kv(label: string, value: string): DocumentFragment {
  const fragment = document.createDocumentFragment();
  fragment.append(el("dt", { text: label }), el("dd", { text: value }));
  return fragment;
}

function errorText(reply: HostReply, language: AdminLang, fallback: string): string {
  const code = reply.error?.code ?? null;
  return errorLabel(code, language) ?? reply.error?.message ?? fallback;
}

function invokeMessage(error: unknown, language: AdminLang): string {
  return error instanceof Error && error.message
    ? error.message
    : t(language, "loadFailed");
}

function sourceText(source: string | null | undefined, language: AdminLang): string {
  if (source === "expected") {
    return t(language, "sourceExpected");
  }
  if (source === "active") {
    return t(language, "sourceActive");
  }
  if (source === "neutral_provisioned") {
    return t(language, "sourceNeutral");
  }
  return t(language, "notConfigured");
}

function shortHash(value: string | null | undefined): string {
  return value ? formatDigest(value) : "—";
}

function personaSummary(info: AppInfo | null, language: AdminLang): HTMLElement {
  const persona = info?.persona;
  const speaker = persona?.speaker;
  const expected = persona?.expected;
  return el("dl", {
    class: "kv",
    children: [
      kv(
        t(language, "statusLabel"),
        persona?.state === "ready"
          ? t(language, "readyState")
          : t(language, "notReadyState"),
      ),
      kv(t(language, "sourceLabel"), sourceText(persona?.source ?? null, language)),
      kv(t(language, "currentPersona"), persona?.profile_id ?? "—"),
      kv(
        t(language, "version"),
        `${persona?.profile_version ?? "—"} / r${persona?.record_version ?? "—"}`,
      ),
      kv(t(language, "assetLabel"), shortHash(persona?.asset_hash ?? null)),
      kv(
        t(language, "expectedLabel"),
        expected
          ? `${expected.profile_id}${expected.profile_version ? `@${expected.profile_version}` : ""}`
          : t(language, "notConfigured"),
      ),
      kv(
        t(language, "matchesLabel"),
        persona?.matches_expected === null || persona?.matches_expected === undefined
          ? "—"
          : persona.matches_expected
            ? t(language, "yes")
            : t(language, "no"),
      ),
      kv(
        t(language, "speakerLabel"),
        speaker
          ? `${speaker.identity}${speaker.display_name ? ` · ${speaker.display_name}` : ""}`
          : "—",
      ),
    ],
  });
}

export function personaPanel(ctx: AdminContext): HTMLElement {
  const language = lang();
  const box = el("section", { class: "drawer-section" });
  const caps = ctx.info?.capabilities.persona;
  const sessionId = ctx.info?.session_id ?? null;
  box.append(
    el("h3", { text: t(language, "personaTitle") }),
    el("p", { class: "muted", text: t(language, "personaHelp") }),
    personaSummary(ctx.info, language),
  );
  const status = el("p", { attrs: { role: "status", "aria-live": "polite" } });
  if (!sessionId) {
    box.append(el("p", { class: "muted", text: t(language, "unavailable") }));
    return box;
  }
  if (!canActivate(caps) && !canPublish(caps) && !canReadBody(caps)) {
    box.append(
      el("p", { class: "muted", text: personaCapabilityNote(language) }),
      status,
    );
    return box;
  }
  const listHost = el("div");
  const refresh = el("button", {
    class: "quiet-button",
    text: t(language, "refresh"),
    attrs: { type: "button" },
  });
  let loading = false;
  let lastList: ProfileList | null = null;

  const bodyPanel = (versionKey: string, recordId: string, recordVersion: number, host: HTMLElement) => {
    const existing = host.querySelector<HTMLElement>(`[data-body-for="${versionKey}"]`);
    if (existing) {
      existing.remove();
      return;
    }
    void (async () => {
      const holder = el("div", { class: "persona-body", attrs: { "data-body-for": versionKey } });
      holder.append(el("p", { class: "muted", text: t(language, "loading") }));
      host.append(holder);
      try {
        const reply = (await ctx.api.profileReadBody(
          sessionId,
          recordId,
          recordVersion,
        )) as HostReply;
        clear(holder);
        if (!reply.ok) {
          holder.append(
            el("p", { class: "is-error", text: errorText(reply, language, t(language, "loadFailed")) }),
          );
          return;
        }
        const body = reply.result as ProfileBody;
        holder.append(
          el("p", { class: "muted", text: t(language, "bodyWarning") }),
          el("p", {
            class: "muted",
            text: `${t(language, "bodyDigest")}: ${formatDigest(body.body_sha256)} · ${formatBytes(body.size_bytes)}`,
          }),
          el("pre", { class: "process-pre", text: bodyText(body) }),
        );
      } catch (error) {
        clear(holder);
        holder.append(el("p", { class: "is-error", text: invokeMessage(error, language) }));
      }
    })();
  };

  const activateVersion = (row: ProfileRow, recordVersion: number): void => {
    if (!window.confirm(t(language, "activateConfirm"))) {
      return;
    }
    void (async () => {
      try {
        const reply = (await ctx.api.profileActivate(
          sessionId,
          row.profileId,
          recordVersion,
        )) as HostReply;
        status.textContent = reply.ok
          ? t(language, "activatedOk")
          : errorText(reply, language, t(language, "loadFailed"));
        if (reply.ok) {
          await ctx.refresh();
          await load();
        }
      } catch (error) {
        status.textContent = invokeMessage(error, language);
      }
    })();
  };

  const renderList = (list: ProfileList | null): void => {
    clear(listHost);
    if (!list) {
      listHost.append(el("p", { class: "muted", text: t(language, "loadFailed") }));
      return;
    }
    const active = activeSummary(list.active, language);
    if (active) {
      listHost.append(
        el("p", {
          class: "muted",
          text: `${t(language, "active")}: ${active.label}`,
        }),
      );
    } else {
      listHost.append(el("p", { class: "muted", text: t(language, "noActive") }));
    }
    for (const row of profileRows(list, language)) {
      const details = el("details", { class: "settings-advanced" });
      details.append(
        el("summary", {
          text: `${row.profileId}${row.isActive ? ` · ${t(language, "active")}` : ""}`,
        }),
      );
      const inner = el("div");
      inner.append(el("p", { class: "muted", text: profileSummaryLine(row, language) }));
      const versions = el("div", { class: "persona-versions" });
      for (const version of row.versions) {
        const line = el("div", { class: "setting-row persona-version" });
        line.append(
          el("span", {
            text: `${version.label}${version.isActiveVersion ? ` · ${t(language, "activeVersion")}` : ""}`,
          }),
          el("span", { class: "muted", text: version.meta }),
        );
        if (canActivate(caps)) {
          const activate = el("button", {
            class: "quiet-button",
            text: t(language, "activate"),
            attrs: { type: "button" },
          });
          activate.onclick = () => activateVersion(row, version.recordVersion);
          line.append(activate);
        }
        if (canReadBody(caps)) {
          const view = el("button", {
            class: "ghost-button",
            text: t(language, "viewBody"),
            attrs: { type: "button" },
          });
          view.onclick = () =>
            bodyPanel(version.key, row.key, version.recordVersion, inner);
          line.append(view);
        }
        versions.append(line);
      }
      inner.append(versions);
      if (row.truncated) {
        inner.append(el("p", { class: "muted", text: t(language, "truncated") }));
      }
      inner.append(
        el("p", {
          class: "muted",
          text: t(language, "versionsTotal").replace("{n}", String(row.versionsTotal)),
        }),
      );
      details.append(inner);
      listHost.append(details);
    }
    if (canImport(caps)) {
      listHost.append(importBlock());
    }
  };
  const load = async (): Promise<void> => {
    if (loading) {
      return;
    }
    loading = true;
    try {
      const reply = (await ctx.api.profileList(sessionId)) as HostReply;
      if (!reply.ok) {
        lastList = null;
        status.textContent = errorText(reply, language, t(language, "loadFailed"));
        renderList(null);
        return;
      }
      status.textContent = "";
      lastList = reply.result as ProfileList;
      renderList(lastList);
    } catch (error) {
      status.textContent = invokeMessage(error, language);
      renderList(null);
    } finally {
      loading = false;
    }
  };

  const importBlock = (): HTMLElement => {
    const block = el("div", { class: "persona-import" });
    const files = el("div");
    const preview = el("div");
    const importStatus = el("p", { attrs: { role: "status", "aria-live": "polite" } });
    const reload = el("button", {
      class: "ghost-button",
      text: t(language, "refresh"),
      attrs: { type: "button" },
    });
    const renderFiles = (list: ImportList | null): void => {
      clear(files);
      const state = importAvailability(list, language);
      if (!state.available || state.rows.length === 0) {
        files.append(el("p", { class: "muted", text: state.message }));
        return;
      }
      for (const file of state.rows) {
        const line = el("div", { class: "setting-row persona-import-row" });
        line.append(
          el("span", { text: file.name }),
          el("span", { class: "muted", text: `${file.size} · ${file.modifiedAt}` }),
        );
        const button = el("button", {
          class: "quiet-button",
          text: t(language, "importPreview"),
          attrs: { type: "button" },
        });
        button.onclick = () => void previewFile(file.name);
        line.append(button);
        files.append(line);
      }
    };
    const previewFile = async (name: string): Promise<void> => {
      clear(preview);
      importStatus.textContent = t(language, "loading");
      try {
        const reply = (await ctx.api.profileImportPreview(sessionId, name)) as HostReply;
        if (!reply.ok) {
          importStatus.textContent = errorText(reply, language, t(language, "loadFailed"));
          return;
        }
        const summary = reply.result as ImportSummary;
        importStatus.textContent = t(language, "confirmHelp");
        const rows = el("dl", {
          class: "kv",
          children: importSummaryRows(summary, language).map((row) =>
            kv(row.label, row.value),
          ),
        });
        preview.append(rows);
        for (const warning of summary.warnings ?? []) {
          if (warning === "review_ref_replaced") {
            preview.append(el("p", { class: "muted", text: t(language, "importWarn") }));
          }
        }
        const publish = el("button", {
          class: "quiet-button",
          text: t(language, "importPublish"),
          attrs: { type: "button" },
        });
        publish.onclick = () => {
          if (!window.confirm(t(language, "importPublishConfirm"))) {
            return;
          }
          void (async () => {
            publish.disabled = true;
            try {
              const reply2 = (await ctx.api.profilePublishReviewed(
                sessionId,
                summary.import_id,
                null,
                true,
              )) as HostReply;
              if (!reply2.ok) {
                importStatus.textContent = errorText(
                  reply2,
                  language,
                  t(language, "loadFailed"),
                );
                return;
              }
              const result = reply2.result as PersonaPublishResult;
              importStatus.textContent = result.reused
                ? t(language, "importReused")
                : t(language, "publishedNew").replace(
                    "{v}",
                    String(result.record_version),
                  );
              clear(preview);
              await ctx.refresh();
              await load();
            } catch (error) {
              importStatus.textContent = invokeMessage(error, language);
            } finally {
              publish.disabled = false;
            }
          })();
        };
        preview.append(publish);
      } catch (error) {
        importStatus.textContent = invokeMessage(error, language);
      }
    };
    const loadFiles = async (): Promise<void> => {
      try {
        const reply = (await ctx.api.profileImportList(sessionId)) as HostReply;
        if (!reply.ok) {
          renderFiles(null);
          importStatus.textContent = errorText(reply, language, t(language, "loadFailed"));
          return;
        }
        renderFiles(reply.result as ImportList);
      } catch (error) {
        renderFiles(null);
        importStatus.textContent = invokeMessage(error, language);
      }
    };
    reload.onclick = () => void loadFiles();
    block.append(
      el("h4", { text: t(language, "importTitle") }),
      el("p", { class: "muted", text: t(language, "importHelp") }),
      el("div", { class: "setting-row", children: [reload] }),
      files,
      importStatus,
      preview,
    );
    void loadFiles();
    return block;
  };

  refresh.onclick = () => void load();
  box.append(el("div", { class: "setting-row", children: [refresh] }), status, listHost);
  void load();
  return box;
}

function preferenceRow(
  row: PreferenceRow,
  language: AdminLang,
  onRevoke: ((row: PreferenceRow) => void) | null,
  onCorrect: (() => void) | null,
): HTMLElement {
  const block = el("div", { class: "pref-row" });
  const head = el("div", { class: "setting-row" });
  head.append(el("span", { class: "pref-title", text: row.title }));
  head.append(el("span", { class: "muted", text: row.stateLabel }));
  if (row.canRevoke && onRevoke) {
    const button = el("button", {
      class: "quiet-button",
      text: t(language, "revoke"),
      attrs: { type: "button" },
    });
    button.onclick = () => onRevoke(row);
    head.append(button);
  }
  if (onCorrect && row.kind === "confirmed") {
    const button = el("button", {
      class: "ghost-button",
      text: t(language, "correctInChat"),
      attrs: { type: "button" },
    });
    button.onclick = onCorrect;
    head.append(button);
  }
  block.append(head);
  if (row.detail) {
    block.append(el("p", { class: "muted", text: row.detail }));
  }
  block.append(
    el("p", {
      class: "muted",
      text: `${t(language, "scope")}: ${row.scope} · ${t(language, "source")}: ${row.sourceLabel} · ${row.recordLabel}`,
    }),
  );
  return block;
}

export function preferencePanel(ctx: AdminContext): HTMLElement {
  const language = lang();
  const box = el("section", { class: "drawer-section" });
  box.append(
    el("h3", { text: t(language, "preferencesTitle") }),
    el("p", { class: "muted", text: t(language, "preferencesHelp") }),
  );
  const sessionId = ctx.info?.session_id ?? null;
  if (!sessionId) {
    box.append(el("p", { class: "muted", text: t(language, "unavailable") }));
    return box;
  }
  const status = el("p", { attrs: { role: "status", "aria-live": "polite" } });
  const host = el("div");
  const refresh = el("button", {
    class: "quiet-button",
    text: t(language, "refresh"),
    attrs: { type: "button" },
  });
  let loading = false;

  const revoke = (row: PreferenceRow): void => {
    if (!ctx.conversationId) {
      return;
    }
    if (!window.confirm(t(language, "revokeConfirm"))) {
      return;
    }
    void (async () => {
      try {
        const reply = (await ctx.api.preferenceRevoke(
          sessionId,
          ctx.conversationId as string,
          row.dimension,
          null,
        )) as HostReply;
        status.textContent = reply.ok
          ? t(language, "revokedOk")
          : errorText(reply, language, t(language, "loadFailed"));
        if (reply.ok) {
          await load();
        }
      } catch (error) {
        status.textContent = invokeMessage(error, language);
      }
    })();
  };

  const render = (list: PreferenceList | null): void => {
    clear(host);
    if (!list) {
      host.append(el("p", { class: "muted", text: t(language, "loadFailed") }));
      return;
    }
    const sections = preferenceSections(list, language);
    for (const note of sections.notes) {
      host.append(el("p", { class: "muted", text: note }));
    }
    const group = (
      title: string,
      rows: PreferenceRow[],
      empty: string,
      onCorrect: (() => void) | null,
    ): HTMLElement => {
      const wrapper = el("div", { class: "pref-group" });
      wrapper.append(el("h4", { text: title }));
      if (rows.length === 0) {
        wrapper.append(el("p", { class: "muted", text: empty }));
        return wrapper;
      }
      for (const row of rows) {
        wrapper.append(preferenceRow(row, language, revoke, onCorrect));
      }
      return wrapper;
    };
    host.append(
      group(t(language, "confirmedGroup"), sections.confirmed, t(language, "noneConfirmed"), ctx.goChat),
      group(t(language, "tentativeGroup"), sections.tentative, t(language, "noneTentative"), null),
      group(t(language, "inactiveGroup"), sections.inactive, t(language, "noneInactive"), null),
    );
    if (sections.confirmed.length > 0) {
      host.append(el("p", { class: "muted", text: t(language, "correctHelp") }));
    }
  };

  const load = async (): Promise<void> => {
    if (loading) {
      return;
    }
    loading = true;
    try {
      const reply = (await ctx.api.preferenceList(
        sessionId,
        ctx.conversationId,
      )) as HostReply;
      if (!reply.ok) {
        status.textContent = errorText(reply, language, t(language, "loadFailed"));
        render(null);
        return;
      }
      status.textContent = "";
      render(reply.result as PreferenceList);
    } catch (error) {
      status.textContent = invokeMessage(error, language);
      render(null);
    } finally {
      loading = false;
    }
  };

  refresh.onclick = () => void load();
  box.append(el("div", { class: "setting-row", children: [refresh] }), status, host);
  void load();
  return box;
}
