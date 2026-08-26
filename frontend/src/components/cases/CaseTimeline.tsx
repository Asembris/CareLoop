import { useNavigate } from "react-router-dom";
import type { CareCase } from "@/types/domain";
import { eventToneColor } from "@/lib/tone";
import { useUi } from "@/hooks/useUi";

/**
 * The spine: one continuous hairline through every event. Evidence hangs off it
 * as an inline citation; a blocked branch is a dashed offshoot that simply stops.
 */
export default function CaseTimeline({ careCase }: { careCase: CareCase }) {
  const { openLens } = useUi();
  const navigate = useNavigate();

  return (
    <section data-scene-exclude style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, letterSpacing: "0.12em", color: "var(--muted-foreground)" }}>
        WHAT CARELOOP DID
      </span>

      <div style={{ position: "relative", paddingLeft: 2 }}>
        <span
          aria-hidden="true"
          style={{
            position: "absolute",
            left: 5,
            top: 10,
            bottom: 12,
            width: 1,
            background: "color-mix(in oklab, currentColor 16%, transparent)"
          }}
        />
        <ol style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {careCase.events.map((e) => {
            const toned = e.tone !== "neutral";
            const toneColor = eventToneColor(e.tone);
            return (
              <li
                key={e.id}
                style={{
                  position: "relative",
                  display: "flex",
                  gap: 18,
                  padding: "9px 0 9px 26px",
                  animation: "clRise 380ms cubic-bezier(0.22,0.61,0.36,1) both"
                }}
              >
                <span
                  aria-hidden="true"
                  style={{
                    position: "absolute",
                    left: 0,
                    top: 15,
                    width: 11,
                    height: 11,
                    borderRadius: "50%",
                    border: "1px solid " + (toned ? toneColor : "color-mix(in oklab, currentColor 35%, transparent)"),
                    background: toned ? toneColor : "var(--background)"
                  }}
                />
                <span
                  style={{
                    fontFamily: "var(--font-mono)",
                    fontSize: 12,
                    color: "var(--muted-foreground)",
                    width: 44,
                    flex: "none",
                    paddingTop: 2
                  }}
                >
                  {e.time}
                </span>
                <span style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                  <span style={{ fontSize: 15.5, lineHeight: 1.5, color: toned ? toneColor : "inherit" }}>{e.text}</span>

                  {e.source ? (
                    <button
                      className="cl-source"
                      onClick={() => openLens(e.source!)}
                      style={{
                        alignSelf: "flex-start",
                        background: "transparent",
                        border: 0,
                        padding: 0,
                        fontSize: 13,
                        color: "var(--state-controlled)",
                        borderBottom: "1px var(--chip-underline, dotted) currentColor"
                      }}
                    >
                      {e.source.source}
                    </button>
                  ) : null}

                  {e.blocked ? (
                    <span
                      style={{
                        display: "flex",
                        flexDirection: "column",
                        gap: 7,
                        marginTop: 6,
                        borderLeft: "1px dashed color-mix(in oklab, var(--state-untrusted) 55%, transparent)",
                        padding: "4px 0 4px 16px"
                      }}
                    >
                      {e.blocked.map((b) => (
                        <span key={b.text} style={{ display: "flex", alignItems: "baseline", gap: 9, fontSize: 14 }}>
                          <span aria-hidden="true" style={{ color: "var(--state-denied)" }}>
                            ×
                          </span>
                          <span style={{ color: "var(--muted-foreground)" }}>{b.text}</span>
                          <span style={{ fontSize: 12.5, color: "var(--state-denied)" }}>Blocked</span>
                        </span>
                      ))}
                      {/* To My Boundaries, not to the Under-the-hood drawer. This is the most
                          likely "explain this" press on the page, and the drawer answers it with
                          rule ids and idempotency keys — the engineer's altitude. `/trust` states
                          the same boundary in the caregiver's words, derived from the ruleset that
                          just refused these calls, and the drawer stays one header press away for
                          whoever wants the raw record. */}
                      <button
                        className="cl-quiet"
                        onClick={() => navigate("/trust")}
                        style={{
                          alignSelf: "flex-start",
                          background: "transparent",
                          border: 0,
                          padding: "2px 0 0",
                          fontSize: 13,
                          color: "var(--nav-ink, var(--muted-foreground))",
                          transition: "color 120ms ease",
                          borderBottom: "1px solid color-mix(in oklab, currentColor 30%, transparent)"
                        }}
                      >
                        Why were these blocked?
                      </button>
                    </span>
                  ) : null}
                </span>
              </li>
            );
          })}
        </ol>
      </div>
    </section>
  );
}
