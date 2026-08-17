import { useScrollReveal } from "@/hooks/useScrollReveal";
import AuthorityGrid from "@/components/trust/AuthorityGrid";
import GuaranteeList from "@/components/trust/GuaranteeList";
import RecoveryLoop from "@/components/trust/RecoveryLoop";
import SecurityEvidencePanel from "@/components/trust/SecurityEvidencePanel";

export default function TrustPage() {
  const reveal = useScrollReveal<HTMLDivElement>();

  return (
    <main style={{ display: "flex", justifyContent: "center", padding: "68px var(--page-gutter) 130px" }}>
      <div
        ref={reveal}
        style={{
          width: "100%",
          maxWidth: "var(--page-max)",
          display: "flex",
          flexDirection: "column",
          gap: 52,
          animation: "clFade 320ms ease both"
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, letterSpacing: "0.12em", color: "var(--muted-foreground)" }}>
            TRUST
          </span>
          <h1
            style={{
              margin: 0,
              fontFamily: "var(--font-serif)",
              fontWeight: 300,
              fontSize: 44,
              lineHeight: 1.12,
              letterSpacing: "-0.02em",
              maxWidth: "20ch"
            }}
          >
            The model never authorizes its own actions.
          </h1>
          <p style={{ margin: 0, fontSize: 16.5, lineHeight: 1.62, color: "var(--muted-foreground)", maxWidth: "62ch" }}>
            CareLoop reads untrusted material — documents, emails, external replies — and can take facts from it. It
            cannot take instructions from it. Authority lives in deterministic code, and completion lives in the outside
            world.
          </p>
        </div>

        <AuthorityGrid />

        <section style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, letterSpacing: "0.12em", color: "var(--muted-foreground)" }}>
            SYSTEM GUARANTEES
          </span>
          <GuaranteeList />
        </section>

        <RecoveryLoop />

        <SecurityEvidencePanel />
      </div>
    </main>
  );
}
