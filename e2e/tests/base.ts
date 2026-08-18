/* The suite's `test`, which is `@playwright/test`'s with the boot loader held off.
 *
 * **Why this exists.** The boot choreography paints an opaque overlay at z-index 9000 for about a
 * second and a half of every page load. Nothing in this suite except `boot.spec.ts` is about it,
 * and every spec here would otherwise pay for it twice: once in wall clock, on twelve files that
 * navigate repeatedly, and once in risk — `a11y.spec.ts` clicks the theme control as its first
 * action on eleven states, and `capture-goldens.spec.ts` photographs the page. A suite whose
 * assertions race a decoration is a suite that eventually fails for a reason nobody can reproduce,
 * which this repository has already paid for once.
 *
 * **It adds no hook to the application.** `careloop:boot` in `sessionStorage` is the loader's own
 * shipped state — how `?boot=off` survives a client-side navigation, and how the loader knows it
 * has already run in this tab. Seeding it is the same move `capture-goldens.spec.ts` already makes
 * with `careloop:theme`: put the browser in a state a real visitor can be in, then navigate
 * normally. There is no test-only branch in production for these tests to take.
 *
 * **Storage rather than `?boot=off` on the URL**, deliberately. Appending a query parameter is the
 * more obvious implementation and it is wrong here: `presentation.spec.ts` asserts the address bar
 * exactly, so the flag would have to be either absent from that spec or written into its
 * expectations — and changing what an existing test asserts in order to accommodate a decoration
 * is precisely the thing not to do. An init script changes nothing observable about the page.
 *
 * **Opting in.** `boot.spec.ts` imports `test` from `@playwright/test` directly, so it drives
 * unseeded contexts and unmodified URLs, and sees exactly what a visitor sees.
 */

import { test as base, type Page } from "@playwright/test";

/** The key the loader reads to decide whether this tab has already had its entrance. */
export const BOOT_STORAGE_KEY = "careloop:boot";

/** Put a page's context in the state `?boot=off` leaves behind, before anything on it runs. */
export async function holdBootOff(page: Page): Promise<void> {
  await page.addInitScript((key: string) => {
    try {
      window.sessionStorage.setItem(key, "off");
    } catch {
      /* A context without storage is one the loader cannot remember anything in either. */
    }
  }, BOOT_STORAGE_KEY);
}

export const test = base.extend({
  page: async ({ page }, use) => {
    await holdBootOff(page);
    await use(page);
  }
});

export { expect } from "@playwright/test";
export type { APIRequestContext, Locator, Page } from "@playwright/test";
