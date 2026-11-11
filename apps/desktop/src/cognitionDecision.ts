/** Decision feedback is independent of list refresh errors. */
import type { AppInfo, HostReply } from "./types";

/** Chat cognition has its own host capability, independent of managed work. */
export function cognitionAvailable(info: AppInfo | null): boolean {
  return Boolean(info?.host.ready && info.capabilities.cognition?.enabled);
}

export interface CognitionDecisionState {
  busy: string | null;
  decisionError: string | null;
  notice: string | null;
}

export async function runCognitionDecision(
  state: CognitionDecisionState,
  candidateId: string,
  decision: "approve" | "reject",
  submit: () => Promise<HostReply>,
  refresh: () => Promise<void>,
  render: () => void,
  errorText: (error: unknown) => string,
  failureText = "提交决定失败",
  noticeText?: (decision: "approve" | "reject") => string,
): Promise<void> {
  if (state.busy) return;
  state.busy = candidateId;
  state.decisionError = null;
  state.notice = null;
  render();
  try {
    const reply = await submit();
    if (!reply.ok) {
      state.decisionError = reply.error?.message ?? failureText;
    } else {
      state.notice = noticeText
        ? noticeText(decision)
        : decision === "approve"
          ? "已提交批准；以实际状态为准。"
          : "已提交拒绝；以实际状态为准。";
    }
  } catch (error) {
    state.decisionError = errorText(error);
  }
  try {
    await refresh();
  } finally {
    state.busy = null;
    render();
  }
}
