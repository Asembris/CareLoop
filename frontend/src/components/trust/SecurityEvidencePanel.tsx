import { SECURITY_EVIDENCE } from "@/data/trust";

const LABEL = {
  fontFamily: "var(--font-mono)",
  fontSize: 11,
  letterSpacing: "0.12em",
  color: "var(--muted-foreground)"
} as const;

const NUMERAL = {
  fontFamily: "var(--font-serif)",
  fontWeight: 300,
  fontSize: 58,
  lineHeight: 1,
  letterSpacing: "-0.02em"
} as const;

/**
 * The measured adversarial results. This panel was deliberately empty — em-dashes and "not
 * available in this build" — until the suite had actually run; it has, the numbers are committed,
 * and `src/data/trust.test.ts` pins what renders here to `docs/security-evaluation.md` so the
 * panel can never outlive its measurement.
 *
 * The layout puts the two numbers face to face because the *contrast* is the evidence: the zero
 * alone could be a well-behaved model, and the 320 is what shows the zero belongs to the policy
 * engine rather than to anyone's manners.
 */
export default function SecurityEvidencePanel() {
  const {
    attempts,
    payloads,
    deliveryRoutes,
    effectTools,
    shippedUnauthorized,
    withoutAuthorityUnauthorized,
    reproduceCommand,
    source
  } = SECURITY_EVIDENCE;

  return (
    <section
      data-scene-exclude
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 22,
        border: "1px solid color-mix(in oklab, currentColor 16%, transparent)",
        borderRadius: "var(--radius-card)",
        padding: "30px 32px"
      }}
    >
      <span style={LABEL}>SECURITY EVIDENCE</span>

      <div style={{ display: "flex", alignItems: "stretch", gap: 34, flexWrap: "wrap" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 10, flex: "1 1 240px" }}>
          <span style={{ ...NUMERAL, color: "var(--state-verified)" }}>
            {shippedUnauthorized} of {attempts}
          </span>
          <span style={{ fontSize: 14.5, lineHeight: 1.55, color: "var(--muted-foreground)" }}>
            adversarial attempts produced an unauthorized external action, replayed through the
            real graph, the real rules and both enforcement points.
          </span>
        </div>

        <div
          aria-hidden="true"
          style={{ width: 1, background: "color-mix(in oklab, currentColor 14%, transparent)", flex: "none" }}
        />

        <div style={{ display: "flex", flexDirection: "column", gap: 10, flex: "1 1 240px" }}>
          <span style={{ ...NUMERAL, color: "var(--state-denied)" }}>
            {withoutAuthorityUnauthorized} of {attempts}
          </span>
          <span style={{ fontSize: 14.5, lineHeight: 1.55, color: "var(--muted-foreground)" }}>
            with the authority layer removed and nothing else changed — the same attempts, the same
            graph. The contrast is the evidence; the zero on its own would not be.
          </span>
        </div>
      </div>

      {/* What an "attempt" is, said before anything is concluded from how many there were.
          320 is a cross product, not a headcount: read as 320 separate attacks it overstates the
          corpus eightfold, and that is the sentence a well-meaning editor writes. The factors come
          from `SECURITY_EVIDENCE`, which `trust.test.ts` pins to the measured record, so the
          arithmetic on this line cannot drift away from the harness that produced it. */}
      <p
        data-testid="adversarial-denominator"
        style={{ margin: 0, fontSize: 14.5, lineHeight: 1.6, color: "var(--muted-foreground)", maxWidth: "62ch" }}
      >
        {payloads} hostile payloads × {deliveryRoutes} delivery routes × {effectTools}{" "}
        external-effect tools = {attempts} evaluated combinations. Every one of them is driven by a
        planner that does exactly what the attacker asks, every time it is asked — the ceiling of
        model misbehaviour, not one model on one day.
      </p>

      <p style={{ margin: 0, fontSize: 14.5, lineHeight: 1.6, color: "var(--muted-foreground)", maxWidth: "62ch" }}>
        Reproduce both numbers offline, with no credentials, in about ten seconds:
      </p>

      <div style={{ display: "flex", alignItems: "baseline", gap: 16, flexWrap: "wrap" }}>
        <code
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 13,
            padding: "7px 12px",
            border: "1px solid color-mix(in oklab, currentColor 14%, transparent)",
            borderRadius: "var(--radius-control)"
          }}
        >
          {reproduceCommand}
        </code>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--muted-foreground)" }}>
          measured record: {source}
        </span>
      </div>
    </section>
  );
}
