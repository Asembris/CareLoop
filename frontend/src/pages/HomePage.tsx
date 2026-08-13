import { useNavigate } from "react-router-dom";
import { useCareLoop } from "@/hooks/useCareLoop";
import DashboardHero from "@/components/dashboard/DashboardHero";
import SectionHeading from "@/components/dashboard/SectionHeading";
import DecisionCard from "@/components/dashboard/DecisionCard";
import CaseRow from "@/components/dashboard/CaseRow";
import { greeting, heroLine, heroSub } from "@/lib/tone";

/**
 * Home. Needs you / Working / Completed.
 * An empty Needs-you band is removed entirely — an empty container reads as a
 * failure, and "Nothing needs you right now." is the success state.
 */
export default function HomePage() {
  const navigate = useNavigate();
  const { needs, working, done } = useCareLoop();

  const manualOnly = needs.length === 1 && needs[0].decision?.kind === "NO_SAFE_ACTION_AVAILABLE";

  return (
    <main style={{ display: "flex", justifyContent: "center", padding: "68px 40px 130px" }}>
      <div style={{ width: "100%", maxWidth: "var(--page-max)", display: "flex", flexDirection: "column", gap: 52 }}>
        <DashboardHero
          greeting={greeting()}
          line={heroLine(needs.length, manualOnly)}
          sub={heroSub(needs.length, working.length, done.length)}
        />

        {needs.length > 0 ? (
          <section
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 16,
              animation: "clRise 380ms cubic-bezier(0.22,0.61,0.36,1) both"
            }}
          >
            <SectionHeading
              label="NEEDS YOU"
              tone="var(--state-decision)"
              ruleTone="color-mix(in oklab, var(--state-decision) 30%, transparent)"
              trailing={needs.length === 1 ? "1 item" : needs.length + " items"}
            />
            {needs.map((c) => (
              <DecisionCard key={c.id} careCase={c} />
            ))}
          </section>
        ) : null}

        {working.length > 0 ? (
          <section style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <SectionHeading label="WORKING" />
            <div style={{ display: "flex", flexDirection: "column" }}>
              {working.map((c) => (
                <CaseRow key={c.id} careCase={c} />
              ))}
            </div>
          </section>
        ) : null}

        {done.length > 0 ? (
          <section style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <SectionHeading label="COMPLETED" />
            <div style={{ display: "flex", flexDirection: "column" }}>
              {done.map((c) => (
                <CaseRow key={c.id} careCase={c} />
              ))}
            </div>
          </section>
        ) : null}

        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 20,
            paddingTop: 12
          }}
        >
          <span style={{ fontSize: 13, color: "var(--muted-foreground)" }}>Demo uses synthetic care data.</span>
          <button
            className="cl-outline"
            onClick={() => navigate("/demo")}
            style={{
              background: "transparent",
              border: "1px solid color-mix(in oklab, currentColor 18%, transparent)",
              borderRadius: "var(--radius-control)",
              padding: "8px 14px",
              fontSize: 13.5
            }}
          >
            Run a scenario
          </button>
        </div>
      </div>
    </main>
  );
}
