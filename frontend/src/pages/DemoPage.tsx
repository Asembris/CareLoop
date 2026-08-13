import { useNavigate } from "react-router-dom";
import { SCENARIOS } from "@/data/mock/scenarios";
import ScenarioCard from "@/components/scenarios/ScenarioCard";
import { useCareLoop } from "@/hooks/useCareLoop";
import type { ScenarioId } from "@/types/domain";

/**
 * The judge's entry point. Starting a scenario calls the adapter and returns to
 * the live dashboard while the run progresses — never a jump to the end state.
 */
export default function DemoPage() {
  const navigate = useNavigate();
  const { runScenario } = useCareLoop();

  const start = (id: ScenarioId) => {
    runScenario(id);
    navigate("/");
  };

  return (
    <main style={{ display: "flex", justifyContent: "center", padding: "96px 40px 120px" }}>
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
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
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
            <ScenarioCard key={s.id} scenario={s} onPick={() => start(s.id)} />
          ))}
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 16, paddingTop: 8 }}>
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
      </div>
    </main>
  );
}
