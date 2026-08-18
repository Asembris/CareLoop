/* The FastAPI contract, asserted against the running backend with no browser involved.
 *
 * **This is the gap the patch exists to close.** `frontend/src/services/realApi.test.ts` asserts
 * the adapter against fixtures the frontend authored, so a backend field rename leaves it green and
 * the demo broken. Here the shapes come from the server, so the two can disagree exactly once.
 *
 * Covers the routes in `frontend/docs/API_INTEGRATION_MAP.md`, checked against `careloop/api/app.py`
 * rather than against that table — the table says itself that the repository is the source of truth.
 */

import { expect, test } from "./base";
import { CAREGIVER_HEADER, SESSION_HEADER, mintSession, withSession } from "./helpers";

test.describe("sessions", () => {
  test("mints a world", async ({ request }) => {
    const minted = await request.post("/api/sessions");
    expect(minted.status()).toBe(201);
    expect(typeof (await minted.json()).session).toBe("string");
  });

  test("a request naming no world is refused 400", async ({ request }) => {
    const refused = await request.get("/api/cases");
    expect(refused.status()).toBe(400);
    // The detail names the header, because the remedy is mechanical and the client can apply it.
    expect((await refused.json()).detail).toContain(SESSION_HEADER);
  });

  test("a request naming a forgotten world is refused 410, not 400 and not 409", async ({
    request
  }) => {
    // 410 is used nowhere else in this API on purpose, so a client can tell "your world is gone,
    // mint another" from the 409 that means a run is in flight. Collapsing them would make the
    // frontend's re-mint path guess.
    const gone = await request.get("/api/cases", {
      headers: withSession("a-world-that-was-never-minted")
    });
    expect(gone.status()).toBe(410);
  });
});

test.describe("the dashboard read", () => {
  test("returns three sections, not a flat list", async ({ request }) => {
    const session = await mintSession(request);
    const cases = await request.get("/api/cases", { headers: withSession(session) });

    expect(cases.status()).toBe(200);
    // The partition is total and disjoint over CaseState, and the frontend flattens it while
    // keeping which section each case arrived in. A fourth key, or a rename, breaks that here.
    expect(Object.keys(await cases.json()).sort()).toEqual(["completed", "needs_you", "working"]);
  });

  test("names a case it does not hold", async ({ request }) => {
    const session = await mintSession(request);
    const missing = await request.get("/api/cases/case-that-never-existed", {
      headers: withSession(session)
    });

    expect(missing.status()).toBe(404);
    expect((await missing.json()).detail).toContain("case-that-never-existed");
  });

  test("names a run it has never heard of", async ({ request }) => {
    const session = await mintSession(request);
    const missing = await request.get("/api/runs/run-nothing-0", {
      headers: withSession(session)
    });
    expect(missing.status()).toBe(404);
  });
});

test.describe("the demo control surface", () => {
  test("is mounted", async ({ request }) => {
    // Asserted rather than assumed: `create_app` mounts it only when asked, and most of the
    // state-driving specs cannot run at all without it.
    const session = await mintSession(request);
    const reset = await request.post("/api/demo/reset", { headers: withSession(session) });

    expect(reset.status()).toBe(200);
    expect(await reset.json()).toMatchObject({ clinic_reset: true });
  });

  test("refuses a scenario key it does not know, and says which it does", async ({ request }) => {
    const session = await mintSession(request);
    const refused = await request.post("/api/demo/scenarios/not-a-scenario", {
      headers: withSession(session)
    });

    expect(refused.status()).toBe(404);
    const { detail } = await refused.json();
    for (const known of ["referral", "injection", "resume", "injection_only"]) {
      expect(detail).toContain(known);
    }
  });
});

test.describe("the caregiver identity", () => {
  // `X-Caregiver` supplies `decided_by` for approve and reject, which is an *audit* fact. Nothing
  // is verified and anyone reaching the port can claim any name.
  const decisions = [
    { verb: "approve", path: "/api/cases/any-case/decisions/apr-0/approve" },
    { verb: "reject", path: "/api/cases/any-case/decisions/apr-0/reject" },
    { verb: "recheck", path: "/api/cases/any-case/recheck" }
  ];

  for (const { verb, path } of decisions) {
    test(`${verb} without it is 400 and never 401`, async ({ request }) => {
      // **A session is supplied on purpose.** Both checks are FastAPI dependencies and `deps`
      // precedes `who` in every one of these signatures, so a request carrying neither reports the
      // missing *session* first. Sending a valid world is what makes this a test about the
      // caregiver header rather than about dependency ordering.
      const session = await mintSession(request);
      const refused = await request.post(path, { headers: withSession(session) });

      expect(refused.status()).toBe(400);

      // The negative is the load-bearing half. 401 would claim an authentication check that is not
      // happening; 400 says what is true, which is that a required field is absent. A status-only
      // assertion would pass if somebody "upgraded" this to 401 and then back again.
      expect(refused.status()).not.toBe(401);
      expect((await refused.json()).detail).toContain(CAREGIVER_HEADER);
    });
  }

  test("the check runs before the case is looked up", async ({ request }) => {
    // `any-case` does not exist, and the answer is still 400 rather than 404. Worth pinning: it is
    // why the tests above need no fixture, and a reordering that looked harmless would turn every
    // one of them into a test of something else.
    const session = await mintSession(request);
    const refused = await request.post("/api/cases/definitely-not-a-case/recheck", {
      headers: withSession(session)
    });
    expect(refused.status()).toBe(400);
  });
});

test.describe("inbound events", () => {
  test("names an event the simulator does not hold", async ({ request }) => {
    const session = await mintSession(request);
    const refused = await request.post("/api/events", {
      headers: withSession(session),
      data: { case_id: "case-x", event_id: "evt-does-not-exist" }
    });

    expect(refused.status()).toBe(404);
    expect((await refused.json()).detail).toContain("evt-does-not-exist");
  });
});
