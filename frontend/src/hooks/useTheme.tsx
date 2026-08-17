import { createContext, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

export type Theme = "light" | "dark";

interface ThemeContextValue {
  theme: Theme;
  toggle(): void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);
const STORAGE_KEY = "careloop:theme";

/** Light is canonical. Dark is a deliberate second theme, not an inversion.
 *
 *  The default follows the system preference; only an explicit toggle is persisted, so a visitor
 *  who never chose keeps following their OS. The same rule runs as a blocking script in
 *  `index.html` so the first frame paints in the right theme — this hook owns every frame after. */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(() => {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === "dark" || stored === "light") return stored;
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  });

  /* Both forms of one decision, written together so they cannot drift. The class is what the
     tokens key off; `data-theme` is the same fact in the form the orchestration field reads — it
     observes attribute mutations on the first `[data-theme]` element and retones without a reload.
     This hook stays the source of truth for both; the field never decides a theme, it follows. */
  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);

  const value = useMemo<ThemeContextValue>(
    () => ({
      theme,
      toggle: () =>
        setTheme((t) => {
          const next = t === "dark" ? "light" : "dark";
          window.localStorage.setItem(STORAGE_KEY, next);
          return next;
        })
    }),
    [theme]
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used inside <ThemeProvider>");
  return ctx;
}
