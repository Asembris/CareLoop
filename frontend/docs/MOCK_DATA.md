# Mock Data

Everything the UI displays today comes from `src/services/mockApi.ts` and the
fixtures in `src/data/mock/`. All of it is synthetic. No real patient
information, no credentials, no private URLs.

Backend endpoints below are **suggested integration points** based on the shapes
described in the project brief. Confirm each against the repository before
relying on it.

## Dashboard cases

- **Represents:** the caregiver's open and closed administrative cases.
- **Defined in:** `src/data/mock/cases.ts` (`seedCases()`), extended at runtime
  by the scenario choreography in `mockApi.ts`.
- **Consumed by:** `HomePage` via `useCareLoop()` → `needs` / `working` / `done`.
- **Replace with:** `GET /api/cases` (suggested).

## Case detail

- **Represents:** one long-lived workflow with its timeline and evidence.
- **Defined in:** the same store; selected by id.
- **Consumed by:** `CasePage`, `OutcomePanel`, `CaseTimeline`, `EvidenceGrid`.
- **Replace with:** `GET /api/cases/{id}` (suggested).

## Agent run

- **Represents:** the current run — stage, action intent, idempotency key,
  policy verdict, evidence digest, verification result.
- **Defined in:** `src/data/mock/runs.ts` (`idleRun()`), mutated by `setRun()`.
- **Consumed by:** `UnderTheHoodDrawer`.
- **Replace with:** `GET /api/runs/{run_id}` (suggested). The drawer already
  reads the same run object the dashboard does — keep it that way.

## Decision (approval)

- **Represents:** the one point where human authority is required.
- **Defined in:** `CareCase.decision` (`DecisionView`), created by scenarios 01
  and 03.
- **Consumed by:** `DecisionCard`.
- **Replace with:**
  `POST /api/cases/{id}/decisions/{approval_id}/approve` and
  `POST /api/cases/{id}/decisions/{approval_id}/reject` (suggested).
- **Critical:** `kind` drives whether Approve/Reject render at all. Map the
  backend's discriminator onto `REQUIRES_HUMAN_APPROVAL` vs
  `NO_SAFE_ACTION_AVAILABLE` in the adapter. Never default to approvable.

## Evidence and provenance

- **Represents:** a fact extracted from an untrusted document, with source, page
  and excerpt.
- **Defined in:** `src/data/mock/evidence.ts` (`EXCERPTS`).
- **Consumed by:** `EvidenceGrid`, `EvidenceLens`, timeline citations.
- **Replace with:** provenance attached to case evidence. The lens degrades
  gracefully without a page anchor, but it needs the excerpt to highlight.

## Blocked actions

- **Represents:** unsafe instructions found inside document content and denied.
- **Defined in:** `TimelineEvent.blocked` in scenario 02.
- **Consumed by:** `CaseTimeline` (dashed branch), `UnderTheHoodDrawer` (verdict + rule).
- **Replace with:** first-class policy events on the case, carrying verdict and
  rule code.

## Verification read-back

- **Represents:** what was authorized vs what the external system actually reports.
- **Defined in:** `CareCase.readback` (`Readback`).
- **Consumed by:** `OutcomePanel`, `VerificationSeal`.
- **Replace with:** the verification result on the case. **Both** values are
  required — a seal that shows a match the backend never checked is worse than
  no seal.

## Scenario controls

- **Represents:** demo scenario start and reset.
- **Defined in:** `mockApi.runScenario()` / `resetDemo()`; launcher copy in
  `src/data/mock/scenarios.ts`.
- **Consumed by:** `DemoPage`.
- **Replace with:** `POST /api/demo/scenarios/{key}` and `POST /api/demo/reset`
  (suggested). Keys must come from the repository.

## Run progression

- **Represents:** asynchronous progress between states.
- **Defined in:** `setTimeout` choreography in `mockApi.ts`.
- **Consumed by:** everything, through `subscribe()`.
- **Replace with:** polling `/api/runs/{id}`, stopping when the run settles.
  Keep the pacing recognisable — the demo depends on seeing intermediate states.
