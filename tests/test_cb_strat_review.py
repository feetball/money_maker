"""Regression tests for the Coinbase strategy review (btc_trend / btc_hold / eth_trend_vt).

PAPER ONLY. One test (or group) per review item:
1. a missing final daily bar is never decided on silently (no change, dated log line);
2. btc_hold / btc_trend only ever enter from nothing or fully exit (no top-ups after a drop);
3. the three strategies have risk_defaults that fit the per-product / total caps together,
   and are enabled by default;
4. eth_trend_vt's lateness note does not quote btc_trend's delay figure; no late note on a
   first entry from an old signal;
5./6. descriptions document allocation changes and the app-backtest figures.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from kalshibot.coinbase.config import CoinbaseRiskSettings
from kalshibot.coinbase.strategies import REGISTRY, resolve_enabled
from kalshibot.coinbase.strategies.btc_hold import BtcHold
from kalshibot.coinbase.strategies.btc_trend import BTC, BtcTrend
from kalshibot.coinbase.strategies.eth_trend_vt import ETH, EthTrendVolTarget
from test_cb_strat_btc import FakeCtx, flat_then, mk_bars, walk, weight_of

NAMES = ("btc_trend", "btc_hold", "eth_trend_vt")


def _ctx(bars, k, *, held_usd: float = 0.0, alloc: float = 1000.0, pid: str = BTC, delay_s: float = 30):
    ctx = FakeCtx(bars, k, pid=pid, products=(pid,), delay_s=delay_s)
    ctx.portfolio.alloc_equity = Decimal(str(alloc))
    ctx.portfolio.values = {pid: Decimal(str(held_usd))} if held_usd else {}
    return ctx


def _drop_final_bar(ctx) -> None:
    """The engine ran after its wait: bar_end is one day past the newest published candle."""
    ctx.bar_end += timedelta(days=1)
    ctx.now = ctx.bar_end + timedelta(seconds=210)


# --------------------------------------------------------------------------- 1. missing final bar


def test_btc_trend_missing_final_bar_makes_no_change_and_says_which_bar() -> None:
    bars = mk_bars(flat_then(100.0, [103.0]))  # an entry signal on the last published close
    ctx = _ctx(bars, len(bars) - 1)
    _drop_final_bar(ctx)
    assert BtcTrend().on_bar(ctx) is None
    msg = ctx.logs[-1][0]
    last = bars[-1].start.date().isoformat()
    want = (bars[-1].start + timedelta(days=1)).date().isoformat()
    assert f"the {want} daily bar (closed " in msg and "is not published" in msg, msg
    assert f"latest candle: the {last} close" in msg, msg
    assert "no change" in msg


def test_eth_trend_vt_missing_final_bar_makes_no_change() -> None:
    closes = [100.0 * 1.001 ** i for i in range(300)]
    bars = mk_bars(closes, pid=ETH)
    ctx = _ctx(bars, len(bars) - 1, pid=ETH)
    _drop_final_bar(ctx)
    assert EthTrendVolTarget().on_bar(ctx) is None
    assert "daily bar (closed " in ctx.logs[-1][0] and "is not published" in ctx.logs[-1][0]


def test_decision_reasons_carry_the_close_date() -> None:
    bars = mk_bars(flat_then(100.0, [103.0]))
    res = BtcTrend().on_bar(_ctx(bars, len(bars) - 1))
    assert f"({bars[-1].start.date().isoformat()})" in res[0].reason


# --------------------------------------------------------------------------- 2. no top-ups


def test_btc_hold_does_not_buy_more_after_a_large_drop() -> None:
    bars = mk_bars(walk(30, 1))
    s = BtcHold()
    # bought $500 at the start; BTC fell 70% -> $150 of a $850 slice (18%): still holding, no buy
    assert s.on_bar(_ctx(bars, 20, held_usd=150.0, alloc=850.0)) is None
    # other strategies doubled the account: 25% of the slice, still no buy
    assert s.on_bar(_ctx(bars, 20, held_usd=500.0, alloc=2000.0)) is None
    # holding only dust (< the $10 minimum trade) or nothing: buy
    assert weight_of(s.on_bar(_ctx(bars, 20, held_usd=4.0))) == 1.0
    assert weight_of(s.on_bar(_ctx(bars, 20))) == 1.0


def test_btc_trend_in_and_holding_never_tops_up() -> None:
    closes = flat_then(100.0, [110.0] + [99.5] * 3)  # in by the 110 signal, now inside the band
    bars = mk_bars(closes)
    s = BtcTrend()
    ctx = _ctx(bars, len(bars) - 1, held_usd=200.0, alloc=1000.0)  # 20% of a grown slice
    assert s.on_bar(ctx) is None
    assert "no trade" in ctx.logs[-1][0]
    # fresh entry signal while already holding something: still no resize
    bars = mk_bars(flat_then(100.0, [103.0]))
    assert s.on_bar(_ctx(bars, len(bars) - 1, held_usd=200.0)) is None
    # holding only dust: enter
    assert weight_of(s.on_bar(_ctx(bars, len(bars) - 1, held_usd=5.0))) == 1.0


def test_btc_trend_exit_sells_whatever_is_held() -> None:
    bars = mk_bars(flat_then(100.0, [110.0] * 5 + [95.0]))
    assert weight_of(BtcTrend().on_bar(_ctx(bars, len(bars) - 1, held_usd=200.0))) == 0.0


# --------------------------------------------------------------------------- 3. allocations and defaults


def test_default_allocations_fit_the_default_risk_caps_together() -> None:
    lim = CoinbaseRiskSettings()
    alloc = {n: REGISTRY[n].risk_defaults["max_allocation_pct"] for n in NAMES}
    btc = alloc["btc_trend"] + alloc["btc_hold"]
    # both BTC sleeves fit the per-product cap with room for btc_hold's BTC to appreciate
    assert btc <= float(lim.max_position_pct_per_product) - 10
    assert sum(alloc.values()) <= float(lim.max_total_exposure_pct)
    assert alloc["btc_trend"] >= alloc["btc_hold"]  # the primary is not smaller than its benchmark
    # every band trade on a $1,000 account clears the $10 minimum trade
    for n in NAMES:
        assert REGISTRY[n].rebalance_band * alloc[n] / 100 * 1000 >= float(lim.min_trade_usd), n


@pytest.mark.parametrize("name", NAMES)
def test_enabled_by_default_without_a_coinbase_section(name: str) -> None:
    assert resolve_enabled(REGISTRY[name]) == (True, "default")
    assert resolve_enabled(REGISTRY[name], config=False) == (False, "config")


def test_config_example_documents_the_built_in_strategy_defaults() -> None:
    """``coinbase.strategies`` stays ``{}`` in the example (uncommented, the block equals the
    built-in defaults - test_cb_config); the double-commented entries under it show exactly the
    built-in values and are valid settings when uncommented."""
    import pathlib

    import yaml

    from kalshibot.coinbase.config import CoinbaseSettings

    lines = (pathlib.Path(__file__).resolve().parents[1] / "config.example.yaml").read_text().splitlines()
    i = lines.index("#   strategies: {}   # {} = the built-in values below; to change one, list it here, e.g.")
    body = []
    for ln in lines[i + 1:]:
        if not ln.startswith("#   #"):
            break
        body.append(ln[len("#   # "):])
    entries = yaml.safe_load("\n".join(body))
    assert set(entries) == set(NAMES)
    st = CoinbaseSettings.model_validate({"strategies": entries})
    for n in NAMES:
        entry = st.strategy(n)
        assert entry.enabled is True and resolve_enabled(REGISTRY[n])[0] is True, n
        assert entry.max_allocation_pct == REGISTRY[n].risk_defaults["max_allocation_pct"], n
        assert REGISTRY[n].resolve_params(entry.params, strict=True) == REGISTRY[n].default_params, n


# --------------------------------------------------------------------------- 4. late notes


def test_eth_late_note_does_not_quote_btc_trends_delay_cost() -> None:
    closes = [100.0 * (1.02 if i % 2 else 0.99) ** 1 * 1.001 ** i for i in range(300)]
    bars = mk_bars(closes, pid=ETH)
    ctx = _ctx(bars, len(bars) - 1, pid=ETH, held_usd=300.0, delay_s=5 * 3600)
    res = EthTrendVolTarget().on_bar(ctx)
    why = res[0].reason
    assert "decided 5.0 h after" in why
    assert "7 pts/yr" not in why and "delay cost not measured in research" in why


def test_btc_trend_no_late_note_on_a_first_entry_from_an_old_signal() -> None:
    closes = flat_then(100.0, [110.0, 101.0, 100.5])  # signal 2 days ago, now inside the band
    bars = mk_bars(closes)
    res = BtcTrend().on_bar(_ctx(bars, len(bars) - 1, delay_s=15 * 3600))  # enabled mid-day, holds nothing
    assert weight_of(res) == 1.0 and "after the" not in res[0].reason
    # a fresh signal decided late is still flagged
    bars = mk_bars(flat_then(100.0, [103.0]))
    res = BtcTrend().on_bar(_ctx(bars, len(bars) - 1, delay_s=15 * 3600))
    assert "decided 15.0 h after" in res[0].reason and "7 pts/yr" in res[0].reason


# --------------------------------------------------------------------------- 5./6. descriptions


def test_descriptions_document_allocation_changes_and_app_backtest_figures() -> None:
    for cls in (BtcTrend, BtcHold):
        assert "next entry" in cls.description, cls.name
    assert "28.3" in BtcTrend.description and "27.7" in BtcTrend.description
    assert "27.7" in BtcHold.description
    assert "neighbouring parameter" in BtcTrend.description
