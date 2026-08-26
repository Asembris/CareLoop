import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import App from "@/app/App";
import type { CaseCard, CaseDetail } from "@/services/backend";

/* The cold first screen. A judge who has never seen CareLoop lands on a live, empty dashboard,
   and that screen is the one place the product states its thesis — who it is for, that the model
   holds no authority, and what to press. The block must appear exactly there and nowhere else:
   not over real work, not over a connection state that is already saying something true, and not
   as a slogan pasted across other surfaces. */

function card(over: Partial<CaseCard>): CaseCard {
  return {
    case_id: "demo-referral",
    state: "EXECUTING",
    section: "working",
    summary: "cardiology referral",
    version: 1,
    created_at: "2026-08-13T10:00:00+00:00",
    updated_at: "2026-08-13T11:00:00+00:00",
    failure_reason: null,
    fact_count: 0,
    action_count: 0,
    decision: null,
    ...over
  };
}

function detailOf(base: CaseCard): CaseDetail {
  return {
    case_id: base.case_id,
    state: base.state,
    section: base.section,
    version: base.version,
    event_id: "evt-0001-referral",
    summary: base.summary,
    created_at: base.created_at,
    updated_at: base.updated_at,
    failure_reason: base.failure_reason,
    decision: base.decision,
    facts: [],
    approvals: [],
    actions: [],
    timeline: []
  };
}

function serve(cards: CaseCard[], { down = false } = {}) {
  const sections = {
    needs_you: cards.filter((c) => c.section === "needs_you"),
    working: cards.filter((c) => c.section === "working"),
    completed: cards.filter((c) => c.section === "completed")
  };
  const details = Object.fromEntries(cards.map((c) => [c.case_id, detailOf(c)]));

  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (down) throw new TypeError("Failed to fetch");
      if (url.endsWith("/api/sessions")) return new Response("", { status: 404 });
      if (url.endsWith("/api/cases")) return new Response(JSON.stringify(sections), { status: 200 });
      if (url.startsWith("/api/cases/")) {
        const found = details[decodeURIComponent(url.split("/")[3])];
        return found ? new Response(JSON.stringify(found), { status: 200 }) : new Response("", { status: 404 });
      }
      return new Response("", { status: 404 });
    })
  );
}

function show(route = "/") {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <App />
    </MemoryRouter>
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the cold first screen explains CareLoop", () => {
  it("frames the product on a live dashboard with nothing on it", async () => {
    serve([]);
    show();

    const intro = await screen.findByTestId("judge-intro");
    // Human purpose first, then the authority frame — in causal order, once.
    expect(intro.textContent).toContain("family caregivers");
    expect(intro.textContent).toContain("no authority to act");
    expect(intro.textContent).toContain("MODEL PROPOSES");
    expect(intro.textContent).toContain("POLICY AUTHORIZES");
    expect(intro.textContent).toContain("EXTERNAL SYSTEM VERIFIES");
    // Scoped honestly: verification is claimed for supported actions, never for every action.
    expect(intro.textContent).toContain("supported actions");
  });

  it("routes its one call to action to the scenario launcher", async () => {
    serve([]);
    show();

    await screen.findByTestId("judge-intro");
    await userEvent.click(screen.getByRole("button", { name: /see careloop work/i }));

    // The launcher's own locked heading — a real route, not a dead end.
    expect(await screen.findByRole("heading", { name: "See CareLoop work." })).toBeInTheDocument();
  });

  it("disappears the moment a case exists", async () => {
    serve([card({})]);
    show();

    await screen.findByText("Cardiology referral");
    expect(screen.queryByTestId("judge-intro")).not.toBeInTheDocument();
  });

  it("never renders over a backend that cannot be read", async () => {
    serve([], { down: true });
    show();

    await screen.findByText("CareLoop can't be reached.");
    expect(screen.queryByTestId("judge-intro")).not.toBeInTheDocument();
  });
});

describe("the thesis treatment exists exactly once", () => {
  const ROOT = join(__dirname, "..", "..");

  function everyFileUnder(directory: string): string[] {
    return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
      const full = join(directory, entry.name);
      return entry.isDirectory() ? everyFileUnder(full) : [full];
    });
  }

  it("spells the three-step frame on one served surface and no other", () => {
    /* The frame is easy to paste everywhere and cheapen. One treatment, on the cold landing
       state, is the decision — so the sweep counts the distinctive third step across everything
       the browser can be served (tests are never served and are excluded by the same rule
       `fonts.test.ts` uses). */
    const served = [
      join(ROOT, "index.html"),
      ...everyFileUnder(join(ROOT, "src")).filter((file) => !/[\\/]test[\\/]|\.test\./.test(file))
    ];
    const carriers = served.filter((file) => readFileSync(file, "utf-8").includes("EXTERNAL SYSTEM VERIFIES"));
    expect(carriers.map((f) => f.split("\\").join("/").split("/src/").pop())).toEqual(
      ["components/dashboard/JudgeIntro.tsx"]
    );
  });
});
