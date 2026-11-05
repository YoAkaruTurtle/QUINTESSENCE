import { el } from "./dom";
import { s } from "./shellText";
import type { SessionOverview } from "./sessionOverview";
import type { TurnTrace, UsageAttempt, UsageContext } from "./usage";

export interface CandidateOverview { configured: boolean; loaded: boolean; loading: boolean; error: string | null; human: number; other: number }
function conversationName(model: SessionOverview): string {
  if (!model.id) return s("noSelection");
  return model.title ?? `${s("conversation")}${model.createdAt ? " · " + new Date(model.createdAt).toLocaleString(document.documentElement.lang, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : ""}`;
}
function card(title: string, scope: string, content: HTMLElement[], action?: HTMLButtonElement): HTMLElement {
  return el("section", { class: "dashboard-card", children: [
    el("div", { class: "dashboard-card-head", children: [el("h2", { text: title }), el("span", { class: "scope-badge", text: scope })] }),
    ...content, action,
  ] });
}
function action(text: string, run: () => void): HTMLButtonElement {
  const button = el("button", { class: "text-action", text: `${text} ↗`, attrs: { type: "button" } });
  button.onclick = run; return button;
}
function ring(value: number | null, label: string, count: string): HTMLElement {
  const frame = el("div", { class: "metric-ring", attrs: { role: "img", "aria-label": `${label}: ${count}` } });
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 120 120"); svg.setAttribute("aria-hidden", "true");
  for (const active of [false, true]) {
    const circle = document.createElementNS(svg.namespaceURI, "circle");
    circle.setAttribute("cx", "60"); circle.setAttribute("cy", "60"); circle.setAttribute("r", "48");
    circle.setAttribute("class", active ? "ring-value" : "ring-track");
    if (active) { circle.setAttribute("stroke-dasharray", `${(value ?? 0) * 301.6} 301.6`); circle.setAttribute("transform", "rotate(-90 60 60)"); }
    svg.append(circle);
  }
  frame.append(svg, el("div", { class: "ring-caption", children: [el("strong", { text: count }), el("span", { text: label })] }));
  return frame;
}

/** Unknown is never rendered as zero: a missing number says so. */
function tokenValue(model: SessionOverview): string {
  if (!model.id) return "—";
  if (model.noRequests) return "0";
  const usage = model.usage;
  if (!usage) return s("unknown");
  if (usage.attempts_total === 0) return s("unknown");
  const tokens = usage.tokens;
  if (!tokens.complete || tokens.total === null) return s("unknown");
  return String(tokens.total);
}
function tokenHelp(model: SessionOverview): string {
  const usage = model.usage;
  if (!usage || usage.attempts_total === 0) return s("usageHelp");
  return `${s("usageHelp")} ${usage.attempts_known}/${usage.attempts_total} ${s("requestsSettled")}`;
}
function speedValue(model: SessionOverview): string {
  if (!model.id) return "—";
  if (model.noRequests) return s("noRequests");
  const latest = model.usage?.latest_request ?? null;
  if (latest === null || latest.tokens_per_second === null) return s("unknown");
  return `${latest.tokens_per_second.toFixed(1)} tok/s`;
}
function contextValue(model: SessionOverview): string {
  if (!model.id) return "—";
  if (model.noRequests) return s("noRequests");
  const latest = model.usage?.latest_context ?? null;
  if (latest === null || latest.bytes === null) return s("unknown");
  return `${latest.bytes.used_bytes} / ${latest.bytes.limit_bytes} B`;
}

export function metricsSummary(model: SessionOverview): HTMLElement {
  const scope = conversationName(model);
  const summary = el("div", { class: "session-summary", children: [
    el("div", { class: "summary-title", children: [el("span", { text: s("scope") }), el("strong", { text: scope })] }),
    el("div", { class: "summary-grid", children: [
      [s("tokens"), tokenValue(model), tokenHelp(model)],
      [s("speed"), speedValue(model), s("speedHelp")],
      [s("context"), contextValue(model), s("contextHelp")],
    ].map(([label, number, help]) => el("div", { class: "summary-metric", children: [el("span", { text: label }), el("strong", { text: number }), el("small", { text: help })] })) }),
  ] });
  return summary;
}

function attemptLine(attempt: UsageAttempt): string {
  const parts: string[] = [`#${attempt.attempt_no} ${attempt.stage}`, attempt.outcome ?? attempt.status];
  if (attempt.usage_known && attempt.total_tokens !== null) parts.push(`${attempt.total_tokens} tok`);
  if (attempt.duration_ms !== null) parts.push(`${attempt.duration_ms} ms`);
  if (attempt.tokens_per_second !== null) parts.push(`${attempt.tokens_per_second.toFixed(1)} tok/s`);
  if (attempt.repair === true) parts.push(`${s("repairTag")}(${attempt.repair_reason ?? s("unknown")})`);
  else if (attempt.kind === "transport_retry") parts.push(s("transportRetry"));
  else if (attempt.repair === null) parts.push(s("unmarkedAttempt"));
  return parts.join(" · ");
}

function contextLine(context: UsageContext): string {
  const bytes = context.bytes === null ? s("unknown") : `${context.bytes.used_bytes}/${context.bytes.limit_bytes} B`;
  return `${context.purpose} · ${bytes}`;
}

function usageCard(model: SessionOverview): HTMLElement {
  const usage = model.usage;
  if (!model.id) return card(s("usage"), s("scope"), [el("div", { class: "metric-number", text: "—" }), el("p", { class: "card-note", text: s("noSelection") })]);
  if (model.noRequests) return card(s("usage"), s("scope"), [el("div", { class: "metric-number", text: "0" }), el("p", { class: "card-note", text: s("noRequests") })]);
  if (usage === null) return card(s("usage"), s("scope"), [el("div", { class: "metric-number", text: "…" }), el("p", { class: "card-note", text: model.usageBusy ? s("runningDetail") : model.usageError ? s("loadError") : s("notRead") })]);
  if (usage.attempts_total === 0) {
    return card(s("usage"), s("scope"), [el("div", { class: "metric-number", text: s("unknown") }), el("p", { class: "card-note", text: s("noRequestRecords") })]);
  }
  return card(s("usage"), s("scope"), [
    el("div", { class: "metric-number", text: usage.tokens.complete && usage.tokens.total !== null ? String(usage.tokens.total) : s("unknown") }),
    el("div", { class: "chart-legend", children: [
      el("div", { children: [el("span", { text: s("attemptsLabel") }), el("strong", { text: String(usage.attempts_total) })] }),
      el("div", { children: [el("span", { text: s("repairsLabel") }), el("strong", { text: String(usage.repairs) })] }),
      el("div", { children: [el("span", { text: s("settledLabel") }), el("strong", { text: `${usage.attempts_known}/${usage.attempts_total}` })] }),
    ] }),
    el("p", { class: "card-note", text: model.usageError ? s("loadError") : usage.tokens.complete ? s("usageHelp") : `${s("usageHelp")} ${s("partialUsage")}` }),
  ]);
}

export function traceSection(trace: TurnTrace | null, state: "loading" | "ready" | "busy" | "error"): HTMLElement {
  const section = el("div", { class: "process-detail" });
  section.appendChild(el("div", { class: "process-label", text: s("executionDetail") }));
  if (state === "busy") { section.appendChild(el("div", { class: "process-note", text: s("runningDetail") })); return section; }
  if (state === "loading") { section.appendChild(el("div", { class: "process-note", text: s("notRead") })); return section; }
  if (state === "error" || trace === null) { section.appendChild(el("div", { class: "process-note", text: s("loadError") })); return section; }
  for (const stage of trace.stages) {
    const reason = stage.skip_reason ? ` · ${stage.skip_reason}` : "";
    section.appendChild(el("div", { class: "process-stage", text: `${stage.kind === "content" ? s("stageContent") : s("stageExpression")} · ${stage.status}${reason}` }));
    for (const context of stage.contexts) {
      const block = el("div", { class: "process-context" });
      block.appendChild(el("div", { class: "process-note", text: contextLine(context) }));
      for (const attempt of context.attempts) block.appendChild(el("div", { class: "process-attempt", text: attemptLine(attempt) }));
      section.appendChild(block);
    }
    if (stage.tool_steps > 0) section.appendChild(el("div", { class: "process-note", text: `${s("toolStepsLabel")} ${stage.tool_steps}` }));
  }
  const counts = trace.counts;
  const unbound = counts.repairs_unbound > 0 ? ` · ${s("unboundRepairsLabel")} ${counts.repairs_unbound}` : "";
  section.appendChild(el("div", { class: "process-note", text: `${s("attemptsLabel")} ${trace.attempts_total} · ${s("repairsLabel")} ${counts.repairs} · ${s("unmarkedLabel")} ${counts.repair_unknown}${unbound} · ${s("toolStepsLabel")} ${counts.tool_steps}` }));
  const final = trace.final;
  if (final === null || final.source === "unknown") {
    section.appendChild(el("div", { class: "process-note", text: `${s("finalSource")} · ${s("unknown")}` }));
  } else {
    const fallback = final.fallback ? ` · ${s("fallbackToDraft")}${final.fallback_reason ? `(${final.fallback_reason})` : ""}` : "";
    section.appendChild(el("div", { class: "process-note", text: `${s("finalSource")} · ${final.source === "expression" ? s("stageExpression") : s("stageContent")}${fallback}` }));
  }
  if (trace.truncated) section.appendChild(el("div", { class: "process-note", text: s("truncatedDetail") }));
  return section;
}

export function dashboard(model: SessionOverview, cognition: CandidateOverview, actions: { settings(): void; review(): void; refresh(): void; details(): void }): HTMLElement {
  const page = el("div", { class: "sol-page dashboard-page" });
  page.append(el("div", { class: "page-heading", children: [
    el("div", { children: [el("div", { class: "eyebrow", text: s("overview") }), el("h1", { text: s("boardTitle") }), el("p", { class: "page-subtitle", text: s("boardSub") })] }),
    el("div", { class: "dashboard-actions", children: [action(s("details"), actions.details), action(s("refresh"), actions.refresh)] }),
  ] }));
  page.append(el("div", { class: "page-scope", text: `${s("scope")} · ${conversationName(model)}` }));
  const grid = el("div", { class: "dashboard-grid" });
  grid.append(card(s("connection"), s("instance"), [
    el("div", { class: `connection-visual ${model.hostReady ? "ready" : ""}`, children: [el("span", { class: "connection-symbol", text: "↗" }), el("div", { children: [el("div", { class: "metric-number", text: model.hostReady ? s("hostReady") : s("hostOffline") }), el("p", { class: "muted", text: model.model ?? s("notRead") })] })] }),
    el("div", { class: "status-caption", text: s("unverified") }), el("p", { class: "card-note", text: s("connectionHelp") }),
  ], action(s("openSettings"), actions.settings)));
  const tasks = model.tasks;
  const total = tasks?.filter(item => !["not_configured", "not_eligible", "obsolete"].includes(item.state)).reduce((sum, item) => sum + item.count, 0) ?? 0;
  const delivered = tasks?.find(item => item.state === "delivered")?.count ?? 0;
  const groups = [
    [s("delivered"), delivered],
    [s("pending"), tasks?.filter(item => ["pending", "outcome_unknown"].includes(item.state)).reduce((n, item) => n + item.count, 0) ?? 0],
    [s("attention"), tasks?.filter(item => ["needs_review", "blocked"].includes(item.state)).reduce((n, item) => n + item.count, 0) ?? 0],
    [s("excluded"), tasks?.filter(item => ["not_configured", "not_eligible", "obsolete"].includes(item.state)).reduce((n, item) => n + item.count, 0) ?? 0],
  ] as const;
  grid.append(card(s("memory"), s("scope"), [
    el("div", { class: "visual-row", children: [ring(total ? delivered / total : null, s("delivered"), total ? `${delivered}/${total}` : "—"), el("div", { class: "chart-legend", children: groups.map(([label, n]) => el("div", { children: [el("span", { text: label }), el("strong", { text: tasks ? String(n) : "—" })] })) })] }),
    el("p", { class: "card-note", text: !model.id ? s("noSelection") : `${s("memoryHelp")} ${model.taskCoverage}/${model.loadedTurns}` }),
  ]));
  const max = Math.max(cognition.human, cognition.other, 1);
  grid.append(card(s("cognition"), s("instance"), [
    el("div", { class: "candidate-chart", children: [[s("human"), cognition.human], [s("otherCandidates"), cognition.other]].map(([label, count]) => {
      const bar = el("i"); bar.style.width = `${cognition.loaded ? Number(count) / max * 100 : 0}%`;
      return el("div", { class: "chart-bar-row", children: [el("span", { text: String(label) }), el("div", { class: "chart-bar", children: [bar] }), el("strong", { text: cognition.loaded ? String(count) : "—" })] });
    }) }),
    el("p", { class: "card-note", text: !cognition.configured ? s("unavailable") : cognition.error ? s("loadError") : cognition.loaded ? s("cognitionHelp") : s("notRead") }),
  ], action(s("review"), actions.review)));
  grid.append(usageCard(model));
  page.append(grid); return page;
}
