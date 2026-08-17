import { AUTHORITY } from "@/data/trust";

/** Model proposes · Policy authorizes · Human approves · Verifier confirms. */
export default function AuthorityGrid() {
  return (
    <section
      data-scene-exclude
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
        gap: 1,
        background: "color-mix(in oklab, currentColor 14%, transparent)",
        border: "1px solid color-mix(in oklab, currentColor 14%, transparent)",
        borderRadius: "var(--radius-card)",
        overflow: "hidden"
      }}
    >
      {AUTHORITY.map((a) => (
        <div
          key={a.who}
          style={{ background: "var(--background)", padding: "22px 20px", display: "flex", flexDirection: "column", gap: 7 }}
        >
          <span style={{ fontSize: 15, fontWeight: 500 }}>{a.who}</span>
          <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, letterSpacing: "0.08em", color: a.toneVar }}>
            {a.verb}
          </span>
          <span style={{ fontSize: 13.5, lineHeight: 1.5, color: "var(--muted-foreground)" }}>{a.note}</span>
        </div>
      ))}
    </section>
  );
}
