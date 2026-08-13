import type { ScenarioMeta } from "@/data/mock/scenarios";

interface ScenarioCardProps {
  scenario: ScenarioMeta;
  onPick(): void;
}

export default function ScenarioCard({ scenario, onPick }: ScenarioCardProps) {
  return (
    <button
      className="cl-scenario cl-row"
      onClick={onPick}
      style={{
        textAlign: "left",
        background: "transparent",
        border: "1px solid color-mix(in oklab, currentColor 16%, transparent)",
        borderRadius: "var(--radius-card)",
        padding: "24px 22px 22px",
        display: "flex",
        flexDirection: "column",
        gap: 10,
        transition: "border-color 160ms ease, background 160ms ease"
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
