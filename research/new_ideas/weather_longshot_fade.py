import pandas as pd, numpy as np
p=pd.read_parquet('research/calibration/panel_hourly.parquet',columns=['ticker','t','bid','ask','y','event_ticker','series_ticker','category','h_to_eet','fee_multiplier','close_time','month','can_close_early'])
p=p[p.category.isin(['Climate and Weather'])].dropna(subset=['bid','ask','h_to_eet'])
p['date']=pd.to_datetime(p.t,unit='s',utc=True)
print(len(p),p.ticker.nunique(),p.event_ticker.nunique(), p.series_ticker.str.replace(r'\d.*','',regex=True).value_counts().head(15))
def fee(px,mult): return np.ceil(np.round(0.07*mult*10*px*(1-px)*100,9))/100/10
def run(lo,hi,hmin,hmax,side='no',label=''):
    d=p[(p.ask>=lo)&(p.ask<=hi)&(p.h_to_eet>=hmin)&(p.h_to_eet<=hmax)&(p.ask-p.bid<=0.06)].sort_values('t').groupby('ticker').head(1)
    # buy NO at 1-bid (taker), pays 1 if y==0
    px=1-d.bid; pnl=(1-d.y)-px-fee(px,d.fee_multiplier)
    d=d.assign(pnl=pnl)
    out=[]
    for name,m in [('train',d.date<'2026-08-15'),('test',d.date>='2026-08-15')]:
        x=d[m]; 
        if len(x)<30: out.append((name,len(x))); continue
        g=x.groupby('event_ticker').pnl.agg(['sum','size']); mu=x.pnl.mean()
        # cluster bootstrap
        rng=np.random.default_rng(0); idx=np.arange(len(g)); bs=[]
        for _ in range(1000):
            s=rng.choice(idx,len(idx)); bs.append(g['sum'].values[s].sum()/g['size'].values[s].sum())
        out.append((name,len(x),len(g),round(100*mu,2),tuple(np.round(100*np.percentile(bs,[2.5,97.5]),2))))
    print(f'{label} ask[{lo},{hi}] h[{hmin},{hmax}]',out)
for lo,hi in [(0.03,0.10),(0.05,0.15),(0.10,0.20)]:
    for hmin,hmax in [(0,12),(12,36),(36,96),(0,96)]:
        run(lo,hi,hmin,hmax)
