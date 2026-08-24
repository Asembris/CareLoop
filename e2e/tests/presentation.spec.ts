/* Theme and the two slide-over drawers.
 *
 * Both are states rather than routes, and that is why they need a browser at all: neither has a
 * URL, so no amount of direct-load testing reaches them, and jsdom has no layout to move.
 */

import { expect, test } from "./base";
import { settled } from "./helpers";

test.describe("theme", () => {
  test("toggles, and says which way it is going", async ({ page }) => {
    await page.goto("/");

    // The control names its *destination*, which is what a screen reader user needs — "Switch to
    // dark" while light. Asserted through the accessible name rather than an icon or a class.
    const toDark = page.getByRole("button", { name: "Switch to dark" });
    await expect(toDark).toBeVisible();

    const light = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);

    await toDark.click();

    await expect(page.getByRole("button", { name: "Switch to light" })).toBeVisible();

    // The computed colour, not the class that produced it. A user perceives the background; the
    // mechanism is free to change, and this is also the value axe's contrast rule reads.
    await expect
      .poll(async () => page.evaluate(() => getComputedStyle(document.body).backgroundColor))
      .not.toBe(light);
  });

  test("survives a reload", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Switch to dark" }).click();
    await expect(page.getByRole("button", { name: "Switch to light" })).toBeVisible();

    await page.reload();

    // Still dark: the choice is a preference, and losing it on every navigation would make the
    // dark theme unusable rather than merely imperfect.
    await expect(page.getByRole("button", { name: "Switch to light" })).toBeVisible();
  });
});

test.describe("the under-the-hood drawer", () => {
  test("opens from the header and closes again", async ({ page }) => {
    await page.goto("/");

    const toggle = page.getByRole("button", { name: "Under the hood" });

    // It is a panel toggle, not a link, and it says so to assistive technology. This is also the
    // reason `/under-the-hood` is reachable only by typing it — nothing in the app links there.
    await expect(toggle).toHaveAttribute("aria-expanded", "false");

    await toggle.click();

    const drawer = page.getByRole("dialog", { name: "Under the hood" });
    await settled(drawer);
    await expect(drawer).toBeVisible();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");

    await drawer.getByRole("button", { name: "Close" }).click();

    // The drawer unmounts rather than hiding, so this is "gone" rather than "invisible".
    await expect(drawer).toHaveCount(0);
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  test("does not change the address", async ({ page }) => {
    await page.goto("/trust");
    await page.getByRole("button", { name: "Under the hood" }).click();
    await settled(page.getByRole("dialog", { name: "Under the hood" }));

    // Deliberately not a route: technical detail never costs the reader their place. A drawer that
    // pushed history would send Back to wherever they were before they opened it.
    await expect(page).toHaveURL(/\/trust$/);
    await expect(
      page.getByRole("heading", { name: "What CareLoop may do, and what it will not." })
    ).toBeVisible();
  });
});
