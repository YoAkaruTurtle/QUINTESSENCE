/** Public tool-step status labels, shared by the process panel and tests. */

const TOOL_STATUS_LABELS: Record<string, string> = {
  planned: "已计划",
  succeeded: "成功",
  failed: "失败",
  running: "运行中",
  started: "运行中",
  cancelled: "已取消",
  rejected: "已拒绝",
  error: "失败",
};

/**
 * A missing status is not evidence that the tool is running: only a real
 * `tool_started` event or a `running` step may say that.  `tool_planned` and a
 * permission refusal therefore render as planned/rejected, never running.
 */
export function toolStatusLabel(status: string | undefined): string {
  if (!status) {
    return "已计划";
  }
  return TOOL_STATUS_LABELS[status] ?? status;
}
