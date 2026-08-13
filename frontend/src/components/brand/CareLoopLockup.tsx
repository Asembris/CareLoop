import CareLoopMark from "./CareLoopMark";

interface CareLoopLockupProps {
  showTagline?: boolean;
  markSize?: number;
}

/**
 * Full brand presentation. Deliberately NOT used inside the product —
 * reserved for splash screens, pitch title frames, README and social assets.
 */
export function CareLoopLockup({ showTagline = true, markSize = 56 }: CareLoopLockupProps) {
  return (
    <div
      style={{ display: "inline-flex", alignItems: "center", gap: 16 }}
      aria-label="CareLoop — Better care. Together."
    >
      <CareLoopMark size={markSize} />
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 34, lineHeight: 0.95, fontWeight: 500, letterSpacing: "-0.045em" }}>CareLoop</div>
        {showTagline ? (
          <div
            style={{
              marginTop: 10,
              fontFamily: "var(--font-mono)",
              fontSize: 9,
              lineHeight: 1,
              fontWeight: 500,
              letterSpacing: "0.28em",
              textTransform: "uppercase",
              color: "var(--brand-copper)"
            }}
          >
            Better care. Together.
          </div>
        ) : null}
      </div>
    </div>
  );
}

export default CareLoopLockup;
