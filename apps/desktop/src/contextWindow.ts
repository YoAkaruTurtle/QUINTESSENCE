import type { UsageSummary } from "./usage";

/** Official DeepSeek model metadata baseline; unknown providers remain unknown. */
export function modelWindow(provider: string | null, model: string | null): number | null {
  if (provider?.toLowerCase() !== "deepseek") return null;
  return model && ["deepseek-flash", "deepseek-v4-pro"].includes(model) ? 1_048_576 : null;
}

/** Most recent content input measurement, never the sum of billed requests. */
export function contextMeasurement(summary: UsageSummary | null, capacity: number | null): {
  used: number | null; capacity: number | null; ratio: number | null;
} {
  const attempt = summary?.latest_content_request;
  const prompt = attempt?.prompt_tokens;
  const used = attempt?.usage_known && typeof prompt === "number" && Number.isFinite(prompt) && prompt >= 0 ? prompt : null;
  return { used, capacity, ratio: used !== null && capacity !== null && capacity > 0 ? used / capacity : null };
}
