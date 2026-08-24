import { useId, useState } from "react";
import { standingLabel } from "@/services/presentation";
import type { Capability, CapabilityStanding } from "@/types/domain";

/** The tone each standing is drawn in.
 *
 *  Colour is never the only signal: every card carries the standing as a **word** as well
 *  (`ON ITS OWN`, `ASKS YOU FIRST`, `CURRENTLY UNAVAILABLE`), so the three groups are told apart by
 *  reading rather than by hue. That matters more here than on most surfaces — the difference between
 *  "does this by itself" and "cannot do this" is the whole content of the page. */
const TONES: Record<CapabilityStanding, string> = {
  AUTONOMOUS: "var(--state-verified)",
  REQUIRES_HUMAN: "var(--state-decision)",
  UNAVAILABLE: "var(--muted-foreground)"
};

function toneFor(standing: string): string {
  return TONES[standing as CapabilityStanding] ?? "var(--muted-foreground)";
}

/**
 * One capability, as My Boundaries renders it.
 *
 * Everything on this card came from `GET /api/boundaries`, which derives it from the ruleset the
 * policy gate actually decides by, the tool registry, and this caregiver's own configured
 * destinations. **Nothing here computes a standing, a requirement or an availability** — the card
 * would look identical if it did, which is exactly why it does not.
 *
 * The conditions are the body rather than a footnote: "only with this provider, only for a patient
 * your documents established" is what a caregiver actually wants to know, and the argument classes
 * and trust levels behind those sentences live in the disclosure below them. Readable language
 * first, the vocabulary that enforces it second.
 */
export default function CapabilityCard({ capability }: { capability: Capability }) {
  const [open, setOpen] = useState(false);
  const detailId = useId();
  const tone = toneFor(capability.standing);
  const unavailable = capability.standing === "UNAVAILABLE";

  return (
    <article
      data-capability={capability.toolName}
      id={capability.toolName}
      data-scene-exclude
      style={{
        border: "1px solid var(--edge, color-mix(in oklab, currentColor 14%, transparent))",
        borderRadius: "var(--radius-card)",
        padding: "22px 24px",
        display: "flex",
        flexDirection: "column",
        gap: 14,
        // Scroll-margin so a `/trust#book_appointment` deep link does not land the card under the
        // sticky header. Without it the highlight plays somewhere the reader cannot see.
        scrollMarginTop: 110
      }}
    >
      <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 16, flexWrap: "wrap" }}>
        <h3
          style={{
            margin: 0,
            fontFamily: "var(--font-serif)",
            fontWeight: 300,
            fontSize: 25,
            letterSpacing: "-0.01em",
            // A dimmed title would make an unavailable capability look broken. It is not broken —
            // it is correctly refused — so it keeps full contrast and the chip carries the state.
            color: "var(--foreground)"
          }}
        >
          {capability.title}
        </h3>
        <span
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 10.5,
            letterSpacing: "0.1em",
            color: tone,
            whiteSpace: "nowrap"
          }}
        >
          {standingLabel(capability.standing)}
        </span>
      </header>

      <p style={{ margin: 0, fontSize: 15.5, lineHeight: 1.6, color: "var(--muted-foreground)", maxWidth: "62ch" }}>
        {capability.summary}
      </p>

      {capability.unavailableReason ? (
        <p
          style={{
            margin: 0,
            fontSize: 15,
            lineHeight: 1.62,
            maxWidth: "62ch",
            padding: "14px 16px",
            borderLeft: `2px solid ${tone}`,
            background: "color-mix(in oklab, currentColor 4%, transparent)"
          }}
        >
          {capability.unavailableReason}
        </p>
      ) : null}

      {capability.conditions.length > 0 ? (
        <div style={{ display: "flex", flexDirection: "column" }}>
          {capability.conditions.map((condition) => (
            <div key={condition.name} className="cl-condition">
              <span className="cl-condition-label">{condition.label}</span>
              <span style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                {condition.value ? (
                  <span style={{ fontFamily: "var(--font-mono)", fontSize: 13.5, wordBreak: "break-word" }}>
                    {condition.value}
                  </span>
                ) : null}
                <span style={{ fontSize: 14.5, lineHeight: 1.55, color: "var(--muted-foreground)" }}>
                  {condition.requirement}
                </span>
              </span>
            </div>
          ))}
        </div>
      ) : null}

      {unavailable ? null : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, fontSize: 14.5, lineHeight: 1.58, color: "var(--muted-foreground)", maxWidth: "62ch" }}>
          <span>{capability.afterwards}</span>
          <span>{capability.ifTheAnswerIsLost}</span>
        </div>
      )}

      <div>
        <button
          className="cl-quiet"
          type="button"
          aria-expanded={open}
          aria-controls={detailId}
          onClick={() => setOpen((was) => !was)}
          style={{
            background: "transparent",
            border: 0,
            padding: "4px 0",
            fontSize: 13,
            color: "var(--nav-ink, var(--muted-foreground))",
            transition: "color 120ms ease",
            display: "flex",
            alignItems: "center",
            gap: 7
          }}
        >
          <span
            aria-hidden="true"
            style={{
              display: "inline-block",
              transform: open ? "rotate(90deg)" : "none",
              transition: "transform 200ms cubic-bezier(0.22,0.61,0.36,1)"
            }}
          >
            ›
          </span>
          Why this is enforced
        </button>

        <div className="cl-disclosure" data-open={open}>
          <div>
            {open ? (
              <dl id={detailId} style={{ margin: "10px 0 0", display: "flex", flexDirection: "column", gap: 8 }}>
                {capability.conditions.map((condition) => (
                  <div
                    key={condition.name}
                    style={{ display: "flex", gap: 10, flexWrap: "wrap", fontFamily: "var(--font-mono)", fontSize: 11.5 }}
                  >
                    <dt style={{ color: "var(--muted-foreground)" }}>{condition.name}</dt>
                    <dd style={{ margin: 0, display: "flex", gap: 10, flexWrap: "wrap" }}>
                      <span>{condition.argClass}</span>
                      {/* Written out rather than left blank. "requires no trust" is a fact the
                          backend derived on purpose, and an empty cell reads as missing data. */}
                      <span style={{ color: "var(--muted-foreground)" }}>
                        {condition.requiredTrust ?? "no trust required"}
                      </span>
                      {condition.trustKey ? (
                        <span style={{ color: "var(--muted-foreground)" }}>from {condition.trustKey}</span>
                      ) : null}
                    </dd>
                  </div>
                ))}
              </dl>
            ) : null}
          </div>
        </div>
      </div>
    </article>
  );
}
