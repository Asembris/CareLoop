/**
 * What CareLoop refuses, whatever it is asked.
 *
 * Four sentences at most, and each one is emitted only because the backend *derived* it from the
 * active configuration — an argument class that cannot be promoted, a trust level a rule really
 * requires, an identity every effect really declares. A ruleset that stopped enforcing one would
 * stop claiming it, which is the difference between this list and the hand-written one it replaced.
 *
 * Deliberately not a rule browser. The rule that refused a *particular* action is already on that
 * case's own timeline, with its id and its reason code; this says what holds before any case exists.
 */
export default function RefusalList({ refusals }: { refusals: string[] }) {
  if (refusals.length === 0) return null;

  return (
    <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column" }}>
      {refusals.map((refusal) => (
        <li
          key={refusal}
          style={{
            display: "flex",
            alignItems: "baseline",
            gap: 14,
            padding: "13px 2px",
            borderBottom: "1px solid color-mix(in oklab, currentColor 10%, transparent)",
            fontSize: 15.5,
            lineHeight: 1.55
          }}
        >
          {/* A neutral mark rather than a red cross. These are refusals working as designed, not
              failures, and painting them as alarms trains a reader to skip them. */}
          <span aria-hidden="true" style={{ color: "var(--muted-foreground)", fontSize: 13 }}>
            —
          </span>
          <span>{refusal}</span>
        </li>
      ))}
    </ul>
  );
}
