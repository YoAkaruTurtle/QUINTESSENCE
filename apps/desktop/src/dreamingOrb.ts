import { invoke } from "@tauri-apps/api/core";
import { dreamingDetails, dreamingLabel, dreamingStatus, type DreamingStatus } from "./dreamingStatus";
import type { HostReply } from "./types";
import "./dreamingOrb.css";

const orb = document.querySelector<HTMLElement>(".orb")!;
const handle = document.querySelector<HTMLElement>(".orb__status")!;
const label = document.querySelector<HTMLElement>(".orb__label")!;
const hide = document.querySelector<HTMLButtonElement>(".orb__hide")!;
const brand = document.querySelector<HTMLImageElement>(".orb__brand")!;
const showArtwork = (): void => {
  if (brand.naturalWidth) brand.parentElement?.setAttribute("data-branded", "");
};
brand.addEventListener("load", showArtwork, { once: true });
if (brand.complete) showArtwork();
let current = dreamingStatus(null, "zh-CN", "dark");
let closed = false;

function render(status: DreamingStatus): void {
  current = status;
  const english = status.language === "en";
  const text = dreamingLabel(status);
  document.documentElement.lang = status.language;
  document.documentElement.dataset.theme = status.theme;
  orb.dataset.state = status.state;
  orb.dataset.degraded = String(Boolean(status.semantic_reason));
  const details = dreamingDetails(status);
  const idle = status.state === "waiting" || status.state === "disabled";
  const visibleText = status.semantic_reason && idle
    ? english ? "Recall limited" : "召回已降级"
    : text;
  handle.title = [text, ...details, english ? "Drag to move" : "拖动悬浮球"].join(" · ");
  if (label.textContent !== visibleText) label.textContent = visibleText;
  const hideText = english ? "Hide Dreaming orb" : "隐藏悬浮球";
  hide.setAttribute("aria-label", hideText);
  hide.title = hideText;
}

async function readStatus(): Promise<void> {
  try {
    const reply = await invoke<HostReply>("dreaming_status");
    render(dreamingStatus(reply.ok ? reply.result : null, current.language, current.theme));
  } catch {
    render(dreamingStatus(null, current.language, current.theme));
  }
  if (!closed) window.setTimeout(readStatus, 750);
}

handle.addEventListener("pointerdown", (event) => {
  if (event.button !== 0) return;
  void invoke("dreaming_orb_drag").catch(() => undefined);
});

function hideOrb(): void {
  void invoke("dreaming_orb_hide").catch(() => undefined);
}
hide.addEventListener("click", hideOrb);
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") hideOrb();
});
window.addEventListener("pagehide", () => { closed = true; }, { once: true });

render(current);
void readStatus();
