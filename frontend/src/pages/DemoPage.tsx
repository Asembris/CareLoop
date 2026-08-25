import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { SCENARIOS } from "@/data/mock/scenarios";
import ScenarioCard from "@/components/scenarios/ScenarioCard";
import { useCareLoop } from "@/hooks/useCareLoop";
import { ensureSession } from "@/services/api";
import type { ScenarioId } from "@/types/domain";

/** Which orchestration-field sequence each scenario's own semantics match.
 *
 *  A card is mapped only where the field is describing what the scenario actually does. 01 is a
 *  referral that completes and is verified; 02 is a document that tries to instruct the agent and
 *  a branch that ends. Both are what the field's `referral` and `injection` sequences draw.
 *
 *  **03 and 04 are deliberately absent.** 03 is `resume` — CareLoop stops for a decision it has no
 *  authority to take, and finishes when the caregiver decides. The field ships a `mismatch`
 *  sequence, which draws a verification that disagrees and a loop that refuses to close; pointing
 *  03 at it would animate a failure this scenario does not have. 04 is `recovery`, and the same
 *  argument applies one step further: its verification *passes* — the whole beat is that CareLoop
 *  reaches a confirmed booking after losing the answer — so `mismatch` would draw the opposite of
 *  what happens, and `referral` would draw a loop that never wobbles. An unmapped card gets the
 *  ambient field, which asserts nothing. A truthful sequence for either is a thing to author, not
 *  to borrow.
 */
const SCENE_ZONES: Partial<Record<ScenarioId, string>> = {
  referral: "referral",
  // Both halves of 02 draw the same sequence, and honestly: the field's `injection` zone draws a
  // branch that ends, which is what happens on the deterministic run every time and on the
  // realistic one whenever the planner takes the bait. The card carries one zone because it is one
  // scenario asked two ways.
  injection: "injection",
  adversarial: "injection"
};

/**
 * The judge's entry point. Starting a scenario calls the adapter and returns to
 * the live dashboard while the run progresses — never a jump to the end state.
 */
export default function DemoPage() {
  const navigate = useNavigate();
  const { runScenario } = useCareLoop();

  /* The world this visitor was given, when the backend gives out worlds at all.
   *
   * Shown because the independent check is part of the claim: `docs/judges.md` asks a judge to
   * read `GET /clinic/slots` and see the appointment in a service CareLoop does not own. Under
   * per-visitor isolation that URL needs to name a world, and without this line the instruction
   * would quietly show an untouched clinic — the honest-looking failure, which is the worst kind. */
  const [world, setWorld] = useState<string | null>(null);
  useEffect(() => {
    let showing = true;
    void ensureSession().then((id) => showing && setWorld(id));
    return () => {
      showing = false;
    };
  }, []);

  const start = (id: ScenarioId) => {
    runScenario(id);
    navigate("/");
  };

  return (
    <main style={{ display: "flex", justifyContent: "center", padding: "96px var(--page-gutter) 120px" }}>
      <div
        style={{
          width: "100%",
          maxWidth: 880,
          display: "flex",
          flexDirection: "column",
          gap: 44,
          animation: "clFade 500ms ease both"
        }}
      >
        <div data-scene-exclude style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, letterSpacing: "0.12em", color: "var(--muted-foreground)" }}>
            DEMO
          </span>
          <h1
            style={{
              margin: 0,
              fontFamily: "var(--font-serif)",
              fontWeight: 300,
              fontSize: 52,
              lineHeight: 1.08,
              letterSpacing: "-0.02em"
            }}
          >
            See CareLoop work.
          </h1>
          <p style={{ margin: 0, fontSize: 17, lineHeight: 1.6, color: "var(--muted-foreground)", maxWidth: "58ch" }}>
            Each scenario runs the real workflow: CareLoop gathers what it needs, asks you only where your authority is
            required, acts, and then checks the outside world before closing the loop.
          </p>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(250px, 1fr))", gap: 18 }}>
          {SCENARIOS.map((s) => (
            <ScenarioCard
              key={s.id}
              scenario={s}
              onPick={() => start(s.id)}
              onPickAlternate={s.alternate ? () => start(s.alternate!.id) : undefined}
              sceneZone={SCENE_ZONES[s.id]}
            />
          ))}
        </div>

        <div data-scene-exclude style={{ display: "flex", alignItems: "center", gap: 16, paddingTop: 8 }}>
          <button
            className="cl-solid"
            onClick={() => navigate("/")}
            style={{
              background: "var(--foreground)",
              color: "var(--background)",
              border: 0,
              borderRadius: "var(--radius-control)",
              padding: "11px 20px",
              fontSize: 14.5,
              fontWeight: 500
            }}
          >
            Skip to the dashboard
          </button>
          <span style={{ fontSize: 13, color: "var(--muted-foreground)" }}>
            Demo uses synthetic care data. No real patient information.
          </span>
        </div>

        {world && (
          <p
            data-scene-exclude
            style={{
              margin: 0,
              fontFamily: "var(--font-mono)",
              fontSize: 11.5,
              lineHeight: 1.7,
              letterSpacing: "0.02em",
              color: "var(--muted-foreground)"
            }}
          >
            YOUR WORLD {world}
            <br />
            Cases and clinic are yours alone — check the appointment yourself at{" "}
            <a
              href={`/clinic/slots?session=${encodeURIComponent(world)}`}
              target="_blank"
              rel="noreferrer"
              style={{ color: "inherit" }}
            >
              /clinic/slots
            </a>
            , a service CareLoop does not own.
          </p>
        )}
      </div>
    </main>
  );
}
