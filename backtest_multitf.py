"""
FarhanFX — Multi-Timeframe Strategy Backtest
Same calendar period for all TFs. ATR filter scales by TF to match
original H1 backtest conditions. Best WR TF (>=50%, >=15 trades)
becomes the default trading TF for that strategy.
"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import MetaTrader5 as mt5
from datetime import datetime, timezone

mt5.initialize(timeout=15000)
mt5.symbol_select('XAUUSDc', True)

# Anchor: 5000 H1 bars → note start timestamp
h1_anchor = mt5.copy_rates_from_pos('XAUUSDc', mt5.TIMEFRAME_H1, 0, 5000)
start_ts  = int(h1_anchor[0]['time'])
start_dt  = datetime.fromtimestamp(start_ts, tz=timezone.utc)
end_dt    = datetime.now(timezone.utc)
hours_win = (end_dt.timestamp() - start_ts) / 3600
print(f"Window: {start_dt.strftime('%Y-%m-%d')} → {end_dt.strftime('%Y-%m-%d')}  ({hours_win:.0f} hrs)")

# Reference D1/H4 for trend filter (fixed, not test data)
_d1r = mt5.copy_rates_from('XAUUSDc', mt5.TIMEFRAME_D1, start_dt, 2000)
_h4r = mt5.copy_rates_from('XAUUSDc', mt5.TIMEFRAME_H4, start_dt, 5000)
mt5.shutdown()

d1_c = [float(r['close']) for r in _d1r]; d1_t = [int(r['time']) for r in _d1r]
h4_c = [float(r['close']) for r in _h4r]; h4_t = [int(r['time']) for r in _h4r]

# ─── Indicators ───────────────────────────────────────────────────────────────
def ema(p, n):
    if len(p) < 2: return p[:]
    n = min(n, len(p)-1) or 1
    k = 2/(n+1); e = [p[0]]
    for x in p[1:]: e.append(x*k + e[-1]*(1-k))
    return e

def atr_s(hi, lo, cl, n=14):
    tr = [hi[0]-lo[0]]
    for i in range(1, len(cl)):
        tr.append(max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1])))
    n = min(n, len(tr))
    a = [sum(tr[:n])/n]
    for i in range(n, len(tr)): a.append((a[-1]*(n-1)+tr[i])/n)
    return [a[0]]*(n-1) + a

def rsi_s(cl, n=14):
    g=[0.0]; lo2=[0.0]
    for i in range(1, len(cl)):
        d = cl[i]-cl[i-1]; g.append(max(d,0)); lo2.append(max(-d,0))
    if len(g) <= n: return [50.0]*len(cl)
    ag = sum(g[1:n+1])/n; al = sum(lo2[1:n+1])/n
    r = [50.0]*n
    for i in range(n, len(cl)):
        ag=(ag*(n-1)+g[i])/n; al=(al*(n-1)+lo2[i])/n
        r.append(100.0 if al==0 else 100-100/(1+ag/al))
    return r

def supertrend(hi, lo, cl, n=10, m=3.0):
    atr = atr_s(hi, lo, cl, n)
    up=[0.0]*len(cl); dn=[0.0]*len(cl); tr=[1]*len(cl)
    for i in range(1, len(cl)):
        mid=(hi[i]+lo[i])/2
        bu=mid-m*atr[i]; bd=mid+m*atr[i]
        up[i]=bu if bu>up[i-1] or cl[i-1]<up[i-1] else up[i-1]
        dn[i]=bd if bd<dn[i-1] or cl[i-1]>dn[i-1] else dn[i-1]
        if tr[i-1]==-1 and cl[i]>dn[i]: tr[i]=1
        elif tr[i-1]==1 and cl[i]<up[i]: tr[i]=-1
        else: tr[i]=tr[i-1]
    return tr

# ─── D1/H4 reference trend ────────────────────────────────────────────────────
d1e50  = ema(d1_c,50); d1e200 = ema(d1_c, min(200,len(d1_c)-1))
h4e50  = ema(h4_c,50); h4e200 = ema(h4_c, min(200,len(h4_c)-1))
d1_bull_v=[d1_c[i]>d1e50[i]>d1e200[i] for i in range(len(d1_c))]
d1_bear_v=[d1_c[i]<d1e50[i]<d1e200[i] for i in range(len(d1_c))]
h4_bull_v=[h4_c[i]>h4e50[i]>h4e200[i] for i in range(len(h4_c))]
h4_bear_v=[h4_c[i]<h4e50[i]<h4e200[i] for i in range(len(h4_c))]

def gtd(ts):
    for j in range(len(d1_t)-1,-1,-1):
        if d1_t[j]<=ts: return d1_bull_v[j],d1_bear_v[j]
    return False,False

def gth(ts):
    for j in range(len(h4_t)-1,-1,-1):
        if h4_t[j]<=ts: return h4_bull_v[j],h4_bear_v[j]
    return False,False

# ─── Simulator ────────────────────────────────────────────────────────────────
def sim(ents, c, h, l, N, atr, max_bars):
    trades=[]
    for (i,sig,sm,tm) in ents:
        sl_d=atr[i]*sm; tp_d=atr[i]*tm
        sl=c[i]-sl_d if sig==1 else c[i]+sl_d
        tp=c[i]+tp_d if sig==1 else c[i]-tp_d
        res=None
        for j in range(i+1,min(i+max_bars,N)):
            if sig==1:
                if l[j]<=sl: res=-sl_d; break
                if h[j]>=tp: res=+tp_d; break
            else:
                if h[j]>=sl: res=-sl_d; break
                if l[j]<=tp: res=+tp_d; break
        if res is None:
            pnl=c[min(i+max_bars,N-1)]-c[i] if sig==1 else c[i]-c[min(i+max_bars,N-1)]
            res=pnl
        trades.append(res)
    return trades

def report(trades, tf):
    if len(trades)<8:
        print(f"       {tf:<5}: {len(trades):>4} trades | -- too few --")
        return 0,0.0,0.0,0.0
    wins=[x for x in trades if x>0]; loss=[x for x in trades if x<=0]
    wr=len(wins)/len(trades)*100
    pf=sum(wins)/abs(sum(loss)) if loss and sum(loss)!=0 else 999
    net=sum(trades)
    mk=">>>" if wr>=55 else (">> " if wr>=50 else "   ")
    print(f"  {mk}  {tf:<5}: {len(trades):>4} trades | WR:{wr:>5.1f}% | PF:{pf:>4.2f} | Net:${net:>8.2f}")
    return len(trades),wr,pf,net

# ─── TF config ────────────────────────────────────────────────────────────────
# Bars to cover the same calendar window + ATR min matching original H1 test
TFS = [
    ('M5',  mt5.TIMEFRAME_M5,  int(hours_win*12)+500, 2.0,  300),
    ('M15', mt5.TIMEFRAME_M15, int(hours_win*4)+500,  2.5,  150),
    ('M30', mt5.TIMEFRAME_M30, int(hours_win*2)+500,  3.0,  80),
    ('H1',  mt5.TIMEFRAME_H1,  int(hours_win)+500,    3.0,  60),
    ('H4',  mt5.TIMEFRAME_H4,  int(hours_win//4)+200, 5.0,  20),
]
# (tf_name, mt5_tf, max_bars, atr_min, max_hold_bars)

# ─── Strategy logic ───────────────────────────────────────────────────────────

def strat_m2b(c,h,l,o,t_arr,atr,rsi,e20,e50,hrs,N,amin):
    ents=[]
    for i in range(20,N-1):
        if atr[i]<amin: continue
        hr=hrs[i]
        if not ((7<=hr<11)or(13<=hr<16)): continue
        rising =e20[i]>e20[i-8]>e20[i-16]
        falling=e20[i]<e20[i-8]<e20[i-16]
        tb =min(l[max(0,i-10):i])<=e20[max(0,i-5)]+atr[i]*0.6
        ts2=max(h[max(0,i-10):i])>=e20[max(0,i-5)]-atr[i]*0.6
        sig=0
        if rising  and c[i]>e20[i] and c[i]>c[i-1] and tb  and c[i]>o[i] and 45<rsi[i]<62 and e20[i]>e50[i]: sig=1
        if falling and c[i]<e20[i] and c[i]<c[i-1] and ts2 and c[i]<o[i] and 38<rsi[i]<55 and e20[i]<e50[i]: sig=-1
        if not sig: continue
        db,dbe=gtd(t_arr[i]); hb,hbe=gth(t_arr[i])
        if sig==1 and (not db or not hb): continue
        if sig==-1 and (not dbe or not hbe): continue
        ents.append((i,sig,1.5,3.0))
    return ents

def strat_pin(c,h,l,o,t_arr,atr,e21,hrs,N,amin):
    ph=[]; pl=[]
    ws=5
    for i in range(ws,N-ws):
        if all(h[i]>=h[j] for j in range(max(0,i-ws),min(N,i+ws+1)) if j!=i): ph.append((i,h[i]))
        if all(l[i]<=l[j] for j in range(max(0,i-ws),min(N,i+ws+1)) if j!=i): pl.append((i,l[i]))
    def near(i,st):
        if st==1:
            if abs(l[i]-e21[i])<atr[i]*0.7: return True
            for (pi,pv) in reversed(pl):
                if 5<i-pi<100: return abs(l[i]-pv)<atr[i]*0.7
        else:
            if abs(h[i]-e21[i])<atr[i]*0.7: return True
            for (pi,pv) in reversed(ph):
                if 5<i-pi<100: return abs(h[i]-pv)<atr[i]*0.7
        return False
    ents=[]
    for i in range(ws+5,N-1):
        if atr[i]<amin: continue
        hr=hrs[i]
        if not ((7<=hr<11)or(12<=hr<16)): continue
        db,dbe=gtd(t_arr[i]); hb,hbe=gth(t_arr[i])
        body=abs(c[i]-o[i]); dw=min(c[i],o[i])-l[i]; uw=h[i]-max(c[i],o[i])
        sig=0
        if dw>=body*2.2 and dw>uw*2.0 and body>atr[i]*0.04 and near(i,1) and db and hb: sig=1
        elif uw>=body*2.2 and uw>dw*2.0 and body>atr[i]*0.04 and near(i,-1) and dbe and hbe: sig=-1
        if sig: ents.append((i,sig,1.5,3.0))
    return ents

def strat_eng(c,h,l,o,t_arr,atr,e21,e50,hrs,N,amin):
    ents=[]
    for i in range(20,N-1):
        if atr[i]<amin: continue
        hr=hrs[i]
        if not ((7<=hr<11)or(12<=hr<16)): continue
        db,dbe=gtd(t_arr[i]); hb,hbe=gth(t_arr[i])
        body=abs(c[i]-o[i])
        be=c[i-1]<o[i-1] and c[i]>o[i] and c[i]>o[i-1] and o[i]<c[i-1] and body>atr[i]*0.3
        se=c[i-1]>o[i-1] and c[i]<o[i] and c[i]<o[i-1] and o[i]>c[i-1] and body>atr[i]*0.3
        at=abs(c[i]-e21[i])<atr[i]*0.7
        sig=0
        if be and at and c[i]>e50[i] and db and hb: sig=1
        elif se and at and c[i]<e50[i] and dbe and hbe: sig=-1
        if sig: ents.append((i,sig,1.5,3.0))
    return ents

def strat_pac(c,h,l,o,t_arr,atr,e21,e50,e200,hrs,N,amin):
    ents=[]
    for i in range(210,N-1):
        if atr[i]<amin: continue
        hr=hrs[i]
        if not ((7<=hr<11)or(12<=hr<16)): continue
        db,dbe=gtd(t_arr[i]); hb,hbe=gth(t_arr[i])
        body=abs(c[i]-o[i]); dw=min(c[i],o[i])-l[i]; uw=h[i]-max(c[i],o[i])
        bp=dw>=body*2.2 and dw>uw*1.5 and body>atr[i]*0.04
        sp=uw>=body*2.2 and uw>dw*1.5 and body>atr[i]*0.04
        be=c[i-1]<o[i-1] and c[i]>o[i] and c[i]>o[i-1] and o[i]<c[i-1] and body>atr[i]*0.25
        se=c[i-1]>o[i-1] and c[i]<o[i] and c[i]<o[i-1] and o[i]>c[i-1] and body>atr[i]*0.25
        ae=abs(c[i]-e21[i])<atr[i]*0.8
        tb=c[i]>e50[i] and e50[i]>e200[i]; ts=c[i]<e50[i] and e50[i]<e200[i]
        sig=0
        if (bp or be) and ae and tb and db and hb:   sig=1
        elif (sp or se) and ae and ts and dbe and hbe: sig=-1
        if sig: ents.append((i,sig,1.5,3.0))
    return ents

def strat_tc(c,h,l,o,t_arr,atr,rsi,e50,e200,stv,hrs,N,amin):
    ents=[]
    for i in range(210,N-1):
        if atr[i]<amin: continue
        hr=hrs[i]
        if not ((7<=hr<11)or(12<=hr<16)): continue
        db,dbe=gtd(t_arr[i]); hb,hbe=gth(t_arr[i])
        n50=abs(c[i]-e50[i])<atr[i]*0.8
        sig=0
        if c[i]>e200[i] and stv[i]==1 and n50 and c[i]>o[i] and 45<rsi[i]<65 and db and hb: sig=1
        elif c[i]<e200[i] and stv[i]==-1 and n50 and c[i]<o[i] and 35<rsi[i]<55 and dbe and hbe: sig=-1
        if sig: ents.append((i,sig,1.5,3.0))
    return ents

# ─── Main backtest loop ───────────────────────────────────────────────────────
STRATS = {
    "M2B/M2S":            strat_m2b,
    "Pin Bar SR":         strat_pin,
    "Engulfing Trend":    strat_eng,
    "PA Confluence":      strat_pac,
    "Trend Continuation": strat_tc,
}

best_tf_map = {}

print(f"\n=== MULTI-TF BACKTEST — XAUUSDc ===\n")

for sname, runner in STRATS.items():
    print(f"--- {sname} ---")
    best = ('H1', 0.0, 0, 0.0)

    for (tf_name, tf_id, max_b, atr_min, max_hold) in TFS:
        mt5.initialize(timeout=12000)
        mt5.symbol_select('XAUUSDc', True)
        rates = mt5.copy_rates_from('XAUUSDc', tf_id, start_dt, max_b)
        mt5.shutdown()

        if rates is None or len(rates) < 100:
            print(f"       {tf_name:<5}: no data"); continue

        oc=[float(r['open'])  for r in rates]; hc=[float(r['high'])  for r in rates]
        lc=[float(r['low'])   for r in rates]; cc=[float(r['close']) for r in rates]
        tc=[int(r['time'])    for r in rates]; Nf=len(cc)
        hrs=[datetime.fromtimestamp(ts,tz=timezone.utc).hour for ts in tc]

        atr_v=atr_s(hc,lc,cc,14); rsi_v=rsi_s(cc,14)
        e20v=ema(cc,20); e21v=ema(cc,21); e50v=ema(cc,50)
        e200v=ema(cc,min(200,Nf-1)); stv=supertrend(hc,lc,cc,10,3.0)

        if sname=="M2B/M2S":
            ents=runner(cc,hc,lc,oc,tc,atr_v,rsi_v,e20v,e50v,hrs,Nf,atr_min)
        elif sname=="Pin Bar SR":
            ents=runner(cc,hc,lc,oc,tc,atr_v,e21v,hrs,Nf,atr_min)
        elif sname=="Engulfing Trend":
            ents=runner(cc,hc,lc,oc,tc,atr_v,e21v,e50v,hrs,Nf,atr_min)
        elif sname=="PA Confluence":
            ents=runner(cc,hc,lc,oc,tc,atr_v,e21v,e50v,e200v,hrs,Nf,atr_min)
        elif sname=="Trend Continuation":
            ents=runner(cc,hc,lc,oc,tc,atr_v,rsi_v,e50v,e200v,stv,hrs,Nf,atr_min)

        trades=sim(ents,cc,hc,lc,Nf,atr_v,max_hold)
        n,wr,pf,net=report(trades,tf_name)

        if n>=15 and wr>best[1]:
            best=(tf_name,wr,n,pf)

    print(f"  => BEST TF: {best[0]} — WR:{best[1]:.1f}% ({best[2]} trades, PF:{best[3]:.2f})\n")
    best_tf_map[sname] = best[0] if best[1]>=50 else 'H1'

print("="*60)
print("BEST DEFAULT TIMEFRAME PER STRATEGY:")
for s,tf in best_tf_map.items():
    print(f"  '{s}': '{tf}'")
print("\n[DONE]")
