import { useNavigate } from "react-router-dom";
import { useCareLoop } from "@/hooks/useCareLoop";
import DashboardHero from "@/components/dashboard/DashboardHero";
import SectionHeading from "@/components/dashboard/SectionHeading";
import DecisionCard from "@/components/dashboard/DecisionCard";
import CaseRow from "@/components/dashboard/CaseRow";
import { connectingHero, greeting, heroLine, heroSub, startingHero, unreachableHero } from "@/lib/tone";

/**
 * Home. Needs you / Working / Completed.
 * An empty Needs-you band is removed entirely — an empty container reads as a
 * failure, and "Nothing needs you right now." is the success state.
 *
 * The success state renders only on a live connection. Before the first read the page is
 * connecting; on an unreachable backend it says so — the calm line asserted over data that could
 * not be read would be indistinguishable from an outage, by construction, which is the one
 * coincidence this dashboard exists to not have.
 */
export default function HomePage() {
  const navigate = useNavigate();
  const { needs, working, done, connection, starting } = useCareLoop();

  const manualOnly = needs.length === 1 && needs[0].decision?.kind === "NO_SAFE_ACTION_AVAILABLE";
  const anyCases = needs.length + working.length + done.length > 0;

  /* Scoped to "and there is nothing to show yet" rather than to the flag alone. The moment a case
     exists it speaks for itself, so this can only ever replace the empty state — never a dashboard
     with work on it, and never a stale flag outliving the run that set it. */
  const awaitingFirstCase = starting && !anyCases;

  const hero =
    connection === "connecting"
      ? connectingHero()
      : connection === "unreachable"
        ? // An unreachable backend outranks a press: the last thing that happened is that the page
          // stopped being able to read the world, and that is what it has to say.
          unreachableHero(anyCases)
        : awaitingFirstCase
          ? startingHero()
          : {
              line: heroLine(needs.length, manualOnly),
              sub: heroSub(needs.length, working.length, done.length)
            };

  return (
    /* The dashboard owns the first screen rather than floating in its top third. Measured before
       this: content ended at 412px of a 737px viewport and the page did not scroll at all, which
       reads as a page that stopped loading rather than as restraint.

       `flex: 1` fills whatever the header leaves, and the closing row is pushed down by an auto
       margin — so it settles at the bottom of the first screen while the dashboard is short, and
       flows normally below the cases once there are enough of them to fill the screen. Nothing is
       pinned and nothing scrolls inside anything. */
    <main
      style={{
        flex: 1,
        display: "flex",
        justifyContent: "center",
        padding: "68px var(--page-gutter) 130px"
      }}
    >
      <div
        style={{
          width: "100%",
          maxWidth: "var(--page-max)",
          display: "flex",
          flexDirection: "column",
          gap: 52
        }}
      >
        <DashboardHero greeting={greeting()} line={hero.line} sub={hero.sub} />

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
            // `auto` rather than a fixed spacer: it collapses to the column gap as soon as the
            // sections above are tall enough to fill the screen, so a busy dashboard is not
            // padded and a quiet one is not half-empty.
            marginTop: "auto",
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
              background: "var(--ground, transparent)",
              border: "1px solid var(--edge, color-mix(in oklab, currentColor 18%, transparent))",
              borderRadius: "var(--radius-control)",
              padding: "8px 14px",
              fontSize: 13.5,
              transition: "background 120ms ease, border-color 120ms ease"
            }}
          >
            Run a scenario
          </button>
        </div>
      </div>
    </main>
  );
}
