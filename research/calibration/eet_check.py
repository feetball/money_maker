"""Is expected_expiration_time (EET) ex ante? Compares EET with the actual close / settlement per category and series.
    uv run --with pandas --with pyarrow python eet_check.py  -> logs/eet_check.txt"""
import sys; sys.path.insert(0, "/root/money_maker/research/data")
import pandas as pd, numpy as np
from loader import load_markets
mk = load_markets()
pd.set_option('display.width', 250); pd.set_option('display.max_columns', 30)
mk["d_close_eet_h"] = (mk.close_time - mk.expected_expiration_time).dt.total_seconds()/3600
mk["d_settle_eet_h"] = (mk.settlement_ts - mk.expected_expiration_time).dt.total_seconds()/3600
mk["eet_min"] = mk.expected_expiration_time.dt.minute
mk["eet_eq_close"] = mk.d_close_eet_h.abs() < 1/60
for cce, g in mk.groupby("can_close_early"):
    print("can_close_early", cce, len(g))
    print("  eet==close:", g.eet_eq_close.mean().round(4), " eet minute==0:", (g.eet_min==0).mean().round(3), "minute in {0,15,30,45}:", g.eet_min.isin([0,15,30,45]).mean().round(3))
    print("  close-eet h quantiles", g.d_close_eet_h.quantile([.01,.05,.25,.5,.75,.95,.99]).round(2).to_dict())
    print("  settle-eet h quantiles", g.d_settle_eet_h.quantile([.01,.05,.25,.5,.75,.95,.99]).round(2).to_dict())
print()
by = mk.groupby("category").agg(n=("ticker","size"), cce=("can_close_early","mean"), eet_eq_close=("eet_eq_close","mean"),
    eet_round=("eet_min", lambda m: (m==0).mean()), med_close_minus_eet=("d_close_eet_h","median"),
    p90_close_minus_eet=("d_close_eet_h", lambda v: v.quantile(.9)), share_close_after_eet=("d_close_eet_h", lambda v: (v>0.02).mean()))
print(by.round(3))
# within-event EET dispersion
ev = mk.groupby("event_ticker").expected_expiration_time.agg(lambda v: (v.max()-v.min()).total_seconds()/3600)
print("\nwithin-event EET spread (h) quantiles:", ev.quantile([.5,.9,.99,1]).to_dict())
# sports: EET relative to occurrence_datetime
s = mk[mk.category=="Sports"].copy()
s["eet_minus_occ"] = (s.expected_expiration_time - s.occurrence_datetime).dt.total_seconds()/3600
print("\nSports eet - occurrence (h):", s.eet_minus_occ.quantile([.01,.1,.5,.9,.99]).round(2).to_dict(), "null occ:", s.occurrence_datetime.isna().mean())
s["close_minus_occ"] = (s.close_time - s.occurrence_datetime).dt.total_seconds()/3600
print("Sports close - occurrence (h):", s.close_minus_occ.quantile([.01,.1,.5,.9,.99]).round(2).to_dict())
# is EET correlated with outcome timing? relation between close and EET for yes vs no
print("\nby result: median close-eet h", mk.groupby(["category","y"]).d_close_eet_h.median().unstack().round(2))
# top series by count with EET==close share
ser = mk.groupby("series_ticker").agg(n=("ticker","size"), cat=("category","first"), eqc=("eet_eq_close","mean"), cce=("can_close_early","mean"), med=("d_close_eet_h","median")).sort_values("n", ascending=False)
print(ser.head(40).round(3))
