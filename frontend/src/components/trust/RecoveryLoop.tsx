import { OBSERVATION_COPY } from "@/services/presentation";

const LABEL = {
  fontFamily: "var(--font-mono)",
  fontSize: 11,
  letterSpacing: "0.12em",
  color: "var(--muted-foreground)"
} as const;

const STEP_LABEL = {
  fontFamily: "var(--font-mono)",
  fontSize: 10.5,
  letterSpacing: "0.08em",
  color: "var(--muted-foreground)"
} as const;

/** The post-effect lifecycle, one step per column. Tones repeat the app's own state colours so
 *  the diagram reads in the same language as the case pages that live it. */
const STEPS = [
  { label: "ACTED", text: "An action was carried out in the outside world", tone: "var(--brand-accent)" },
  { label: "READ BACK", text: "The external record is read independently", tone: "var(--state-controlled)" },
  { label: "NOT CERTIFIED", text: "The read cannot confirm it — the case stops", tone: "var(--state-denied)" },
  { label: "A PERSON", text: "Parked. CareLoop never retries the action", tone: "var(--state-decision)" },
  { label: "LOOK AGAIN", text: "A caregiver asks for one more observation", tone: "var(--state-verified)" }
] as const;

/** The five observation words, in the order the backend defines them. */
const VOCABULARY = ["MATCHED", "DIVERGED", "ABSENT", "UNAVAILABLE", "NOT_OBSERVED"] as const;

/**
 * The recovery loop: knowledge without authority.
 *
 * The subtlest thing the backend does deserves a visual home on the page about trust. Once an
 * effect has been attempted and cannot be certified, the case stops — no replan, no retry — and
 * the only way forward is a person asking CareLoop to *look* again. The request carries no tool,
 * no arguments and no target: the backend resolves what to read from its own durable record, and
 * the run that follows performs exactly one observation and nothing else.
 */
export default function RecoveryLoop() {
  return (
    <section style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <span style={LABEL}>WHEN THE WORLD DISAGREES</span>

      <p style={{ margin: 0, fontSize: 16, lineHeight: 1.6, color: "var(--muted-foreground)", maxWidth: "62ch" }}>
        A booking CareLoop cannot confirm is never retried and never replanned — it stops, with a
        person. What a caregiver can ask for is another <em>look</em>: one independent read of the
        exact record in question, resolved entirely from CareLoop&apos;s own audit trail. The
        request recovers knowledge, never authority.
      </p>

      <div style={{ display: "flex", alignItems: "stretch", gap: 0, flexWrap: "wrap" }}>
        {STEPS.map((step, index) => (
          <div key={step.label} style={{ display: "flex", alignItems: "stretch", flex: "1 1 150px" }}>
            {index > 0 ? (
              <span
                aria-hidden="true"
                style={{
                  alignSelf: "center",
                  padding: "0 10px",
                  color: "var(--muted-foreground)",
                  fontSize: 13
                }}
              >
                →
              </span>
            ) : null}
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 7,
                padding: "14px 16px",
                border: "1px solid color-mix(in oklab, currentColor 14%, transparent)",
                borderRadius: 4,
                borderTop: `2px solid ${step.tone}`,
                flex: 1
              }}
            >
              <span style={STEP_LABEL}>{step.label}</span>
              <span style={{ fontSize: 13.5, lineHeight: 1.5 }}>{step.text}</span>
            </div>
          </div>
        ))}
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <span style={STEP_LABEL}>WHAT A READ CAN PRODUCE</span>
        {VOCABULARY.map((word) => (
          <div key={word} style={{ display: "flex", gap: 14, alignItems: "baseline" }}>
            <span
              style={{
                fontFamily: "var(--font-mono)",
                fontSize: 12,
                letterSpacing: "0.06em",
                width: 118,
                flex: "none"
              }}
            >
              {word}
            </span>
            <span style={{ fontSize: 14, lineHeight: 1.55, color: "var(--muted-foreground)" }}>
              {OBSERVATION_COPY[word]}
            </span>
          </div>
        ))}
      </div>

      <p style={{ margin: 0, fontSize: 14, lineHeight: 1.6, color: "var(--muted-foreground)", maxWidth: "62ch" }}>
        A re-check creates no proposal, consults no planner, raises no approval and records no new
        action. It is one observation of one record — and if the observer is missing, verification
        refuses to certify rather than quietly looking away.
      </p>
    </section>
  );
}
