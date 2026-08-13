# API Integration Map

```
UI
│
├── Demo launcher (/demo)
│   ├── Run scenario ─────────► CareLoopApi.runScenario(id)
│   └── (reset before start) ─► CareLoopApi.resetDemo()
│
├── Home (/)
│   ├── initial load ────────► CareLoopApi.getDashboard()
│   ├── live progress ───────► CareLoopApi.subscribe()   // polling in the real adapter
│   ├── Approve ─────────────► CareLoopApi.approveAction({ approved: true })
│   └── Choose another ──────► CareLoopApi.approveAction({ approved: false })
│
├── Case detail (/case/:id) ─► CareLoopApi.getCase(caseId)
│
└── Under the hood (drawer) ─► CareLoopApi.getRun(runId)
```

## Suggested endpoint mapping

These follow the shapes described in the project brief. **Confirm each against
the repository** — the repository is the source of truth, not this table.

| Adapter method | Suggested endpoint |
|---|---|
| `getDashboard()` | `GET /api/cases` |
| `getCase(id)` | `GET /api/cases/{id}` |
| `getRun(runId)` | `GET /api/runs/{run_id}` |
| `approveAction({approved: true})` | `POST /api/cases/{id}/decisions/{approval_id}/approve` |
| `approveAction({approved: false})` | `POST /api/cases/{id}/decisions/{approval_id}/reject` |
| `runScenario(id)` | `POST /api/demo/scenarios/{key}` |
| `resetDemo()` | `POST /api/demo/reset` |
| `subscribe()` | poll `GET /api/runs/{run_id}`, stop on settle |

## Polling

The backend returns 202 + a run id for asynchronous work. In `realApi.subscribe`:

1. Poll `/api/runs/{id}` at roughly 1s.
2. Refetch the affected case when the run's stage changes.
3. Emit a full `DashboardState` so every subscriber updates together.
4. **Stop polling when the run settles** (settled, failed, or halted).
5. Animate in React — a judge cannot tell polling from a socket. Do not add
   WebSockets for this.

## Six questions to answer before writing the adapter

The design depends on these; each has a fallback, but confirm rather than guess.

1. Does the decision payload expose a kind discriminator
   (`REQUIRES_HUMAN_APPROVAL` vs `NO_SAFE_ACTION_AVAILABLE`)? The Approve button
   renders off that field alone.
2. Does the verification result carry **both** the authorized and the observed
   values? The seal needs both sides; a mismatch needs the real observed value.
3. Do timeline events carry a semantic type and human-facing payload, or only
   raw log text? If only raw text, the mapping belongs in the adapter — and
   ideally in one backend field instead.
4. Does evidence provenance include source document, page and excerpt?
5. Are blocked actions first-class events with verdict and rule code, and is
   there a per-case count?
6. What are the exact demo scenario keys, and do runs emit intermediate states
   or only terminal ones? The choreography depends on intermediate transitions.

## Rules that survive integration

- The frontend never invents policy decisions, completion state or verification
  results. Backend remains the source of truth.
- Durable case state does not move into React.
- Failures surface honestly: "CareLoop couldn't book this appointment. Nothing
  was changed." A verification mismatch must not render as a completed case.
- Do not weaken backend CORS; use the `/api` dev proxy in `vite.config.ts`.
