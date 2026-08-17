# CareLoop UI

The approved CareLoop frontend: an autonomous administrative agent for family
caregivers, designed so a caregiver sees calm and a judge sees rigor.

![Home](reference/screenshots/home-needs-you-light.png)

## Quick start

```bash
npm install
npm run dev     # http://localhost:5173
```

Runs entirely on mock data. No backend, no `.env` needed.

Open `/demo` and run scenario 01 to see the full loop: gather → one human
decision → execute → independent verification.

## Stack

React 18 · TypeScript (strict) · Vite · React Router.
No UI framework, no CSS framework — inline styles over CSS custom properties,
with the CareLoop mark as inline SVG.

Behind it all, an orchestration field: a WebGL composition painted between the
paper and the interface, self-hosted, outside the React tree, and decorative in
the strict sense — `?motion=off` gives you the application unchanged, byte for
byte. See **[docs/MOTION.md](docs/MOTION.md)**.

## Routes

- `/` — Home: Needs you / Working / Completed
- `/demo` — scenario launcher
- `/case/:caseId` — case detail: outcome, timeline, evidence
- `/trust` — the authority model and system guarantees

Under the hood and Evidence open as drawers over any screen.

## Screenshots

`reference/screenshots/` — ten golden states, light and dark.

## Next step

Read **[HANDOFF.md](HANDOFF.md)** before changing anything. It covers the
design lock, the domain contract, and exactly where the real API connects.

Demo uses synthetic care data. No real patient information.
