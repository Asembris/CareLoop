import CareLoopMark from "./CareLoopMark";

interface CareLoopLogoProps {
  /** Drop the wordmark when horizontal space is genuinely constrained. */
  compact?: boolean;
}

/** Product-level identity: [mark] CareLoop. Used in the header, nowhere else. */
export function CareLoopLogo({ compact = false }: CareLoopLogoProps) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
      <CareLoopMark size={compact ? 20 : 23} />
      {compact ? null : (
        <span style={{ fontSize: 15, fontWeight: 600, letterSpacing: "-0.025em", whiteSpace: "nowrap" }}>
          CareLoop
        </span>
      )}
    </span>
  );
}

export default CareLoopLogo;
