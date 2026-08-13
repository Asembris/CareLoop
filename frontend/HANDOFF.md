# CareLoop UI Handoff

This package contains the approved CareLoop frontend from Claude Design.

Your first priority is visual and behavioral fidelity.

Do not redesign the UI.

Do not alter spacing, typography, colors, navigation, logo geometry, copy, or
interaction hierarchy unless explicitly requested by the user.

The current frontend intentionally uses mock adapters. The next engineering
phase is to connect these existing interfaces to the real CareLoop backend
without changing the approved frontend experience.

---

## 1. Purpose

CareLoop is an autonomous administrative agent for family caregivers. The UI has
one job: make it obvious, within seconds, that the product is running the
administrative work rather than chatting about it — and that the model is not
the authority.

Three ideas the interface must keep expressing:

1. Work happens continuously in the background.
2. A human appears in the loop only where authority genuinely belongs to them.
3. A case does not close until an independent read-back of the external system
   agrees with what was authorized.

## 2. Current state

- Complete, runnable React + TypeScript + Vite application.
- All screens, states, drawers and scenario choreography implemented.
- 100% mock data via a single adapter (`src/services/mockApi.ts`).
- No backend, no environment variables, no network calls required to run.
- No raster logo anywhere. The mark is inline SVG.

## 3. How to run

```bash
npm install
npm run dev          # http://localhost:5173
```

Start at `/demo` for the judge/demo path, or `/` for the dashboard.

## 4. Build commands

```bash
npm run build        # tsc --noEmit && vite build
npm run preview      # serve the production build
npm run typecheck    # tsc --noEmit
```

**Verification status — read this.** These commands are the intended contract,
but they were **not executed** in the design environment that produced this
package (it has no Node runtime). Run `npm install && npm run build` as your
first action and fix anything that surfaces before touching the UI. There is no
lockfile in the package; `npm install` will generate one.

## 5. Directory map

```
src/
  app/App.tsx                     providers + routes + both drawers
  pages/                          HomePage, DemoPage, CasePage, TrustPage
  components/
    brand/                        CareLoopMark, CareLoopLogo, CareLoopLockup
    navigation/AppHeader.tsx      the locked navbar
    dashboard/                    DashboardHero, SectionHeading, DecisionCard, CaseRow
    cases/                        OutcomePanel, VerificationSeal, CaseTimeline,
                                  EvidenceGrid, EvidenceLens
    trust/                        AuthorityGrid, GuaranteeList, SecurityEvidencePanel
    scenarios/ScenarioCard.tsx
    under-the-hood/UnderTheHoodDrawer.tsx
  data/
    mock/                         cases.ts, runs.ts, scenarios.ts, evidence.ts
    trust.ts                      authority rows, guarantees, agent stages
  hooks/                          useCareLoop, useTheme, useUi
  lib/tone.ts                     state → color/copy mapping
  services/api.ts                 THE SEAM — swap mockApi for realApi here
  services/mockApi.ts             timed scenario choreography
  styles/tokens.css               all theme tokens (exact values)
  styles/global.css               resets + the five keyframes
  styles/components.css           the only class-based CSS: hover states
  types/domain.ts                 the frontend domain contract
```

## 6. Routes

| Route | Screen | Notes |
|---|---|---|
| `/` | Home | Needs you / Working / Completed |
| `/demo` | Demo launcher | Three scenarios; entry point for judges |
| `/case/:caseId` | Case detail | Outcome, spine timeline, evidence |
| `/trust` | Trust | Authority model + guarantees |

Under the hood and Evidence are **drawers**, not routes — they open over any
screen so technical detail never costs the user their place.

## 7. Component map

- `AppHeader` — logo lockup, three nav links with a 1px forest underline for the
  active one, single separator, 32×32 theme toggle, quiet "Maya".
- `DecisionCard` — the gap in the loop. Renders Approve/Reject **only** for
  `REQUIRES_HUMAN_APPROVAL`.
- `CaseRow` — one line per case; a rotating arc while working, a closed ring when done.
- `CaseTimeline` — the spine; evidence citations and dashed blocked branches hang off it.
- `VerificationSeal` — the 900ms ring-and-check closure. Plays once, only on a real match.
- `UnderTheHoodDrawer` — agent path, authority model, live run record.
- `EvidenceLens` — excerpt with the extracted span highlighted, plus trust tiers.

## 8. Design system

Full detail in `docs/DESIGN_SYSTEM.md`. Summary:

- Roughly 90% of every screen is paper, ink and hairline rules. Color appears
  only when it carries state.
- Type: Newsreader 300 (one editorial line per screen), Instrument Sans (all UI),
  IBM Plex Mono (labels, IDs, rule codes).
- Four motion durations: 120 / 220 / 420 / 900ms. Transform and opacity only.
- Everything is inline style + CSS custom properties. The only class-based CSS
  in the app is the hover file.

## 9. Logo system

Three levels, all vector:

- **Level A** `CareLoopLogo` — `[mark] CareLoop`, 23px mark. Header only.
- **Level B** `CareLoopLockup` — mark + wordmark + "BETTER CARE. TOGETHER."
  Splash, pitch frames, README, social. **Never inside the product UI.**
- **Level C** `CareLoopMark` — symbol alone. Favicon (`public/favicon.svg`),
  app icon, constrained mobile.

The mark's geometry is locked and verified legible at 16/20/23/32/64/128px in
both themes. `monochrome` renders it in `currentColor` for print and documents.
Fix optical problems by adjusting geometry, never by adding shadows or outlines.

## 10. Theme architecture

`src/styles/tokens.css` defines `:root` (light) and `.dark`.
`useTheme` toggles the `dark` class on `<html>` and persists to localStorage.

Light is canonical; dark is a deliberate second theme, not an inversion — the
forest green lightens to sage, the copper warms, and nothing glows.

Muted text is computed per theme (`color-mix` of foreground into background at
76% light / 62% dark) so it never resolves through an inherited `currentColor`.
Both values were measured for WCAG AA at 14px.

## 11. Mock state architecture

One store lives inside `mockApi`. `CareLoopProvider` subscribes to it, and the
dashboard, case detail and Under the Hood drawer all read the same
`DashboardState` — the drawer never fabricates a parallel trace.

Case and run identifiers are already shared (`case.runId` ↔ `run.id`), which is
what makes the backend swap surgical.

## 12. Scenario behavior

| Scenario | Timings | What must remain true |
|---|---|---|
| 01 Referral | 1100 / 2400 / 3700ms, then 1300 / 2900ms after approval | Exactly one human decision. Verification closes the loop. |
| 02 Malicious document | 1200 / 2600 / 4100 / 5400ms | Three unsafe instructions isolated; the legitimate branch continues and completes. |
| 03 Verification mismatch | 1300 / 2900ms | Case must **not** show as completed. No Approve button. |

Do not speed these up. The pacing is the demo.

## 13. Domain types

`src/types/domain.ts` is the contract. The types that carry real safety weight:

- `DecisionKind` — `REQUIRES_HUMAN_APPROVAL` vs `NO_SAFE_ACTION_AVAILABLE`.
  The second must never render an approval affordance.
- `VerificationState` — `PENDING | MATCH | MISMATCH`. A `MISMATCH` may not be
  presented as a completed case.
- `PolicyVerdict`, `RunStage`, `Authority` — drawer vocabulary.

If the backend's field names differ, adapt them inside `services/`, not in components.

## 14. API adapter boundary

```ts
// src/services/api.ts
export const api: CareLoopApi = mockApi;   // today
export const api: CareLoopApi = realApi;   // after integration
```

`CareLoopApi` is the whole surface: `getDashboard`, `getCase`, `getRun`,
`approveAction`, `runScenario`, `resetDemo`, `subscribe`.

## 15. Where real API wiring should happen

Create `src/services/realApi.ts` implementing `CareLoopApi` against the
FastAPI backend, and change one line in `api.ts`. `subscribe` becomes polling:
POST returns a run id, poll `/api/runs/{id}` at ~1s, re-emit `DashboardState`,
**stop polling when the run settles**. Dev requests go through the `/api` proxy
in `vite.config.ts` — do not weaken backend CORS.

See `docs/API_INTEGRATION_MAP.md` for the full mapping and
`docs/MOCK_DATA.md` for every mocked value and its replacement.

## 16. What must NOT be redesigned

### UI LOCK

The current UI has been approved.

Do not proactively:
- redesign the navbar
- change the logo
- replace the serif
- alter the color palette
- introduce gradients
- add dashboard widgets
- add chat UI
- change page hierarchy
- add glassmorphism
- replace subtle borders with cards/shadows
- change copy for stylistic reasons
- simplify the Trust page
- remove Under the Hood
- turn the product into a generic SaaS dashboard

Backend integration should preserve the existing frontend contract.

Locked copy includes, verbatim:

- "Nothing needs you right now."
- "One decision is waiting."
- "The model never authorizes its own actions."
- "See CareLoop work."
- "Demo uses synthetic care data. No real patient information."

## 17. Visual regression references

`reference/screenshots/` holds ten golden states (light and dark) captured at
1280px wide. `reference/DESIGN_REFERENCE.md` documents the measurements.
`reference/original-prototype.dc.html` is the approved design-environment
prototype, kept for reference only — it is not part of the build.

When implementation and written description appear to conflict, use the supplied
screenshots and current source implementation as the visual source of truth.

## 18. Known placeholders

- All case, document and clinic data is synthetic. No real patient information.
- Security-evidence numbers on `/trust` are intentionally blank — do not fill
  them until the adversarial suite has actually run.
- `Maya` in the header is a label, not an account menu. There is no auth.
- Document previews are styled excerpts, not rendered PDFs.
- Timestamps in scenario timelines use the browser clock at run time.
- No tests ship in this package (see §20).

## 19. Accessibility notes

- `aria-current="page"` on the active nav item; `aria-expanded` on Under the hood —
  the active state is a subtle underline, so it must also be announced.
- Theme toggle is icon-only with `aria-label` and `title`.
- The header mark is `aria-hidden` because the wordmark beside it says CareLoop;
  `CareLoopMark` takes a `title` prop for standalone use.
- Muted text meets AA at 14px in both themes. Never encode state in color alone —
  every state also carries a word and a distinct mark shape.
- All motion respects `prefers-reduced-motion` via one global rule.
- Timeline is an ordered list. Drawers use `role="dialog"` with labels.
- **Not yet done:** focus trapping and Escape-to-close in the two drawers, and a
  live region announcing case state changes. Both are worth adding.

## 20. Recommended integration order

1. `npm install && npm run build`. Fix anything before touching UI.
2. Compare every screenshot in `reference/screenshots/` against the running app.
3. Read the backend's real schemas. Confirm the six questions in
   `docs/API_INTEGRATION_MAP.md` — especially the decision-kind discriminator
   and whether verification returns both authorized and observed values.
4. Write `realApi.ts` for reads only (`getDashboard`, `getCase`, `getRun`) and
   swap `api.ts`. The UI should look identical against real data.
5. Add polling in `subscribe`, with a stop condition on settle.
6. Wire `approveAction` (approve and reject paths).
7. Wire `runScenario` / `resetDemo` to the real demo endpoints.
8. Add tests: dashboard categorization, approve/reject calls, polling stops when
   complete, failed run displays, mismatch cannot render as complete,
   `NO_SAFE_ACTION_AVAILABLE` renders no Approve button.
9. Re-check both themes and the reference screenshots before shipping.
