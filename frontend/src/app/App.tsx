import { Route, Routes } from "react-router-dom";
import { useApplicationReady } from "@/boot/ready";
import { ThemeProvider } from "@/hooks/useTheme";
import { CareLoopProvider } from "@/hooks/useCareLoop";
import { UiProvider } from "@/hooks/useUi";
import AppHeader from "@/components/navigation/AppHeader";
import ConnectionBanner from "@/components/navigation/ConnectionBanner";
import IdentityStrip from "@/components/navigation/IdentityStrip";
import UnderTheHoodDrawer from "@/components/under-the-hood/UnderTheHoodDrawer";
import EvidenceLens from "@/components/cases/EvidenceLens";
import HomePage from "@/pages/HomePage";
import DemoPage from "@/pages/DemoPage";
import CasePage from "@/pages/CasePage";
import BoundariesPage from "@/pages/BoundariesPage";
import NotFoundPage from "@/pages/NotFoundPage";

export default function App() {
  /* The last effect of the initial mount, and therefore the earliest moment at which CareLoop is
     honestly usable. The boot layer listens; nothing in the application does, and nothing here
     changes if the loader is absent. See `boot/ready.ts` for why this boundary and not another. */
  useApplicationReady();

  return (
    <ThemeProvider>
      <CareLoopProvider>
        <UiProvider>
          <div
            style={{
              minHeight: "100vh",
              // A flex column so a page that wants the whole first screen can ask for it with
              // `flex: 1` instead of subtracting a header height it would have to keep in sync.
              // The header wraps to a second row on narrow viewports, so any hard-coded offset
              // is wrong exactly where it is hardest to notice.
              display: "flex",
              flexDirection: "column",
              fontFamily: "var(--font-sans)",
              fontSize: 16,
              lineHeight: 1.5,
              // Transparent, not `--background`: the paper is painted by html/body in global.css
              // so the orchestration field can sit between the paper and this element. Repainting
              // the same token here would put an opaque sheet over the field and hide it
              // completely — which looks exactly like WebGL having failed.
              background: "transparent",
              color: "var(--foreground)",
              transition: "color 400ms ease"
            }}
          >
            <AppHeader />
            <IdentityStrip />
            <ConnectionBanner />
            <Routes>
              <Route path="/" element={<HomePage />} />
              <Route path="/demo" element={<DemoPage />} />
              <Route path="/case/:caseId" element={<CasePage />} />
              {/* The route is unchanged and must stay so: `/trust` is what `docs/judges.md`, the
                  captured goldens and any link a judge has written down already point at. The page
                  behind it is now derived rather than written, and the nav says so. */}
              <Route path="/trust" element={<BoundariesPage />} />
              {/* Last, and it swallows nothing above it: React Router v6 ranks by specificity
                  rather than by declaration order, so `/case/:caseId` still wins for a case that
                  does not exist and answers with its own recovery state. `/case/` has no id to
                  bind and reaches this instead, which is right — there is no case for the detail
                  route to be about. */}
              <Route path="*" element={<NotFoundPage />} />
            </Routes>
            <UnderTheHoodDrawer />
            <EvidenceLens />
          </div>
        </UiProvider>
      </CareLoopProvider>
    </ThemeProvider>
  );
}
