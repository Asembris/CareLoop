import { useNavigate } from "react-router-dom";
import type { CareCase } from "@/types/domain";
import { caseTone } from "@/lib/tone";

/** One line per case. Working rows carry a slow arc; completed rows a closed ring. */
export default function CaseRow({ careCase }: { careCase: CareCase }) {
  const navigate = useNavigate();
  const tone = caseTone(careCase);
  const isWorking = careCase.section === "working";

  return (
    <button
      className="cl-row"
      onClick={() => navigate("/case/" + careCase.id)}
      style={{
        textAlign: "left",
        background: "transparent",
        border: 0,
        borderBottom: "1px solid color-mix(in oklab, currentColor 10%, transparent)",
        padding: "17px 8px 17px 4px",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 20,
        animation: "clRise 380ms cubic-bezier(0.22,0.61,0.36,1) both"
      }}
    >
      <span style={{ display: "flex", alignItems: "center", gap: 14, minWidth: 0 }}>
        <svg viewBox="0 0 12 12" width="10" height="10" aria-hidden="true" style={{ flex: "none", color: tone }}>
          {isWorking ? (
            <circle
              cx="6"
              cy="6"
              r="4.4"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.7"
              strokeLinecap="round"
              strokeDasharray="12 15.6"
              style={{ transformOrigin: "6px 6px", animation: "clSpin 2.4s linear infinite" }}
            />
          ) : (
            <circle cx="6" cy="6" r="4.4" fill="none" stroke="currentColor" strokeWidth="1.7" />
          )}
        </svg>
        <span style={{ fontSize: 16.5, fontWeight: 500, whiteSpace: "nowrap" }}>{careCase.title}</span>
        <span
          style={{
            fontSize: 14.5,
            color: "var(--muted-foreground)",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap"
          }}
        >
          {careCase.statusText}
        </span>
      </span>
      <span
        style={{ fontSize: 13, color: "var(--muted-foreground)", fontVariantNumeric: "tabular-nums", flex: "none" }}
      >
        {careCase.updatedLabel}
      </span>
    </button>
  );
}
