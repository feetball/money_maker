"""SpotPaperBroker persistence (contract §6 rule 8, §7): a restart restores the account exactly,
and a randomized session keeps every ledger identity exact after each step."""

from __future__ import annotations

import random
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_cb_broker import identity
from test_cb_broker_fakes import PID, TIER, FakeSpotMD, make_product, make_trade, standard_md

from kalshibot.coinbase.broker import SpotPaperBroker
from kalshibot.coinbase.paper import SpotOrderIntent
from kalshibot.coinbase.store import SpotStore, StoreLockedError
from kalshibot.money import ZERO


def D(x: object) -> Decimal:
    return Decimal(str(x))


def snapshot(b: SpotPaperBroker) -> dict[str, Any]:
    return {
        "account": {k: v for k, v in b.account().to_json().items()},
        "positions": b.positions_json(),
        "open_orders": [o.to_json() | {"taker_notional": str(o.taker_notional), "maker_notional":
                                       str(o.maker_notional), "maker_fees": str(o.maker_fees)}
                        for o in b.open_orders()],
        "consumed": b.consumed(),
        "cursors": dict(b._cursors),
        "mark": b.mark_of(PID),
        "stats": b.strategy_stats(),
        "ids": dict(b._ids),
        "day_start": b._day_start,
    }


async def session_part1(b: SpotPaperBroker, md: FakeSpotMD) -> None:
    await b.place_order(SpotOrderIntent(PID, "buy", quote_size=D(150), strategy="s1"))
    await b.place_order(SpotOrderIntent(PID, "buy", base_size=D(1), order_type="limit", limit_price=D(99),
                                        tif="gtc", post_only=True, strategy="s2"))
    await b.place_order(SpotOrderIntent(PID, "sell", base_size=D("0.5"), order_type="limit", limit_price=D(104),
                                        tif="gtc", post_only=True, strategy="s1"))


async def session_part2(b: SpotPaperBroker, md: FakeSpotMD) -> None:
    await b.maintain()
    await b.place_order(SpotOrderIntent(PID, "sell", base_size=D("0.4"), strategy="s1"))
    await b.mark()
    b.equity_snapshot()


async def test_restart_restores_exactly(tmp_path: Path) -> None:
    path = tmp_path / "coinbase.sqlite3"
    md = standard_md()
    store = SpotStore(path, exclusive=True)
    with pytest.raises(StoreLockedError):
        SpotStore(path, exclusive=True)  # one writer process per database
    b = SpotPaperBroker(md, store, clock=md.clock, fee_tier=TIER)
    twin = SpotPaperBroker(md, SpotStore(":memory:"), clock=md.clock, fee_tier=TIER)  # never restarted

    for br in (b, twin):
        await session_part1(br, md)
    md.clock.advance(5)
    md.add_trades([make_trade(10, 99, "0.4", "buy", md.clock.now),  # s2's bid: burns queue 1 of 1... partially
                   make_trade(11, "98.5", "0.3", "buy", md.clock.now)])  # through 99: fills 0.3
    for br in (b, twin):
        await session_part2(br, md)
    before = snapshot(b)
    assert before == snapshot(twin)
    assert before["open_orders"] and before["positions"] and before["consumed"] and before["cursors"] == {PID: 11}

    store.close()
    store2 = SpotStore(path, exclusive=True)
    b2 = SpotPaperBroker(md, store2, clock=md.clock, fee_tier=TIER)
    after = snapshot(b2)
    assert after == before  # cash, reservations, positions, marks (no new book needed), queue, cursors, ids
    identity(b2)

    # continue both: re-delivered prints are ignored, new ones fill identically
    md.clock.advance(5)
    md.add_trades([make_trade(11, 50, 100, "buy", md.clock.now),  # duplicate id (<= cursor): ignored
                   make_trade(12, 99, 5, "buy", md.clock.now),
                   make_trade(13, 104, 5, "sell", md.clock.now)])
    md.set_book(PID, bids=[(103, 2)], asks=[(104, 1)])
    for br in (b2, twin):
        await br.maintain()
        await br.mark()
        br.equity_snapshot()
        await br.place_order(SpotOrderIntent(PID, "buy", quote_size=D(40), order_type="limit",
                                             limit_price=D(104), strategy="s3"))
    assert snapshot(b2) == snapshot(twin)
    assert [f.to_json() for f in b2.store.list_fills()] == [f.to_json() for f in twin.store.list_fills()]  # type: ignore[union-attr]
    assert len(b2.store.list_equity()) == 2  # type: ignore[union-attr]
    store2.close()


async def test_new_account_row_is_written_once(tmp_path: Path) -> None:
    md = standard_md()
    s = SpotStore(tmp_path / "a.sqlite3")
    SpotPaperBroker(md, s, clock=md.clock, starting_balance=D(250))
    assert s.get_account()["cash"] == D(250)  # type: ignore[index]
    # an existing account wins over the configured starting balance
    b = SpotPaperBroker(md, s, clock=md.clock, starting_balance=D(999))
    assert b.account().starting_balance == D(250)
    s.close()


# --------------------------------------------------------------------------- randomized identity


def ledger_checks(b: SpotPaperBroker, start: Decimal) -> None:
    identity(b)
    st = b.store
    assert st is not None
    fills = st.list_fills(limit=None)
    a = b.account()
    assert a.fees_paid == sum((f.fee for f in fills), ZERO)
    assert a.realized_pnl == sum((f.realized_pnl for f in fills if f.side == "sell"), ZERO)
    cash_flow = sum((f.cash_delta for f in fills), ZERO)
    assert a.cash + a.reserved_cash == start + cash_flow
    assert a.cash >= 0 and all(o.reserved >= 0 for o in b.open_orders())
    assert a.reserved_cash == sum((o.reserved for o in b.open_orders()), ZERO)
    qty: dict[tuple[str, str], Decimal] = {}
    for f in fills:
        k = (f.strategy, f.product_id)
        qty[k] = qty.get(k, ZERO) + (f.base_size if f.side == "buy" else -f.base_size)
    held = {p.key: p.quantity for p in b.positions()}
    assert held == {k: v for k, v in qty.items() if v != 0}
    assert all(v >= 0 for v in qty.values())  # never short
    for p in b.positions():
        assert b.available_to_sell(p.strategy, p.product_id) >= 0
    # the store agrees with memory
    assert {p.key: (p.quantity, p.cost_basis) for p in st.list_positions()} == {
        p.key: (p.quantity, p.cost_basis) for p in b.positions()}
    assert st.get_account()["cash"] == a.cash  # type: ignore[index]


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
async def test_randomized_session_keeps_identities(seed: int) -> None:
    rng = random.Random(seed)
    md = FakeSpotMD()
    pids = [PID, "ALT-USD"]
    md.products[PID] = make_product(PID)
    md.products["ALT-USD"] = make_product("ALT-USD", base_increment="0.001", quote_increment="0.0001",
                                          min_market_funds="1")
    start = D(1000)
    b = SpotPaperBroker(md, SpotStore(":memory:"), clock=md.clock, fee_tier=TIER, starting_balance=start)
    mids = {PID: D(100), "ALT-USD": D("2.5")}
    tid = 0

    def new_book(pid: str) -> None:
        m = mids[pid] = max(mids[pid] * D(str(round(rng.uniform(0.97, 1.03), 4))), D("0.5"))
        tick = D("0.01") if pid == PID else D("0.0001")
        bid = (m * D("0.999")).quantize(tick)
        ask = max((m * D("1.001")).quantize(tick), bid + tick)
        md.set_book(pid, bids=[(bid - i * tick * 10, D(str(round(rng.uniform(0.1, 3), 2)))) for i in range(5)],
                    asks=[(ask + i * tick * 10, D(str(round(rng.uniform(0.1, 3), 2)))) for i in range(5)])

    for pid in pids:
        new_book(pid)
    strategies = ["a", "b", "c"]
    for _ in range(120):
        md.clock.advance(rng.choice([1, 5, 30, 400]))
        pid = rng.choice(pids)
        strat = rng.choice(strategies)
        op = rng.random()
        book = md.books[pid]
        if op < 0.15:
            new_book(pid)
            await b.mark()
        elif op < 0.35:
            await b.place_order(SpotOrderIntent(pid, "buy", quote_size=D(rng.randint(5, 200)), strategy=strat))
        elif op < 0.5:
            avail = b.available_to_sell(strat, pid)
            if avail > 0:
                frac = D(str(rng.choice([0.25, 0.5, 1])))
                await b.place_order(SpotOrderIntent(pid, "sell", base_size=avail * frac, strategy=strat))
        elif op < 0.65:
            side = rng.choice(["buy", "sell"])
            ref = book.best_bid if side == "buy" else book.best_ask
            if ref is None:
                continue
            avail = b.available_to_sell(strat, pid)
            size = D(str(round(rng.uniform(0.2, 2), 2))) if side == "buy" else avail / 2
            if size <= 0:
                continue
            await b.place_order(SpotOrderIntent(pid, side, base_size=size, order_type="limit",  # type: ignore[arg-type]
                                                limit_price=ref, tif="gtc", post_only=rng.random() < 0.7,
                                                expires_in_s=rng.choice([60, 600, None]), strategy=strat))
        elif op < 0.85:
            trades = []
            for _ in range(rng.randint(1, 4)):
                tid += 1
                maker = rng.choice(["buy", "sell"])
                ref = book.best_bid if maker == "buy" else book.best_ask
                if ref is None:
                    continue
                px = ref * D(str(round(rng.uniform(0.995, 1.005), 4)))
                px = px.quantize(D("0.01") if pid == PID else D("0.0001"))
                trades.append(make_trade(tid, px, D(str(round(rng.uniform(0.05, 2), 3))), maker, md.clock.now, pid))
            md.add_trades(trades)
            if rng.random() < 0.3:
                new_book(pid)
            await b.maintain()
        elif op < 0.95:
            opens = b.open_orders()
            if opens:
                await b.cancel_order(rng.choice(opens).id, "random cancel")
        else:
            b.equity_snapshot()
        ledger_checks(b, start)
    assert b.account().fills > 0
