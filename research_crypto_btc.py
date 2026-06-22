"""
Research best BTC/USDT algo strategy — same rigorous empirical method used
for the forex strategies (Trend Continuation etc.): fetch real historical
data, test several strategy archetypes, compare WR/PF/trades, pick the best.
Binance public data (no API key needed for OHLCV).
"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import ccxt
from datetime import datetime, timezone

ex = ccxt.binance()
SYMBOL = 'BTC/USDT'
TF = '1h'

def fetch_all(symbol, timeframe, since_iso, limit=1000):
    since = ex.parse8601(since_iso)
    all_rows = []
    while True:
        batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=limit)
        if not batch:
            break
        all_rows += batch
        last_ts = batch[-1][0]
        if last_ts == since or len(batch) < limit:
            if len(batch) < limit:
                break
            since = last_ts + 1
        else:
            since = last_ts + 1
        if len(all_rows) > 20000:
            break
    return all_rows

print(f"Fetching {SYMBOL} {TF} from Binance (2 years)...")
rows = fetch_all(SYMBOL, TF, '2023-06-01T00:00:00Z')
print(f"Got {len(rows)} bars: {datetime.fromtimestamp(rows[0][0]/1000,tz=timezone.utc)} -> {datetime.fromtimestamp(rows[-1][0]/1000,tz=timezone.utc)}")

t  = [r[0]//1000 for r in rows]
o  = [r[1] for r in rows]
h  = [r[2] for r in rows]
l  = [r[3] for r in rows]
c  = [r[4] for r in rows]
v  = [r[5] for r in rows]
N  = len(c)
hrs = [datetime.fromtimestamp(ts, tz=timezone.utc).hour for ts in t]

def ema(p, n):
    n = min(n, len(p)-1) or 1; k = 2/(n+1); e = [p[0]]
    for x in p[1:]: e.append(x*k + e[-1]*(1-k))
    return e

def atr_s(hi, lo, cl, n=14):
    tr = [hi[0]-lo[0]]
    for i in range(1, len(cl)):
        tr.append(max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1])))
    n = min(n, len(tr)); a = [sum(tr[:n])/n]
    for i in range(n, len(tr)): a.append((a[-1]*(n-1)+tr[i])/n)
    return [a[0]]*(n-1) + a

def rsi_s(cl, n=14):
    g = [0.0]; lo2 = [0.0]
    for i in range(1, len(cl)):
        d = cl[i]-cl[i-1]; g.append(max(d,0)); lo2.append(max(-d,0))
    if len(g) <= n: return [50.0]*len(cl)
    ag = sum(g[1:n+1])/n; al = sum(lo2[1:n+1])/n; r = [50.0]*n
    for i in range(n, len(cl)):
        ag = (ag*(n-1)+g[i])/n; al = (al*(n-1)+lo2[i])/n
        r.append(100.0 if al==0 else 100-100/(1+ag/al))
    return r

def supertrend(hi, lo, cl, n=10, m=3.0):
    atr = atr_s(hi, lo, cl, n); up=[0.0]*len(cl); dn=[0.0]*len(cl); tr=[1]*len(cl)
    for i in range(1, len(cl)):
        mid = (hi[i]+lo[i])/2; bu = mid-m*atr[i]; bd = mid+m*atr[i]
        up[i] = bu if bu>up[i-1] or cl[i-1]<up[i-1] else up[i-1]
        dn[i] = bd if bd<dn[i-1] or cl[i-1]>dn[i-1] else dn[i-1]
        if tr[i-1]==-1 and cl[i]>dn[i]: tr[i]=1
        elif tr[i-1]==1 and cl[i]<up[i]: tr[i]=-1
        else: tr[i]=tr[i-1]
    return tr

def macd(cl, fast=12, slow=26, sig=9):
    ef, es = ema(cl, fast), ema(cl, slow)
    line = [a-b for a,b in zip(ef,es)]
    sigl = ema(line, sig)
    return line, sigl

def bbands(cl, n=20, k=2.0):
    mid = []
    for i in range(len(cl)):
        win = cl[max(0,i-n+1):i+1]
        mid.append(sum(win)/len(win))
    std = []
    for i in range(len(cl)):
        win = cl[max(0,i-n+1):i+1]
        m = mid[i]
        std.append((sum((x-m)**2 for x in win)/len(win))**0.5)
    upper = [m+k*s for m,s in zip(mid,std)]
    lower = [m-k*s for m,s in zip(mid,std)]
    return mid, upper, lower

def donchian(hi, lo, n=20):
    dh, dl = [], []
    for i in range(len(hi)):
        win_h = hi[max(0,i-n):i]; win_l = lo[max(0,i-n):i]
        dh.append(max(win_h) if win_h else hi[i])
        dl.append(min(win_l) if win_l else lo[i])
    return dh, dl

atr14   = atr_s(h, l, c, 14)
rsi14   = rsi_s(c, 14)
ema50   = ema(c, 50)
ema100  = ema(c, 100)
ema200  = ema(c, min(200, N-1))
st10    = supertrend(h, l, c, 10, 3.0)
macd_l, macd_s = macd(c)
bb_mid, bb_up, bb_lo = bbands(c, 20, 2.0)
dc_h, dc_l = donchian(h, l, 20)

def sim(entries, sl_m, tp_m, mx=120):
    trades = []
    for (i, sig) in entries:
        sl_d = atr14[i]*sl_m; tp_d = atr14[i]*tp_m
        sl = c[i]-sl_d if sig==1 else c[i]+sl_d
        tp = c[i]+tp_d if sig==1 else c[i]-tp_d
        res = None
        for j in range(i+1, min(i+mx, N)):
            if sig==1:
                if l[j]<=sl: res=-sl_d; break
                if h[j]>=tp: res=+tp_d; break
            else:
                if h[j]>=sl: res=-sl_d; break
                if l[j]<=tp: res=+tp_d; break
        if res is None:
            j2 = min(i+mx, N-1)
            res = (c[j2]-c[i]) if sig==1 else (c[i]-c[j2])
        trades.append(res)
    return trades

def show(label, trades):
    if len(trades) < 10:
        print(f"  {label:<55} -- {len(trades)} trades (too few)")
        return
    wins = [x for x in trades if x>0]; loss = [x for x in trades if x<=0]
    wr = len(wins)/len(trades)*100
    pf = sum(wins)/abs(sum(loss)) if loss and sum(loss)!=0 else 999
    net = sum(trades)
    mk = ">>>" if wr>=55 else (">> " if wr>=50 else "   ")
    print(f"  {mk} {label:<55} {len(trades):>4} | WR:{wr:>5.1f}% | PF:{pf:>5.2f} | Net:${net:>9.2f}")

print("\n=== STRATEGY A: Trend Continuation (EMA200+Supertrend+pullback EMA50) ===")
for near_m in [0.6, 0.8, 1.2]:
    for rsi_lo, rsi_hi in [(40,68),(45,65),(35,60)]:
        ents = []
        for i in range(210, N-1):
            near = abs(c[i]-ema50[i]) < atr14[i]*near_m
            if c[i]>ema200[i] and st10[i]==1 and near and c[i]>o[i] and rsi_lo<rsi14[i]<rsi_hi:
                ents.append((i,1))
            elif c[i]<ema200[i] and st10[i]==-1 and near and c[i]<o[i] and 100-rsi_hi<rsi14[i]<100-rsi_lo:
                ents.append((i,-1))
        for sl_m, tp_m in [(1.5,3.0),(1.0,2.0),(2.0,3.0)]:
            trades = sim(ents, sl_m, tp_m)
            show(f"near={near_m} RSI={rsi_lo}-{rsi_hi} SL{sl_m}/TP{tp_m}", trades)

print("\n=== STRATEGY B: Donchian Breakout (20-period channel break + trend filter) ===")
for sl_m, tp_m in [(1.5,3.0),(2.0,4.0),(1.0,2.5)]:
    ents = []
    for i in range(210, N-1):
        if c[i] > dc_h[i] and c[i] > ema200[i]:
            ents.append((i,1))
        elif c[i] < dc_l[i] and c[i] < ema200[i]:
            ents.append((i,-1))
    trades = sim(ents, sl_m, tp_m)
    show(f"Donchian20 trend-filtered SL{sl_m}/TP{tp_m}", trades)

print("\n=== STRATEGY C: Bollinger Mean-Reversion (touch band + RSI extreme, counter-trend) ===")
for rsi_thresh in [25,30,35]:
    ents = []
    for i in range(210, N-1):
        if l[i] <= bb_lo[i] and rsi14[i] < rsi_thresh:
            ents.append((i,1))
        elif h[i] >= bb_up[i] and rsi14[i] > (100-rsi_thresh):
            ents.append((i,-1))
    for sl_m, tp_m in [(1.0,1.5),(1.5,2.0)]:
        trades = sim(ents, sl_m, tp_m, mx=48)
        show(f"BB-meanrev RSI<{rsi_thresh} SL{sl_m}/TP{tp_m}", trades)

print("\n=== STRATEGY D: MACD Momentum Crossover + EMA200 trend filter ===")
ents = []
for i in range(210, N-1):
    cross_up   = macd_l[i-1] < macd_s[i-1] and macd_l[i] > macd_s[i]
    cross_down = macd_l[i-1] > macd_s[i-1] and macd_l[i] < macd_s[i]
    if cross_up and c[i] > ema200[i]:
        ents.append((i,1))
    elif cross_down and c[i] < ema200[i]:
        ents.append((i,-1))
for sl_m, tp_m in [(1.5,3.0),(2.0,3.0),(1.0,2.0)]:
    trades = sim(ents, sl_m, tp_m)
    show(f"MACD-cross+EMA200 SL{sl_m}/TP{tp_m}", trades)

print("\n=== STRATEGY E: EMA50/EMA200 Golden/Death Cross (slow trend-follow) ===")
ents = []
for i in range(210, N-1):
    cross_up   = ema50[i-1] < ema200[i-1] and ema50[i] > ema200[i]
    cross_down = ema50[i-1] > ema200[i-1] and ema50[i] < ema200[i]
    if cross_up: ents.append((i,1))
    elif cross_down: ents.append((i,-1))
for sl_m, tp_m in [(2.0,4.0),(1.5,4.5),(3.0,6.0)]:
    trades = sim(ents, sl_m, tp_m, mx=200)
    show(f"EMA50/200 cross SL{sl_m}/TP{tp_m}", trades)

print("\n[DONE]")
