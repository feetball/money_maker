import { BrowserRouter, Route, Routes } from "react-router";
import { ConfirmProvider } from "./components/ConfirmDialog";
import { KalshiSection, Layout, LEGACY_KALSHI_PAGES, LegacyKalshiRedirect } from "./components/Layout";
import { OverviewProvider } from "./lib/overview";
import { StatusProvider } from "./lib/status";
import { StreamProvider } from "./lib/stream";
import { ThemeProvider } from "./lib/theme";
import { ToastProvider } from "./lib/toast";
import { Analytics } from "./pages/Analytics";
import { BacktestPage, Backtests } from "./pages/Backtests";
import {
  CoinbaseAnalytics,
  CoinbaseBacktestPage,
  CoinbaseBacktests,
  CoinbaseDashboard,
  CoinbaseHistory,
  CoinbaseMarkets,
  CoinbasePositions,
  CoinbaseSettings,
  CoinbaseSignals,
  CoinbaseStrategies,
} from "./pages/coinbase";
import { Dashboard } from "./pages/Dashboard";
import { History } from "./pages/History";
import { Markets } from "./pages/Markets";
import { NotFound } from "./pages/NotFound";
import { Overview } from "./pages/Overview";
import { PositionsOrders } from "./pages/PositionsOrders";
import { Settings } from "./pages/Settings";
import { Signals } from "./pages/Signals";
import { Strategies } from "./pages/Strategies";

/**
 * History-API routing (real paths). When served by FastAPI, every non-/api GET that
 * is not a file in dist/ must return dist/index.html so deep links work.
 *
 * Two separate paper venues (docs/COINBASE_CONTRACT.md §14):
 *   /            Overview (both venues side by side)
 *   /kalshi/*    Kalshi pages (KalshiSection adds the Kalshi banner, alerts and scope)
 *   /coinbase/*  Coinbase pages (each page renders its own Coinbase banner)
 * Pre-venue Kalshi paths (/positions, /backtests/12?…) redirect to /kalshi/….
 */
export function App() {
  return (
    <ThemeProvider>
      <ToastProvider>
        <ConfirmProvider>
          <StreamProvider>
            <StatusProvider>
              <OverviewProvider>
                <BrowserRouter>
                  <Routes>
                    <Route element={<Layout />}>
                      <Route index element={<Overview />} />
                      <Route path="kalshi" element={<KalshiSection />}>
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
                        <Route path="*" element={<NotFound />} />
                      </Route>
                      <Route path="coinbase">
                        <Route index element={<CoinbaseDashboard />} />
                        <Route path="positions" element={<CoinbasePositions />} />
                        <Route path="history" element={<CoinbaseHistory />} />
                        <Route path="strategies" element={<CoinbaseStrategies />} />
                        <Route path="signals" element={<CoinbaseSignals />} />
                        <Route path="markets" element={<CoinbaseMarkets />} />
                        <Route path="analytics" element={<CoinbaseAnalytics />} />
                        <Route path="backtests" element={<CoinbaseBacktests />} />
                        <Route path="backtests/:id" element={<CoinbaseBacktestPage />} />
                        <Route path="settings" element={<CoinbaseSettings />} />
                        <Route path="*" element={<NotFound />} />
                      </Route>
                      {LEGACY_KALSHI_PAGES.map((p) => (
                        <Route key={p} path={`${p}/*`} element={<LegacyKalshiRedirect />} />
                      ))}
                      <Route path="*" element={<NotFound />} />
                    </Route>
                  </Routes>
                </BrowserRouter>
              </OverviewProvider>
            </StatusProvider>
          </StreamProvider>
        </ConfirmProvider>
      </ToastProvider>
    </ThemeProvider>
  );
}
