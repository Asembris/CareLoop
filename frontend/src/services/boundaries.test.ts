import { afterEach, describe, expect, it, vi } from "vitest";
import { RealCareLoopApi } from "./realApi";
import { mockApi } from "./mockApi";
import type { BoundariesView } from "./backend";

/* The adapter half of My Boundaries: one read, no polling, no invention.
 *
 * What is asserted here is transport and *restraint*. The statement's contents are derived and
 * proven in `careloop/policy/boundaries.py` and `tests/test_boundaries.py`; the risk on this side is
 * that the frontend quietly starts computing, defaulting or remembering some of it — which would
 * reinstate the second source of truth the whole feature exists to delete. So the sharp assertions
 * are that the wire shape arrives untouched, that a failure resolves `null` rather than anything
 * plausible, and that nothing is cached across reads. */

const STATEMENT: BoundariesView = {
  capabilities: [
    {
      tool_name: "book_appointment",
      standing: "AUTONOMOUS",
      unavailable_because: null,
      unmet_trust_keys: [],
      independently_observed: true,
      recovery_class: "IDEMPOTENT_REPLAYABLE",
      arguments: [
        {
          name: "provider_id",
          arg_class: "DESTINATION",
          required_trust: "TRUSTED",
          trust_key: "provider_id",
          configured_value: "PROV-NORTHGATE-CARDIOLOGY",
          authored_value: null,
          identifies: true
        },
        {
          name: "reason",
          arg_class: "VALUE",
          required_trust: null,
          trust_key: null,
          configured_value: null,
          authored_value: "CareLoop appointment scheduling request.",
          identifies: false
        }
      ]
    },
    {
      tool_name: "grant_portal_access",
      standing: "UNAVAILABLE",
      unavailable_because: "NO_TRUSTED_SOURCE",
      unmet_trust_keys: ["role"],
      independently_observed: false,
      recovery_class: "NOT_REPLAYABLE",
      arguments: []
    }
  ],
  refusals: ["UNTRUSTED_DESTINATION", "SECOND_RECORDED_EFFECT"]
};

function answering(status: number, body: unknown = ""): string[] {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      calls.push(url);
      if (url.endsWith("/api/sessions")) return new Response("", { status: 404 });
      return new Response(body === "" ? "" : JSON.stringify(body), { status });
    })
  );
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("reading the boundary", () => {
  it("asks /api/boundaries exactly once", async () => {
    const calls = answering(200, STATEMENT);
    await new RealCareLoopApi().getBoundaries();
    expect(calls.filter((url) => url.endsWith("/api/boundaries"))).toHaveLength(1);
  });

  it("returns the backend's own shape, field for field", async () => {
    answering(200, STATEMENT);
    // Deep equality rather than spot checks: any field the adapter dropped, renamed, defaulted or
    // added would fail here, and every one of those is a way of quietly deriving something.
    expect(await new RealCareLoopApi().getBoundaries()).toEqual(STATEMENT);
  });

  it("keeps an unavailable capability's reason and unmet key", async () => {
    answering(200, STATEMENT);
    const read = await new RealCareLoopApi().getBoundaries();
    const grant = read?.capabilities.find((c) => c.tool_name === "grant_portal_access");
    // The reason and the key are what let the page say *why* without inventing a sentence. An
    // adapter that carried the standing but dropped these would force the UI to make one up.
    expect(grant?.unavailable_because).toBe("NO_TRUSTED_SOURCE");
    expect(grant?.unmet_trust_keys).toEqual(["role"]);
  });

  it("is not polled with the dashboard", async () => {
    const calls = answering(200, STATEMENT);
    const api = new RealCareLoopApi();
    await api.getBoundaries();
    await api.getBoundaries();
    // Two calls for two asks: no cache, and no shared timer with the poller. A boundary changes
    // when the ruleset or the caregiver record changes, so it is read when the page opens and the
    // page is what decides to ask again.
    expect(calls.filter((url) => url.endsWith("/api/boundaries"))).toHaveLength(2);
  });
});

describe("when the boundary cannot be read", () => {
  it.each([
    ["a backend that refuses", 401],
    ["a backend that has no such route", 404],
    ["a forgotten workspace", 410],
    ["a backend that broke", 500]
  ])("resolves null for %s", async (_label, status) => {
    answering(status);
    expect(await new RealCareLoopApi().getBoundaries()).toBeNull();
  });

  it("resolves null when nothing answers at all", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("offline");
      })
    );
    expect(await new RealCareLoopApi().getBoundaries()).toBeNull();
  });

  it("never substitutes a statement of its own", async () => {
    answering(500);
    const read = await new RealCareLoopApi().getBoundaries();
    // The one failure mode that would matter: a fallback shaped like a real answer. A caregiver
    // reading invented limits cannot tell them from enforced ones, which is strictly worse than
    // being told the page could not be read.
    expect(read).toBeNull();
    expect(read).not.toEqual(expect.objectContaining({ capabilities: expect.anything() }));
  });
});

describe("the mock adapter", () => {
  it("refuses to describe a boundary", async () => {
    // For the reason it refuses a re-check and the recovery scenario: a boundary is a claim about
    // live enforcement, and a choreography cannot make one. Returning something plausible here
    // would look correct and be exactly the hand-written authority claim this feature removed.
    expect(await mockApi.getBoundaries()).toBeNull();
  });
});
