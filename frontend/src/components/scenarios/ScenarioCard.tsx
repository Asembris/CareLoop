import type { ScenarioMeta } from "@/data/mock/scenarios";

interface ScenarioCardProps {
  scenario: ScenarioMeta;
  onPick(): void;
  /** Start the card's `alternate` run instead. Only called when the card declares one. */
  onPickAlternate?(): void;
  /** The orchestration field sequence this card's own semantics match, when one does.
   *
   *  Optional, and absent is a real answer rather than a missing one: a card with no zone gets
   *  the ambient field, which claims nothing. See `SCENE_ZONES` in `DemoPage` for why scenario
   *  03 has none. The card's behaviour does not change either way — the field listens to the
   *  same hover and click the button already had. */
  sceneZone?: string;
}

export default function ScenarioCard({ scenario, onPick, onPickAlternate, sceneZone }: ScenarioCardProps) {
  const alternate = scenario.alternate;

  /* The card is a `<button>`, so the alternate cannot be one: a button inside a button is invalid
     HTML that browsers reposition, and the outer click would fire on the way out anyway. The
     alternate is therefore a sibling, and the two live in a wrapper that carries the scene zone
     the card used to carry itself — the orchestration field listens to hover and click on the
     zone, and a judge hovering the alternate is still hovering this scenario. */
  const card = (
    <button
      data-scene-zone={alternate ? undefined : sceneZone}
      data-scene-exclude
      // `cl-scenario` alone. It used to carry `cl-row` as well for the ground shift; both
      // classes define --edge at equal specificity, so which one won was decided by source
      // order in components.css — a card whose resting border depended on the order of an
      // unrelated rule. The ground shift is part of `.cl-scenario` now.
      className="cl-scenario"
      onClick={onPick}
      style={{
        textAlign: "left",
        background: "var(--ground, transparent)",
        border: "1px solid var(--edge, color-mix(in oklab, currentColor 16%, transparent))",
        borderRadius: "var(--radius-card)",
        padding: "24px 22px 22px",
        display: "flex",
        flexDirection: "column",
        gap: 10,
        transition: "border-color 120ms ease, background 120ms ease"
      }}
    >
      <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, letterSpacing: "0.08em", color: scenario.toneVar }}>
        {scenario.num}
      </span>
      <span style={{ fontSize: 17, fontWeight: 500, letterSpacing: "-0.01em" }}>{scenario.title}</span>
      <span style={{ fontSize: 14.5, lineHeight: 1.55, color: "var(--muted-foreground)" }}>{scenario.blurb}</span>
      <span style={{ fontSize: 13.5, marginTop: 6, color: scenario.toneVar }}>Run scenario →</span>
    </button>
  );

  if (!alternate) return card;

  return (
    <div
      data-scene-zone={sceneZone}
      data-scene-exclude
      style={{ display: "flex", flexDirection: "column", gap: 10 }}
    >
      {card}
      <div style={{ display: "flex", flexDirection: "column", gap: 4, padding: "0 22px" }}>
        <button
          className="cl-quiet"
          onClick={onPickAlternate}
          style={{
            alignSelf: "flex-start",
            background: "transparent",
            border: 0,
            padding: 0,
            fontSize: 13.5,
            textAlign: "left",
            color: "var(--nav-ink, var(--muted-foreground))",
            transition: "color 120ms ease",
            borderBottom: "1px solid color-mix(in oklab, currentColor 30%, transparent)"
          }}
        >
          {alternate.label}
        </button>
        <span style={{ fontSize: 12.5, lineHeight: 1.5, color: "var(--muted-foreground)" }}>
          {alternate.note}
        </span>
      </div>
    </div>
  );
}
