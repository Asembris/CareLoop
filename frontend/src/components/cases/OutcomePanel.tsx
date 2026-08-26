import type { CareCase } from "@/types/domain";
import { caseTone } from "@/lib/tone";
import {
  INDEPENDENT_OF_OWN_REPORT,
  OBSERVED_VALUES_WITHHELD,
  RECOVERY_PROOF_BODY,
  RECOVERY_PROOF_TITLE,
  recoveryProofRows
} from "@/services/presentation";
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
      data-scene-exclude
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
              <span style={LABEL}>RESULT</span>
              <span style={{ fontSize: 15, color: tone }}>{readback.result}</span>
            </div>
            {/* One cell for what verification observed, sentence first. It used to be two — a
                plain phrase under OBSERVED AT CLINIC and the bare enum under OBSERVATION — which
                asked a cold reader to work out that they were the same fact at two altitudes. The
                human reading leads and the backend's closed vocabulary word sits under it, still
                verbatim, because a judge checking the trail deserves the exact word it records. */}
            <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
              <span style={LABEL}>OBSERVATION</span>
              <span style={{ fontSize: 15 }}>{readback.observed}</span>
              {readback.observation ? (
                <span
                  style={{
                    fontFamily: "var(--font-mono)",
                    fontSize: 12.5,
                    letterSpacing: "0.06em",
                    color: OBSERVATION_TONE[readback.observation] ?? "inherit"
                  }}
                >
                  {readback.observation}
                </span>
              ) : null}
            </div>
          </div>
        ) : null}

        {careCase.authorisedBy ? (
          <div
            style={{
              display: "flex",
              alignItems: "baseline",
              gap: 12,
              flexWrap: "wrap",
              marginTop: 6,
              paddingTop: 12,
              borderTop: "1px solid color-mix(in oklab, currentColor 10%, transparent)"
            }}
          >
            <span style={LABEL}>ALLOWED BY</span>
            <span style={{ fontFamily: "var(--font-mono)", fontSize: 13 }}>
              {careCase.authorisedBy.ruleId}
            </span>
            {/* A real anchor rather than a router push: the fragment is what scrolls the matching
                capability into view and plays its locate highlight, and a programmatic navigate
                would leave `:target` unset. Same-origin and same route, so nothing reloads. */}
            <a
              className="cl-quiet"
              href={`/trust#${careCase.authorisedBy.toolName}`}
              style={{ fontSize: 13, color: "var(--nav-ink, var(--muted-foreground))", transition: "color 120ms ease" }}
            >
              What this means →
            </a>
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
            {/* Only over a MATCHED independent read. Any wider — "every settled case", or even
                "every observed case" — and the line claims independence for verifications that
                only checked CareLoop's own record. */}
            {readback.observation === "MATCHED" ? (
              <span style={{ fontSize: 14, lineHeight: 1.6, color: "var(--muted-foreground)" }}>
                {INDEPENDENT_OF_OWN_REPORT}
              </span>
            ) : null}
            {readback.observation !== "NOT_OBSERVED" ? (
              <span style={{ fontSize: 12.5, lineHeight: 1.6, color: "var(--muted-foreground)" }}>
                {OBSERVED_VALUES_WITHHELD}
              </span>
            ) : null}
          </div>
        ) : null}

        {/* Only on a case whose own trail records a *settled* recovery — see `recoveryProofFor`.
            A case that is still waiting has no block, because there is nothing yet to claim. */}
        {careCase.recovery ? (
          <div
            data-testid="recovery-proof"
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 12,
              marginTop: 10,
              paddingTop: 16,
              borderTop: "1px solid color-mix(in oklab, currentColor 10%, transparent)"
            }}
          >
            <span style={{ ...LABEL, letterSpacing: "0.1em", color: "var(--state-verified)" }}>
              {RECOVERY_PROOF_TITLE}
            </span>
            <span style={{ fontSize: 14.5, lineHeight: 1.6, color: "var(--muted-foreground)" }}>
              {RECOVERY_PROOF_BODY}
            </span>
            <div style={{ display: "flex", gap: 28, flexWrap: "wrap", fontVariantNumeric: "tabular-nums" }}>
              {recoveryProofRows(careCase.recovery).map((row) => (
                <div key={row.label} style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                  <span style={LABEL}>{row.label}</span>
                  <span style={{ fontSize: 14 }}>{row.value}</span>
                </div>
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </section>
  );
}
