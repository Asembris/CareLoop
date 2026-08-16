/* The deployed project: the same specs, against the live demo through CloudFront.
 *
 * **Opt-in, never blocking, and the reason is cost rather than confidence.** The deployment runs
 * real Bedrock models — Nova Micro for triage, Nova 2 Lite for the planner and extraction — so
 * every scenario a spec presses spends real tokens and takes seconds rather than milliseconds. The
 * working agreement's "offline at $0" property covers the local project; it does **not** cover this
 * one, which is exactly why this is a separate config rather than a flag on the other. A flag is
 * something the next caller passes by accident.
 *
 * What only this can see: the CDN, the cache policy, header forwarding, the shape of the bundle
 * that was actually shipped, and whether the unit still carries `--isolate-sessions`. CI runs the
 * code and not the deployment, so none of those is reachable from the local project — and a check
 * against the origin rather than the distribution would pass in exactly the cases worth catching.
 *
 * **Safe to run while somebody is using the demo.** Every spec mints its own session, and a session
 * is its own store, run registry and clinic, so nothing here touches a visitor's world. That is the
 * same argument `scripts/verify_deploy.py` makes, and it is the same isolation these specs assert.
 *
 * **The routing specs will fail here until the deploy is refreshed.** They assert the not-found
 * page, which is in this repository and not yet in the shipped bundle. That is a true report about
 * the deployment rather than a broken spec, and it is precisely the class of drift this project
 * exists to catch.
 */

import { defineConfig, devices } from "@playwright/test";

/** The live distribution. Overridable; documented; never written into a spec. */
export const DEPLOYED_URL =
  process.env.CARELOOP_E2E_DEPLOYED_URL ?? "https://d2heuwvhvrdyig.cloudfront.net";

export default defineConfig({
  testDir: "./tests",

  // Serial. Every run costs model calls, and a burst of parallel scenarios against one small
  // instance measures its queue rather than the product.
  workers: 1,
  fullyParallel: false,

  // One retry, unlike the local project's none. The difference is honest rather than convenient:
  // this crosses a CDN and a public network to a t4g.small, so a transient failure here really can
  // be transport. Locally nothing crosses a machine boundary and a retry would hide a defect.
  retries: 1,

  forbidOnly: !!process.env.CI,
  reporter: [["list"]],

  // Generous: the slowest run measured end to end against this deployment is 14.6s, minutes after
  // a redeploy, and a scenario press waits for the graph.
  timeout: 120_000,

  use: {
    baseURL: DEPLOYED_URL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "off"
  },

  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }]

  // No `webServer`. There is nothing to start: the thing under test is already running, and a
  // config that could start one would be a config that might quietly test localhost instead.
});
