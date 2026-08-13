import { GUARANTEES } from "@/data/trust";

export default function GuaranteeList() {
  return (
    <div style={{ display: "flex", flexDirection: "column" }}>
      {GUARANTEES.map((g) => (
        <div
          key={g.code}
          style={{
            display: "flex",
            alignItems: "baseline",
            gap: 14,
            padding: "14px 2px",
            borderBottom: "1px solid color-mix(in oklab, currentColor 10%, transparent)"
          }}
        >
          <span aria-hidden="true" style={{ color: "var(--state-verified)", fontSize: 14 }}>
            ✓
          </span>
          <span style={{ fontSize: 15.5 }}>{g.text}</span>
          <span
            style={{
              marginLeft: "auto",
              fontFamily: "var(--font-mono)",
              fontSize: 11.5,
              color: "var(--muted-foreground)"
            }}
          >
            {g.code}
          </span>
        </div>
      ))}
    </div>
  );
}
