import { useNavigate } from "react-router-dom";

/**
 * Any address that is not one of the four routes.
 *
 * Before this existed, `<Routes>` matched nothing and rendered null, so the header and the two
 * closed drawers were the entire page — no content, no 404, no console error. The origin serves
 * `index.html` for every unmatched path so client-side routing can take it, which is correct; there
 * was simply nothing on this side to take it.
 *
 * Deliberately the same surface as the unknown-case state in `CasePage`: a quiet line and one way
 * back. Both are a person arriving somewhere that is not there, and answering the two differently
 * would be a distinction drawn for the implementation's benefit rather than the reader's. What they
 * do *not* share is the sentence — "that case is no longer here" names a case, and an address that
 * matched no route never named one.
 */
export default function NotFoundPage() {
  const navigate = useNavigate();

  return (
    <main style={{ display: "flex", justifyContent: "center", padding: "68px 40px 130px" }}>
      <div
        style={{
          width: "100%",
          maxWidth: "var(--page-max)",
          display: "flex",
          flexDirection: "column",
          gap: 16
        }}
      >
        <h1 style={{ margin: 0, fontFamily: "var(--font-serif)", fontWeight: 300, fontSize: 34 }}>
          That page isn't here.
        </h1>
        <p
          style={{
            margin: 0,
            fontSize: 16,
            lineHeight: 1.6,
            color: "var(--muted-foreground)",
            maxWidth: "52ch"
          }}
        >
          The link may be out of date. Everything CareLoop is working on is on the dashboard.
        </p>
        <button
          className="cl-quiet"
          onClick={() => navigate("/")}
          style={{
            alignSelf: "flex-start",
            background: "transparent",
            border: 0,
            padding: "4px 0",
            fontSize: 13.5,
            color: "var(--muted-foreground)"
          }}
        >
          ← Home
        </button>
      </div>
    </main>
  );
}
