import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import type { CareCase, DashboardState, ScenarioId } from "@/types/domain";
import { api, emptyDashboard } from "@/services/api";

interface CareLoopContextValue extends DashboardState {
  needs: CareCase[];
  working: CareCase[];
  done: CareCase[];
  caseById(id: string): CareCase | undefined;
  approve(caseId: string, decisionId: string): void;
  reject(caseId: string, decisionId: string): void;
  /** Ask CareLoop to look at the external record again. Takes no decision id: a re-check is not
   *  a decision, and there is nothing for one to name. */
  recheck(caseId: string): void;
  runScenario(id: ScenarioId): void;
  resetDemo(): void;
}

const CareLoopContext = createContext<CareLoopContextValue | null>(null);

export function CareLoopProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<DashboardState>(emptyDashboard);

  // The adapter polls GET /api/cases and re-emits this same shape, stopping once nothing is
  // moving. Every verb restarts it, which is why nothing here has to know when to look again.
  useEffect(() => api.subscribe(setState), []);

  const approve = useCallback((caseId: string, decisionId: string) => {
    void api.approveAction({ caseId, decisionId, approved: true });
  }, []);

  const reject = useCallback((caseId: string, decisionId: string) => {
    void api.approveAction({ caseId, decisionId, approved: false });
  }, []);

  const recheck = useCallback((caseId: string) => {
    void api.recheckCase(caseId);
  }, []);

  const runScenario = useCallback((id: ScenarioId) => {
    void api.runScenario(id);
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
      approve,
      reject,
      recheck,
      runScenario,
      resetDemo
    };
  }, [state, approve, reject, recheck, runScenario, resetDemo]);

  return <CareLoopContext.Provider value={value}>{children}</CareLoopContext.Provider>;
}

export function useCareLoop(): CareLoopContextValue {
  const ctx = useContext(CareLoopContext);
  if (!ctx) throw new Error("useCareLoop must be used inside <CareLoopProvider>");
  return ctx;
}
