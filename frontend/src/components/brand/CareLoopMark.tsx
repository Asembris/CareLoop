interface CareLoopMarkProps {
  size?: number;
  /** Render in a single colour (documents, print, watermarks). */
  monochrome?: boolean;
  /** Supply only when the mark stands alone and carries the brand name. */
  title?: string;
  className?: string;
}

/**
 * The CareLoop mark: two figures whose bodies sweep into one shared point.
 * The larger forest figure is the caregiver, the smaller sage figure the person
 * cared for; their paths converge without meeting, leaving the loop open at the
 * top — the gap a human decision fills.
 *
 * Geometry is locked. Verified legible at 16/20/23/32/64/128px, light and dark.
 */
export function CareLoopMark({ size = 23, monochrome = false, title, className = "" }: CareLoopMarkProps) {
  const forest = monochrome ? "currentColor" : "var(--brand-forest)";
  const sage = monochrome ? "currentColor" : "var(--brand-sage)";
  const titleId = title ? "careloop-mark-title" : undefined;

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      role={title ? "img" : undefined}
      aria-hidden={title ? undefined : true}
      aria-labelledby={titleId}
      preserveAspectRatio="xMidYMid meet"
      style={{ display: "block", flex: "none" }}
    >
      {title ? <title id={titleId}>{title}</title> : null}
      <circle cx="18.5" cy="11.5" r="5.4" fill={forest} />
      <circle cx="45.5" cy="15" r="4.3" fill={sage} />
      <path
        d="M32 55.5c11.5-7.2 24.5-14.7 24.5-24.2 0-8-6.8-11.3-12-7.9-2.5 1.6-3.9 3.7-4.3 6"
        stroke={sage}
        strokeWidth="5.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M32 55.5C20 48 7.5 40.5 7.5 30.5c0-8.5 7.5-12 13-8.5 2.6 1.7 4.1 3.9 4.5 6.5"
        stroke={forest}
        strokeWidth="6.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export default CareLoopMark;
