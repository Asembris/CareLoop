/* What the API refuses to tell a browser.
 *
 * Invariant 6 withholds rule ids and trust levels from the *model*, because denial text re-enters
 * the planner's prompt. Over HTTP the axis moves: a browser proposes nothing, so trust levels,
 * citations and `matched_rule_id` are exposed on purpose — they are the demo. What is withheld
 * instead is `TimelineEvent.diagnostic`, **wholesale**, and payloads pass an allowlist.
 *
 * Asserted against `injection_only` on purpose. That is the case where every tool in the registry
 * is proposed and refused, so its timeline carries the richest diagnostics in the product — the
 * arguments, the rule internals, the trust levels the engine computed. A withholding test run
 * against a quiet case would pass by having nothing to leak.
 *
 * **Measured, so the assertion's weight is visible.** Driving that scenario through the real graph
 * and reading the *store* directly gives five `POLICY_DENY` entries whose diagnostics are 500 to
 * 631 characters each, opening:
 *
 *     upload_document.portal_id resolved below TRUSTED. A portal named by document content is …
 *     grant_portal_access.grantee resolved below TRUSTED. Who receives access is DESTINATION-cl…
 *
 * None of that reaches the API. There is something to withhold and it is withheld.
 */

import { expect, test } from "@playwright/test";
import { mintSession, runScenario, withSession } from "./helpers";

/** A hand-written mirror of `careloop.api.projections.TIMELINE_PAYLOAD_FIELDS`.
 *
 *  **Duplicated deliberately, and never generated.** The backend's allowlist is what makes a new
 *  payload key invisible until somebody decides about it; this is the same decision asked a second
 *  time, from outside the process, by something that cannot be edited in the same motion as the
 *  code that started emitting the key. A key that shows up here is a review, not a merge conflict.
 */
const ALLOWED_PAYLOAD_KEYS = new Set([
  "tool_name", "approval_id", "event_id", "document_ids", "opened_by", "key", "source_doc", "page",
  "args", "rationale",
  "verdict", "reason_code", "matched_rule_id",
  "from", "to", "reason", "status", "decided_by",
  "category", "urgency", "patient_id", "kind", "stage", "check", "trust_level",
  "held_trust_level", "from_trust_level", "to_trust_level", "arg_class", "validated_by",
  "idempotency_key", "outcome",
  "read_back", "observation"
]);

/** The fields `TimelineEventView` declares. Anything else arriving is a field nobody projected. */
const TIMELINE_EVENT_FIELDS = new Set(["seq", "at", "kind", "actor", "summary", "payload"]);

test.describe("a case whose timeline is full of refusals", () => {
  test("carries no diagnostic anywhere, and no payload key nobody decided about", async ({
    request
  }) => {
    const session = await mintSession(request);
    expect((await runScenario(request, session, "injection_only")).status()).toBe(202);

    const detail = await request.get("/api/cases/demo-injection-only", {
      headers: withSession(session)
    });
    expect(detail.status()).toBe(200);
    const { timeline } = await detail.json();

    // The case really did refuse things — otherwise everything below is vacuous.
    const kinds = timeline.map((entry: { kind: string }) => entry.kind);
    expect(kinds).toContain("POLICY_DENY");
    expect(kinds).toContain("MANUAL_HANDOFF_REQUIRED");
    expect(timeline.length).toBeGreaterThan(5);

    for (const entry of timeline) {
      // Withheld wholesale rather than filtered: it is unbounded free text with a dozen writers,
      // and its own docstring promises it may carry raw arguments and rule internals. A filter
      // over free text is the sanitiser this project refuses everywhere else.
      expect(entry).not.toHaveProperty("diagnostic");

      expect(new Set(Object.keys(entry))).toEqual(TIMELINE_EVENT_FIELDS);

      for (const key of Object.keys(entry.payload)) {
        expect(
          ALLOWED_PAYLOAD_KEYS.has(key),
          `timeline payload key "${key}" is not on the allowlist — decide about it rather than ` +
            `adding it here reflexively`
        ).toBe(true);
      }
    }
  });

  test("still exposes what the demo is for", async ({ request }) => {
    // The other half, and the reason this is not simply "hide everything": a POLICY_DENY that
    // reached the browser with no rule id and no reason code would make the under-the-hood panel
    // and the timeline's refusal branch unbuildable. Withholding is scoped, not total.
    const session = await mintSession(request);
    await runScenario(request, session, "injection_only");

    const detail = await request.get("/api/cases/demo-injection-only", {
      headers: withSession(session)
    });
    const { timeline } = await detail.json();

    const denial = timeline.find((entry: { kind: string }) => entry.kind === "POLICY_DENY");
    expect(denial.payload.verdict).toBe("DENY");
    expect(denial.payload.reason_code).toBeTruthy();
  });
});
