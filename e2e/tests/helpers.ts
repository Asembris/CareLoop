/* Shared plumbing for the specs. No assertions live here.
 *
 * A helper that asserts is a helper whose failures point at the wrong file, so these only fetch,
 * wait and return. What is true about a response is stated in the spec that cares.
 */

import { expect, type APIRequestContext, type Locator, type Page } from "@playwright/test";

/** The header a request carries its world in. `careloop.api.sessions.SESSION_HEADER`. */
export const SESSION_HEADER = "X-CareLoop-Session";

/** The audit identity a decision is recorded against. Not authentication — see `careloop/api/deps.py`. */
export const CAREGIVER_HEADER = "X-Caregiver";

/** The name the shipped frontend records decisions under, so the API and UI paths agree. */
export const CAREGIVER = "Maya";

/** A world of this caller's own.
 *
 *  Every API spec mints one rather than sharing: a world is a store, a run registry and a clinic,
 *  so two specs that shared one would see each other's cases and each other's resets.
 */
export async function mintSession(request: APIRequestContext): Promise<string> {
  const minted = await request.post("/api/sessions");
  if (minted.status() !== 201) {
    throw new Error(`could not mint a session: ${minted.status()} ${await minted.text()}`);
  }
  return (await minted.json()).session;
}

export function withSession(session: string): Record<string, string> {
  return { [SESSION_HEADER]: session };
}

/** Press a demo scenario and block until its run settles.
 *
 *  `?wait=` is what a test uses instead of polling — the verbs return 202 before the graph has
 *  done anything, and a spec that raced that would be asserting on whichever moment it caught.
 *  The shipped frontend never passes it, deliberately, because the dashboard exists to show work
 *  happening; a spec is the opposite case and wants the settled truth.
 */
export async function runScenario(
  request: APIRequestContext,
  session: string,
  key: string,
  seconds = 90
) {
  return request.post(`/api/demo/scenarios/${key}?wait=${seconds}`, {
    headers: withSession(session)
  });
}

/** Wait until an element has stopped moving.
 *
 *  **Never a fixed sleep.** A scan taken mid-transit reports whatever was under the element at that
 *  instant, and `elmPartiallyObscured` and `bgOverlap` are exactly the review items a moving panel
 *  produces — so a sleep long enough to be safe today is a flake tomorrow and a false green in
 *  between.
 *
 *  Built on `getAnimations()` rather than an `animationend` listener, and the difference is a real
 *  race: the drawer mounts on open and runs a 320ms CSS *animation* (`clSlide`), so a listener
 *  attached after that animation has already finished never fires and the wait hangs until timeout.
 *  `Animation.finished` resolves immediately for an animation that is already done, which is the
 *  behaviour a wait actually wants. `transitionend` would never fire here at all — there is no
 *  transition on this element.
 *
 *  The stable-bounding-box poll is the backstop, for anything animated by a means the Web
 *  Animations API does not report.
 */
export async function settled(locator: Locator): Promise<void> {
  await locator.waitFor({ state: "visible" });

  await locator.evaluate(
    (element) =>
      Promise.all(element.getAnimations().map((animation) => animation.finished)).then(
        () => undefined,
        () => undefined
      )
  );

  let previous = "";
  await expect
    .poll(async () => {
      const box = JSON.stringify(await locator.boundingBox());
      const stable = box === previous;
      previous = box;
      return stable;
    })
    .toBe(true);
}

/** The session the *browser* minted, once the page has asked for one.
 *
 *  The `request` fixture and the page are different clients and therefore different worlds. A spec
 *  that seeds through `request` and then asserts through `page` is asserting about somebody else's
 *  cases, which is the single easiest way to write an e2e test that passes for the wrong reason.
 *  This reads the id the app itself stored, so API-driven setup and UI assertions share one world.
 */
export async function sessionFromPage(page: Page): Promise<string> {
  return page.waitForFunction(() => window.sessionStorage.getItem("careloop.session")).then(
    (handle) => handle.jsonValue() as Promise<string>
  );
}
