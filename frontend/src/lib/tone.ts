import type { CareCase, EventTone } from "@/types/domain";

/** One tone per case, derived from state. Color never carries meaning alone —
 *  every state also carries a word and a distinct mark shape. */
export function caseTone(c: CareCase): string {
  if (c.mismatch) return "var(--state-denied)";
  if (c.section === "needs") return "var(--state-decision)";
  if (c.section === "done") return "var(--state-verified)";
  return "var(--brand-accent)";
}

export function eventToneColor(tone: EventTone): string {
  switch (tone) {
    case "verified":
      return "var(--state-verified)";
    case "denied":
      return "var(--state-denied)";
    case "decision":
      return "var(--state-decision)";
    case "untrusted":
      return "var(--state-untrusted)";
    default:
      return "inherit";
  }
}

export function greeting(now: Date = new Date()): string {
  const h = now.getHours();
  if (h < 12) return "Good morning, Maya.";
  if (h < 18) return "Good afternoon, Maya.";
  return "Good evening, Maya.";
}

/** Before the first read has answered, the page may not claim anything about the world. */
export function connectingHero(): { line: string; sub: string } {
  return { line: "Connecting to CareLoop…", sub: "Your dashboard will appear in a moment." };
}

/** The backend cannot be read. Saying so replaces the calm success line entirely: "Nothing needs
 *  you right now." on an unreachable backend would be a confident falsehood, and this product's
 *  one claim is that what is on screen is true. */
export function unreachableHero(showingLastKnown: boolean): { line: string; sub: string } {
  return {
    line: "CareLoop can't be reached.",
    sub: showingLastKnown
      ? "Retrying automatically — the cases below are the last state this page saw."
      : "Retrying automatically. Nothing can be shown until the connection returns."
  };
}

export function heroLine(needsCount: number, isManualFollowUp: boolean): string {
  if (needsCount === 0) return "Nothing needs you right now.";
  if (needsCount === 1) return isManualFollowUp ? "One case needs a person." : "One decision is waiting.";
  return needsCount + " decisions are waiting.";
}

export function heroSub(needsCount: number, workingCount: number, doneCount: number): string {
  if (needsCount > 0) {
    return "Everything else is moving without you — " + workingCount + " in progress, " + doneCount + " closed.";
  }
  if (workingCount > 0) {
    return (
      "CareLoop is quietly handling " + workingCount + (workingCount === 1 ? " case" : " cases") + " in the background."
    );
  }
  return "Nothing open. CareLoop will pick up the next thing that arrives.";
}
