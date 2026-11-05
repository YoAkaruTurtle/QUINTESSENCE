/**
 * RG08: ordered assistant bubbles and the restrained reveal state.
 *
 * The host already stores and returns the model's explicit ordered messages.
 * This module never splits a reply on punctuation, newlines or length: a
 * legacy single `text` is exactly one bubble.  The animation tracker is
 * DOM-free so the "play once per bubble, never replay on a poll re-render"
 * rule is testable without a browser.
 */

export const MAX_REPLY_BUBBLES = 4;

/** The whole stagger stays under a fifth of a second; it is not a fake wait. */
export const BUBBLE_STAGGER_MS = 55;

export const BUBBLE_REVEAL_CLASS = "bubble-reveal";

export interface BubbleSource {
  message_id?: string | null;
  text: string | null;
  messages?: string[] | null;
  redacted?: boolean;
}

/**
 * The ordered bubbles of one assistant message.
 *
 * Explicit host `messages` win when they are a plausible 1..4 string list;
 * otherwise the legacy `text` is one bubble.  A redacted message has no body
 * and is rendered by the existing redaction path instead.
 */
export function replyBubbles(message: BubbleSource): string[] {
  if (message.redacted) {
    return [];
  }
  const explicit = message.messages;
  if (
    Array.isArray(explicit) &&
    explicit.length >= 1 &&
    explicit.length <= MAX_REPLY_BUBBLES &&
    explicit.every(
      (value) => typeof value === "string" && value.trim().length > 0,
    )
  ) {
    return [...explicit];
  }
  return message.text !== null && message.text !== "" ? [message.text] : [];
}

/** The presentation delay of one bubble; no request waits on this value. */
export function bubbleDelayMs(index: number): number {
  const bounded = Math.max(0, Math.min(index, MAX_REPLY_BUBBLES - 1));
  return bounded * BUBBLE_STAGGER_MS;
}

export function bubbleRevealKey(
  messageId: string | null | undefined,
  index: number,
): string {
  return `${messageId ?? "anonymous"}:${index}`;
}

/**
 * Only the newest turn animates.  History that was already on screen keeps
 * its settled layout, which keeps polling re-renders quiet.
 */
export function shouldRevealTurn(
  messageTurnId: string,
  newestTurnId: string | null,
): boolean {
  return newestTurnId !== null && messageTurnId === newestTurnId;
}

/** Claims each bubble key exactly once; a reset is only for a switch. */
export class BubbleRevealTracker {
  private readonly seen = new Set<string>();

  claim(key: string): boolean {
    if (this.seen.has(key)) {
      return false;
    }
    this.seen.add(key);
    return true;
  }

  reset(): void {
    this.seen.clear();
  }

  get size(): number {
    return this.seen.size;
  }
}
