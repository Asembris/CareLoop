# Routes

React Router v6, defined in `src/app/App.tsx`.

| Path | Component | Purpose | Entered from |
|---|---|---|---|
| `/` | `HomePage` | Needs you / Working / Completed | Logo, Home nav, after starting a scenario |
| `/demo` | `DemoPage` | Scenario launcher | "Run a scenario" on Home; direct link for judges |
| `/case/:caseId` | `CasePage` | Outcome, timeline, evidence | Any case row, "Open case" / "Review situation" |
| `/trust` | `TrustPage` | Authority model, guarantees, security evidence | Trust nav |

## Non-routes

Two surfaces are deliberately **not** routes, so technical detail never costs
the user their place:

- **Under the hood** — right drawer, toggled from the header or from
  "Why were these blocked?" in a case timeline. State in `useUi`.
- **Evidence lens** — right drawer, opened from a timeline citation or an
  evidence card. State in `useUi`.

## Notes

- Home is active for `/` and any `/case/*` path — a case is part of the
  dashboard's territory, not a separate destination.
- An unknown `caseId` renders a quiet recovery state, not a crash.
- The demo launcher is the intended first frame for judges and for the video.
  If you prefer the `?demo=true` convention from the original brief, add a
  redirect — do not move the launcher into the dashboard.
