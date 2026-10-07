import { BrowserRouter, Route, Routes } from "react-router";
import { ConfirmProvider } from "./components/ConfirmDialog";
import { Layout, LegacyKalshiRedirect } from "./components/Layout";
import { StatusProvider } from "./lib/status";
import { StreamProvider } from "./lib/stream";
import { ThemeProvider } from "./lib/theme";
import { ToastProvider } from "./lib/toast";
import { Analytics } from "./pages/Analytics";
import { BacktestPage, Backtests } from "./pages/Backtests";
import { Dashboard } from "./pages/Dashboard";
import { History } from "./pages/History";
import { Markets } from "./pages/Markets";
import { NotFound } from "./pages/NotFound";
import { PositionsOrders } from "./pages/PositionsOrders";
import { Settings } from "./pages/Settings";
import { Signals } from "./pages/Signals";
import { Strategies } from "./pages/Strategies";

/**
 * History-API routing (real paths). When served by FastAPI, every non-/api GET that
 * is not a file in dist/ must return dist/index.html so deep links work.
 * The former /kalshi/* paths redirect to /*.
 */
export function App() {
  return (
    <ThemeProvider>
      <ToastProvider>
        <ConfirmProvider>
          <StreamProvider>
            <StatusProvider>
              <BrowserRouter>
                <Routes>
                  <Route element={<Layout />}>
                    <Route index element={<Dashboard />} />
                    <Route path="positions" element={<PositionsOrders />} />
                    <Route path="history" element={<History />} />
                    <Route path="strategies" element={<Strategies />} />
                    <Route path="signals" element={<Signals />} />
                    <Route path="markets" element={<Markets />} />
                    <Route path="analytics" element={<Analytics />} />
                    <Route path="backtests" element={<Backtests />} />
                    <Route path="backtests/:id" element={<BacktestPage />} />
                    <Route path="settings" element={<Settings />} />
                    <Route path="kalshi/*" element={<LegacyKalshiRedirect />} />
                    <Route path="*" element={<NotFound />} />
                  </Route>
                </Routes>
              </BrowserRouter>
            </StatusProvider>
          </StreamProvider>
        </ConfirmProvider>
      </ToastProvider>
    </ThemeProvider>
  );
}
