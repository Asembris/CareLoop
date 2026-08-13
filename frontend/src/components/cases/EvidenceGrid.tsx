import type { EvidenceItem } from "@/types/domain";
import { useUi } from "@/hooks/useUi";

/** Extracted facts, each carrying the document it came from. */
export default function EvidenceGrid({ evidence }: { evidence: EvidenceItem[] }) {
  const { openLens } = useUi();
  if (evidence.length === 0) return null;

  return (
    <section style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, letterSpacing: "0.12em", color: "var(--muted-foreground)" }}>
        EVIDENCE
      </span>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 14 }}>
        {evidence.map((ev) => (
          <button
            key={ev.fact}
            className="cl-outline-soft"
            onClick={() => openLens(ev.excerpt)}
            style={{
              textAlign: "left",
              background: "transparent",
              border: "1px solid color-mix(in oklab, currentColor 14%, transparent)",
              borderRadius: 4,
              padding: "18px 20px",
              display: "flex",
              flexDirection: "column",
              gap: 7
            }}
          >
            <span style={{ fontSize: 15, fontWeight: 500 }}>{ev.fact}</span>
            <span style={{ fontSize: 13.5, color: "var(--muted-foreground)" }}>{ev.source}</span>
            <span style={{ fontSize: 12.5, color: "var(--state-controlled)" }}>{ev.trust}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
