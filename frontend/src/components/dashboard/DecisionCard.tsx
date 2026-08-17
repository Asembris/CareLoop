import { useState } from "react";
import { useNavigate } from "react-router-dom";
import type { CareCase } from "@/types/domain";
import { caseTone } from "@/lib/tone";
import { useCareLoop } from "@/hooks/useCareLoop";

/**
 * The gap in the loop. Approve / Reject render ONLY for
 * REQUIRES_HUMAN_APPROVAL — NO_SAFE_ACTION_AVAILABLE must never present
 * an approval affordance it does not have authority for.
 *
 * The re-check button is a separate axis and deliberately not a third decision.
 * It renders on `decision.recheckable`, which is server truth, and never on the
 * reason code — the backend refuses a re-check for handoffs that recorded no
 * action identity or name an action nothing can observe, and those carry the
 * same code as ones that can. It asks CareLoop to LOOK again, never to act:
 * it is styled as an outline rather than the solid primary for that reason, and
 * it never appears beside Approve.
 */
export default function DecisionCard({ careCase }: { careCase: CareCase }) {
  const navigate = useNavigate();
  const { approve, reject, recheck } = useCareLoop();
  // What the person's press actually produced. "sending" disables the buttons; a decided state
  // replaces them with an acknowledgment; a refused press puts the buttons back, because a card
  // claiming "on it" about a request the backend answered 409 would be a small lie in the exact
  // place the product promises not to tell them.
  const [sending, setSending] = useState(false);
  const [decided, setDecided] = useState<"approved" | "declined" | null>(null);
  const decision = careCase.decision;
  if (!decision) return null;

  const decide = async (approved: boolean) => {
    setSending(true);
    const result = await (approved
      ? approve(careCase.id, decision.id)
      : reject(careCase.id, decision.id));
    setSending(false);
    if (result.accepted) setDecided(approved ? "approved" : "declined");
  };

  const tone = caseTone(careCase);
  const canApprove = decision.kind === "REQUIRES_HUMAN_APPROVAL" && decided === null;
  const canRecheck = decision.kind !== "REQUIRES_HUMAN_APPROVAL" && decision.recheckable;
  const kindLabel =
    decision.kind === "REQUIRES_HUMAN_APPROVAL" ? "DECISION NEEDED" : "MANUAL FOLLOW-UP REQUIRED";

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
        <span style={{ fontSize: 13.5, color: "var(--muted-foreground)", paddingTop: 14 }}>
          {decided === null
            ? decision.after
            : decided === "approved"
              ? "On it. CareLoop is acting — then checking the result independently."
              : "Declined. CareLoop will look for another way, and will not repeat this one."}
        </span>
        <div style={{ display: "flex", alignItems: "center", gap: 10, paddingTop: 14 }}>
          {decided !== null ? (
            /* The acknowledgment: your press registered and work started. Rendered only after
               the backend accepted (202) — a refused press restores the buttons instead. */
            <span
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                fontSize: 14.5,
                animation: "clFade 220ms ease both"
              }}
            >
              <svg viewBox="0 0 12 12" width="11" height="11" aria-hidden="true" style={{ color: tone }}>
                <circle
                  cx="6"
                  cy="6"
                  r="4.4"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.7"
                  strokeLinecap="round"
                  strokeDasharray="12 15.6"
                  style={{ transformOrigin: "6px 6px", animation: "clSpin 2.4s linear infinite" }}
                />
              </svg>
              <span role="status">{decided === "approved" ? "Approved" : "Declined"}</span>
            </span>
          ) : null}
          {canApprove ? (
            <>
              <button
                className="cl-outline"
                disabled={sending}
                onClick={() => void decide(false)}
                style={{
                  background: "var(--ground, transparent)",
                  border: "1px solid var(--edge, color-mix(in oklab, currentColor 20%, transparent))",
                  borderRadius: "var(--radius-control)",
                  padding: "10px 16px",
                  fontSize: 14.5,
                  transition: "background 120ms ease, border-color 120ms ease, opacity 120ms ease"
                }}
              >
                {decision.rejectLabel}
              </button>
              <button
                className="cl-solid"
                disabled={sending}
                onClick={() => void decide(true)}
                style={{
                  background: "var(--foreground)",
                  color: "var(--background)",
                  border: 0,
                  borderRadius: "var(--radius-control)",
                  padding: "10px 18px",
                  fontSize: 14.5,
                  fontWeight: 500,
                  // The sending dim is `:disabled` in components.css, not an inline opacity.
                  // Inline, it outranked `.cl-solid:hover` and left the two buttons that matter
                  // most — Approve and Decline — as the only solid controls with no hover at all.
                  transition: "opacity 120ms ease"
                }}
              >
                {decision.approveLabel}
              </button>
            </>
          ) : null}
          {canRecheck ? (
            <button
              className="cl-outline"
              onClick={() => recheck(careCase.id)}
              style={{
                background: "var(--ground, transparent)",
                border: "1px solid var(--edge, color-mix(in oklab, currentColor 20%, transparent))",
                borderRadius: "var(--radius-control)",
                padding: "10px 16px",
                fontSize: 14.5,
                transition: "background 120ms ease, border-color 120ms ease"
              }}
            >
              {decision.recheckLabel}
            </button>
          ) : null}
          <button
            className="cl-outline"
            onClick={() => navigate("/case/" + careCase.id)}
            style={{
              background: "var(--ground, transparent)",
              border: "1px solid var(--edge, color-mix(in oklab, currentColor 20%, transparent))",
              borderRadius: "var(--radius-control)",
              padding: "10px 16px",
              fontSize: 14.5,
              transition: "background 120ms ease, border-color 120ms ease"
            }}
          >
            {decision.kind === "REQUIRES_HUMAN_APPROVAL" ? "Open case" : "Review situation"}
          </button>
        </div>
      </div>
    </article>
  );
}
