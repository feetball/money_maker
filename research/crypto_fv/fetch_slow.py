import sys, kclient, fetch_kalshi
kclient.MIN_INTERVAL = 0.6
for arg in sys.argv[1:]:
    s, _, g = arg.partition(":")
    mr = fetch_kalshi._fetch_all_events(s, None)
    fetch_kalshi.fetch_candles(s, mr, group=int(g or 1))
