/**
 * Theme: dark-first; "system" follows prefers-color-scheme. The choice is stored
 * per viewer (localStorage) and applied as <html data-theme="dark|light">.
 */
import { createContext, useContext, useEffect, useMemo, useSyncExternalStore, type ReactNode } from "react";
import { useStoredState } from "./hooks";

export type ThemePref = "system" | "dark" | "light";
export type ResolvedTheme = "dark" | "light";

const mq = typeof window !== "undefined" && window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;

function subscribeScheme(cb: () => void) {
  mq?.addEventListener("change", cb);
  return () => mq?.removeEventListener("change", cb);
}
const systemScheme = (): ResolvedTheme => (mq?.matches ? "light" : "dark");

interface ThemeValue {
  pref: ThemePref;
  resolved: ResolvedTheme;
  setPref: (p: ThemePref) => void;
}

const ThemeContext = createContext<ThemeValue>({ pref: "system", resolved: "dark", setPref: () => undefined });

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [pref, setPref] = useStoredState<ThemePref>("kalshibot.theme", "system");
  const system = useSyncExternalStore(subscribeScheme, systemScheme, () => "dark" as const);
  const resolved: ResolvedTheme = pref === "system" ? system : pref;

  useEffect(() => {
    const root = document.documentElement;
    if (pref === "system") delete root.dataset.theme;
    else root.dataset.theme = pref;
    const meta = document.querySelector('meta[name="theme-color"]');
    meta?.setAttribute("content", resolved === "dark" ? "#0d0d0d" : "#f9f9f7");
  }, [pref, resolved]);

  const value = useMemo(() => ({ pref, resolved, setPref }), [pref, resolved, setPref]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export const useTheme = () => useContext(ThemeContext);
