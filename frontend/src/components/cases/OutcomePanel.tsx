import type { CareCase } from "@/types/domain";
import { caseTone } from "@/lib/tone";
import { OBSERVED_VALUES_WITHHELD } from "@/services/presentation";
import VerificationSeal from "./VerificationSeal";

const LABEL = {
  fontFamily: "var(--font-mono)",
  fontSize: 10.5,
  letterSpacing: "0.08em",
  color: "var(--muted-foreground)"
} as const;

/** Colour never carries the outcome alone — the word itself is the badge, and the sentence under
 *  it says what the word means. */
const OBSERVATION_TONE: Record<string, string> = {
  MATCHED: "var(--state-verified)",
  DIVERGED: "var(--state-denied)",
  ABSENT: "var(--state-denied)",
  UNAVAILABLE: "var(--state-decision)",
  NOT_OBSERVED: "var(--muted-foreground)"
};

/**
 * Outcome first, and the external read-back beside it. A mismatch renders the
 * real observed value and never presents itself as a completed case.
 */
export default function OutcomePanel({ careCase }: { careCase: CareCase }) {
  if (!careCase.outcome) return null;
  const tone = caseTone(careCase);
  const readback = careCase.readback;

  return (
    <section
      style={{
        display: "flex",
        gap: 34,
        alignItems: "center",
        flexWrap: "wrap",
        border: "1px solid color-mix(in oklab, " + tone + " 32%, transparent)",
        borderRadius: "var(--radius-card)",
        padding: "30px 32px",
        background: "color-mix(in oklab, " + tone + " 4%, transparent)"
      }}
    >
      {careCase.verified ? <VerificationSeal /> : null}
      <div style={{ display: "flex", flexDirection: "column", gap: 9, flex: "1 1 320px" }}>
        <span style={{ ...LABEL, letterSpacing: "0.1em" }}>OUTCOME</span>
        <span style={{ fontSize: 20, fontWeight: 500, letterSpacing: "-0.012em" }}>{careCase.outcome.title}</span>
        <span style={{ fontSize: 15, color: "var(--muted-foreground)", lineHeight: 1.55 }}>
          {careCase.outcome.body}
        </span>

        {readback ? (
          <div
            style={{
              display: "flex",
              gap: 34,
              flexWrap: "wrap",
              marginTop: 8,
              paddingTop: 14,
              borderTop: "1px solid color-mix(in oklab, currentColor 10%, transparent)",
              fontVariantNumeric: "tabular-nums"
            }}
          >
            <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
              <span style={LABEL}>AUTHORIZED</span>
              <span style={{ fontSize: 15 }}>{readback.authorized}</span>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
              <span style={LABEL}>OBSERVED AT CLINIC</span>
              <span style={{ fontSize: 15 }}>{readback.observed}</span>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
              <span style={LABEL}>RESULT</span>
              <span style={{ fontSize: 15, color: tone }}>{readback.result}</span>
            </div>
            {readback.observation ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                <span style={LABEL}>OBSERVATION</span>
                <span
                  style={{
                    fontFamily: "var(--font-mono)",
                    fontSize: 13,
                    letterSpacing: "0.06em",
                    color: OBSERVATION_TONE[readback.observation] ?? "inherit"
                  }}
                >
                  {readback.observation}
                </span>
              </div>
            ) : null}
          </div>
        ) : null}

        {readback?.observationText ? (
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 6,
              marginTop: 6,
              paddingTop: 12,
              borderTop: "1px solid color-mix(in oklab, currentColor 10%, transparent)"
            }}
          >
            <span style={{ fontSize: 14, lineHeight: 1.6, color: "var(--muted-foreground)" }}>
              {readback.observationText}
            </span>
            {readback.observation !== "NOT_OBSERVED" ? (
              <span style={{ fontSize: 12.5, lineHeight: 1.6, color: "var(--muted-foreground)" }}>
                {OBSERVED_VALUES_WITHHELD}
              </span>
            ) : null}
          </div>
        ) : null}
      </div>
    </section>
  );
}
