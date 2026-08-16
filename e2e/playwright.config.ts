/* The local project: a real backend on loopback, driven by scripted agents.
 *
 * **Unmocked by default.** The whole value of this layer is that it talks to the real FastAPI
 * contract — the 119 Vitest tests replace the network with `vi.stubGlobal("fetch", …)` and assert
 * against fixtures the frontend authored, so a backend field rename leaves them green and the demo
 * broken. Two states use `page.route`, both named and justified in their own spec; everything else
 * reaches the real store, the real policy engine and a real clinic over HTTP.
 *
 * **The stack it starts.** Two processes, exactly as the deployment runs them: Demo World on 8100
 * and CareLoop on 8010, with the built frontend served from CareLoop's own origin so the browser is
 * same-origin against `/api` and no CORS middleware is ever involved. `scripts/e2e_serve.py` binds
 * scripted agents in place of a model — see its docstring for why `demo_serve.py` cannot be used
 * here — and runs `--isolate-sessions`, so every browser context mints its own world and specs are
 * parallel-safe by construction rather than by ordering.
 *
 * **8010, not 8000.** Measured on the development machine: something unrelated already holds 8000,
 * and uvicorn's failure is an opaque WinError about socket permissions. A default that collides
 * with whatever else a developer runs is a default that costs everyone the same afternoon.
 *
 * Both are overridable, and no host is written into a spec.
 */

import { defineConfig, devices } from "@playwright/test";
import { existsSync } from "node:fs";
import { resolve } from "node:path";

const ROOT = resolve(import.meta.dirname, "..");

const API_PORT = process.env.CARELOOP_E2E_PORT ?? "8010";
const CLINIC_PORT = process.env.CARELOOP_E2E_CLINIC_PORT ?? "8100";

/** Where the suite points. Documented default; never a hard-coded host in a spec. */
export const BASE_URL = process.env.CARELOOP_E2E_BASE_URL ?? `http://127.0.0.1:${API_PORT}`;

const CLINIC_URL = `http://127.0.0.1:${CLINIC_PORT}`;

/** The interpreter that runs the two servers.
 *
 *  The repository venv on either platform, then whatever is on PATH — the same resolution order
 *  `deploy/push.sh` uses, and for the same reason: this is about finding an interpreter rather than
 *  about which one. CI installs the project into the job's own Python and falls through to `python`.
 */
function python(): string {
  if (process.env.CARELOOP_PYTHON) return process.env.CARELOOP_PYTHON;
  for (const candidate of [".venv/Scripts/python.exe", ".venv/bin/python"]) {
    const absolute = resolve(ROOT, candidate);
    // Absolute, and quoted at the call site. A *relative* `.venv/Scripts/python.exe` is not a
    // command any Windows shell will resolve, and the failure is silent in the worst way: with
    // `reuseExistingServer` on, a stack somebody happened to start by hand is used instead and
    // the config looks like it works until the day nothing is already running.
    if (existsSync(absolute)) return absolute;
  }
  return "python";
}

const PY = python();

export default defineConfig({
  testDir: "./tests",

  // Parallel by default. Each browser context mints its own session and therefore its own store,
  // run registry and clinic, so two specs cannot see each other's cases — the same property that
  // lets two judges open the deployed URL at once.
  fullyParallel: true,

  // **Two, measured, and not the default.** Playwright defaults to half the cores, which is 6 here
  // and wrong: at 6 and at 4 workers, `page.goto` timed out after 30s on six of ten tests, then
  // the survivors ran in 300ms once the others gave up. The backend is not what saturates — it
  // served twelve concurrent page loads in 0.7s total while that was happening — it is browser
  // memory, on a machine with 2.9 GB free of 16 GB. At two workers the same file runs in 3.4s with
  // nothing failing.
  //
  // Pinned rather than left to the default because the default is a function of core count and the
  // constraint is memory, so a 12-core box with a full RAM disk gets the worst of both. It also
  // matches what a two-core GitHub runner can actually do. The fix for a slow suite is fewer,
  // better tests — never a retry that hides this.
  //
  // **This is a local memory measurement, not a correctness constraint.** It was taken on a machine
  // with 2.9 GB of 16 GB free; a CI runner has more headroom, and nothing about the suite breaks at
  // a higher number. Raise it if your machine can take it — but re-measure rather than assume, and
  // do not read a failure at 6 workers as a defect in the thing being tested.
  workers: 2,

  // A spec that only passes on the second attempt is a spec nobody can trust. The local stack is
  // deterministic — loopback only, scripted agents, no real model — so a retry would hide a real
  // defect rather than absorb real flakiness. The deployed project sets its own budget.
  retries: 0,

  // `.only` left in a spec silently narrows CI to one test and reports green.
  forbidOnly: !!process.env.CI,

  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : [["list"]],

  use: {
    baseURL: BASE_URL,
    // Kept only for a failure: a trace per passing test is hundreds of megabytes of artefact
    // nobody opens.
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "off"
  },

  // Chromium alone. axe's engine is Chromium-shaped, and three browsers triples the runtime to
  // re-prove the same DOM. Cross-browser rendering is a different question from the one this
  // suite asks, and it is not worth answering badly here.
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],

  webServer: [
    {
      command: `"${PY}" -m demoworld --port ${CLINIC_PORT}`,
      cwd: ROOT,
      url: `${CLINIC_URL}/clinic/slots`,
      reuseExistingServer: !process.env.CI,
      stdout: "pipe",
      stderr: "pipe",
      timeout: 60_000
    },
    {
      // `--static-dir frontend/dist` is what makes this same-origin. The server refuses to start
      // if the bundle is absent, naming `npm run build` — failing loudly beats serving a 404.
      command:
        `"${PY}" scripts/e2e_serve.py --port ${API_PORT} ` +
        `--clinic-url ${CLINIC_URL} --static-dir frontend/dist`,
      cwd: ROOT,
      url: `${BASE_URL}/api/health`,
      reuseExistingServer: !process.env.CI,
      stdout: "pipe",
      stderr: "pipe",
      timeout: 60_000
    }
  ]
});
