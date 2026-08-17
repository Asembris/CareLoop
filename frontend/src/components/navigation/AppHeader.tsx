import type { CSSProperties } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import CareLoopLogo from "@/components/brand/CareLoopLogo";
import { useTheme } from "@/hooks/useTheme";
import { useUi } from "@/hooks/useUi";

const NAV_BUTTON: CSSProperties = {
  position: "relative",
  background: "transparent",
  border: 0,
  padding: "4px 0",
  fontSize: 14,
  // A nav item may move to another row; its label may never break mid-phrase. Without this,
  // "Under the hood" renders as three stacked words the first time the header runs out of room.
  whiteSpace: "nowrap",
  transition: "color 160ms ease"
};

const UNDERLINE: CSSProperties = {
  position: "absolute",
  left: "15%",
  right: "15%",
  bottom: -9,
  height: 1,
  background: "var(--brand-forest)",
  transition: "opacity 160ms ease"
};

export default function AppHeader() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { theme, toggle } = useTheme();
  const { hoodOpen, toggleHood } = useUi();

  const homeActive = pathname === "/" || pathname.startsWith("/case");
  const trustActive = pathname === "/trust";
  const themeLabel = theme === "dark" ? "Switch to light" : "Switch to dark";
  const muted = "var(--muted-foreground)";

  return (
    <header
      style={{
        position: "sticky",
        top: 0,
        zIndex: 40,
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: "10px 40px",
        flexWrap: "wrap",
        padding: "18px var(--page-gutter)",
        borderBottom: "1px solid color-mix(in oklab, currentColor 12%, transparent)",
        background: "var(--background)"
      }}
    >
      <button
        onClick={() => navigate("/")}
        aria-label="CareLoop — home"
        style={{ display: "flex", alignItems: "center", background: "transparent", border: 0, padding: 0 }}
      >
        <CareLoopLogo />
      </button>

      <nav style={{ display: "flex", alignItems: "center", gap: "var(--nav-gap)" }}>
        <button
          className="cl-navlink"
          onClick={() => navigate("/")}
          aria-current={homeActive ? "page" : undefined}
          style={{ ...NAV_BUTTON, color: homeActive ? "var(--foreground)" : muted }}
        >
          Home
          <span aria-hidden="true" style={{ ...UNDERLINE, opacity: homeActive ? 0.75 : 0 }} />
        </button>

        <button
          className="cl-navlink"
          onClick={() => navigate("/trust")}
          aria-current={trustActive ? "page" : undefined}
          style={{ ...NAV_BUTTON, color: trustActive ? "var(--foreground)" : muted }}
        >
          Trust
          <span aria-hidden="true" style={{ ...UNDERLINE, opacity: trustActive ? 0.75 : 0 }} />
        </button>

        <button
          className="cl-navlink"
          onClick={toggleHood}
          aria-expanded={hoodOpen}
          style={{ ...NAV_BUTTON, color: hoodOpen ? "var(--foreground)" : muted }}
        >
          Under the hood
          <span aria-hidden="true" style={{ ...UNDERLINE, opacity: hoodOpen ? 0.75 : 0 }} />
        </button>

        <span
          aria-hidden="true"
          style={{
            width: 1,
            height: 20,
            background: "color-mix(in oklab, currentColor 14%, transparent)",
            margin: "0 4px"
          }}
        />

        <button
          className="cl-icon-btn"
          onClick={toggle}
          aria-label={themeLabel}
          title={themeLabel}
          style={{
            width: 32,
            height: 32,
            background: "transparent",
            border: "1px solid color-mix(in oklab, currentColor 14%, transparent)",
            padding: 0,
            borderRadius: "var(--radius-control)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: muted,
            transition: "color 160ms ease, border-color 160ms ease"
          }}
        >
          {theme === "dark" ? (
            <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
              <circle cx="10" cy="10" r="3.6" />
              <path d="M10 2.4v1.8M10 15.8v1.8M17.6 10h-1.8M4.2 10H2.4M15.4 4.6l-1.3 1.3M5.9 14.1l-1.3 1.3M15.4 15.4l-1.3-1.3M5.9 5.9L4.6 4.6" />
            </svg>
          ) : (
            <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
              <path d="M16.2 12.4A6.8 6.8 0 0 1 7.6 3.8a6.8 6.8 0 1 0 8.6 8.6z" />
            </svg>
          )}
        </button>

        <span style={{ fontSize: 14, color: muted }}>Maya</span>
      </nav>
    </header>
  );
}
