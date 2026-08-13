import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import type { CareCase, DashboardState, ScenarioId } from "@/types/domain";
import { api } from "@/services/api";
import { idleRun } from "@/data/mock/runs";
import { seedCases } from "@/data/mock/cases";

interface CareLoopContextValue extends DashboardState {
  needs: CareCase[];
  working: CareCase[];
  done: CareCase[];
  caseById(id: string): CareCase | undefined;
  approve(caseId: string, decisionId: string): void;
  reject(caseId: string, decisionId: string): void;
  runScenario(id: ScenarioId): void;
  resetDemo(): void;
}

const CareLoopContext = createContext<CareLoopContextValue | null>(null);

export function CareLoopProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<DashboardState>({ cases: seedCases(), run: idleRun() });

  // The mock adapter pushes; the real adapter should poll /api/runs/:id and
  // re-emit the same shape, stopping once the run settles.
  useEffect(() => api.subscribe(setState), []);

  const approve = useCallback((caseId: string, decisionId: string) => {
    void api.approveAction({ caseId, decisionId, approved: true });
  }, []);

  const reject = useCallback((caseId: string, decisionId: string) => {
    void api.approveAction({ caseId, decisionId, approved: false });
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
      runScenario,
      resetDemo
    };
  }, [state, approve, reject, runScenario, resetDemo]);

  return <CareLoopContext.Provider value={value}>{children}</CareLoopContext.Provider>;
}

export function useCareLoop(): CareLoopContextValue {
  const ctx = useContext(CareLoopContext);
  if (!ctx) throw new Error("useCareLoop must be used inside <CareLoopProvider>");
  return ctx;
}
