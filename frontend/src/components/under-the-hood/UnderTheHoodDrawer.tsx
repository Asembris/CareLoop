import { useCareLoop } from "@/hooks/useCareLoop";
import { useUi } from "@/hooks/useUi";
import { AGENT_STAGES, AUTHORITY } from "@/data/trust";
import type { AgentRun, RunStage } from "@/types/domain";

const STAGE_ORDER: RunStage[] = ["TRIAGE", "EVIDENCE", "PLANNER", "GATE", "EXECUTE", "VERIFY", "SETTLED"];

const SECTION_LABEL = {
  fontFamily: "var(--font-mono)",
  fontSize: 10.5,
  letterSpacing: "0.1em",
  color: "var(--muted-foreground)"
} as const;

function runRows(run: AgentRun): { k: string; v: string; tone: string }[] {
  return [
    { k: "RUN", v: run.stageLabel, tone: "var(--brand-accent)" },
    { k: "ACTION INTENT", v: run.actionIntent, tone: "inherit" },
    { k: "IDEMPOTENCY KEY", v: run.idempotencyKey, tone: "inherit" },
    {
      k: "POLICY VERDICT",
      v: run.policyDetail,
      tone: run.policyVerdict === "DENY" ? "var(--state-denied)" : "inherit"
    },
    { k: "EVIDENCE DIGEST", v: run.evidenceDigest, tone: "inherit" },
    {
      k: "VERIFICATION",
      v: run.verificationDetail,
      tone:
        run.verification === "MISMATCH"
          ? "var(--state-denied)"
          : run.verification === "MATCH"
            ? "var(--state-verified)"
            : "inherit"
    }
  ];
}

/**
 * Progressive disclosure for judges and engineers. Reads the SAME run object
 * that drives the dashboard — never a separately faked trace.
 */
export default function UnderTheHoodDrawer() {
  const { hoodOpen, toggleHood } = useUi();
  const { run } = useCareLoop();
  if (!hoodOpen) return null;

  const activeIndex = STAGE_ORDER.indexOf(run.stage);

  return (
    <aside
      role="dialog"
      aria-label="Under the hood"
      style={{
        position: "fixed",
        top: 0,
        right: 0,
        bottom: 0,
        width: 430,
        maxWidth: "92vw",
        zIndex: 60,
        background: "var(--background)",
        borderLeft: "1px solid color-mix(in oklab, currentColor 16%, transparent)",
        boxShadow: "-24px 0 60px color-mix(in oklab, var(--foreground) 10%, transparent)",
        display: "flex",
        flexDirection: "column",
        animation: "clSlide 320ms cubic-bezier(0.22,0.61,0.36,1) both",
        overflowY: "auto"
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "20px 26px",
          borderBottom: "1px solid color-mix(in oklab, currentColor 12%, transparent)",
          position: "sticky",
          top: 0,
          zIndex: 2,
          background: "var(--background)"
        }}
      >
        <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, letterSpacing: "0.12em", color: "var(--muted-foreground)" }}>
          UNDER THE HOOD
        </span>
        <button
          className="cl-quiet"
          onClick={toggleHood}
          aria-label="Close"
          style={{ background: "transparent", border: 0, padding: "4px 8px", fontSize: 16, color: "var(--muted-foreground)" }}
        >
          ×
        </button>
      </div>

      <div style={{ padding: 26, display: "flex", flexDirection: "column", gap: 32 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <span style={SECTION_LABEL}>AGENT PATH</span>
          <div style={{ display: "flex", flexDirection: "column" }}>
            {AGENT_STAGES.map((name, i) => {
              const active = i === activeIndex;
              const past = activeIndex > i;
              return (
                <div key={name} style={{ display: "flex", alignItems: "center", gap: 12, padding: "9px 0" }}>
                  <span
                    aria-hidden="true"
                    style={{
                      width: 7,
                      height: 7,
                      borderRadius: "50%",
                      flex: "none",
                      background: active
                        ? "var(--state-decision)"
                        : past
                          ? "var(--brand-accent)"
                          : "color-mix(in oklab, currentColor 25%, transparent)"
                    }}
                  />
                  <span style={{ fontSize: 14.5, opacity: active ? 1 : past ? 0.75 : 0.35 }}>{name}</span>
                  <span
                    style={{
                      marginLeft: "auto",
                      fontFamily: "var(--font-mono)",
                      fontSize: 11,
                      color: "var(--muted-foreground)"
                    }}
                  >
                    {active ? "running" : past ? "done" : ""}
                  </span>
                </div>
              );
            })}
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <span style={SECTION_LABEL}>AUTHORITY</span>
          {AUTHORITY.map((a) => (
            <div
              key={a.who}
              style={{
                display: "flex",
                alignItems: "baseline",
                gap: 12,
                padding: "7px 0",
                borderBottom: "1px solid color-mix(in oklab, currentColor 9%, transparent)"
              }}
            >
              <span style={{ fontSize: 14.5, width: 78, flex: "none" }}>{a.who}</span>
              <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, letterSpacing: "0.06em", color: a.toneVar }}>
                {a.verb}
              </span>
            </div>
          ))}
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <span style={SECTION_LABEL}>CURRENT RUN</span>
          {runRows(run).map((r) => (
            <div
              key={r.k}
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 3,
                padding: "7px 0",
                borderBottom: "1px solid color-mix(in oklab, currentColor 9%, transparent)"
              }}
            >
              <span style={{ fontFamily: "var(--font-mono)", fontSize: 10.5, letterSpacing: "0.06em", color: "var(--muted-foreground)" }}>
                {r.k}
              </span>
              <span style={{ fontFamily: "var(--font-mono)", fontSize: 12.5, color: r.tone, wordBreak: "break-all" }}>
                {r.v}
              </span>
            </div>
          ))}
        </div>

        <p style={{ margin: 0, fontSize: 13, lineHeight: 1.55, color: "var(--muted-foreground)" }}>
          Running on the mock adapter: these values mirror the shape of the real run record. Wire to GET /api/runs/:id.
        </p>
      </div>
    </aside>
  );
}
