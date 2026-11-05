import { el } from "./dom";

/** Small finite choices are always visible; no platform popup or hidden menu. */
export class ChoiceControl {
  readonly element = el("div", { class: "choice-group", attrs: { role: "radiogroup" } });
  private selected = "";
  private blocked = false;
  private buttons: HTMLButtonElement[] = [];
  constructor(private options: [string, string][], value: string) {
    this.element.style.setProperty("--choice-count", String(options.length));
    this.buttons = options.map(([id, label], index) => {
      const button = el("button", { class: "choice-option", text: label, attrs: { type: "button", role: "radio" } });
      button.onclick = () => { this.value = id; };
      button.onkeydown = event => {
        const offset = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1 : event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 0;
        if (!offset && event.key !== "Home" && event.key !== "End") return;
        event.preventDefault();
        const next = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1 : (index + offset + options.length) % options.length;
        this.value = options[next][0]; this.buttons[next].focus();
      };
      this.element.append(button);
      return button;
    });
    this.value = value;
  }
  get value(): string { return this.selected; }
  set value(value: string) {
    this.selected = this.options.some(([id]) => id === value) ? value : "";
    this.element.style.setProperty("--choice-index", String(Math.max(0, this.options.findIndex(([id]) => id === this.selected))));
    this.element.dataset.unselected = String(!this.selected);
    this.buttons.forEach((button, i) => {
      const active = this.options[i][0] === this.selected;
      button.setAttribute("aria-checked", String(active));
      button.tabIndex = active || (!this.selected && i === 0) ? 0 : -1;
    });
  }
  set selectedIndex(index: number) { this.value = this.options[index]?.[0] ?? ""; }
  get disabled(): boolean { return this.blocked; }
  set disabled(value: boolean) {
    this.blocked = value;
    this.element.setAttribute("aria-disabled", String(value));
    this.buttons.forEach(button => { button.disabled = value; });
  }
}
