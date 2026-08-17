import type { ScenarioMeta } from "@/data/mock/scenarios";

interface ScenarioCardProps {
  scenario: ScenarioMeta;
  onPick(): void;
}

export default function ScenarioCard({ scenario, onPick }: ScenarioCardProps) {
  return (
    <button
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
}
