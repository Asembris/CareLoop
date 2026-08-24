import { describe, expect, it } from "vitest";
import {
  afterwardsCopy,
  boundariesFor,
  conditionRequirement,
  recoveryCopy,
  refusalCopy,
  standingLabel,
  standingSummary,
  unavailableReason
} from "./presentation";
import type { BoundariesView, CapabilityView } from "./backend";

/* The copy half of My Boundaries.
 *
 * Every function under test is a *lookup* keyed on a backend enum, and the tests are written to
 * fail if one ever stops being that. Two properties matter more than the wording:
 *
 *   1. **Nothing is recomputed.** `boundariesFor` must carry the backend's standing through
 *      unchanged, including one it has never heard of. A build that decided a standing for itself
 *      would be the second source of truth this feature exists to have deleted, and it would look
 *      right on the shipped configuration — which is why it is asserted rather than reviewed.
 *   2. **"Currently", never "never".** `UNAVAILABLE` is a fact about this caregiver's configuration.
 *      Copy implying a permanent product limitation claims more than the backend derived.
 */

function capability(over: Partial<CapabilityView> = {}): CapabilityView {
  return {
    tool_name: "book_appointment",
    standing: "AUTONOMOUS",
    unavailable_because: null,
    unmet_trust_keys: [],
    independently_observed: true,
    recovery_class: "IDEMPOTENT_REPLAYABLE",
    arguments: [],
    ...over
  };
}

function view(...capabilities: CapabilityView[]): BoundariesView {
  return { capabilities, refusals: [] };
}

describe("standings are rendered, never decided", () => {
  it("carries the backend's standing through unchanged", () => {
    const read = boundariesFor(
      view(
        capability({ tool_name: "book_appointment", standing: "AUTONOMOUS" }),
        capability({ tool_name: "send_email", standing: "REQUIRES_HUMAN" }),
        capability({ tool_name: "grant_portal_access", standing: "UNAVAILABLE" })
      )
    );
    expect(read.capabilities.map((c) => [c.toolName, c.standing])).toEqual([
      ["book_appointment", "AUTONOMOUS"],
      ["send_email", "REQUIRES_HUMAN"],
      ["grant_portal_access", "UNAVAILABLE"]
    ]);
  });

  it("does not re-derive a standing from the fields beside it", () => {
    // A capability the backend calls AUTONOMOUS while carrying an unmet key is a contradiction the
    // backend cannot produce — and if it ever did, the frontend must report what it was told rather
    // than quietly correcting it. Correcting it here is how the two would start disagreeing.
    const read = boundariesFor(
      view(capability({ standing: "AUTONOMOUS", unmet_trust_keys: ["role"] }))
    );
    expect(read.capabilities[0].standing).toBe("AUTONOMOUS");
    expect(read.capabilities[0].unavailableReason).toBeUndefined();
  });

  it("keeps a capability whose standing this build has never heard of", () => {
    const read = boundariesFor(view(capability({ standing: "SOMETHING_NEW" })));
    expect(read.capabilities).toHaveLength(1);
    expect(read.capabilities[0].standing).toBe("SOMETHING_NEW");
    expect(read.capabilities[0].summary).toContain("cannot describe");
    expect(standingLabel("SOMETHING_NEW")).toBe("UNKNOWN");
  });

  it("renders no raw enum as a label", () => {
    for (const standing of ["AUTONOMOUS", "REQUIRES_HUMAN", "UNAVAILABLE"]) {
      expect(standingLabel(standing)).not.toContain("_");
      expect(standingSummary(standing)).not.toContain(standing);
    }
  });
});

describe("unavailability is about the current configuration", () => {
  const reason = unavailableReason("NO_TRUSTED_SOURCE", ["role"]);

  it("says what is missing, in words", () => {
    expect(reason).toContain("a trusted role");
    expect(reason).not.toContain("role_id");
    expect(reason).not.toContain("TRUSTED");
  });

  it("scopes the claim to this caregiver rather than to the product", () => {
    expect(reason.toLowerCase()).toContain("your current configuration");
  });

  it("never claims a permanent limitation", () => {
    // The exact sentences a well-meaning editor writes, and every one of them would be false: a
    // different configuration answers differently, which is why the backend derives this per caller.
    for (const forbidden of [
      "can never",
      "will never be able",
      "is not supported",
      "impossible",
      "not possible"
    ]) {
      expect(reason.toLowerCase()).not.toContain(forbidden);
    }
  });

  it("names more than one missing thing readably", () => {
    expect(unavailableReason("NO_TRUSTED_SOURCE", ["role", "portal_id"])).toContain(
      "a trusted role and a trusted portal"
    );
  });

  it("distinguishes the three ways of being out of reach", () => {
    const source = unavailableReason("NO_TRUSTED_SOURCE", ["role"]);
    const decision = unavailableReason("NO_ROUTE_TO_A_DECISION", []);
    const rule = unavailableReason("NO_AUTHORISING_RULE", []);
    expect(new Set([source, decision, rule]).size).toBe(3);
    expect(decision).toContain("approval");
    expect(rule).toContain("No rule");
  });

  it("still says something for a reason it has never heard of", () => {
    expect(unavailableReason("SOMETHING_NEW", [])).toContain("cannot currently authorise");
  });

  it("is attached only to an unavailable capability", () => {
    const read = boundariesFor(
      view(
        capability({ standing: "UNAVAILABLE", unavailable_because: "NO_TRUSTED_SOURCE", unmet_trust_keys: ["role"] }),
        capability({ tool_name: "send_email", standing: "REQUIRES_HUMAN" })
      )
    );
    expect(read.capabilities[0].unavailableReason).toBeTruthy();
    expect(read.capabilities[1].unavailableReason).toBeUndefined();
  });
});

describe("conditions describe what the rules require and no more", () => {
  it("says a destination must come from configuration", () => {
    expect(conditionRequirement("DESTINATION", "TRUSTED", false, "AUTONOMOUS")).toContain(
      "your configuration"
    );
  });

  it("separates authority from destination", () => {
    expect(conditionRequirement("AUTHORITY", "TRUSTED", false, "REQUIRES_HUMAN")).not.toEqual(
      conditionRequirement("DESTINATION", "TRUSTED", false, "REQUIRES_HUMAN")
    );
  });

  it("says a validated value was checked, not merely present", () => {
    expect(conditionRequirement("VALUE", "CONTROLLED", false, "AUTONOMOUS")).toContain("checked");
  });

  it("says an authored field is not the model's to write", () => {
    const copy = conditionRequirement("VALUE", null, true, "AUTONOMOUS");
    expect(copy).toContain("written by CareLoop");
    expect(copy).toContain("not by the model");
  });

  it("claims no requirement where the rules impose none", () => {
    // `required_trust: null` exists so the backend can say "the rules require nothing of this".
    // Rendering a guard here would assert one that does not exist — session J's defect, in copy.
    const autonomous = conditionRequirement("VALUE", null, false, "AUTONOMOUS");
    expect(autonomous).toContain("nothing in the active policy");

    const asked = conditionRequirement("VALUE", null, false, "REQUIRES_HUMAN");
    expect(asked).toContain("before you approve");
  });

  it("carries the configured value and the trust word onto the row", () => {
    const read = boundariesFor(
      view(
        capability({
          arguments: [
            {
              name: "provider_id",
              arg_class: "DESTINATION",
              required_trust: "TRUSTED",
              trust_key: "provider_id",
              configured_value: "PROV-NORTHGATE-CARDIOLOGY",
              authored_value: null,
              identifies: true
            }
          ]
        })
      )
    );
    const [condition] = read.capabilities[0].conditions;
    expect(condition.label).toBe("Only with");
    expect(condition.value).toBe("PROV-NORTHGATE-CARDIOLOGY");
    expect(condition.trustLabel).toBe("Confirmed");
    expect(condition.requiredTrust).toBe("TRUSTED");
  });

  it("shows the authored literal as the row's value", () => {
    const read = boundariesFor(
      view(
        capability({
          arguments: [
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
        })
      )
    );
    const [condition] = read.capabilities[0].conditions;
    expect(condition.value).toBe("CareLoop appointment scheduling request.");
    expect(condition.trustLabel).toBeUndefined();
  });
});

describe("what happens afterwards", () => {
  it("keeps an independent read-back apart from a self-check", () => {
    expect(afterwardsCopy(true)).toContain("other system's own record");
    expect(afterwardsCopy(false)).toContain("simulated");
  });

  it("does not call a simulated effect verified", () => {
    expect(afterwardsCopy(false).toLowerCase()).not.toContain("independently");
  });
});

describe("what happens when the answer is lost", () => {
  const replayable = recoveryCopy("IDEMPOTENT_REPLAYABLE");

  it("claims one appointment rather than one request", () => {
    // The load-bearing distinction. Reconciliation really does issue a second POST; what it cannot
    // do is create a second appointment. `tests/test_recovery_copy.py` sweeps this file for the
    // tidier, false sentence.
    expect(replayable).toContain("No second appointment is created");
    expect(replayable).toContain("asks again");
  });

  it("says the identity was frozen beforehand", () => {
    expect(replayable).toContain("froze");
  });

  it("says a non-replayable effect stops for a person", () => {
    expect(recoveryCopy("NOT_REPLAYABLE")).toContain("hands the case to you");
  });
});

describe("refusals", () => {
  it("renders each principle as a sentence a caregiver can read", () => {
    for (const principle of [
      "UNTRUSTED_DESTINATION",
      "UNEARNED_VALUE_TRUST",
      "DOCUMENT_AUTHORITY",
      "SECOND_RECORDED_EFFECT"
    ]) {
      const copy = refusalCopy(principle);
      expect(copy).not.toBe("");
      expect(copy).not.toContain("_");
    }
  });

  it("drops a principle it has no copy for rather than rendering an empty line", () => {
    const read = boundariesFor({
      capabilities: [],
      refusals: ["UNTRUSTED_DESTINATION", "SOMETHING_NEW"]
    });
    expect(read.refusals).toHaveLength(1);
  });

  it("renders only the principles the backend actually sent", () => {
    const read = boundariesFor({ capabilities: [], refusals: ["DOCUMENT_AUTHORITY"] });
    expect(read.refusals).toEqual([refusalCopy("DOCUMENT_AUTHORITY")]);
  });
});
