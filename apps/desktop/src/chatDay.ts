/** Local calendar helpers for chat day separators.  No host state is involved. */

function dayKey(date: Date): string {
  return [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0"),
  ].join("-");
}

export function chatDayKey(value: string | null | undefined): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return dayKey(date);
}

export function chatDayLabel(
  value: string,
  now: Date = new Date(),
  locale: string = "zh-CN",
): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const current = dayKey(date);
  const today = dayKey(now);
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  const chinese = locale.toLowerCase().startsWith("zh");
  if (current === today) return chinese ? "今天" : "Today";
  if (current === dayKey(yesterday)) return chinese ? "昨天" : "Yesterday";
  return date.toLocaleDateString(locale, {
    month: "short",
    day: "numeric",
    ...(date.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }),
  });
}
