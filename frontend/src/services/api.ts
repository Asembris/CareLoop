import type { CareLoopApi } from "@/types/domain";
import { mockApi } from "./mockApi";

/* ------------------------------------------------------------------ *
 * THE INTEGRATION SEAM.
 *
 * Today the UI runs entirely on the mock adapter. To connect the real
 * CareLoop backend, implement realApi against the same CareLoopApi
 * interface and swap the export below. No component should change.
 *
 *   export const api: CareLoopApi = realApi;
 *
 * Suggested mapping (confirm against the repository — these are NOT
 * assumed to exist as written):
 *   getDashboard   → GET  /api/cases
 *   getCase        → GET  /api/cases/{id}
 *   getRun         → GET  /api/runs/{run_id}
 *   approveAction  → POST /api/cases/{id}/decisions/{approval_id}/approve
 *                    POST /api/cases/{id}/decisions/{approval_id}/reject
 *   runScenario    → POST /api/demo/scenarios/{key}
 *   resetDemo      → POST /api/demo/reset
 *   subscribe      → poll /api/runs/{run_id} until the run settles,
 *                    then stop polling (see docs/API_INTEGRATION_MAP.md)
 * ------------------------------------------------------------------ */

export const api: CareLoopApi = mockApi;

/** Base URL for the real adapter. Unused while running on mocks. */
export const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? "";
