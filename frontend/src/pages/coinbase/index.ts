/**
 * Coinbase venue pages (docs/COINBASE_CONTRACT.md §14). App.tsx (F1) routes to these
 * names under /coinbase. Every page renders <VenueBanner venue="coinbase" /> first and
 * runs inside <VenueScope venue="coinbase"> (see ./shared.tsx CbPage).
 */
export { CoinbaseAnalytics } from "./Analytics";
export { CoinbaseBacktestPage, CoinbaseBacktests } from "./Backtests";
export { CoinbaseDashboard } from "./Dashboard";
export { CoinbaseHistory } from "./History";
export { CoinbaseMarkets } from "./Markets";
export { CoinbasePositions } from "./Positions";
export { CoinbaseSettings } from "./Settings";
export { CoinbaseSignals } from "./Signals";
export { CoinbaseStrategies } from "./Strategies";

// Pieces other parts of the shell may reuse (top bar, Overview).
export { CbEngineControls as CoinbaseEngineControls, CbEnginePill as CoinbaseEnginePill, CbStreamIndicator as CoinbaseStreamIndicator } from "./shared";
