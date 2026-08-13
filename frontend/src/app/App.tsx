import { Route, Routes } from "react-router-dom";
import { ThemeProvider } from "@/hooks/useTheme";
import { CareLoopProvider } from "@/hooks/useCareLoop";
import { UiProvider } from "@/hooks/useUi";
import AppHeader from "@/components/navigation/AppHeader";
import UnderTheHoodDrawer from "@/components/under-the-hood/UnderTheHoodDrawer";
import EvidenceLens from "@/components/cases/EvidenceLens";
import HomePage from "@/pages/HomePage";
import DemoPage from "@/pages/DemoPage";
import CasePage from "@/pages/CasePage";
import TrustPage from "@/pages/TrustPage";

export default function App() {
  return (
    <ThemeProvider>
      <CareLoopProvider>
        <UiProvider>
          <div
            style={{
              minHeight: "100vh",
              fontFamily: "var(--font-sans)",
              fontSize: 16,
              lineHeight: 1.5,
              background: "var(--background)",
              color: "var(--foreground)",
              transition: "background 400ms ease, color 400ms ease"
            }}
          >
            <AppHeader />
            <Routes>
              <Route path="/" element={<HomePage />} />
              <Route path="/demo" element={<DemoPage />} />
              <Route path="/case/:caseId" element={<CasePage />} />
              <Route path="/trust" element={<TrustPage />} />
            </Routes>
            <UnderTheHoodDrawer />
            <EvidenceLens />
          </div>
        </UiProvider>
      </CareLoopProvider>
    </ThemeProvider>
  );
}
