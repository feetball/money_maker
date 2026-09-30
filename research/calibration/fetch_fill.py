"""Fill the candle gaps of the research/data dataset for LIVE-era markets (settled after the 2026-07-28 cutoff).

Why: research/data fetched hourly candles only for the top-4-by-volume markets per event (and <=16 per series/day).
Final volume is outcome-correlated, so calibration on that subset is biased for cheap strikes; restricting to
`event_fully_covered` events instead selects low-volatility ladder events (few strikes traded) -> biased the other
way. Fetching candles for EVERY volume>0 live-source market in markets.parquet removes the within-event selection
for the live era. ~1,900 batch calls (GET /markets/candlesticks, <=100 tickers, <=10k candles per call).

Cache: gzip JSON under research/calibration/raw/candles_h (same key scheme as research/data/kalshi_client.py).
Output: research/calibration/candles_fill_hourly.parquet (same schema as research/data/candles_hourly.parquet).

    uv run --with pandas --with pyarrow --with numpy --with httpx python fetch_fill.py [max_minutes]
    uv run ... python fetch_fill.py build     # offline rebuild from cache
"""
from __future__ import annotations

import fcntl
import logging
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "data"))
import kalshi_client  # noqa: E402

kalshi_client.RAW_DIR = HERE / "raw"          # write the cache in OUR directory only
kalshi_client.MIN_FREE_BYTES = 700 * 1024 * 1024
from kalshi_client import CacheMiss, DiskFullError, KalshiClient  # noqa: E402
import fetch_candles as fc  # noqa: E402  (reuse pack() and the candle parser conventions)

log = logging.getLogger("fill")
OUT = HERE / "candles_fill_hourly.parquet"


def todo() -> pd.DataFrame:
    mk = pd.read_parquet(HERE.parent / "data" / "markets.parquet")
    have = set(pd.read_parquet(HERE.parent / "data" / "candles_hourly.parquet", columns=["ticker"])["ticker"])
    m = mk[(mk["source"] == "live") & (mk["volume"] > 0) & mk["result"].isin(["yes", "no"]) & ~mk["ticker"].isin(have)].copy()
    m["open_ts"] = m["open_time"].dt.as_unit("s").astype("int64")
    m["close_ts"] = m["close_time"].dt.as_unit("s").astype("int64")
    m["h_start"] = np.maximum(m["open_ts"], m["close_ts"] - fc.HOURLY_LOOKBACK) // 3600 * 3600
    m["h_end"] = (m["close_ts"] // 3600 + 2) * 3600
    m["close_date"] = m["close_time"].dt.strftime("%Y-%m-%d")
    return m


def jobs_for(m: pd.DataFrame):
    jobs = []
    # events that already have partial coverage first (they fix the within-event bias), then the rest
    ev_has = set(pd.read_parquet(HERE.parent / "data" / "candles_hourly.parquet", columns=["ticker"])["ticker"])
    mk = pd.read_parquet(HERE.parent / "data" / "markets.parquet", columns=["ticker", "event_ticker"])
    partial_events = set(mk[mk["ticker"].isin(ev_has)]["event_ticker"])
    m = m.assign(prio=~m["event_ticker"].isin(partial_events))
    for (prio, d), g in m.groupby(["prio", "close_date"], sort=True):
        jobs += [(t, a, b) for t, a, b in fc.pack(g, 60)]
    return jobs


def parse(out: dict, hstart: dict) -> list:
    rows = []
    for t, cs in out.items():
        for c in cs:
            ep = int(c["end_period_ts"])
            if ep <= hstart.get(t, 0):
                continue
            yb, ya, p = c.get("yes_bid") or {}, c.get("yes_ask") or {}, c.get("price") or {}
            g = fc._g
            rows.append((t, 60, ep, g(yb, "open"), g(yb, "high"), g(yb, "low"), g(yb, "close"),
                         g(ya, "open"), g(ya, "high"), g(ya, "low"), g(ya, "close"),
                         g(p, "open"), g(p, "high"), g(p, "low"), g(p, "close"), g(p, "mean"), g(p, "previous"),
                         fc._flt(c.get("volume_fp", c.get("volume"))),
                         fc._flt(c.get("open_interest_fp", c.get("open_interest")))))
    return rows


def to_df(rows: list) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=fc.CANDLE_COLS)
    for c in fc.CANDLE_COLS[3:]:
        df[c] = df[c].astype("float64" if c in ("volume", "open_interest") else "float32")
    df["period"] = df["period"].astype("int16")
    df["end_period_ts"] = df["end_period_ts"].astype("int64")
    return df


def run(max_minutes: float, offline: bool = False):
    m = todo()
    jobs = jobs_for(m)
    hstart = dict(zip(m["ticker"], m["h_start"]))
    log.info("%d markets to fill -> %d batch jobs (offline=%s)", len(m), len(jobs), offline)
    client = KalshiClient(rate=3.0, offline=offline)
    t_end = time.time() + max_minutes * 60
    lock = threading.Lock()
    st = {"i": 0, "done": 0, "miss": 0, "rows": [], "n": 0, "stop": False}

    def worker():
        while True:
            with lock:
                if st["stop"] or st["i"] >= len(jobs) or time.time() > t_end:
                    return
                tickers, s, e = jobs[st["i"]]
                st["i"] += 1
            try:
                out = fc.fetch_batch(client, list(tickers), s, e, 60)
            except CacheMiss:
                with lock:
                    st["miss"] += 1
                continue
            except DiskFullError:
                log.error("disk nearly full, stopping")
                st["stop"] = True
                return
            except Exception as ex:  # noqa: BLE001
                log.warning("job failed (%d tickers): %s", len(tickers), ex)
                continue
            r = to_df(parse(out, hstart))
            with lock:
                st["rows"].append(r)
                st["n"] += len(r)
                st["done"] += 1
                if st["done"] % 100 == 0:
                    log.info("jobs %d/%d rows %d [%s]", st["done"], len(jobs), st["n"], client.stats())

    ths = [threading.Thread(target=worker) for _ in range(1 if offline else 3)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    log.info("finished %d/%d jobs, cache-miss %d (%s)", st["done"], len(jobs), st["miss"], client.stats())
    df = pd.concat(st["rows"], ignore_index=True) if st["rows"] else to_df([])
    df = df.drop_duplicates(["ticker", "end_period_ts"]).sort_values(["ticker", "end_period_ts"])
    df.to_parquet(OUT, index=False, compression="zstd")
    log.info("wrote %s: %d candles, %d tickers", OUT, len(df), df["ticker"].nunique())


if __name__ == "__main__":
    (HERE / "logs").mkdir(exist_ok=True)
    lockf = open(HERE / "logs" / "fetch_fill.lock", "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("fetch_fill.py already running")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(HERE / "logs" / "fetch_fill.log")])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if sys.argv[1:] == ["build"]:
        run(1e9, offline=True)
    else:
        run(float(sys.argv[1]) if len(sys.argv) > 1 else 60)
