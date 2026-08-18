# e2e/

Playwright against a real running backend, plus `@axe-core/playwright` accessibility scanning.

A peer of `frontend/`, `demoworld/` and `deploy/`: nothing under `careloop/` imports anything here,
and nothing here decides anything the application would otherwise decide.

## Why this is not inside `frontend/`

`@playwright/test`'s postinstall downloads roughly **150 MB of browsers on every `npm ci`**. The
`frontend` CI job runs `npm ci` and has nothing to do with e2e, so the moment Playwright lands in
`frontend/package.json` that job starts paying the download on every run — and so does every
developer who only wanted to build the bundle.

The two ways to stop that are `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1` in the frontend job's
environment, or keeping these dependencies in their own `package.json`. **This repository takes the
second**, for the reason it prefers an omission a sweep can find over a flag a caller can pass: the
frontend's dependency tree does not contain Playwright at all, so the download cannot happen there
by construction rather than because somebody remembered a variable. A new call site — a second CI
job, a Docker build, a developer's shell — inherits the property without knowing it exists.

`deploy/push.sh` excludes this directory: test code never lands on the instance.

## Running it

The stack is started for you. Two processes, exactly as the deployment runs them — Demo World on
8100 and CareLoop on 8010 with the built frontend served from its own origin.

```bash
cd frontend && npm run build      # once; the server refuses to start without frontend/dist
cd ../e2e && npm ci
npx playwright install chromium   # ~150 MB, once, and this step is not offline
npm test                          # the local project
npm run test:deployed             # the deployed project, opt-in
```

`npm test` reaches no model provider and no network beyond loopback, so it is offline and $0 once
the browser is installed. **`npm run test:deployed` is neither** — it drives the live demo, which
runs Bedrock Nova, so every scenario press spends real tokens. That is why it is opt-in, why it
never blocks a merge, and why it is a separate config rather than a flag on this one.

## What the local project runs against

`scripts/e2e_serve.py` — the real graph, policy engine, store, tool boundary, clinic and read-back,
with **scripted agents in place of a model**. `scripts/demo_serve.py` cannot be used: it resolves
models from the environment, unset means the mock, and `build_model` constructs that mock with no
script, so the first triage call raises and every case fails before classifying anything.

## The boot loader is held off by default

Every spec imports `test` from `tests/base.ts` rather than from `@playwright/test`. It is
`@playwright/test`'s, with one init script that seeds `careloop:boot` — the loader's own shipped
state, the thing `?boot=off` writes — so the overlay never appears. Nothing in this suite except
`tests/boot.spec.ts` is about the loader, and a suite whose assertions race a decoration is one that
eventually fails for a reason nobody can reproduce.

Seeded in storage rather than appended to the URL because `presentation.spec.ts` asserts the address
bar exactly; a query parameter would have to be written into its expectations, and changing what an
existing test asserts in order to accommodate a decoration is the thing not to do.

**To opt in**, import `test` from `@playwright/test` directly, as `boot.spec.ts` does. It then drives
unmodified URLs and sees exactly what a visitor sees.

## Conventions

- **Unmocked by default.** Exactly three states use `page.route`, each justified in its own spec.
- **Role and accessible name first.** Never a CSS class, `nth-child`, DOM structure, XPath or a
  coordinate. `data-testid` only where an element has no accessible name — and every such use is an
  accessibility defect noted in the commit that added it, not a workaround.
- **No fixed sleeps.** Playwright's auto-waiting and web-first assertions absorb timing; the drawer
  is the one exception and waits on `animationend` with a stable-bounding-box backstop.
- **`incomplete` is asserted, not just `violations`.** Its allowlist is hand-written, entry by
  entry, with the reason beside it. It is never auto-populated from a failing run.
