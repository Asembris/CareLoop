interface SectionHeadingProps {
  label: string;
  tone?: string;
  ruleTone?: string;
  trailing?: string;
}

/** MONO / TRACKED label + hairline rule. The only section chrome in the product. */
export default function SectionHeading({ label, tone, ruleTone, trailing }: SectionHeadingProps) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
      <span
        style={{
          fontFamily: "var(--font-mono)",
          fontSize: 11,
          letterSpacing: "0.12em",
          color: tone ?? "var(--muted-foreground)"
        }}
      >
        {label}
      </span>
      <span
        style={{
          flex: 1,
          height: 1,
          background: ruleTone ?? "color-mix(in oklab, currentColor 12%, transparent)"
        }}
      />
      {trailing ? (
        <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--muted-foreground)" }}>
          {trailing}
        </span>
      ) : null}
    </div>
  );
}
