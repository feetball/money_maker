"""Yahoo Finance 1-min index bars (last ~29 days only). -> data/spot_<NAME>.csv (ts=bar START)"""
import csv, os, sys, time
import httpx
HERE = os.path.dirname(os.path.abspath(__file__))
cl = httpx.Client(timeout=30, headers={"User-Agent": "Mozilla/5.0"})
def main(symbol, name, days=29):
    now = int(time.time()); rows = {}
    start = now - days * 86400
    t = start
    while t < now:
        e = min(t + 7 * 86400, now)
        r = cl.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}", params={"interval": "1m", "period1": t, "period2": e})
        time.sleep(0.5)
        res = r.json()["chart"]["result"]
        if res:
            res = res[0]; ts = res.get("timestamp") or []
            q = res["indicators"]["quote"][0]
            for i, x in enumerate(ts):
                if q["close"][i] is not None:
                    rows[x] = (x, q["low"][i], q["high"][i], q["open"][i], q["close"][i], q["volume"][i] or 0)
        t = e
    with open(os.path.join(HERE, "data", f"spot_{name}.csv"), "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["ts", "low", "high", "open", "close", "volume"])
        for k in sorted(rows): w.writerow(rows[k])
    print(name, len(rows))
if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
