import { useUi } from "@/hooks/useUi";
import type { CaseDocument, PolicyRefusal } from "@/types/domain";

/**
 * The case's source documents, each openable in full. The evidence grid shows what was extracted;
 * this shows what was *read* — including a document whose text is an attack, which renders here
 * as inert quoted data. That pairing is the flagship beat, so the strip exists on every case
 * rather than only when a refusal happened to fire.
 */
export default function DocumentStrip({
  documents,
  refusals
}: {
  documents: CaseDocument[];
  refusals: PolicyRefusal[];
}) {
  const { openDocument } = useUi();
  if (documents.length === 0) return null;

  return (
    <section style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 12 }}>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, letterSpacing: "0.12em", color: "var(--muted-foreground)" }}>
          SOURCE DOCUMENTS
        </span>
        {refusals.length > 0 ? (
          <span style={{ fontSize: 13, color: "var(--state-denied)" }}>
            {refusals.length === 1 ? "1 action refused by policy" : `${refusals.length} actions refused by policy`}
          </span>
        ) : null}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 14 }}>
        {documents.map((doc) => (
          <button
            key={doc.name}
            className="cl-outline-soft"
            onClick={() => openDocument(doc, refusals)}
            style={{
              textAlign: "left",
              background: "transparent",
              border: "1px solid color-mix(in oklab, currentColor 14%, transparent)",
              borderRadius: 4,
              padding: "16px 20px",
              display: "flex",
              flexDirection: "column",
              gap: 6
            }}
          >
            <span style={{ fontFamily: "var(--font-mono)", fontSize: 13.5 }}>{doc.name}</span>
            <span style={{ fontSize: 13, color: "var(--muted-foreground)" }}>
              {doc.pages.length === 1 ? "1 page" : `${doc.pages.length} pages`} · read as data, never as
              instructions
            </span>
          </button>
        ))}
      </div>
    </section>
  );
}
