import { defineConfig, devices } from "@playwright/test";

// The dossier is a static artifact, so this config is deliberately smaller than e2e/'s: one
// browser, no servers to orchestrate beyond a file server, and no application to seed.
//
// `serve.py` publishes site/ at /CareLoop/ and nothing at /, so `baseURL` carries the project path
// and every navigation in the spec is relative to it. A gate that ran at the origin root would
// pass on a page whose assets are root-absolute, which is exactly the failure GitHub project pages
// produce and exactly what this must catch.
const PORT = Number(process.env.SITE_PORT ?? 8801);

export default defineConfig({
  testDir: ".",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : [["list"]],
  timeout: 60_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: `http://127.0.0.1:${PORT}/CareLoop/`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `python ../../site/tests/serve.py --port ${PORT}`,
    url: `http://127.0.0.1:${PORT}/CareLoop/`,
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
    stdout: "ignore",
    stderr: "pipe",
  },
});
