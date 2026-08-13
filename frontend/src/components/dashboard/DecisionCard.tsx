import { useNavigate } from "react-router-dom";
import type { CareCase } from "@/types/domain";
import { caseTone } from "@/lib/tone";
import { useCareLoop } from "@/hooks/useCareLoop";

/**
 * The gap in the loop. Approve / Reject render ONLY for
 * REQUIRES_HUMAN_APPROVAL — NO_SAFE_ACTION_AVAILABLE must never present
 * an approval affordance it does not have authority for.
 */
export default function DecisionCard({ careCase }: { careCase: CareCase }) {
  const navigate = useNavigate();
  const { approve, reject } = useCareLoop();
  const decision = careCase.decision;
  if (!decision) return null;

  const tone = caseTone(careCase);
  const canApprove = decision.kind === "REQUIRES_HUMAN_APPROVAL";
  const kindLabel = canApprove ? "DECISION NEEDED" : "MANUAL FOLLOW-UP REQUIRED";

  return (
    <article
      style={{
        border: "1px solid color-mix(in oklab, " + tone + " 38%, transparent)",
        borderRadius: "var(--radius-card)",
        padding: "28px 28px 24px",
        display: "flex",
        flexDirection: "column",
        gap: 20,
        background: "color-mix(in oklab, " + tone + " 5%, transparent)",
        animation: "clRise 420ms cubic-bezier(0.22,0.61,0.36,1) both"
      }}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, letterSpacing: "0.1em", color: tone }}>
          {kindLabel}
        </span>
        <h2 style={{ margin: 0, fontSize: 21, fontWeight: 500, letterSpacing: "-0.012em" }}>{decision.title}</h2>
        <p style={{ margin: 0, fontSize: 15.5, lineHeight: 1.58, color: "var(--muted-foreground)", maxWidth: "60ch" }}>
          {decision.body}
        </p>
      </div>

      <div style={{ display: "flex", gap: 34, flexWrap: "wrap" }}>
        {decision.facts.map((f) => (
          <div key={f.label} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <span
              style={{
                fontFamily: "var(--font-mono)",
                fontSize: 10.5,
                letterSpacing: "0.08em",
                color: "var(--muted-foreground)"
              }}
            >
              {f.label}
            </span>
            <span style={{ fontSize: 15, fontWeight: 500, fontVariantNumeric: "tabular-nums" }}>{f.value}</span>
          </div>
        ))}
      </div>

      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 24,
          flexWrap: "wrap",
          paddingTop: 4,
          borderTop: "1px solid color-mix(in oklab, currentColor 10%, transparent)"
        }}
      >
        <span style={{ fontSize: 13.5, color: "var(--muted-foreground)", paddingTop: 14 }}>{decision.after}</span>
        <div style={{ display: "flex", gap: 10, paddingTop: 14 }}>
          {canApprove ? (
            <>
              <button
                className="cl-outline"
                onClick={() => reject(careCase.id, decision.id)}
                style={{
                  background: "transparent",
                  border: "1px solid color-mix(in oklab, currentColor 20%, transparent)",
                  borderRadius: "var(--radius-control)",
                  padding: "10px 16px",
                  fontSize: 14.5
                }}
              >
                {decision.rejectLabel}
              </button>
              <button
                className="cl-solid"
                onClick={() => approve(careCase.id, decision.id)}
                style={{
                  background: "var(--foreground)",
                  color: "var(--background)",
                  border: 0,
                  borderRadius: "var(--radius-control)",
                  padding: "10px 18px",
                  fontSize: 14.5,
                  fontWeight: 500
                }}
              >
                {decision.approveLabel}
              </button>
            </>
          ) : null}
          <button
            className="cl-outline"
            onClick={() => navigate("/case/" + careCase.id)}
            style={{
              background: "transparent",
              border: "1px solid color-mix(in oklab, currentColor 20%, transparent)",
              borderRadius: "var(--radius-control)",
              padding: "10px 16px",
              fontSize: 14.5
            }}
          >
            {canApprove ? "Open case" : "Review situation"}
          </button>
        </div>
      </div>
    </article>
  );
}
