/** A small, local-only avatar lightbox shared by chat and settings. */
import { el } from "./dom";
import { uiText } from "./locale";

export interface AvatarPreviewOptions {
  kind: "assistant" | "user";
  label: string;
  letter: string;
  /** Raw data URL. `undefined` reads the currently applied avatar; null means default. */
  image?: string | null;
  trigger?: HTMLElement | null;
}

function appliedImage(kind: "assistant" | "user"): string {
  const root = document.documentElement;
  const custom = kind === "assistant"
    ? root.dataset.assistantAvatar === "true"
    : root.dataset.userAvatar === "true";
  if (custom) {
    return getComputedStyle(root).getPropertyValue(`--${kind}-avatar`).trim();
  }
  return kind === "assistant" ? 'url("/brand.png")' : "";
}

export function openAvatarPreview(options: AvatarPreviewOptions): void {
  if (document.querySelector(".avatar-preview-dialog")) return;
  const image = options.image === undefined
    ? appliedImage(options.kind)
    : options.image
      ? `url("${options.image}")`
      : options.kind === "assistant" ? 'url("/brand.png")' : "";
  const face = el("div", {
    class: `chat-avatar avatar-preview-face is-${options.kind}`,
    text: options.letter,
    attrs: { "aria-hidden": "true" },
  });
  if (image) {
    face.style.backgroundImage = image;
    face.style.fontSize = "0";
  }
  const dialog = el("dialog", {
    class: "avatar-preview-dialog",
    attrs: { "aria-labelledby": "avatar-preview-title" },
    children: [
      el("div", {
        class: "avatar-preview-card",
        children: [
          face,
          el("strong", { text: options.label, attrs: { id: "avatar-preview-title" } }),
          el("span", { class: "muted", text: uiText("点击空白处或按 Esc 关闭") }),
        ],
      }),
    ],
  });
  const close = el("button", {
    class: "quiet-button avatar-preview-close",
    text: uiText("关闭"),
    attrs: { type: "button", "aria-label": uiText("关闭头像预览") },
  });
  dialog.querySelector(".avatar-preview-card")?.appendChild(close);
  close.onclick = () => dialog.close();
  dialog.addEventListener("click", event => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("close", () => {
    dialog.remove();
    if (options.trigger?.isConnected) {
      options.trigger.focus({ preventScroll: true });
    }
  }, { once: true });
  document.body.append(dialog);
  dialog.showModal();
  close.focus();
}
