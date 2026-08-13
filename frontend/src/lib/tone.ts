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
