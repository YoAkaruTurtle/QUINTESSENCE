/** Local presentation preferences; no model or instance business data. */
export interface Appearance {
  light_accent: string | null;
  light_background: string | null;
  wallpaper: string | null;
  assistant_avatar: string | null;
  user_avatar: string | null;
}
export const DEFAULT_APPEARANCE: Appearance = {
  light_accent: null, light_background: null, wallpaper: null,
  assistant_avatar: null, user_avatar: null,
};
export function validColor(value: unknown): value is string {
  return typeof value === "string" && /^#[0-9a-f]{6}$/i.test(value);
}
export function foreground(background: string): string {
  const rgb = [1, 3, 5].map(i => parseInt(background.slice(i, i + 2), 16) / 255)
    .map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4);
  const luminance = rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
  return (luminance + .05) / .05 >= 1.05 / (luminance + .05) ? "#111111" : "#ffffff";
}
export function displayImage(value: unknown): string | null {
  return typeof value === "string" && value.length <= 3_000_000 &&
    /^data:image\/(?:png|jpeg);base64,[A-Za-z0-9+/]+={0,2}$/.test(value) ? value : null;
}
export function applyAppearance(value: Appearance, theme: string): void {
  const root = document.documentElement;
  const accent = validColor(value.light_accent) ? value.light_accent : "#a82e40";
  const background = validColor(value.light_background) ? value.light_background : "#f5f4f1";
  root.style.setProperty("--light-accent", accent);
  root.style.setProperty("--light-background", background);
  root.style.setProperty("--light-ink", foreground(background));
  root.style.setProperty("--light-surface", foreground(background) === "#111111" ? "#ffffff" : `color-mix(in srgb, ${background} 90%, white)`);
  root.style.setProperty("--light-on-accent", foreground(accent));
  root.style.setProperty("--chat-wallpaper", displayImage(value.wallpaper) ? `url("${value.wallpaper}")` : "none");
  root.dataset.wallpaper = String(Boolean(displayImage(value.wallpaper)));
  for (const role of ["assistant", "user"] as const) {
    const image = displayImage(value[`${role}_avatar`]);
    root.style.setProperty(`--${role}-avatar`, image ? `url("${image}")` : "none");
    root.dataset[`${role}Avatar`] = String(Boolean(image));
  }
  root.dataset.theme = theme;
}

/** Decode and re-encode user-selected raster files. Strip metadata, bound size. */
export async function importDisplayImage(file: File, avatar: boolean): Promise<string> {
  if (!["image/jpeg", "image/png", "image/webp"].includes(file.type) || file.size > 15_000_000) {
    throw new Error("请选择不超过 15 MB 的 PNG、JPEG 或 WebP 图片");
  }
  const source = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error("无法读取图片"));
    reader.readAsDataURL(file);
  });
  const image = new Image();
  image.src = source;
  await image.decode().catch(() => { throw new Error("图片无法解码"); });
  const canvas = document.createElement("canvas");
  const limit = avatar ? 320 : 1920;
  if (avatar) {
    canvas.width = canvas.height = 320;
  } else {
    const scale = Math.min(1, limit / Math.max(image.naturalWidth, image.naturalHeight));
    canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
  }
  const context = canvas.getContext("2d");
  if (!context) throw new Error("无法处理图片");
  if (avatar) {
    const side = Math.min(image.naturalWidth, image.naturalHeight);
    context.drawImage(image, (image.naturalWidth - side) / 2, (image.naturalHeight - side) / 2, side, side, 0, 0, 320, 320);
  } else context.drawImage(image, 0, 0, canvas.width, canvas.height);
  const result = canvas.toDataURL("image/jpeg", .85);
  if (result.length > (avatar ? 400_000 : 3_000_000)) throw new Error("图片过大，请选择更小的图片");
  return result;
}
