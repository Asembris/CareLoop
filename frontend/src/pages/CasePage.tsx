import { useScrollReveal } from "@/hooks/useScrollReveal";
import { useNavigate, useParams } from "react-router-dom";
import { useCareLoop } from "@/hooks/useCareLoop";
import { caseTone } from "@/lib/tone";
import OutcomePanel from "@/components/cases/OutcomePanel";
import CaseTimeline from "@/components/cases/CaseTimeline";
import EvidenceGrid from "@/components/cases/EvidenceGrid";
import DocumentStrip from "@/components/cases/DocumentStrip";

/** Outcome first, then the spine, then the evidence it rests on. */
export default function CasePage() {
  const { caseId } = useParams();
  const navigate = useNavigate();
  const { caseById } = useCareLoop();
  const careCase = caseId ? caseById(caseId) : undefined;
  // Above the early return, because hooks may not be called conditionally — and keyed on the case,
  // because this page renders its recovery state until the poller answers. An unkeyed effect would
  // run once against a column that does not exist yet and mark nothing, ever.
  const reveal = useScrollReveal<HTMLDivElement>(careCase?.id);

  if (!careCase) {
    return (
      <main style={{ display: "flex", justifyContent: "center", padding: "68px var(--page-gutter) 130px" }}>
        <div style={{ width: "100%", maxWidth: "var(--page-max)", display: "flex", flexDirection: "column", gap: 16 }}>
          <h1 style={{ margin: 0, fontFamily: "var(--font-serif)", fontWeight: 300, fontSize: 34 }}>
            That case is no longer here.
          </h1>
          <button
            className="cl-quiet"
            onClick={() => navigate("/")}
            style={{ alignSelf: "flex-start", background: "transparent", border: 0, padding: "4px 0", fontSize: 13.5, color: "var(--nav-ink, var(--muted-foreground))", transition: "color 120ms ease" }}
          >
            ← Home
          </button>
        </div>
      </main>
    );
  }

  const tone = caseTone(careCase);

  return (
    <main style={{ display: "flex", justifyContent: "center", padding: "52px var(--page-gutter) 130px" }}>
      <div
        ref={reveal}
        style={{
          width: "100%",
          maxWidth: "var(--page-max)",
          display: "flex",
          flexDirection: "column",
          gap: 44,
          animation: "clFade 320ms ease both"
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <button
            className="cl-quiet"
            onClick={() => navigate("/")}
            style={{
              alignSelf: "flex-start",
              background: "transparent",
              border: 0,
              padding: "4px 0",
              fontSize: 13.5,
              color: "var(--nav-ink, var(--muted-foreground))",
              transition: "color 120ms ease"
            }}
          >
            ← Home
          </button>
          <h1
            style={{
              margin: 0,
              fontFamily: "var(--font-serif)",
              fontWeight: 300,
              fontSize: 40,
              lineHeight: 1.1,
              letterSpacing: "-0.02em"
            }}
          >
            {careCase.title}
          </h1>
          <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
            <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, letterSpacing: "0.1em", color: tone }}>
              {careCase.statusLabel}
            </span>
            <span style={{ fontSize: 15, color: "var(--muted-foreground)" }}>{careCase.headline}</span>
          </div>
        </div>

        <OutcomePanel careCase={careCase} />
        <CaseTimeline careCase={careCase} />
        <EvidenceGrid evidence={careCase.evidence} />
        <DocumentStrip documents={careCase.documents ?? []} refusals={careCase.refusals ?? []} />
      </div>
    </main>
  );
}
