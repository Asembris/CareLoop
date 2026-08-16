/* The two states `page.route` is spent on, and why each one earns it.
 *
 * **Everything else in this suite is unmocked, and that is the point of the layer.** These two are
 * the exception, and the justification is the same for both, stated once because it is one fact
 * about the world rather than two conveniences:
 *
 *   Demo World exposes four routes — `GET /clinic/slots`, `POST /clinic/appointments`,
 *   `GET /clinic/appointments/{id}` and `POST /demo/reset`. **None of them mutates an appointment
 *   that already exists.** So there is no way, over HTTP, from a browser or from a spec, to make
 *   the clinic's record disagree with what CareLoop executed. `tests/test_verify_readback.py`
 *   produces this divergence in-process by moving the appointment behind CareLoop's back, and a
 *   browser cannot reach in-process objects.
 *
 * A third candidate — a policy DENY card — is deliberately **not** mocked. `injection_only`
 * produces it for real, deterministically, and `tests/withholding.spec.ts` drives it. Mocking a
 * state the real backend produces would be an unjustified mock.
 *
 * **What these therefore do and do not prove.** They assert that the frontend renders the two
 * post-effect states correctly *given* the projections the backend documents — the card's copy,
 * the re-check affordance, the seal's mismatch. They prove nothing about whether the backend
 * produces those projections; `tests/test_post_effect_lifecycle.py` and
 * `tests/test_observation_outcome.py` own that, in-process, where the divergence is reachable.
 */

import { expect, test, type Page } from "@playwright/test";

const CASE_ID = "demo-referral";
const AT = "2026-08-16T09:00:00+00:00";

/** A `CaseCard` carrying a post-effect handoff a caregiver may ask to re-check. */
function parkedCard() {
  return {
    case_id: CASE_ID,
    state: "AWAITING_HUMAN",
    section: "needs_you",
    summary: "Cardiology referral needs an outpatient appointment.",
    version: 9,
    created_at: AT,
    updated_at: AT,
    failure_reason: null,
    fact_count: 6,
    action_count: 1,
    decision: {
      approval_id: "apr-7",
      tool_name: "book_appointment",
      reason_code: "EXTERNAL_STATE_NOT_CONFIRMED",
      // Written for the planner; the adapter substitutes the caregiver-facing copy.
      reason_text: "the external state could not be confirmed",
      args: {
        provider_id: "PROV-NORTHGATE-CARDIOLOGY",
        patient_id: "PT-4417",
        slot_start: "2026-09-03T09:30:00",
        reason: "cardiology review"
      },
      requested_at: AT,
      // Server truth, and the only thing that may render the affordance. A client deriving this
      // from `reason_code` would draw the button on handoffs the backend refuses with a 409.
      recheckable: true
    }
  };
}

/** The detail for that case: an executed booking whose independent read-back disagreed. */
function parkedDetail() {
  const card = parkedCard();
  return {
    case_id: CASE_ID,
    state: card.state,
    section: card.section,
    version: card.version,
    event_id: "evt-0001-referral",
    summary: card.summary,
    created_at: AT,
    updated_at: AT,
    failure_reason: null,
    decision: card.decision,
    facts: [],
    approvals: [],
    actions: [
      {
        idempotency_key: "a".repeat(64),
        action_type: "book_appointment",
        args_digest: "b".repeat(64),
        result: JSON.stringify({ confirmation_id: "APT-0001" }),
        recorded_at: AT
      }
    ],
    timeline: [
      {
        seq: 1,
        at: AT,
        kind: "PLAN_PROPOSED",
        actor: "planner",
        summary: "book_appointment proposed",
        payload: { tool_name: "book_appointment", args: card.decision.args }
      },
      {
        seq: 2,
        at: AT,
        kind: "ACTION_EXECUTED",
        actor: "system",
        summary: "book_appointment executed",
        payload: { tool_name: "book_appointment", outcome: "EXECUTED" }
      },
      {
        seq: 3,
        at: AT,
        kind: "VERIFICATION_FAILED",
        actor: "system",
        summary: "verification failed",
        // `read_back: "true"` is what makes this a *disagreement* rather than a closed-loop
        // failure, and `observation` says which of the five things the read established.
        payload: { tool_name: "book_appointment", read_back: "true", observation: "DIVERGED" }
      },
      {
        seq: 4,
        at: AT,
        kind: "MANUAL_HANDOFF_REQUIRED",
        actor: "system",
        summary: "manual handoff required",
        payload: { tool_name: "book_appointment", reason_code: "EXTERNAL_STATE_NOT_CONFIRMED" }
      }
    ]
  };
}

/** Answer the two read routes with the crafted world, leaving every other request real. */
async function serveParkedCase(page: Page): Promise<void> {
  await page.route("**/api/cases", (route) =>
    route.fulfill({
      json: { needs_you: [parkedCard()], working: [], completed: [] }
    })
  );
  await page.route(`**/api/cases/${CASE_ID}`, (route) => route.fulfill({ json: parkedDetail() }));
}

test.describe("a case whose result could not be confirmed", () => {
  test("says so, and offers to look again rather than to approve", async ({ page }) => {
    await serveParkedCase(page);
    await page.goto("/");

    const card = page.getByRole("article");
    await expect(card.getByRole("heading", { name: "The result could not be confirmed" })).toBeVisible();

    // The distinction the whole post-effect lifecycle rests on: CareLoop will not repeat the
    // *action*, but a caregiver who has checked the other system can ask it to repeat the *look*.
    await expect(card.getByRole("button", { name: "Check the clinic again" })).toBeVisible();

    // **No approval affordance.** A manual handoff is a status, not a capability — it raises no
    // `Approval`, so there is nothing to approve, and a button here would offer authority over a
    // call the backend would refuse.
    await expect(card.getByRole("button", { name: "Approve" })).toHaveCount(0);
    await expect(card.getByRole("button", { name: "Reject" })).toHaveCount(0);
    await expect(card.getByRole("button", { name: "Not now" })).toHaveCount(0);
  });

  test("the re-check sends no body, and the case id is the entire request", async ({ page }) => {
    await serveParkedCase(page);

    const sent: { url: string; body: string | null }[] = [];
    await page.route("**/api/cases/*/recheck", async (route) => {
      sent.push({ url: route.request().url(), body: route.request().postData() });
      await route.fulfill({
        status: 202,
        json: { run_id: "run-demo-referral-2", case_id: CASE_ID, state: "RUNNING", executed_nodes: [], error: null }
      });
    });

    await page.goto("/");
    await page.getByRole("button", { name: "Check the clinic again" }).click();
    await expect.poll(() => sent.length).toBeGreaterThan(0);

    // The security property of the route, asserted from the client side of it: there is nothing in
    // the request through which a caller could name a tool, an argument, a digest, a confirmation
    // id or an action index. What gets read is resolved from durable server state alone.
    expect(sent[0].body).toBeNull();
    expect(sent[0].url).toContain(`/api/cases/${CASE_ID}/recheck`);
  });

  test("the case detail shows the disagreement rather than a completion", async ({ page }) => {
    await serveParkedCase(page);
    await page.goto(`/case/${CASE_ID}`);

    // A verification mismatch must never render as a completed case. This is the seal's MISMATCH
    // side, and the copy is chosen to say the clinic disagreed rather than that CareLoop failed.
    await expect(page.getByText("The clinic's record disagrees — not marked complete")).toBeVisible();
  });
});
