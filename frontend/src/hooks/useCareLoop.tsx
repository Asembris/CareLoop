import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import type { ApprovalResult, CareCase, DashboardState, ScenarioId } from "@/types/domain";
import { api, emptyDashboard } from "@/services/api";

interface CareLoopContextValue extends DashboardState {
  needs: CareCase[];
  working: CareCase[];
  done: CareCase[];
  caseById(id: string): CareCase | undefined;
  /** The result comes back so a card can acknowledge honestly: `accepted` false means the
   *  backend refused (409/404) and nothing was set in motion, so nothing may claim it was. */
  approve(caseId: string, decisionId: string): Promise<ApprovalResult>;
  reject(caseId: string, decisionId: string): Promise<ApprovalResult>;
  /** Ask CareLoop to look at the external record again. Takes no decision id: a re-check is not
   *  a decision, and there is nothing for one to name. */
  recheck(caseId: string): Promise<ApprovalResult>;
  /** A scenario has been asked for and no case has arrived yet. Set on the press and cleared when
   *  the backend answers — including when it refuses, because a page claiming work is starting
   *  over a request that was declined is the same small lie a decided card refuses to tell. */
  starting: boolean;
  runScenario(id: ScenarioId): void;
  resetDemo(): void;
}

const CareLoopContext = createContext<CareLoopContextValue | null>(null);

export function CareLoopProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<DashboardState>(emptyDashboard);

  // The adapter polls GET /api/cases and re-emits this same shape, stopping once nothing is
  // moving. Every verb restarts it, which is why nothing here has to know when to look again.
  useEffect(
    () =>
      api.subscribe((next) => {
        setState(next);
        // The case arrived, so the press is no longer the most recent thing that happened. Cleared
        // here as well as on the response so that a later empty dashboard — after a reset, or in a
        // world that was evicted — cannot inherit a stale "starting" from a run that finished.
        if (next.cases.length > 0) setStarting(false);
      }),
    []
  );

  const approve = useCallback(
    (caseId: string, decisionId: string) => api.approveAction({ caseId, decisionId, approved: true }),
    []
  );

  const reject = useCallback(
    (caseId: string, decisionId: string) => api.approveAction({ caseId, decisionId, approved: false }),
    []
  );

  const recheck = useCallback((caseId: string) => api.recheckCase(caseId), []);

  const [starting, setStarting] = useState(false);

  const runScenario = useCallback((id: ScenarioId) => {
    setStarting(true);
    // `caseId` is empty when the POST never returned a run — a refused press, or a backend that
    // could not be reached through the retry budget. Either way nothing was set in motion, so the
    // dashboard goes back to saying so rather than waiting on a case that is not coming.
    void api.runScenario(id).then(
      (run) => setStarting(Boolean(run.caseId)),
      () => setStarting(false)
    );
  }, []);

  const resetDemo = useCallback(() => {
    void api.resetDemo();
  }, []);

  const value = useMemo<CareLoopContextValue>(() => {
    const bySection = (section: CareCase["section"]) => state.cases.filter((c) => c.section === section);
    return {
      ...state,
      needs: bySection("needs"),
      working: bySection("working"),
      done: bySection("done"),
      caseById: (id: string) => state.cases.find((c) => c.id === id),
      starting,
      approve,
      reject,
      recheck,
      runScenario,
      resetDemo
    };
  }, [state, starting, approve, reject, recheck, runScenario, resetDemo]);

  return <CareLoopContext.Provider value={value}>{children}</CareLoopContext.Provider>;
}

export function useCareLoop(): CareLoopContextValue {
  const ctx = useContext(CareLoopContext);
  if (!ctx) throw new Error("useCareLoop must be used inside <CareLoopProvider>");
  return ctx;
}
