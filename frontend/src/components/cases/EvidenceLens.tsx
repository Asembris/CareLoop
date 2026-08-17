import { Fragment, useRef } from "react";
import type { ReactNode } from "react";
import { useDialog } from "@/hooks/useDialog";
import { useUi } from "@/hooks/useUi";
import type { LensView } from "@/hooks/useUi";
import type { CaseDocument, EvidenceExcerpt, PolicyRefusal } from "@/types/domain";

/** Fallback when an excerpt carries no journey (the mock's data predates it). */
const TIERS = [
  { color: "var(--state-untrusted)", text: "Document content — untrusted" },
  { color: "var(--state-controlled)", text: "Extracted and validated — usable as fact" },
  { color: "var(--brand-accent)", text: "Never a source of authority" }
];

const STEP_COLOR: Record<string, string> = {
  untrusted: "var(--state-untrusted)",
  controlled: "var(--state-controlled)",
  trusted: "var(--brand-accent)",
  muted: "var(--muted-foreground)"
};

const FRAME = {
  border: "1px solid color-mix(in oklab, currentColor 14%, transparent)",
  borderRadius: 4,
  padding: 20,
  fontSize: 14.5,
  lineHeight: 1.7,
  background: "color-mix(in oklab, currentColor 3%, transparent)"
} as const;

const HIGHLIGHT = {
  background: "color-mix(in oklab, var(--brand-copper) 28%, transparent)",
  padding: "1px 3px",
  borderRadius: 2
} as const;

const MONO_LABEL = {
  fontFamily: "var(--font-mono)",
  fontSize: 11,
  letterSpacing: "0.12em",
  color: "var(--muted-foreground)"
} as const;

/** Where a fact came from, and what it is allowed to do. Opens over any screen. */
export default function EvidenceLens() {
  const { lens, closeLens } = useUi();
  if (!lens) return null;
  return <LensPanel lens={lens} onClose={closeLens} />;
}

/** Split from the gate above so the dialog hooks only run while the lens is open. */
function LensPanel({ lens, onClose }: { lens: LensView; onClose: () => void }) {
  const panel = useRef<HTMLElement>(null);
  useDialog(panel, onClose);

  return (
    <aside
      ref={panel}
      role="dialog"
      aria-modal="true"
      aria-label={lens.kind === "document" ? "Source document" : "Evidence"}
      tabIndex={-1}
      style={{
        position: "fixed",
        top: 0,
        right: 0,
        bottom: 0,
        width: lens.kind === "document" ? 560 : 460,
        maxWidth: "var(--drawer-max)",
        zIndex: 70,
        background: "var(--background)",
        borderLeft: "1px solid color-mix(in oklab, currentColor 16%, transparent)",
        boxShadow: "-24px 0 60px color-mix(in oklab, var(--foreground) 12%, transparent)",
        display: "flex",
        flexDirection: "column",
        animation: "clSlide 300ms cubic-bezier(0.22,0.61,0.36,1) both",
        overflowY: "auto"
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "20px 26px",
          borderBottom: "1px solid color-mix(in oklab, currentColor 12%, transparent)",
          position: "sticky",
          top: 0,
          zIndex: 2,
          background: "var(--background)"
        }}
      >
        <span style={MONO_LABEL}>{lens.kind === "document" ? "SOURCE DOCUMENT" : "EVIDENCE"}</span>
        <button
          className="cl-quiet"
          onClick={onClose}
          aria-label="Close"
          style={{ background: "transparent", border: 0, padding: "4px 8px", fontSize: 16, color: "var(--muted-foreground)" }}
        >
          ×
        </button>
      </div>

      {lens.kind === "document" ? (
        <DocumentBody document={lens.document} refusals={lens.refusals} />
      ) : (
        <ExcerptBody excerpt={lens.excerpt} />
      )}
    </aside>
  );
}

function ExcerptBody({ excerpt }: { excerpt: EvidenceExcerpt }) {
  return (
    <div style={{ padding: "28px 26px", display: "flex", flexDirection: "column", gap: 22 }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
        <span style={{ fontSize: 19, fontWeight: 500, letterSpacing: "-0.012em" }}>{excerpt.fact}</span>
        <span style={{ fontSize: 14, color: "var(--muted-foreground)" }}>{excerpt.source}</span>
      </div>

      <div style={FRAME}>
        <span style={{ color: "var(--muted-foreground)" }}>{excerpt.before}</span>
        <span style={HIGHLIGHT}>{excerpt.highlight}</span>
        <span style={{ color: "var(--muted-foreground)" }}>{excerpt.after}</span>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {(
          excerpt.journey?.map((step) => ({ color: STEP_COLOR[step.tone] ?? "currentColor", text: step.text })) ??
          TIERS
        ).map((t) => (
          <div key={t.text} style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 14 }}>
            <span aria-hidden="true" style={{ width: 7, height: 7, borderRadius: "50%", background: t.color }} />
            <span style={{ color: "var(--muted-foreground)" }}>{t.text}</span>
          </div>
        ))}
      </div>

      <span style={{ fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--muted-foreground)", wordBreak: "break-all" }}>
        {excerpt.digest}
      </span>
    </div>
  );
}

/**
 * A whole source document, page by page, exactly as extraction recorded it — the poisoned
 * continuity notice included, which is the point. The text is rendered as quoted data inside a
 * frame that says so; the values extraction lifted into facts are highlighted; and the case's
 * policy refusals sit directly beneath, so the attack and its answer share one view.
 */
function DocumentBody({ document, refusals }: { document: CaseDocument; refusals: PolicyRefusal[] }) {
  return (
    <div style={{ padding: "28px 26px", display: "flex", flexDirection: "column", gap: 24 }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: 16, letterSpacing: "-0.01em" }}>{document.name}</span>
        <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 14 }}>
          <span
            aria-hidden="true"
            style={{ width: 7, height: 7, borderRadius: "50%", background: "var(--state-untrusted)", flexShrink: 0 }}
          />
          <span style={{ color: "var(--muted-foreground)" }}>
            Untrusted document content — CareLoop reads it for facts, never for instructions.
          </span>
        </div>
      </div>

      {document.pages.map((page) => (
        <div key={page.page} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <span style={MONO_LABEL}>PAGE {page.page}</span>
          <div style={{ ...FRAME, whiteSpace: "pre-wrap", color: "var(--muted-foreground)" }}>
            {withHighlights(page.text, page.highlights)}
          </div>
        </div>
      ))}

      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <span style={MONO_LABEL}>THE POLICY&apos;S ANSWER</span>
        {refusals.length > 0 ? (
          <>
            <p style={{ margin: 0, fontSize: 14, lineHeight: 1.6, color: "var(--muted-foreground)" }}>
              Anything in a document that reads like an instruction holds no authority. These are
              the actions policy refused on this case:
            </p>
            {refusals.map((refusal, index) => (
              <div
                key={index}
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: 4,
                  padding: "10px 14px",
                  borderLeft: "2px solid color-mix(in oklab, var(--state-denied) 55%, transparent)"
                }}
              >
                <span style={{ fontSize: 14, color: "var(--state-denied)" }}>
                  <span aria-hidden="true">✕ </span>
                  {refusal.text}
                </span>
                <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--muted-foreground)" }}>
                  {[refusal.ruleId, refusal.reasonCode].filter(Boolean).join(" · ")}
                </span>
              </div>
            ))}
          </>
        ) : (
          <p style={{ margin: 0, fontSize: 14, lineHeight: 1.6, color: "var(--muted-foreground)" }}>
            This document is data. Nothing it says can instruct CareLoop — and on this case, no
            unsafe action was ever proposed.
          </p>
        )}
      </div>
    </div>
  );
}

/** Page text with each extracted value marked. Longest values first, so a value that contains
 *  another (a full name over an id inside it) wins the span; each remaining segment is searched
 *  for the next value, so repeated occurrences are all marked. */
function withHighlights(text: string, values: string[]): ReactNode {
  const ordered = [...values].sort((a, b) => b.length - a.length);
  let segments: (string | ReactNode)[] = [text];

  for (const value of ordered) {
    segments = segments.flatMap((segment) => {
      if (typeof segment !== "string" || segment.length === 0) return [segment];
      const parts = segment.split(value);
      if (parts.length === 1) return [segment];
      const out: (string | ReactNode)[] = [];
      parts.forEach((part, i) => {
        out.push(part);
        if (i < parts.length - 1) {
          out.push(
            <span key={`${value}-${i}-${part.length}`} style={{ ...HIGHLIGHT, color: "var(--foreground)" }}>
              {value}
            </span>
          );
        }
      });
      return out;
    });
  }

  return segments.map((segment, i) => <Fragment key={i}>{segment}</Fragment>);
}
