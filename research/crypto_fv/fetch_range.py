"""Fetch settled KXBTC (hourly range) events + 1-min bid/ask candles for final 70 min (reuses fetch_kalshi)."""
import sys, kclient, fetch_kalshi
kclient.MIN_INTERVAL = 0.3  # ~3.3 req/s
for arg in sys.argv[1:]:
    s, _, g = arg.partition(":")
    mr = fetch_kalshi._fetch_all_events(s, None)
    fetch_kalshi.fetch_candles(s, mr, group=int(g or 1))
