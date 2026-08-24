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

/** The time-of-day line above the dashboard. Warm, and about nobody.
 *
 *  It used to name a caregiver — "Good morning, Maya." — which was the last invented identity on
 *  the product, and the most prominent: the first line of the first screen, above an
 *  `IdentityStrip` whose entire job is to say the demo caregiver is synthetic. In production it
 *  would have greeted every real caregiver by somebody else's name.
 *
 *  It is **not** replaced by a real name, and that is deliberate rather than a shortcut. A greeting
 *  fed from an identity would need one on every render — before `GET /api/auth` has answered, in
 *  demo mode where there is no verified principal to draw from, and on a production page whose
 *  token has expired — and every one of those needs a placeholder, which is how an invented name
 *  gets reintroduced. A greeting that addresses nobody cannot be wrong about anybody. */
export function greeting(now: Date = new Date()): string {
  const h = now.getHours();
  if (h < 12) return "Good morning.";
  if (h < 18) return "Good afternoon.";
  return "Good evening.";
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

/** Every world this demo holds is in use.
 *
 *  Not `unreachableHero`, and the difference is the whole point of having it. The backend is up and
 *  answering; what it has run out of is worlds to hand out, because each visitor gets their own case
 *  store and their own clinic and there are sixty-four of them. "CareLoop can't be reached" would be
 *  a false statement about a healthy service, and it would send whoever is reading it to refresh —
 *  which is the one thing that does not help. Waiting does.
 *
 *  `showingLastKnown` is carried for `unreachableHero`'s reason: a page that already has cases on it
 *  is showing something real and stale, and a page that has none has nothing to explain itself. */
export function atCapacityHero(showingLastKnown: boolean): { line: string; sub: string } {
  return {
    line: "The demo is full right now.",
    sub: showingLastKnown
      ? "Every world is in use — the cases below are the last state this page saw. Try again in a few minutes."
      : "Every world is in use. They free up as visitors finish; try again in a few minutes."
  };
}

/** Between the press and the first case.
 *
 *  Measured on the deployed demo: pressing a scenario navigated home in 67ms and then showed
 *  "Nothing needs you right now. / Nothing open." for 0.9s on the referral and **2.1s** on the
 *  injection scenario — the one a judge is most likely to press. The button that starts the demo
 *  showed nothing happening first.
 *
 *  That is the same defect the connection states exist to prevent: a calm line asserted over a
 *  world the page has not read yet. Here the page has read it, and the claim is wrong for the
 *  other reason — work was just asked for, so "nothing open" is stale rather than unknown. The
 *  five locked hero lines are untouched; this is a new state getting its own words, exactly as
 *  `connectingHero` and `unreachableHero` did. */
export function startingHero(): { line: string; sub: string } {
  return {
    line: "Starting a scenario…",
    sub: "CareLoop is opening the case. It will appear here in a moment."
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
