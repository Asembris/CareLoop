import { useRef } from "react";
import { useCareLoop } from "@/hooks/useCareLoop";
import { useDialog } from "@/hooks/useDialog";
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
    /* EVIDENCE DIGEST was here. Dropped rather than filled: there is no digest of evidence
       anywhere in the backend — `args_digest` on an approval and an action digests *arguments*,
       not evidence — so widening the API's payload allowlist would not produce one, because it
       does not exist to be allowed. Putting the citation in a slot labelled EVIDENCE DIGEST would
       have the panel imply a value that is not there. An absent row is honest; a relabelled one
       is not. `AgentRun.evidenceDigest` stays on the type, unread, so `mockApi` still typechecks. */
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
  if (!hoodOpen) return null;
  return <HoodPanel onClose={toggleHood} />;
}

/** Split from the gate above so the dialog hooks only run while the drawer is open. */
function HoodPanel({ onClose }: { onClose: () => void }) {
  const { run } = useCareLoop();
  const panel = useRef<HTMLElement>(null);
  useDialog(panel, onClose);

  const activeIndex = STAGE_ORDER.indexOf(run.stage);

  return (
    <aside
      ref={panel}
      role="dialog"
      aria-modal="true"
      aria-label="Under the hood"
      tabIndex={-1}
      style={{
        position: "fixed",
        top: 0,
        right: 0,
        bottom: 0,
        width: 430,
        maxWidth: "var(--drawer-max)",
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
          onClick={onClose}
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
                  {/* 0.65 / 0.85 / 1, not 0.35 / 0.75 / 1. These are informational text, not
                      controls — no role, no handler, nothing focusable — so WCAG 1.4.3's
                      "inactive user interface component" exemption does not reach them, and the
                      dimming is itself the information that a stage has not been reached. At 0.35
                      a future stage rendered 2.08:1 in light and 2.80:1 in dark against a 4.5:1
                      requirement; 0.65 gives 4.71 and 6.56. `past` rose from 0.75 to 0.85 for the
                      hierarchy rather than for contrast: with the floor at 0.65 the old value left
                      a 0.10 gap between "not yet" and "done", which is not a distinction anybody
                      could see. The dot's colour and the "running"/"done" label carry it too. */}
                  <span style={{ fontSize: 14.5, opacity: active ? 1 : past ? 0.85 : 0.65 }}>{name}</span>
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
          Live run record from GET /api/runs/:id. Policy verdicts and rule ids come from the case's own audit trail.
        </p>
      </div>
    </aside>
  );
}
