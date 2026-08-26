import { useNavigate } from "react-router-dom";

/** The one thesis treatment in the product, shown only on a live dashboard with nothing on it.
 *
 *  A cold visitor's first screen is an empty inbox, and an empty inbox says nothing about what
 *  CareLoop is or why its shape matters. This block answers that once — who it is for, what the
 *  model may not do, and what to press — and then gets out of the way: the moment a case exists
 *  the dashboard speaks for itself, so the block never renders over real work, over a connection
 *  state that is already saying something, or over a press that is still starting.
 *
 *  It renders in exactly one place, deliberately. The three-word frame is easy to paste on every
 *  surface and cheapen; the sweep in `src/test/judge-intro.test.tsx` fails if the treatment
 *  appears a second time anywhere in the served source.
 *
 *  "VERIFIES", not "proves", and "supported actions", not "every action": three of the four
 *  effects are simulated and their completion says so on their own cases. A first screen claiming
 *  uniform verification would be the small lie the rest of this product refuses to tell.
 */
export default function JudgeIntro() {
  const navigate = useNavigate();

  const step = (text: string) => (
    <span
      style={{
        fontFamily: "var(--font-mono)",
        fontSize: 11,
        letterSpacing: "0.1em",
        whiteSpace: "nowrap"
      }}
    >
      {text}
    </span>
  );

  return (
    <section
      data-scene-exclude
      data-testid="judge-intro"
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 16,
        border: "1px solid var(--edge, color-mix(in oklab, currentColor 16%, transparent))",
        borderRadius: "var(--radius-card)",
        padding: "26px 28px",
        animation: "clRise 380ms cubic-bezier(0.22,0.61,0.36,1) both"
      }}
    >
      <p style={{ margin: 0, fontSize: 16.5, lineHeight: 1.55 }}>
        CareLoop handles care administration for family caregivers — referrals, appointments,
        records — and the model itself has no authority to act.
      </p>

      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", color: "var(--muted-foreground)" }}>
        {step("MODEL PROPOSES")}
        <span aria-hidden="true">→</span>
        {step("POLICY AUTHORIZES")}
        <span aria-hidden="true">→</span>
        {step("EXTERNAL SYSTEM VERIFIES")}
      </div>

      <p style={{ margin: 0, fontSize: 14.5, lineHeight: 1.6, color: "var(--muted-foreground)", maxWidth: "62ch" }}>
        The model proposes a next step; a deterministic policy decides whether it may happen; and
        supported actions are then checked against the outside system's own record before the case
        closes.
      </p>

      <button
        className="cl-solid"
        onClick={() => navigate("/demo")}
        style={{
          alignSelf: "flex-start",
          background: "var(--foreground)",
          color: "var(--background)",
          border: 0,
          borderRadius: "var(--radius-control)",
          padding: "11px 20px",
          fontSize: 14.5,
          fontWeight: 500
        }}
      >
        See CareLoop work →
      </button>
    </section>
  );
}
