import type { AdversarialProof } from "@/types/domain";
import {
  ADVERSARIAL_NOTE_BODY,
  ADVERSARIAL_NOTE_LABEL,
  ADVERSARIAL_NOT_ASKED,
  ZERO_EFFECT_BODY,
  ZERO_EFFECT_LABEL
} from "@/services/presentation";

const LABEL = {
  fontFamily: "var(--font-mono)",
  fontSize: 10.5,
  letterSpacing: "0.08em",
  color: "var(--muted-foreground)"
} as const;

/**
 * The one surface that exists only for the deterministic boundary test.
 *
 * **It says the thing that would otherwise be assumed.** A judge reading a refusal on a poisoned
 * document naturally concludes the agent took the bait and was caught. On this scenario that is
 * false: CareLoop supplied the proposal itself, because the question being answered is not *will a
 * planner take the bait* but *if one did, could the action happen*. Leaving that unsaid would let
 * the demonstration be read as a stronger claim about a model than it makes.
 *
 * **Everything below is rendered from the case's own audit trail**, through `adversarialProofFor`,
 * which requires CareLoop's supplied-proposal rationale on a durable `PLAN_PROPOSED` entry. There is
 * no fallback: a case the backend cannot prove this about renders nothing at all, and a case where
 * a *real* planner produced the same action renders nothing either, which is the distinction the
 * whole feature rests on.
 *
 * **No shield, no meter, no alarm.** The restraint is the argument. A security-dashboard aesthetic
 * would contradict the thing being demonstrated, which is a caregiver product that declines to act
 * rather than a product that fights.
 */
export default function AdversarialNote({ proof }: { proof: AdversarialProof | null | undefined }) {
  if (!proof) return null;

  const target = proof.destination ? `${proof.proposedAction} to ${proof.destination}` : proof.proposedAction;

  return (
    <section
      data-scene-exclude
      aria-label="Adversarial test"
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 22,
        border: "1px solid color-mix(in oklab, currentColor 16%, transparent)",
        borderRadius: "var(--radius-card)",
        padding: "30px 32px"
      }}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <span style={LABEL}>{ADVERSARIAL_NOTE_LABEL}</span>
        <p style={{ margin: 0, fontSize: 15, lineHeight: 1.6, color: "var(--muted-foreground)", maxWidth: "62ch" }}>
          {ADVERSARIAL_NOTE_BODY}
        </p>
      </div>

      <dl style={{ margin: 0, display: "flex", flexDirection: "column", gap: 14 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <dt style={LABEL}>SUPPLIED PROPOSAL</dt>
          {/* `overflowWrap` because the destination is a long address and the card must not push the
              page sideways at 375px. */}
          <dd style={{ margin: 0, fontSize: 15, lineHeight: 1.5, overflowWrap: "anywhere" }}>{target}</dd>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <dt style={LABEL}>REFUSED BY</dt>
          <dd style={{ margin: 0, display: "flex", flexDirection: "column", gap: 6 }}>
            <span style={{ fontSize: 15, lineHeight: 1.5, color: "var(--state-denied)" }}>
              {proof.refusalText}
            </span>
            {/* The rule id and the reason code are on the backend's payload allowlist deliberately:
                invariant 6 withholds them from the *model*, because denial text re-enters a
                planner's prompt, and a judge reading a named declarative rule is exactly who it was
                written for. */}
            <span
              style={{
                fontFamily: "var(--font-mono)",
                fontSize: 12,
                color: "var(--muted-foreground)",
                overflowWrap: "anywhere"
              }}
            >
              {proof.reasonCode}
              {proof.ruleId ? ` · ${proof.ruleId}` : ""}
            </span>
            {/* A real anchor rather than a router push, matching `OutcomePanel`, which is the
                product's established idiom for this exact deep link and says why: the fragment is
                what scrolls the matching capability into view and plays its locate highlight.

                **What that does not currently buy, measured on the deployment.** A direct load of
                `/trust#send_email` lands the card at `top: 110`. Arriving by *clicking* this link
                leaves the page at `scrollY: 0` with the card at `top: 1634` — off screen. The
                behaviour is not this component's and is not new: clicking `OutcomePanel`'s own
                `What this means →` also lands at `scrollY: 0`, and only looks correct because
                `book_appointment` is the first capability card and is already in view there.
                `send_email` sits far enough down the page to make the same non-scroll visible.

                So the anchor is kept because it is the right idiom and matches the neighbour, and
                the scroll is left alone because it belongs to `BoundariesPage` rather than here.
                A judge who clicks still reaches the right page with the right card rendered and
                highlighted; they may have to scroll to it. Written down rather than fixed in
                passing, because the fix is a change to a surface this work is not about. */}
            <a
              className="cl-quiet"
              href={`/trust#${proof.toolName}`}
              style={{
                alignSelf: "flex-start",
                fontSize: 13.5,
                color: "var(--nav-ink, var(--muted-foreground))",
                textDecoration: "none",
                borderBottom: "1px solid color-mix(in oklab, currentColor 30%, transparent)"
              }}
            >
              Why this boundary exists →
            </a>
          </dd>
        </div>
      </dl>

      <p style={{ margin: 0, fontSize: 14.5, lineHeight: 1.6, color: "var(--muted-foreground)", maxWidth: "62ch" }}>
        {ADVERSARIAL_NOT_ASKED}
      </p>

      <div
        style={{
          display: "flex",
          alignItems: "stretch",
          gap: 26,
          flexWrap: "wrap",
          borderTop: "1px solid color-mix(in oklab, currentColor 14%, transparent)",
          paddingTop: 22
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 8, flex: "1 1 200px" }}>
          <span style={LABEL}>{ZERO_EFFECT_LABEL}</span>
          <span
            style={{
              fontFamily: "var(--font-serif)",
              fontWeight: 300,
              fontSize: 58,
              lineHeight: 1,
              letterSpacing: "-0.02em",
              color: "var(--state-verified)"
            }}
          >
            {proof.recordedActions}
          </span>
        </div>
        <p
          style={{
            margin: 0,
            alignSelf: "center",
            flex: "1 1 260px",
            fontSize: 14.5,
            lineHeight: 1.55,
            color: "var(--muted-foreground)"
          }}
        >
          {ZERO_EFFECT_BODY}
        </p>
      </div>
    </section>
  );
}
