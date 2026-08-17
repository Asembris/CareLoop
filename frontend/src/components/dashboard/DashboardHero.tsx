interface DashboardHeroProps {
  greeting: string;
  line: string;
  sub: string;
}

/** The one serif moment on the dashboard: a sentence, not a stat block. */
export default function DashboardHero({ greeting, line, sub }: DashboardHeroProps) {
  return (
    /* `data-scene-exclude`: the orchestration field reads this element's rect and drops its
       resting density to almost nothing inside it, so the sentence is never read through
       topology. Marked here rather than around the page column, because the field should frame
       the interface — the margins keep their structure. */
    <div data-scene-exclude style={{ display: "flex", flexDirection: "column", gap: 13 }}>
      <span style={{ fontSize: 14.5, color: "var(--muted-foreground)" }}>{greeting}</span>
      <h1
        style={{
          margin: 0,
          fontFamily: "var(--font-serif)",
          fontWeight: 300,
          fontSize: 46,
          lineHeight: 1.1,
          letterSpacing: "-0.02em",
          maxWidth: "20ch",
          textWrap: "pretty"
        }}
      >
        {line}
      </h1>
      <p style={{ margin: 0, fontSize: 16, color: "var(--muted-foreground)" }}>{sub}</p>
    </div>
  );
}
