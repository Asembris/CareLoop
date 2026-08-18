import { useEffect } from "react";

/** The event the boot layer listens for. `frontend/public/careloop-boot.js`. */
export const READY_EVENT = "careloop:ready";

/* Once per page load, and the flag is raised **at dispatch** rather than when the frames are
   scheduled. `StrictMode` mounts, tears down and remounts in development: a flag raised on
   scheduling would be true for the second mount while the first mount's frames had already been
   cancelled by its own cleanup, so nothing would ever be announced and the loader would sit on its
   timeout for nine seconds. The loader's own `ready()` is idempotent as well, which is the far
   half of two guards. */
let announced = false;

/** Test seam: nothing in the application resets this, and nothing should. */
export function resetApplicationReadyForTests(): void {
  announced = false;
}

/**
 * Announce that CareLoop is genuinely usable.
 *
 * ## What "ready" means here, and what it deliberately does not
 *
 * The earliest truthful boundary is **the first committed frame of the routed application, after
 * it has painted**. Everything before that is either invisible or a lie; everything after it is
 * something the application is already showing honestly on its own.
 *
 * There is no async bootstrap to wait for, and that was checked rather than assumed: no
 * `React.lazy`, no `Suspense`, no dynamic import, no service worker. `BrowserRouter` initialises
 * synchronously. The theme is resolved before any of this runs — the blocking script in
 * `index.html` writes `class="dark"` and `data-theme` before the first stylesheet, so the loader
 * already painted in the right theme.
 *
 * **The backend is excluded on purpose.** `CareLoopProvider` starts from `emptyDashboard()` and
 * mints its session lazily on the first request, so nothing here is waiting on the network. A
 * loader that held on until `GET /api/cases` answered would sit over an unreachable backend for
 * the full nine seconds, and it would hide `connecting` and the connection banner — which are
 * designed application states saying something true, not loading states standing in for one.
 *
 * ## Why this is an effect in `App` and not two frames after `createRoot().render()`
 *
 * A parent's effect runs after every child's, so this is the last effect of the initial mount:
 * after `ThemeProvider` has written the theme, after `CareLoopProvider` has subscribed, after the
 * matched route has mounted. `render()` returns before React has committed anything — the initial
 * render is scheduled on a task, so a `requestAnimationFrame` in `main.tsx` can and does run
 * first. The double frame then turns "committed" into "painted": one to reach the next frame's
 * work, one to be after that frame has been presented.
 *
 * Failing to announce is survivable by construction — the loader leaves on its own timeout and the
 * reaper behind it — so this never throws into the application it exists to announce.
 */
export function useApplicationReady(): void {
  useEffect(() => {
    if (announced) return;

    let second = 0;
    const first = requestAnimationFrame(() => {
      second = requestAnimationFrame(() => {
        if (announced) return;
        announced = true;
        try {
          window.dispatchEvent(new CustomEvent(READY_EVENT));
        } catch {
          /* An environment without CustomEvent has no loader to tell either. */
        }
      });
    });

    return () => {
      cancelAnimationFrame(first);
      if (second) cancelAnimationFrame(second);
    };
  }, []);
}
