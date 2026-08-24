import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { useScrollReveal } from "@/hooks/useScrollReveal";
import { api } from "@/services/api";
import { boundariesFor } from "@/services/presentation";
import BoundaryIdentityLine from "@/components/boundaries/BoundaryIdentityLine";
import CapabilityCard from "@/components/boundaries/CapabilityCard";
import RefusalList from "@/components/boundaries/RefusalList";
import RecoveryLoop from "@/components/trust/RecoveryLoop";
import SecurityEvidencePanel from "@/components/trust/SecurityEvidencePanel";
import type { Boundaries, Capability, CapabilityStanding } from "@/types/domain";

/* My Boundaries.
 *
 * The page that used to state CareLoop's authority as hand-written copy and now reads it from the
 * thing that enforces it. Every capability, condition, availability and refusal below arrives from
 * `GET /api/boundaries`, derived server-side from the ruleset the policy gate decides by, the tool
 * registry and this caller's own configured destinations.
 *
 * **There is no fallback copy, and that is the feature rather than an omission.** If the backend
 * cannot be read the page says so and shows nothing else. A page that quietly substituted its own
 * account of what CareLoop may do would be indistinguishable, to a reader, from one telling the
 * truth — and it would be the second source of truth this whole surface exists to have deleted. */

const LABEL = {
  fontFamily: "var(--font-mono)",
  fontSize: 11,
  letterSpacing: "0.12em",
  color: "var(--muted-foreground)"
} as const;

/** The three bands, in the order a caregiver's questions arrive.
 *
 *  *What does it do for me* comes before *what does it need me for*, which comes before *what is
 *  out of reach* — the last is the least actionable and is not where a page should open. A band with
 *  no capabilities is not rendered at all: an empty container reads as a fault, and "no capability
 *  is currently unavailable" is a fine thing for a page to say by saying nothing. */
const BANDS: { standing: CapabilityStanding; label: string; blurb: string }[] = [
  {
    standing: "AUTONOMOUS",
    label: "CARELOOP MAY, ON ITS OWN",
    blurb: "Nothing is asked of you. These run only when every condition below is established."
  },
  {
    standing: "REQUIRES_HUMAN",
    label: "CARELOOP MUST ASK YOU",
    blurb: "You see the exact action first. Approving releases that one action and nothing else."
  },
  {
    standing: "UNAVAILABLE",
    // Deliberately longer than the card's own chip, and not only to avoid saying the same string
    // twice on one screen: "under your boundaries" is the scope the backend actually derived. A
    // heading reading "CareLoop cannot do this" would claim a product limitation nobody measured.
    label: "CURRENTLY UNAVAILABLE UNDER YOUR BOUNDARIES",
    blurb: "Nothing your account currently configures can authorize these, so they are refused."
  }
];

type Reading = "loading" | "unreadable" | "read";

export default function BoundariesPage() {
  const reveal = useScrollReveal<HTMLDivElement>();
  const [reading, setReading] = useState<Reading>("loading");
  const [boundaries, setBoundaries] = useState<Boundaries | null>(null);

  /* Which capability a `/trust#<tool>` link asked for.
   *
   * Read here rather than left to the browser's own `:target`, because the fragment is resolved at
   * navigation time and these cards do not exist yet -- they arrive with the boundary response.
   * Measured on the deployed demo before this: the URL was right, the card was in the DOM with the
   * matching id, `:target` was null, nothing scrolled and no highlight played. */
  const targeted = useLocation().hash.replace(/^#/, "");

  useEffect(() => {
    let live = true;
    void api.getBoundaries().then((view) => {
      if (!live) return;
      setBoundaries(view ? boundariesFor(view) : null);
      setReading(view ? "read" : "unreadable");
    });
    return () => {
      live = false;
    };
  }, []);

  /* Bring the asked-for card into view once it exists.
   *
   * Keyed on `reading` as well as on the fragment, so it runs when the cards are really rendered
   * rather than on a page that is still a skeleton. `block: "center"` rather than the default,
   * because the header is sticky and the default puts the card's top edge underneath it.
   *
   * Motion is asked for, never assumed: a reader who has said they do not want it gets the same
   * destination without the travel, which is the bargain the `clLocate` rule makes too. */
  useEffect(() => {
    if (reading !== "read" || !targeted) return;

    /* Two frames, for the reason `useApplicationReady` waits two: a scroll issued in the same tick
       as the render is racing the browser's own navigation-time scroll restoration, and loses.
       Measured on the deployed demo — the highlight played on the right card and the page stayed at
       `scrollY: 0`, with a manual `scrollIntoView` from the console moving it 421px, which is what
       told the two apart. Cancelled on unmount so a reader who navigates away mid-wait is not
       yanked by a page they have left. */
    let frame = 0;
    const armed = requestAnimationFrame(() => {
      frame = requestAnimationFrame(() => {
        const card = document.getElementById(targeted);
        if (!card) return;
        /* Instant, and **never** `behavior: "smooth"`. Two reasons, and the first is that it does
           not work: measured on the deployed demo, a smooth `scrollIntoView` and a smooth
           `window.scrollTo` both moved the page zero pixels in an embedded browser where the same
           call with the default behaviour moved it 2049 — a silent no-op, which is the worst way
           for a judge-facing affordance to fail. The second is that it is the wrong choice anyway:
           this is a link arriving from another page, native fragment navigation lands instantly,
           and animating it means the 1600ms highlight is half spent before the card appears.
           Landing instantly also gives a reader who asked for no motion exactly what they asked
           for, without a media query deciding it for them.

           Optional call, and not defensiveness for its own sake: this is decoration, and decoration
           that can throw takes the whole page down with it. An environment without
           `scrollIntoView` still gets the card, the highlight and every word on it. */
        card.scrollIntoView?.({ block: "center" });
      });
    });
    return () => {
      cancelAnimationFrame(armed);
      cancelAnimationFrame(frame);
    };
  }, [reading, targeted]);

  const grouped = (standing: CapabilityStanding): Capability[] =>
    (boundaries?.capabilities ?? []).filter((capability) => capability.standing === standing);

  return (
    <main style={{ display: "flex", justifyContent: "center", padding: "68px var(--page-gutter) 130px" }}>
      <div
        ref={reveal}
        style={{
          width: "100%",
          maxWidth: "var(--page-max)",
          display: "flex",
          flexDirection: "column",
          gap: 52,
          animation: "clFade 320ms ease both"
        }}
      >
        <div data-scene-exclude style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <span style={LABEL}>MY BOUNDARIES</span>
          <h1
            style={{
              margin: 0,
              fontFamily: "var(--font-serif)",
              fontWeight: 300,
              fontSize: 44,
              lineHeight: 1.12,
              letterSpacing: "-0.02em",
              maxWidth: "20ch"
            }}
          >
            What CareLoop may do, and what it will not.
          </h1>
          <p style={{ margin: 0, fontSize: 16.5, lineHeight: 1.62, color: "var(--muted-foreground)", maxWidth: "62ch" }}>
            CareLoop reads untrusted material — documents, emails, external replies — and can take
            facts from it. It cannot take instructions from it. Everything on this page is read from
            the policy that stops it, not written alongside it.
          </p>
          <BoundaryIdentityLine />
        </div>

        {reading === "loading" ? <Skeleton /> : null}

        {reading === "unreadable" ? (
          <section
            data-scene-exclude
            role="status"
            style={{ display: "flex", flexDirection: "column", gap: 12, maxWidth: "62ch" }}
          >
            <h2 style={{ margin: 0, fontFamily: "var(--font-serif)", fontWeight: 300, fontSize: 26 }}>
              CareLoop can’t read your boundaries right now.
            </h2>
            <p style={{ margin: 0, fontSize: 15.5, lineHeight: 1.6, color: "var(--muted-foreground)" }}>
              This page only ever shows what the running policy says. Rather than describe limits it
              cannot currently confirm, it shows nothing. Try again in a moment.
            </p>
          </section>
        ) : null}

        {reading === "read"
          ? BANDS.map(({ standing, label, blurb }) => {
              const capabilities = grouped(standing);
              if (capabilities.length === 0) return null;
              return (
                <section
                  key={standing}
                  data-scene-exclude
                  data-band={standing}
                  style={{ display: "flex", flexDirection: "column", gap: 16 }}
                >
                  <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                    <h2 style={{ ...LABEL, margin: 0, fontWeight: 400 }}>{label}</h2>
                    <p style={{ margin: 0, fontSize: 15, lineHeight: 1.6, color: "var(--muted-foreground)", maxWidth: "62ch" }}>
                      {blurb}
                    </p>
                  </div>
                  {capabilities.map((capability) => (
                    <CapabilityCard
                      key={capability.toolName}
                      capability={capability}
                      locate={capability.toolName === targeted}
                    />
                  ))}
                </section>
              );
            })
          : null}

        {reading === "read" && boundaries && boundaries.refusals.length > 0 ? (
          <section data-scene-exclude style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <h2 style={{ ...LABEL, margin: 0, fontWeight: 400 }}>CARELOOP WILL REFUSE TO</h2>
            <RefusalList refusals={boundaries.refusals} />
          </section>
        ) : null}

        {/* Explanatory, and deliberately kept: both describe mechanisms rather than making a claim
            about what this caregiver may do, so neither is a second source of truth for the bands
            above. The recovery loop is what happens *after* an effect; the evidence panel is the
            measured adversarial result, pinned to `docs/security-evaluation.md` by its own test. */}
        <RecoveryLoop />
        <SecurityEvidencePanel />
      </div>
    </main>
  );
}

/** Three quiet blocks while the one request is in flight.
 *
 *  Shaped like the cards they precede so the page does not jump when they are replaced, and marked
 *  `aria-hidden` with a live region beside it: a screen reader is told "loading" once rather than
 *  read three empty boxes. */
function Skeleton() {
  return (
    <div data-scene-exclude style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <span role="status" style={{ ...LABEL }}>
        Reading your boundaries…
      </span>
      {[0, 1, 2].map((row) => (
        <div
          key={row}
          aria-hidden="true"
          style={{
            height: 132,
            border: "1px solid var(--edge, color-mix(in oklab, currentColor 10%, transparent))",
            borderRadius: "var(--radius-card)",
            background: "color-mix(in oklab, currentColor 3%, transparent)",
            animation: "clFade 320ms ease both",
            animationDelay: `${row * 70}ms`
          }}
        />
      ))}
    </div>
  );
}
