"""Backtester (ARCHITECTURE.md §10): replays historical data through ``Strategy`` classes.

* :mod:`kalshibot.backtest.runner` - ``run_backtest`` (sync) / ``run_backtest_async``: the same
  strategy classes, the real ``RiskManager`` and ``PaperBroker`` on a replay clock; metrics,
  equity curve, trades, by-month breakdown.
* :mod:`kalshibot.backtest.data` - the historical data: ``hourly`` (research/data candles,
  needs pyarrow) and ``minute`` (research/crypto_fv 1-minute candles + Coinbase spot), with
  no-look-ahead market snapshots and synthetic books.

This package ``__init__`` imports nothing, so the rest of the app works whether or not the
runner's optional dependencies are present: the API resolves
``kalshibot.backtest.runner.run_backtest`` lazily (``POST /api/backtests`` answers 501 when it
is missing) and ``kalshibot backtest`` says so too.

Calling convention used by the API/CLI: keyword arguments filtered to the runner's
signature, from ``strategy`` (registry name), ``strategy_cls``, ``params`` (validated,
defaults merged), ``start``/``end`` (ISO date strings or None; ``end`` dates are inclusive),
``starting_balance`` (float) and ``settings`` (runner options: an optional ``backtest:``
config section, or keyword arguments from the CLI). A synchronous runner runs in a daemon
thread; an ``async`` one is awaited. The result (``BacktestResult.to_json()``) provides
``metrics``, ``equity_curve`` (``[{ts, equity}]``), ``trades`` and ``by_month``.
"""
