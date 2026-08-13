import { describe, expect, it } from "vitest";
import { api, emptyDashboard } from "./api";
import { mockApi } from "./mockApi";
import { realApi } from "./realApi";

/* The seam is one line, which is what makes it easy to point back at the mock while debugging —
   and easy to leave pointed there. These are the two ways that would ship silently: the export
   still on `mockApi`, and the dashboard seeded with cases nobody's backend produced. */

describe("the integration seam", () => {
  it("points at the real backend", () => {
    expect(api).toBe(realApi);
    expect(api).not.toBe(mockApi);
  });

  it("starts empty rather than seeded with invented cases", async () => {
    const initial = emptyDashboard();
    expect(initial.cases).toEqual([]);
    expect(initial.run.id).toBe("");
    expect(initial.run.stage).toBe("IDLE");
  });

  it("carries no leftover mock values into the drawer's first paint", () => {
    // The mock's idle run shipped a plausible-looking idempotency key and evidence digest. On a
    // real backend those would be the first thing an opened drawer showed, and they would be
    // fiction.
    const { run } = emptyDashboard();
    expect(run.idempotencyKey).toBe("none yet");
    expect(run.actionIntent).toBe("none proposed");
    expect(run.policyDetail).toBe("pending");
    expect(run.verificationDetail).toBe("pending");
  });
});
