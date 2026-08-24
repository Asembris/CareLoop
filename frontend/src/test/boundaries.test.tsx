import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import App from "@/app/App";
import { forgetAuth } from "@/services/auth";
import type { BoundariesView, CapabilityView } from "@/services/backend";

/* My Boundaries, driven through the real page against a stubbed backend.
 *
 * The claims worth proving here are not about wording. They are:
 *
 *   1. **The screen says what the backend said.** Change the backend's answer and the page changes
 *      with it — including in the direction that would be embarrassing, where a capability the
 *      shipped configuration calls autonomous is rendered as needing a person because that is what
 *      the response said. A page that ignored the response and rendered `rules.yaml`'s real answer
 *      would pass a weaker test and be the exact defect this feature removed.
 *
 *   2. **A backend it cannot read produces no authority claims at all.** No fallback copy, no cached
 *      last-good statement, nothing that a reader could mistake for enforced limits.
 *
 * The page is reached at `/trust`, which is unchanged: the route a judge may have written down
 * still works, and the nav is what now says `My Boundaries`. */

const CAPABILITIES: Record<string, CapabilityView> = {
  booking: {
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
  email: {
    tool_name: "send_email",
    standing: "REQUIRES_HUMAN",
    unavailable_because: null,
    unmet_trust_keys: [],
    independently_observed: false,
    recovery_class: "NOT_REPLAYABLE",
    arguments: [
      {
        name: "to",
        arg_class: "DESTINATION",
        required_trust: "TRUSTED",
        trust_key: "caregiver_email",
        configured_value: "rosa.marlowe@carer.example",
        authored_value: null,
        identifies: true
      }
    ]
  },
  grant: {
    tool_name: "grant_portal_access",
    standing: "UNAVAILABLE",
    unavailable_because: "NO_TRUSTED_SOURCE",
    unmet_trust_keys: ["role"],
    independently_observed: false,
    recovery_class: "NOT_REPLAYABLE",
    arguments: [
      {
        name: "role",
        arg_class: "AUTHORITY",
        required_trust: "TRUSTED",
        trust_key: "role",
        configured_value: null,
        authored_value: null,
        identifies: true
      }
    ]
  }
};

const STATEMENT: BoundariesView = {
  capabilities: [CAPABILITIES.booking, CAPABILITIES.email, CAPABILITIES.grant],
  refusals: ["UNTRUSTED_DESTINATION", "DOCUMENT_AUTHORITY", "SECOND_RECORDED_EFFECT"]
};

/** A backend that answers the boundary with `body`, and everything else with an empty dashboard. */
function serve(body: BoundariesView | null, status = 200): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url.endsWith("/api/sessions")) return new Response("", { status: 404 });
      if (url.endsWith("/api/auth")) {
        return new Response(JSON.stringify({ mode: "DEMO" }), { status: 200 });
      }
      if (url.endsWith("/api/boundaries")) {
        return body
          ? new Response(JSON.stringify(body), { status: 200 })
          : new Response("", { status });
      }
      if (url.endsWith("/api/cases")) {
        return new Response(JSON.stringify({ needs_you: [], working: [], completed: [] }), {
          status: 200
        });
      }
      return new Response("", { status: 404 });
    })
  );
}

function show(): void {
  render(
    <MemoryRouter initialEntries={["/trust"]}>
      <App />
    </MemoryRouter>
  );
}

/** The card for one tool. Throws when absent, so `waitFor` retries rather than passing `null` on. */
function card(toolName: string): HTMLElement {
  const found = document.querySelector(`[data-capability="${toolName}"]`);
  if (!found) throw new Error(`no capability card for ${toolName}`);
  return found as HTMLElement;
}

function noCards(): boolean {
  return document.querySelector("[data-capability]") === null;
}

afterEach(() => {
  vi.unstubAllGlobals();
  forgetAuth();
});

describe("the page renders what the backend derived", () => {
  it("groups capabilities under the standing the backend gave them", async () => {
    serve(STATEMENT);
    show();

    await screen.findByText("CARELOOP MAY, ON ITS OWN");
    expect(screen.getByText("CARELOOP MUST ASK YOU")).toBeInTheDocument();
    expect(screen.getByText("CURRENTLY UNAVAILABLE UNDER YOUR BOUNDARIES")).toBeInTheDocument();

    expect(within(card("book_appointment")).getByText("ON ITS OWN")).toBeInTheDocument();
    expect(within(card("send_email")).getByText("ASKS YOU FIRST")).toBeInTheDocument();
    expect(within(card("grant_portal_access")).getByText("CURRENTLY UNAVAILABLE")).toBeInTheDocument();
  });

  it("follows the backend even when it contradicts the shipped configuration", async () => {
    // The sharp version of "derived, not hardcoded". `book_appointment` is autonomous in the real
    // system; if the response says a person is required, the page must say a person is required.
    // A page rendering its own knowledge of `rules.yaml` would pass the previous test and fail this
    // one, which is the whole reason this one exists.
    serve({
      capabilities: [{ ...CAPABILITIES.booking, standing: "REQUIRES_HUMAN" }],
      refusals: []
    });
    show();

    await waitFor(() => card("book_appointment"));
    expect(within(card("book_appointment")).getByText("ASKS YOU FIRST")).toBeInTheDocument();
    expect(screen.queryByText("CARELOOP MAY, ON ITS OWN")).not.toBeInTheDocument();
  });

  it("shows the caregiver's own configured destination on the condition it belongs to", async () => {
    serve(STATEMENT);
    show();

    const booking = await waitFor(() => card("book_appointment"));
    expect(within(booking).getByText("PROV-NORTHGATE-CARDIOLOGY")).toBeInTheDocument();
    expect(within(booking).getByText("Only with")).toBeInTheDocument();
  });

  it("says which field CareLoop writes itself", async () => {
    serve(STATEMENT);
    show();

    const booking = await waitFor(() => card("book_appointment"));
    expect(within(booking).getByText("CareLoop appointment scheduling request.")).toBeInTheDocument();
    expect(within(booking).getByText(/written by CareLoop, not by the model/)).toBeInTheDocument();
  });

  it("keeps an independently verified effect apart from a simulated one", async () => {
    serve(STATEMENT);
    show();

    await waitFor(() => card("book_appointment"));
    expect(within(card("book_appointment")).getByText(/other system's own record/)).toBeInTheDocument();
    // The claim the deleted GUARANTEES list got wrong: only two of the four effects are read back,
    // and saying so per capability is the whole reason it was worth deriving.
    expect(within(card("send_email")).getByText(/simulated/)).toBeInTheDocument();
  });

  it("renders only the refusals the backend sent", async () => {
    serve(STATEMENT);
    show();

    await screen.findByText("CARELOOP WILL REFUSE TO");
    expect(screen.getByText(/appears only inside a document/)).toBeInTheDocument();
    expect(screen.getByText(/permission to act/)).toBeInTheDocument();
    // UNEARNED_VALUE_TRUST was not in the response, so its sentence must not be on the page.
    expect(screen.queryByText(/nothing has checked/)).not.toBeInTheDocument();
  });

  it("omits a band nothing falls into", async () => {
    serve({ capabilities: [CAPABILITIES.booking], refusals: [] });
    show();

    await screen.findByText("CARELOOP MAY, ON ITS OWN");
    expect(screen.queryByText("CURRENTLY UNAVAILABLE UNDER YOUR BOUNDARIES")).not.toBeInTheDocument();
    expect(screen.queryByText("CARELOOP WILL REFUSE TO")).not.toBeInTheDocument();
  });
});

describe("an unavailable capability", () => {
  it("says why, scoped to the current configuration", async () => {
    serve(STATEMENT);
    show();

    const grant = await waitFor(() => card("grant_portal_access"));
    expect(within(grant).getByText(/a trusted role/)).toBeInTheDocument();
    expect(within(grant).getByText(/your current configuration/i)).toBeInTheDocument();
  });

  it("never reads as a permanent product limitation", async () => {
    serve(STATEMENT);
    show();

    const grant = await waitFor(() => card("grant_portal_access"));
    const text = (grant.textContent ?? "").toLowerCase();
    for (const forbidden of ["can never", "is not supported", "impossible", "not possible"]) {
      expect(text).not.toContain(forbidden);
    }
  });

  it("offers nothing to press", async () => {
    serve(STATEMENT);
    show();

    const grant = await waitFor(() => card("grant_portal_access"));
    // The only control on the card is the disclosure. No approve, no request, no "enable" —
    // there is nothing a caregiver could do here, and a button implying otherwise would be a lie.
    const buttons = within(grant).getAllByRole("button");
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toHaveAccessibleName(/why this is enforced/i);
  });

  it("makes no promise about what happens afterwards", async () => {
    serve(STATEMENT);
    show();

    const grant = await waitFor(() => card("grant_portal_access"));
    expect(within(grant).queryByText(/Afterwards/)).not.toBeInTheDocument();
  });
});

describe("when the boundary cannot be read", () => {
  it.each([
    ["a refusal", 401],
    ["a broken backend", 500]
  ])("shows no authority claims at all after %s", async (_label, status) => {
    serve(null, status);
    show();

    await screen.findByText("CareLoop can’t read your boundaries right now.");
    expect(screen.queryByText("CARELOOP MAY, ON ITS OWN")).not.toBeInTheDocument();
    expect(screen.queryByText("CARELOOP MUST ASK YOU")).not.toBeInTheDocument();
    expect(screen.queryByText("CARELOOP WILL REFUSE TO")).not.toBeInTheDocument();
    expect(noCards()).toBe(true);
  });

  it("does not fall back to the copy this page replaced", async () => {
    serve(null, 500);
    show();

    await screen.findByText("CareLoop can’t read your boundaries right now.");
    // The five sentences `data/trust.ts` used to hold. None of them may reappear from anywhere.
    for (const claim of [
      /Deterministic policy gate/,
      /bound to the exact arguments/,
      /Durable idempotency/,
      /Completion requires an independent external read-back/
    ]) {
      expect(screen.queryByText(claim)).not.toBeInTheDocument();
    }
  });

  it("says nothing while it is still asking", async () => {
    // A page that rendered its bands before the answer arrived would flash claims it had not read.
    let release: (value: Response) => void = () => {};
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.endsWith("/api/sessions")) return new Response("", { status: 404 });
        if (url.endsWith("/api/auth")) {
          return new Response(JSON.stringify({ mode: "DEMO" }), { status: 200 });
        }
        if (url.endsWith("/api/boundaries")) {
          return new Promise<Response>((resolve) => {
            release = resolve;
          });
        }
        return new Response(JSON.stringify({ needs_you: [], working: [], completed: [] }), {
          status: 200
        });
      })
    );
    show();

    await screen.findByText("Reading your boundaries…");
    expect(noCards()).toBe(true);

    release(new Response(JSON.stringify(STATEMENT), { status: 200 }));
    await waitFor(() => card("book_appointment"));
  });
});

describe("the technical detail is behind a real disclosure", () => {
  it("is closed, announced, and opens on press", async () => {
    serve(STATEMENT);
    show();

    const booking = await waitFor(() => card("book_appointment"));
    const toggle = within(booking).getByRole("button", { name: /why this is enforced/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    // The vocabulary the rules are written in, second rather than first.
    expect(within(booking).getByText("DESTINATION")).toBeInTheDocument();
    expect(within(booking).getByText("TRUSTED")).toBeInTheDocument();
  });

  it("keeps the raw vocabulary out of the page until it is asked for", async () => {
    serve(STATEMENT);
    show();

    await waitFor(() => card("book_appointment"));
    expect(within(card("book_appointment")).queryByText("DESTINATION")).not.toBeInTheDocument();
  });

  it("says so when the rules require no trust of a field", async () => {
    serve(STATEMENT);
    show();

    const booking = await waitFor(() => card("book_appointment"));
    await userEvent.click(within(booking).getByRole("button", { name: /why this is enforced/i }));
    // `reason` genuinely has no trust requirement. An empty cell would read as missing data; the
    // backend derived "none", so the page says "none".
    expect(within(booking).getByText("no trust required")).toBeInTheDocument();
  });
});

describe("whose boundaries these are", () => {
  it("says the demo caregiver is synthetic and the enforcement is not", async () => {
    serve(STATEMENT);
    show();

    const line = await screen.findByTestId("boundary-identity");
    expect(line).toHaveTextContent(/synthetic caregiver in a synthetic world/);
    expect(line).toHaveTextContent(/enforcement is real/);
  });

  it("does not claim a verified identity in demo mode", async () => {
    serve(STATEMENT);
    show();

    const line = await screen.findByTestId("boundary-identity");
    expect(line).not.toHaveTextContent(/verified/i);
    expect(line).not.toHaveTextContent(/signed in/i);
  });
});
