/**
 * The signature closure state: a ring draws, then a check lands.
 * Plays once, only when an external read-back matched. Never decorative.
 */
export default function VerificationSeal({ size = 78 }: { size?: number }) {
  return (
    <svg
      viewBox="0 0 72 72"
      width={size}
      height={size}
      aria-hidden="true"
      style={{ flex: "none", color: "var(--state-verified)" }}
    >
      <circle
        cx="36"
        cy="36"
        r="28"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeDasharray="176"
        transform="rotate(-90 36 36)"
        style={{ animation: "clDraw 900ms cubic-bezier(0.22,0.61,0.36,1) both" }}
      />
      <path
        d="M25 37 l 8 8 l 15 -18"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeDasharray="35"
        style={{ animation: "clCheck 900ms cubic-bezier(0.22,0.61,0.36,1) both" }}
      />
    </svg>
  );
}
