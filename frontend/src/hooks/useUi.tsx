import { createContext, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";
import type { EvidenceExcerpt } from "@/types/domain";

interface UiContextValue {
  hoodOpen: boolean;
  toggleHood(): void;
  lens: EvidenceExcerpt | null;
  openLens(excerpt: EvidenceExcerpt): void;
  closeLens(): void;
}

const UiContext = createContext<UiContextValue | null>(null);

export function UiProvider({ children }: { children: ReactNode }) {
  const [hoodOpen, setHoodOpen] = useState(false);
  const [lens, setLens] = useState<EvidenceExcerpt | null>(null);

  const value = useMemo<UiContextValue>(
    () => ({
      hoodOpen,
      toggleHood: () => setHoodOpen((v) => !v),
      lens,
      openLens: (excerpt: EvidenceExcerpt) => setLens(excerpt),
      closeLens: () => setLens(null)
    }),
    [hoodOpen, lens]
  );

  return <UiContext.Provider value={value}>{children}</UiContext.Provider>;
}

export function useUi(): UiContextValue {
  const ctx = useContext(UiContext);
  if (!ctx) throw new Error("useUi must be used inside <UiProvider>");
  return ctx;
}
