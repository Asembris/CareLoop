/**
 * Deliberately empty until the adversarial suite has actually run.
 * Do not populate these with placeholder numbers — an unmeasured claim
 * undoes the whole page.
 */
export default function SecurityEvidencePanel() {
  return (
    <section
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 14,
        border: "1px dashed color-mix(in oklab, currentColor 22%, transparent)",
        borderRadius: "var(--radius-card)",
        padding: "26px 28px"
      }}
    >
      <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, letterSpacing: "0.12em", color: "var(--muted-foreground)" }}>
        SECURITY EVIDENCE
      </span>
      <p style={{ margin: 0, fontSize: 15, lineHeight: 1.6, color: "var(--muted-foreground)", maxWidth: "60ch" }}>
        Adversarial test results are not available in this build. When the red-team suite runs, this panel reports the
        measured numbers — unauthorized external actions, injection attempts blocked, excessive-agency attempts. Nothing
        is displayed here until it has actually been measured.
      </p>
      <div
        style={{
          display: "flex",
          gap: 28,
          flexWrap: "wrap",
          fontFamily: "var(--font-mono)",
          fontSize: 13,
          color: "var(--muted-foreground)",
          opacity: 0.7
        }}
      >
        <span>UNAUTHORIZED EXTERNAL ACTIONS &nbsp; —</span>
        <span>INJECTIONS BLOCKED &nbsp; —</span>
        <span>EXCESSIVE AGENCY &nbsp; —</span>
      </div>
    </section>
  );
}
