import { describe, expect, it } from "vitest";
import { greeting } from "./tone";

/* The greeting names nobody, and this is the guard that keeps it that way.
 *
 * It used to say "Good morning, Maya." — the last invented identity in the product, on the first
 * line of the first screen, directly above an `IdentityStrip` that exists to say the demo caregiver
 * is synthetic. In production it would have greeted every real caregiver by a name that was not
 * theirs.
 *
 * Asserted as a property over the whole day rather than as three string equalities, because the
 * failure this guards against is somebody adding a fourth branch — or a name — to one of them. */

/** Every hour of the day, so no branch of the greeting escapes the sweep. */
const HOURS = Array.from({ length: 24 }, (_, hour) => hour);

function at(hour: number): string {
  return greeting(new Date(2026, 7, 13, hour));
}

describe("the dashboard greeting", () => {
  it("addresses nobody, at any hour", () => {
    for (const hour of HOURS) {
      const said = at(hour);
      // No comma-and-a-name, which is the shape the invented identity took and the shape any
      // replacement would take. A greeting with no vocative cannot be wrong about anybody.
      expect(said, `hour ${hour}`).not.toMatch(/,/);
      expect(said, `hour ${hour}`).not.toMatch(/Maya/i);
    }
  });

  it("is still warm and time-aware", () => {
    // The fix removed a name, not the tone. Three distinct greetings across the day, each a
    // complete sentence — a bare "Dashboard" would have been a different and worse change.
    expect(at(9)).toBe("Good morning.");
    expect(at(14)).toBe("Good afternoon.");
    expect(at(21)).toBe("Good evening.");
    expect(new Set(HOURS.map(at)).size).toBe(3);
  });

  it("says something for every hour", () => {
    for (const hour of HOURS) {
      expect(at(hour), `hour ${hour}`).toMatch(/^Good (morning|afternoon|evening)\.$/);
    }
  });
});
