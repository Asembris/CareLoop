import { useRef } from "react";
import { useDialog } from "@/hooks/useDialog";
import { useUi } from "@/hooks/useUi";
import type { EvidenceExcerpt } from "@/types/domain";

const TIERS = [
  { color: "var(--state-untrusted)", text: "Document content — untrusted" },
  { color: "var(--state-controlled)", text: "Extracted and validated — usable as fact" },
  { color: "var(--brand-accent)", text: "Never a source of authority" }
];

/** Where a fact came from, and what it is allowed to do. Opens over any screen. */
export default function EvidenceLens() {
  const { lens, closeLens } = useUi();
  if (!lens) return null;
  return <LensPanel lens={lens} onClose={closeLens} />;
}

/** Split from the gate above so the dialog hooks only run while the lens is open. */
function LensPanel({ lens, onClose }: { lens: EvidenceExcerpt; onClose: () => void }) {
  const panel = useRef<HTMLElement>(null);
  useDialog(panel, onClose);

  return (
    <aside
      ref={panel}
      role="dialog"
      aria-modal="true"
      aria-label="Evidence"
      tabIndex={-1}
      style={{
        position: "fixed",
        top: 0,
        right: 0,
        bottom: 0,
        width: 460,
        maxWidth: "94vw",
        zIndex: 70,
        background: "var(--background)",
        borderLeft: "1px solid color-mix(in oklab, currentColor 16%, transparent)",
        boxShadow: "-24px 0 60px color-mix(in oklab, var(--foreground) 12%, transparent)",
        display: "flex",
        flexDirection: "column",
        animation: "clSlide 300ms cubic-bezier(0.22,0.61,0.36,1) both",
        overflowY: "auto"
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "20px 26px",
          borderBottom: "1px solid color-mix(in oklab, currentColor 12%, transparent)"
        }}
      >
        <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, letterSpacing: "0.12em", color: "var(--muted-foreground)" }}>
          EVIDENCE
        </span>
        <button
          className="cl-quiet"
          onClick={onClose}
          aria-label="Close"
          style={{ background: "transparent", border: 0, padding: "4px 8px", fontSize: 16, color: "var(--muted-foreground)" }}
        >
          ×
        </button>
      </div>

      <div style={{ padding: "28px 26px", display: "flex", flexDirection: "column", gap: 22 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
          <span style={{ fontSize: 19, fontWeight: 500, letterSpacing: "-0.012em" }}>{lens.fact}</span>
          <span style={{ fontSize: 14, color: "var(--muted-foreground)" }}>{lens.source}</span>
        </div>

        <div
          style={{
            border: "1px solid color-mix(in oklab, currentColor 14%, transparent)",
            borderRadius: 4,
            padding: 20,
            fontSize: 14.5,
            lineHeight: 1.7,
            background: "color-mix(in oklab, currentColor 3%, transparent)"
          }}
        >
          <span style={{ color: "var(--muted-foreground)" }}>{lens.before}</span>
          <span style={{ background: "color-mix(in oklab, var(--brand-copper) 28%, transparent)", padding: "1px 3px", borderRadius: 2 }}>
            {lens.highlight}
          </span>
          <span style={{ color: "var(--muted-foreground)" }}>{lens.after}</span>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {TIERS.map((t) => (
            <div key={t.text} style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 14 }}>
              <span aria-hidden="true" style={{ width: 7, height: 7, borderRadius: "50%", background: t.color }} />
              <span style={{ color: "var(--muted-foreground)" }}>{t.text}</span>
            </div>
          ))}
        </div>

        <span style={{ fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--muted-foreground)", wordBreak: "break-all" }}>
          {lens.digest}
        </span>
      </div>
    </aside>
  );
}
