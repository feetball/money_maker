"""Pydantic response/request models for the REST API (ARCHITECTURE.md §12).

Response models carry the exact §12 field names. They allow extra keys
(``extra="allow"``), so additive fields (``status_reason``, ``kill_switch_reason``,
``open_fees``, ...) pass through to the frontend. Request bodies forbid unknown keys.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Account",
    "AccountResetRequest",
    "AnalyticsResponse",
    "AnalyticsStats",
    "BacktestCreateRequest",
    "BacktestCreateResponse",
    "BacktestDetail",
    "BacktestSummary",
    "CalibrationBucket",
    "EngineStatus",
    "EquityPoint",
    "ExchangeStatus",
    "FillOut",
    "KillSwitchRequest",
    "LogOut",
    "MarketRow",
    "OrderOut",
    "PositionOut",
    "Readiness",
    "RiskResponse",
    "RiskUtilization",
    "SettlementOut",
    "SignalOut",
    "StatusResponse",
    "StrategyOut",
    "StrategyPatch",
    "StrategyStats",
]


class _Out(BaseModel):
    model_config = ConfigDict(extra="allow")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- status / engine -----------------------------------------------------------------


class EngineStatus(_Out):
    running: bool
    started_at: str | None
    last_tick_at: str | None
    tick_count: int
    universe_size: int
    last_error: str | None
    kill_switch: bool


class ExchangeStatus(_Out):
    trading_active: bool | None


class StatusResponse(_Out):
    mode: Literal["paper"]
    engine: EngineStatus
    exchange: ExchangeStatus
    server_time: str


class KillSwitchRequest(_In):
    on: bool
    reason: str | None = None


# -- account ---------------------------------------------------------------------------


class Account(_Out):
    starting_balance: float
    cash: float
    reserved_cash: float
    positions_liquidation_value: float
    positions_mid_value: float
    equity: float
    equity_mid: float
    realized_pnl: float
    unrealized_pnl: float
    fees_paid: float
    #: profit swept out of cash (AccountSettings.profit_sweep_pct); excluded from `equity`
    reserved_profit: float
    #: equity + reserved_profit
    net_worth: float
    total_pnl: float
    total_return_pct: float
    todays_pnl: float
    max_drawdown_pct: float
    open_positions: int
    open_orders: int
    settled_trades: int
    win_rate: float | None


class AccountResetRequest(_In):
    starting_balance: float | None = Field(default=None, gt=0, le=1e9)


class EquityPoint(_Out):
    ts: str
    equity: float
    equity_mid: float
    cash: float
    realized_pnl: float
    unrealized_pnl: float


# -- portfolio -------------------------------------------------------------------------


class PositionOut(_Out):
    ticker: str
    title: str
    event_ticker: str
    side: str
    count: int
    avg_price: float | None
    cost_basis: float
    mark_price: float | None
    liquidation_value: float | None
    unrealized_pnl: float | None
    fair_value: float | None
    expected_edge_total: float | None
    strategy: str
    opened_at: str | None
    close_time: str | None
    yes_bid: float | None
    yes_ask: float | None
    url: str | None


class OrderOut(_Out):
    id: int
    ticker: str
    title: str
    side: str
    action: str
    count: int
    filled_count: int
    limit_price: float | None
    avg_fill_price: float | None
    tif: str
    status: str
    strategy: str
    reason: str
    expected_edge: float | None
    fair_value: float | None
    group_id: str | None
    queue_ahead: float | None
    created_at: str | None
    updated_at: str | None
    expires_at: str | None
    fees: float | None


class FillOut(_Out):
    id: int
    order_id: int
    ticker: str
    title: str
    side: str
    action: str
    count: int
    price: float | None
    fee: float | None
    is_taker: bool
    ts: str | None
    strategy: str


class SettlementOut(_Out):
    id: int
    ticker: str
    title: str
    result: str
    side: str
    count: int
    payout: float | None
    cost_basis: float | None
    pnl: float | None
    ts: str | None
    strategy: str


# -- strategies ----------------------------------------------------------------------


class StrategyStats(_Out):
    orders: int
    fills: int
    open_positions: int
    settled: int
    realized_pnl: float
    unrealized_pnl: float
    fees: float
    win_rate: float | None
    exposure: float


class StrategyOut(_Out):
    name: str
    description: str
    enabled: bool
    params: dict[str, Any]
    param_schema: dict[str, dict[str, Any]]
    backtestable: bool
    stats: StrategyStats


class StrategyPatch(_In):
    enabled: bool | None = None
    params: dict[str, Any] | None = None


# -- risk ------------------------------------------------------------------------------


class RiskUtilization(_Out):
    total_exposure: float
    total_exposure_pct: float
    by_event: list[dict[str, Any]]
    by_strategy: list[dict[str, Any]]
    orders_last_minute: int
    daily_pnl: float


class RiskResponse(_Out):
    limits: dict[str, Any]
    utilization: RiskUtilization
    kill_switch: bool


# -- feeds -----------------------------------------------------------------------------


class SignalOut(_Out):
    ts: str | None
    strategy: str
    ticker: str
    title: str
    side: str | None
    count: int | None
    limit_price: float | None
    fair_value: float | None
    expected_edge: float | None
    reason: str
    decision: str
    decision_reason: str


class LogOut(_Out):
    ts: str | None
    level: str
    kind: str
    message: str
    data: Any = None


class MarketRow(_Out):
    ticker: str
    event_ticker: str
    title: str
    category: str
    yes_bid: float | None
    yes_ask: float | None
    spread: float | None
    last_price: float | None
    volume_24h: float
    open_interest: float
    close_time: str | None
    url: str | None


# -- analytics -------------------------------------------------------------------------


class Readiness(_Out):
    ready: bool
    reasons: list[str]


class AnalyticsStats(_Out):
    count: int
    contracts: float | None
    total_pnl: float | None
    mean_pnl_per_contract: float | None
    mean_pnl_per_trade: float | None
    ci_low: float | None
    ci_high: float | None
    ci_basis: str
    expected_edge_total: float | None
    realized_pnl: float | None
    brier: float | None
    win_rate: float | None
    max_drawdown: float | None
    max_drawdown_pct: float | None
    readiness: Readiness | None


class CalibrationBucket(_Out):
    bucket: str
    n: int
    mean_fair_value: float
    realized_rate: float


class AnalyticsResponse(_Out):
    overall: AnalyticsStats
    by_strategy: dict[str, AnalyticsStats]
    calibration: list[CalibrationBucket]
    readiness: Readiness


# -- backtests -------------------------------------------------------------------------


class BacktestSummary(_Out):
    id: int
    strategy: str
    params: dict[str, Any]
    start: str | None
    end: str | None
    status: str
    created_at: str | None
    metrics: dict[str, Any] | None


class BacktestCreateRequest(_In):
    strategy: str = Field(min_length=1)
    params: dict[str, Any] | None = None
    start: str | None = None
    end: str | None = None
    starting_balance: float | None = Field(default=None, gt=0, le=1e9)


class BacktestCreateResponse(_Out):
    id: int
    status: str


class BacktestDetail(_Out):
    id: int
    strategy: str
    params: dict[str, Any]
    status: str
    error: str | None
    metrics: dict[str, Any] | None
    equity_curve: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    by_month: list[dict[str, Any]]
