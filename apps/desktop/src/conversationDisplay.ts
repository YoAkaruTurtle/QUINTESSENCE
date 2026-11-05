/**
 * UI-F04 display-only conversation layer (DOM-free).
 *
 * Archive and rename never touch a conversation record: the trusted host owns
 * `ui-preferences.json` (scoped by the F03 instance fingerprint) and the WebView
 * only merges the returned current-instance slice into what it already shows.
 * Everything here is pure so `node --test` can exercise grouping, merging and
 * validation without a WebView.
 */

export interface ArchivedConversation {
  conversation_id: string;
  archived_at_ms: number;
}

export interface DisplayLimits {
  archived: number;
  titles: number;
  title_chars: number;
}

export interface DisplayPreferences {
  /** True only when the host could prove its instance identity. */
  scoped: boolean;
  archived: ArchivedConversation[];
  titles: Record<string, string>;
  limits: DisplayLimits;
}

export const MAX_TITLE_CHARS = 40;

export const EMPTY_DISPLAY_PREFERENCES: DisplayPreferences = {
  scoped: false,
  archived: [],
  titles: {},
  limits: { archived: 200, titles: 500, title_chars: MAX_TITLE_CHARS },
};

export type InvokeFn = (
  command: string,
  args?: Record<string, unknown>,
) => Promise<unknown>;

export class DisplayPreferenceError extends Error {
  readonly code: string;
  constructor(code: string, message: string) {
    super(message);
    this.name = "DisplayPreferenceError";
    this.code = code;
  }
}

const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const CONTROL = /[\u0000-\u001f\u007f-\u009f]/;

export function validConversationId(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value.trim());
}

function limit(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? Math.floor(value)
    : fallback;
}

export function parseDisplayPreferences(
  value: unknown,
): DisplayPreferences | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const source = value as {
    scoped?: unknown;
    archived?: unknown;
    titles?: unknown;
    limits?: unknown;
  };
  if (typeof source.scoped !== "boolean") {
    return null;
  }
  const archived: ArchivedConversation[] = [];
  const seen = new Set<string>();
  for (const entry of Array.isArray(source.archived) ? source.archived : []) {
    const item = (entry ?? {}) as {
      conversation_id?: unknown;
      archived_at_ms?: unknown;
    };
    if (!validConversationId(item.conversation_id)) {
      continue;
    }
    const id = item.conversation_id.trim().toLowerCase();
    if (seen.has(id)) {
      continue;
    }
    seen.add(id);
    archived.push({
      conversation_id: id,
      archived_at_ms:
        typeof item.archived_at_ms === "number" &&
        Number.isFinite(item.archived_at_ms)
          ? item.archived_at_ms
          : 0,
    });
  }
  const titles: Record<string, string> = {};
  const rawTitles =
    source.titles && typeof source.titles === "object"
      ? (source.titles as Record<string, unknown>)
      : {};
  for (const [id, title] of Object.entries(rawTitles)) {
    if (!validConversationId(id) || typeof title !== "string") {
      continue;
    }
    const checked = validateConversationTitle(title);
    if (!checked.ok) {
      continue;
    }
    titles[id.trim().toLowerCase()] = checked.value;
  }
  const limits = (source.limits ?? {}) as {
    archived?: unknown;
    titles?: unknown;
    title_chars?: unknown;
  };
  return {
    scoped: source.scoped,
    archived,
    titles,
    limits: {
      archived: limit(limits.archived, EMPTY_DISPLAY_PREFERENCES.limits.archived),
      titles: limit(limits.titles, EMPTY_DISPLAY_PREFERENCES.limits.titles),
      title_chars: limit(
        limits.title_chars,
        EMPTY_DISPLAY_PREFERENCES.limits.title_chars,
      ),
    },
  };
}

export type TitleCheck =
  | { ok: true; value: string }
  | { ok: false; reason: "empty" | "tooLong" | "control" };

/** Mirrors the host validator: trim, 1..=40 code points, no control chars. */
export function validateConversationTitle(raw: string): TitleCheck {
  const value = raw.trim();
  if (!value) {
    return { ok: false, reason: "empty" };
  }
  if (Array.from(value).length > MAX_TITLE_CHARS) {
    return { ok: false, reason: "tooLong" };
  }
  if (CONTROL.test(value)) {
    return { ok: false, reason: "control" };
  }
  return { ok: true, value };
}

export interface DisplayConversation {
  conversation_id: string;
  title: string | null;
}

/** The single display-title override layer used by the list and every selector. */
export function displayTitle<T extends DisplayConversation>(
  item: T,
  preferences: DisplayPreferences,
): string | null {
  return preferences.titles[item.conversation_id] ?? item.title;
}

export function mergeDisplayTitles<T extends DisplayConversation>(
  items: T[],
  preferences: DisplayPreferences,
): T[] {
  return items.map((item) => {
    const title = displayTitle(item, preferences);
    return title === item.title ? item : ({ ...item, title } as T);
  });
}

export function archivedIdSet(
  preferences: DisplayPreferences,
): Set<string> {
  return new Set(preferences.archived.map((entry) => entry.conversation_id));
}

export function isArchived(
  conversationId: string,
  preferences: DisplayPreferences,
): boolean {
  return preferences.archived.some(
    (entry) => entry.conversation_id === conversationId,
  );
}

export function visibleConversations<T extends DisplayConversation>(
  items: T[],
  preferences: DisplayPreferences,
): T[] {
  const archived = archivedIdSet(preferences);
  return items.filter((item) => !archived.has(item.conversation_id));
}

/** Newest first, only for the settings restore list. */
export function sortedArchived(
  preferences: DisplayPreferences,
): ArchivedConversation[] {
  return [...preferences.archived].sort(
    (left, right) => right.archived_at_ms - left.archived_at_ms,
  );
}

export function archiveLimitReached(
  preferences: DisplayPreferences,
  conversationId: string,
): boolean {
  return (
    !isArchived(conversationId, preferences) &&
    preferences.archived.length >= preferences.limits.archived
  );
}

export function titleLimitReached(
  preferences: DisplayPreferences,
  conversationId: string,
): boolean {
  return (
    preferences.titles[conversationId] === undefined &&
    Object.keys(preferences.titles).length >= preferences.limits.titles
  );
}

export type ConversationGroupKey =
  | "today"
  | "yesterday"
  | "week"
  | "older"
  | "unknown";

export interface ConversationGroup<T> {
  key: ConversationGroupKey;
  items: T[];
}

export interface SidebarModel<T> {
  groups: ConversationGroup<T>[];
  visibleCount: number;
  archivedCount: number;
  /** The host read failed; the list is unfiltered and says so. */
  notice: "unavailable" | null;
}

const GROUP_ORDER: ConversationGroupKey[] = [
  "today",
  "yesterday",
  "week",
  "older",
  "unknown",
];

function startOfDay(date: Date): number {
  return new Date(
    date.getFullYear(),
    date.getMonth(),
    date.getDate(),
  ).getTime();
}

/** Only the host-provided `updated_at`; nothing new is stored. */
export function conversationGroupKey(
  updatedAt: string,
  now: Date,
): ConversationGroupKey {
  const parsed = Date.parse(updatedAt);
  if (!Number.isFinite(parsed)) {
    return "unknown";
  }
  const days = Math.round(
    (startOfDay(now) - startOfDay(new Date(parsed))) / 86400000,
  );
  if (days <= 0) {
    return "today";
  }
  if (days === 1) {
    return "yesterday";
  }
  if (days < 7) {
    return "week";
  }
  return "older";
}

/** Host order is preserved inside a group; empty groups are omitted. */
export function groupConversations<T extends { updated_at: string }>(
  items: T[],
  now: Date,
): ConversationGroup<T>[] {
  const buckets = new Map<ConversationGroupKey, T[]>();
  for (const item of items) {
    const key = conversationGroupKey(item.updated_at, now);
    const bucket = buckets.get(key);
    if (bucket) {
      bucket.push(item);
    } else {
      buckets.set(key, [item]);
    }
  }
  return GROUP_ORDER.filter((key) => buckets.has(key)).map((key) => ({
    key,
    items: buckets.get(key) as T[],
  }));
}

function replyFailure(reply: unknown): DisplayPreferenceError {
  const value = reply as
    | { error?: { code?: unknown; message?: unknown } }
    | null
    | undefined;
  const code =
    typeof value?.error?.code === "string"
      ? value.error.code
      : "DISPLAY_PREFERENCE_FAILED";
  const message =
    typeof value?.error?.message === "string" ? value.error.message : "";
  return new DisplayPreferenceError(code, message);
}

async function readPreferences(
  invoke: InvokeFn,
  command: string,
  args?: Record<string, unknown>,
): Promise<DisplayPreferences> {
  const reply = (await invoke(command, args)) as
    | { ok?: unknown; result?: unknown }
    | null
    | undefined;
  if (!reply || reply.ok !== true) {
    throw replyFailure(reply);
  }
  const parsed = parseDisplayPreferences(reply.result);
  if (!parsed) {
    throw new DisplayPreferenceError(
      "INVALID_PAYLOAD",
      "会话显示偏好不可读",
    );
  }
  return parsed;
}

export function loadDisplayPreferences(
  invoke: InvokeFn,
): Promise<DisplayPreferences> {
  return readPreferences(invoke, "conversation_display_preferences");
}

export type DisplayAction =
  | "rename"
  | "clear_title"
  | "archive"
  | "unarchive"
  | "restore_all";

/** One closed-set write.  A rejected reply is thrown, never treated as success. */
export function setDisplayPreference(
  invoke: InvokeFn,
  action: DisplayAction,
  conversationId?: string,
  title?: string,
): Promise<DisplayPreferences> {
  const args: Record<string, unknown> = { action };
  if (conversationId !== undefined) {
    args.conversationId = conversationId;
  }
  if (title !== undefined) {
    args.title = title;
  }
  return readPreferences(invoke, "set_conversation_display_preference", args);
}
