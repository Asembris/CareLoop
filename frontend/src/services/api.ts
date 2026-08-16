import type { CareLoopApi, DashboardState } from "@/types/domain";
import { emptyRun, realApi } from "./realApi";

/** Which world this browser is using, or `null` when the backend serves one to everybody.
 *
 *  Re-exported here rather than reached for inside a component, so `services/` stays the only
 *  place that knows the backend has a shape. The deployed demo gives every visitor their own case
 *  store and their own clinic; a page shows the id so that `GET /clinic/slots?session=…` — the
 *  check `docs/judges.md` asks a judge to run for themselves — reaches the same world they have
 *  been watching rather than an untouched one. */
export { ensureSession } from "./realApi";

/* ------------------------------------------------------------------ *
 * THE INTEGRATION SEAM — now pointed at the real backend.
 *
 * `CareLoopApi` is the whole surface, and swapping this one line is what
 * the design package built it for. Not one component changed: every
 * divergence between what CareLoop stores and what the UI asks for is
 * resolved in `realApi.ts` and `presentation.ts`.
 *
 *   getDashboard   → GET  /api/cases          (three arrays, flattened)
 *   getCase        → GET  /api/cases/{id}
 *   getRun         → GET  /api/runs/{run_id}  + the case's own timeline
 *   approveAction  → POST /api/cases/{id}/decisions/{approval_id}/approve
 *                    POST /api/cases/{id}/decisions/{approval_id}/reject
 *   runScenario    → POST /api/demo/scenarios/{key}
 *   resetDemo      → POST /api/demo/reset
 *   subscribe      → poll GET /api/cases at ~1s, stop when nothing moves
 *
 * `mockApi` is kept, unreferenced, as the record of the approved demo
 * pacing and as something to point this line back at while debugging.
 * ------------------------------------------------------------------ */

export const api: CareLoopApi = realApi;

/** What the dashboard holds before the first read comes back.
 *
 *  Empty rather than seeded. The provider used to start from `data/mock/cases.ts`, which against a
 *  real backend meant two invented cases on screen until the first poll replaced them — a
 *  dashboard that shows work nobody did, for as long as the network takes. */
export function emptyDashboard(): DashboardState {
  return { cases: [], run: emptyRun() };
}

/** Base URL for the adapter. Empty by default: every request is a relative `/api` path, which the
 *  vite dev proxy forwards and a production deployment serves same-origin, so backend CORS is
 *  never involved. */
export const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? "";
