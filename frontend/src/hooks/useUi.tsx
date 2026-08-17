import { createContext, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";
import type { CaseDocument, EvidenceExcerpt, PolicyRefusal } from "@/types/domain";

/** What the lens is currently showing: one extracted fact, or a whole source document with the
 *  case's policy refusals beside it. The refusals ride along with the document deliberately —
 *  the pairing "read the attack, see the refusal" is the view, not two views. */
export type LensView =
  | { kind: "excerpt"; excerpt: EvidenceExcerpt }
  | { kind: "document"; document: CaseDocument; refusals: PolicyRefusal[] };

interface UiContextValue {
  hoodOpen: boolean;
  toggleHood(): void;
  lens: LensView | null;
  openLens(excerpt: EvidenceExcerpt): void;
  openDocument(document: CaseDocument, refusals: PolicyRefusal[]): void;
  closeLens(): void;
}

const UiContext = createContext<UiContextValue | null>(null);

export function UiProvider({ children }: { children: ReactNode }) {
  const [hoodOpen, setHoodOpen] = useState(false);
  const [lens, setLens] = useState<LensView | null>(null);

  const value = useMemo<UiContextValue>(
    () => ({
      hoodOpen,
      toggleHood: () => setHoodOpen((v) => !v),
      lens,
      openLens: (excerpt: EvidenceExcerpt) => setLens({ kind: "excerpt", excerpt }),
      openDocument: (document: CaseDocument, refusals: PolicyRefusal[]) =>
        setLens({ kind: "document", document, refusals }),
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
