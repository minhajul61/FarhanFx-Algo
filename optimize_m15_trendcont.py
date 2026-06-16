"""
Redesign Trend-Continuation-style entry rules specifically for M15 —
not reusing H1-tuned values. Tests EMA pairs, RSI zones, near-EMA
distance, ATR minimums, and SL/TP ratios tuned for M15's faster rhythm.
XAUUSDc, same ~10-month calendar window as all prior backtests today.
"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import MetaTrader5 as mt5
from datetime import datetime, timezone

mt5.initialize(timeout=15000)
mt5.symbol_select('XAUUSDc', True)

h1_anchor = mt5.copy_rates_from_pos('XAUUSDc', mt5.TIMEFRAME_H1, 0, 5000)
start_dt  = datetime.fromtimestamp(int(h1_anchor[0]['time']), tz=timezone.utc)
print(f"Window: {start_dt.strftime('%Y-%m-%d')} -> now")

rates    = mt5.copy_rates_from('XAUUSDc', mt5.TIMEFRAME_M15, start_dt, 40000)
d1_rates = mt5.copy_rates_from('XAUUSDc', mt5.TIMEFRAME_D1,  start_dt, 500)
h4_rates = mt5.copy_rates_from('XAUUSDc', mt5.TIMEFRAME_H4,  start_dt, 2000)
mt5.shutdown()

print(f"M15 bars: {len(rates)}  D1: {len(d1_rates)}  H4: {len(h4_rates)}\n")

o=[float(r['open'])  for r in rates]; h=[float(r['high'])  for r in rates]
l=[float(r['low'])   for r in rates]; c=[float(r['close']) for r in rates]
t=[int(r['time'])    for r in rates]; N=len(c)
hrs=[datetime.fromtimestamp(ts,tz=timezone.utc).hour for ts in t]

d1_c=[float(r['close']) for r in d1_rates]; d1_t=[int(r['time']) for r in d1_rates]
h4_c=[float(r['close']) for r in h4_rates]; h4_t=[int(r['time']) for r in h4_rates]

def ema(p,n):
    n=min(n,len(p)-1) or 1; k=2/(n+1); e=[p[0]]
    for x in p[1:]: e.append(x*k+e[-1]*(1-k))
    return e

def atr_s(hi,lo,cl,n=14):
    tr=[hi[0]-lo[0]]
    for i in range(1,len(cl)):
        tr.append(max(hi[i]-lo[i],abs(hi[i]-cl[i-1]),abs(lo[i]-cl[i-1])))
    n=min(n,len(tr)); a=[sum(tr[:n])/n]
    for i in range(n,len(tr)): a.append((a[-1]*(n-1)+tr[i])/n)
    return [a[0]]*(n-1)+a

def rsi_s(cl,n=14):
    g=[0.0]; lo2=[0.0]
    for i in range(1,len(cl)):
        d=cl[i]-cl[i-1]; g.append(max(d,0)); lo2.append(max(-d,0))
    if len(g)<=n: return [50.0]*len(cl)
    ag=sum(g[1:n+1])/n; al=sum(lo2[1:n+1])/n; r=[50.0]*n
    for i in range(n,len(cl)):
        ag=(ag*(n-1)+g[i])/n; al=(al*(n-1)+lo2[i])/n
        r.append(100.0 if al==0 else 100-100/(1+ag/al))
    return r

def supertrend(hi,lo,cl,n=10,m=3.0):
    atr=atr_s(hi,lo,cl,n); up=[0.0]*len(cl); dn=[0.0]*len(cl); tr=[1]*len(cl)
    for i in range(1,len(cl)):
        mid=(hi[i]+lo[i])/2; bu=mid-m*atr[i]; bd=mid+m*atr[i]
        up[i]=bu if bu>up[i-1] or cl[i-1]<up[i-1] else up[i-1]
        dn[i]=bd if bd<dn[i-1] or cl[i-1]>dn[i-1] else dn[i-1]
        if tr[i-1]==-1 and cl[i]>dn[i]: tr[i]=1
        elif tr[i-1]==1 and cl[i]<up[i]: tr[i]=-1
        else: tr[i]=tr[i-1]
    return tr

atr14 = atr_s(h,l,c,14)
rsi14 = rsi_s(c,14)
st10  = supertrend(h,l,c,10,3.0)

d1e50=ema(d1_c,50); d1e200=ema(d1_c,min(200,len(d1_c)-1))
h4e50=ema(h4_c,50); h4e200=ema(h4_c,min(200,len(h4_c)-1))
d1b=[d1_c[i]>d1e50[i]>d1e200[i] for i in range(len(d1_c))]
d1s=[d1_c[i]<d1e50[i]<d1e200[i] for i in range(len(d1_c))]
h4b=[h4_c[i]>h4e50[i]>h4e200[i] for i in range(len(h4_c))]
h4s=[h4_c[i]<h4e50[i]<h4e200[i] for i in range(len(h4_c))]

def gtd(ts):
    for j in range(len(d1_t)-1,-1,-1):
        if d1_t[j]<=ts: return d1b[j],d1s[j]
    return False,False
def gth(ts):
    for j in range(len(h4_t)-1,-1,-1):
        if h4_t[j]<=ts: return h4b[j],h4s[j]
    return False,False

def sim(ents, sl_m, tp_m, mx=200):
    trades=[]
    for (i,sig) in ents:
        sl_d=atr14[i]*sl_m; tp_d=atr14[i]*tp_m
        sl=c[i]-sl_d if sig==1 else c[i]+sl_d
        tp=c[i]+tp_d if sig==1 else c[i]-tp_d
        res=None
        for j in range(i+1,min(i+mx,N)):
            if sig==1:
                if l[j]<=sl: res=-sl_d; break
                if h[j]>=tp: res=+tp_d; break
            else:
                if h[j]>=sl: res=-sl_d; break
                if l[j]<=tp: res=+tp_d; break
        if res is None:
            pnl=c[min(i+mx,N-1)]-c[i] if sig==1 else c[i]-c[min(i+mx,N-1)]
            res=pnl
        trades.append(res)
    return trades

def show(label, trades):
    if len(trades)<10:
        print(f"    {label:<60} -- {len(trades)} trades (too few)")
        return None
    wins=[x for x in trades if x>0]; loss=[x for x in trades if x<=0]
    wr=len(wins)/len(trades)*100
    pf=sum(wins)/abs(sum(loss)) if loss and sum(loss)!=0 else 999
    net=sum(trades)
    mk=">>>" if wr>=55 else (">> " if wr>=50 else "   ")
    print(f"  {mk} {label:<60} {len(trades):>4} | WR:{wr:>5.1f}% | PF:{pf:>4.2f} | Net:${net:>8.2f}")
    return wr

print("=== M15 TREND CONTINUATION REDESIGN — XAUUSDc ===\n")

best = (None, 0, None)

# Sweep: EMA pairs, near-distance mult, RSI zone width, min ATR, SL/TP ratio
EMA_PAIRS   = [(20,50), (50,100), (50,150), (100,200)]
NEAR_MULTS  = [0.4, 0.6, 0.8, 1.2]
RSI_ZONES   = [("tight",48,58,42,52), ("med",45,62,38,55), ("wide",40,68,32,60)]
ATR_MINS    = [1.0, 1.5, 2.5]
SLTP_RATIOS = [(1.0,2.0), (1.5,3.0), (1.0,1.5), (1.5,2.5)]

for (fastN, slowN) in EMA_PAIRS:
    eF = ema(c, fastN); eS = ema(c, slowN)
    for near_mult in NEAR_MULTS:
        for (zname, bMin, bMax, sMax, sMin) in RSI_ZONES:
            for atrmin in ATR_MINS:
                ents = []
                start_i = max(slowN+5, 210)
                for i in range(start_i, N-1):
                    if atr14[i] < atrmin: continue
                    hr = hrs[i]
                    if not ((7<=hr<11)or(12<=hr<16)): continue
                    db,dbe = gtd(t[i]); hb,hbe = gth(t[i])
                    near = abs(c[i]-eF[i]) < atr14[i]*near_mult
                    sig = 0
                    if c[i]>eS[i] and st10[i]==1 and near and c[i]>o[i] and bMin<rsi14[i]<bMax and db and hb: sig=1
                    elif c[i]<eS[i] and st10[i]==-1 and near and c[i]<o[i] and sMin<rsi14[i]<sMax and dbe and hbe: sig=-1
                    if sig: ents.append((i,sig))
                if len(ents) < 15: continue
                for (sl_m, tp_m) in SLTP_RATIOS:
                    trades = sim(ents, sl_m, tp_m)
                    if len(trades) < 15: continue
                    wins=[x for x in trades if x>0]; loss=[x for x in trades if x<=0]
                    wr = len(wins)/len(trades)*100
                    pf = sum(wins)/abs(sum(loss)) if loss and sum(loss)!=0 else 999
                    if wr >= best[1] and len(trades) >= 15:
                        label = f"EMA{fastN}/{slowN} near={near_mult} RSI={zname} ATRmin={atrmin} SL{sl_m}/TP{tp_m}"
                        best = (label, wr, (len(trades), pf, sum(trades)))

print("Searching combinations (this prints only the running best)...\n")

# Re-run with verbose output only for promising configs (WR>=48 in the quick pass)
best2 = (None, 0, None)
for (fastN, slowN) in EMA_PAIRS:
    eF = ema(c, fastN); eS = ema(c, slowN)
    for near_mult in NEAR_MULTS:
        for (zname, bMin, bMax, sMax, sMin) in RSI_ZONES:
            for atrmin in ATR_MINS:
                ents = []
                start_i = max(slowN+5, 210)
                for i in range(start_i, N-1):
                    if atr14[i] < atrmin: continue
                    hr = hrs[i]
                    if not ((7<=hr<11)or(12<=hr<16)): continue
                    db,dbe = gtd(t[i]); hb,hbe = gth(t[i])
                    near = abs(c[i]-eF[i]) < atr14[i]*near_mult
                    sig = 0
                    if c[i]>eS[i] and st10[i]==1 and near and c[i]>o[i] and bMin<rsi14[i]<bMax and db and hb: sig=1
                    elif c[i]<eS[i] and st10[i]==-1 and near and c[i]<o[i] and sMin<rsi14[i]<sMax and dbe and hbe: sig=-1
                    if sig: ents.append((i,sig))
                if len(ents) < 15: continue
                for (sl_m, tp_m) in SLTP_RATIOS:
                    trades = sim(ents, sl_m, tp_m)
                    if len(trades) < 15: continue
                    wins=[x for x in trades if x>0]; loss=[x for x in trades if x<=0]
                    wr = len(wins)/len(trades)*100
                    pf = sum(wins)/abs(sum(loss)) if loss and sum(loss)!=0 else 999
                    net = sum(trades)
                    if wr >= 48:
                        label = f"EMA{fastN}/{slowN} near={near_mult} RSI={zname} ATRmin={atrmin} SL{sl_m}/TP{tp_m}"
                        mk = ">>>" if wr>=55 else (">> " if wr>=50 else "   ")
                        print(f"  {mk} {label:<60} {len(trades):>4} | WR:{wr:>5.1f}% | PF:{pf:>4.2f} | Net:${net:>8.2f}")
                        if wr > best2[1]:
                            best2 = (label, wr, (len(trades), pf, net))

print("\n" + "="*70)
if best2[0]:
    print(f"BEST CONFIG: {best2[0]}")
    print(f"  -> {best2[2][0]} trades | WR:{best2[1]:.1f}% | PF:{best2[2][1]:.2f} | Net:${best2[2][2]:.2f}")
else:
    print("No configuration reached 48%+ WR with >=15 trades on M15.")
print("[DONE]")
