import { useEffect, useRef } from "react";

/** 60ms apart, as briefed — enough to read as a sequence rather than a single block moving. */
const STAGGER_MS = 60;

/** How long the observer has to say anything at all before its work is undone. See `watchdog`. */
const OBSERVER_WATCHDOG_MS = 1000;

/**
 * Reveal a column's sections as they arrive, once.
 *
 * Attach the returned ref to a flex column; its **direct children** are the reveal units. Nothing
 * is marked in JSX, so a page opts in by holding the ref and cannot half-adopt this by tagging
 * some children and forgetting others.
 *
 * ## The failure mode this is built around
 *
 * Content that never becomes visible is worse than no animation at all, so the hidden state is
 * never the resting state of anything:
 *
 * 1. **It is applied by JavaScript, at mount, and only to children already below the fold.**
 *    The stylesheet has no rule that hides anything on its own — `[data-reveal="pending"]` is
 *    set here or not at all. If this module never runs, never loads, or throws, every section is
 *    visible, and anything above the fold was never a candidate in the first place. That second
 *    half is also what keeps the reference screenshots honest: the first screen is untouched.
 *
 * 2. **A watchdog undoes the whole thing if the observer never reports.** Deliberately a watchdog
 *    on the *observer* rather than an unconditional timer that reveals everything: an
 *    `IntersectionObserver` delivers an initial callback for each target shortly after `observe`,
 *    so silence means the mechanism is not working and the safe response is to clear every pending
 *    child. A timer that revealed sections regardless would fire while the reader is still on the
 *    first screen and there would be nothing left to reveal — it would not be a failsafe, it would
 *    be an off switch on a delay.
 *
 * 3. **Focus reveals immediately.** A pending section is transparent but still in the accessibility
 *    tree and still focusable, so tabbing into one would move focus to something invisible. Any
 *    `focusin` inside a pending child reveals it before the caret lands.
 *
 * 4. **`prefers-reduced-motion` skips the mechanism entirely** — no attribute is set, so there is
 *    no state to transition and nothing to undo. The global reduced-motion rule in `global.css`
 *    zeroes durations; this never gets that far.
 *
 * The e2e a11y harness neutralises the pending state directly (`freezeAnimations` forces
 * `[data-reveal]` visible), so a scan reads the whole page rather than whatever a headless browser
 * happened to have scrolled into view.
 *
 * @param readyKey Re-runs the effect when it changes. A page whose content arrives *after* mount
 *   must pass one: `CasePage` renders its recovery state while the case is still loading, so at
 *   first mount the ref is null, the effect returns early, and without a key nothing would ever be
 *   marked once the real column appeared. Pass the identity of the thing being rendered
 *   (`careCase?.id`), not a loading boolean — a page that swaps one case for another needs the
 *   same re-run. Omit it for static pages.
 */
export function useScrollReveal<T extends HTMLElement>(readyKey?: unknown) {
  const ref = useRef<T>(null);

  useEffect(() => {
    const container = ref.current;
    if (!container) return;
    if (typeof IntersectionObserver === "undefined") return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;

    const pending = Array.from(container.children).filter(
      (el): el is HTMLElement =>
        el instanceof HTMLElement && el.getBoundingClientRect().top >= window.innerHeight
    );
    if (pending.length === 0) return;

    for (const el of pending) el.dataset.reveal = "pending";

    let reported = false;

    const reveal = (el: HTMLElement, order = 0) => {
      if (el.dataset.reveal !== "pending") return;
      el.style.transitionDelay = order * STAGGER_MS + "ms";
      el.dataset.reveal = "shown";
    };

    const observer = new IntersectionObserver(
      (entries) => {
        reported = true;
        // Staggered within one batch. Sections that come into view together are what the eye reads
        // as a sequence; two batches a scroll apart are separate events and each starts over.
        entries
          .filter((entry) => entry.isIntersecting)
          .forEach((entry, index) => {
            reveal(entry.target as HTMLElement, index);
            observer.unobserve(entry.target);
          });
      },
      // A section counts as arrived a little before its top edge, so it is already settled by the
      // time it is properly on screen rather than animating under the reader's eye.
      { rootMargin: "0px 0px -10% 0px" }
    );

    for (const el of pending) observer.observe(el);

    // Focus must never land on something transparent.
    const onFocusIn = (event: FocusEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      const host = pending.find((el) => el.contains(target));
      if (host) {
        observer.unobserve(host);
        reveal(host);
      }
    };
    container.addEventListener("focusin", onFocusIn);

    const watchdog = window.setTimeout(() => {
      if (reported) return;
      observer.disconnect();
      for (const el of pending) reveal(el);
    }, OBSERVER_WATCHDOG_MS);

    return () => {
      window.clearTimeout(watchdog);
      container.removeEventListener("focusin", onFocusIn);
      observer.disconnect();
      // Unmounting mid-reveal must not leave a detached node's state behind if React reuses it.
      for (const el of pending) if (el.dataset.reveal === "pending") reveal(el);
    };
  }, [readyKey]);

  return ref;
}
