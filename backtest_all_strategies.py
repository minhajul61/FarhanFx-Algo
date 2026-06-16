"""
FarhanFX — Full Strategy Backtest
Tests every strategy in server.py against 5000 H1 bars of XAUUSDc.
Keeps strategies with WR >= 50%, flags those below for removal.
SL = 1.5×ATR, TP = 3.0×ATR (consistent for fair comparison)
D1+H4 trend filter + London/NY session filter applied where strategy logic uses them.
"""
import sys, math
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import MetaTrader5 as mt5
from datetime import datetime, timezone

# ─── Data fetch ───────────────────────────────────────────────────────────────
mt5.initialize(timeout=10000)
mt5.symbol_select('XAUUSDc', True)
rates    = mt5.copy_rates_from_pos('XAUUSDc', mt5.TIMEFRAME_H1,  0, 5000)
rates_d1 = mt5.copy_rates_from_pos('XAUUSDc', mt5.TIMEFRAME_D1,  0, 500)
rates_h4 = mt5.copy_rates_from_pos('XAUUSDc', mt5.TIMEFRAME_H4,  0, 1500)
rates_d1_50 = mt5.copy_rates_from_pos('XAUUSDc', mt5.TIMEFRAME_D1, 0, 300)
mt5.shutdown()

o  = [float(r['open'])  for r in rates]
h  = [float(r['high'])  for r in rates]
l  = [float(r['low'])   for r in rates]
c  = [float(r['close']) for r in rates]
t  = [int(r['time'])    for r in rates]
N  = len(c)

d1_c = [float(r['close']) for r in rates_d1]; d1_t = [int(r['time']) for r in rates_d1]
h4_c = [float(r['close']) for r in rates_h4]; h4_t = [int(r['time']) for r in rates_h4]
bar_hrs = [datetime.fromtimestamp(ts, tz=timezone.utc).hour for ts in t]

# ─── Indicators ───────────────────────────────────────────────────────────────
def ema(p, n):
    k = 2/(n+1); e = [p[0]]
    for x in p[1:]: e.append(x*k + e[-1]*(1-k))
    return e

def atr_s(hi, lo, cl, n=14):
    tr = [hi[0]-lo[0]]
    for i in range(1, len(cl)):
        tr.append(max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1])))
    a = [sum(tr[:n])/n]
    for i in range(n, len(tr)): a.append((a[-1]*(n-1)+tr[i])/n)
    return [a[0]]*(n-1) + a

def rsi_s(cl, n=14):
    g=[0]; lo2=[0]
    for i in range(1, len(cl)):
        d = cl[i]-cl[i-1]; g.append(max(d,0)); lo2.append(max(-d,0))
    ag = sum(g[1:n+1])/n; al = sum(lo2[1:n+1])/n
    r = [50]*n
    for i in range(n, len(cl)):
        ag=(ag*(n-1)+g[i])/n; al=(al*(n-1)+lo2[i])/n
        r.append(100 if al==0 else 100-100/(1+ag/al))
    return r

def macd_s(cl, fast=12, slow=26, sig=9):
    ef = ema(cl, fast); es = ema(cl, slow)
    ml = [ef[i]-es[i] for i in range(len(cl))]
    sl = ema(ml, sig)
    return ml, sl, [ml[i]-sl[i] for i in range(len(cl))]

def stoch_s(hi, lo, cl, k=14, d=3):
    ks = []
    for i in range(len(cl)):
        s = max(0, i-k+1)
        hh = max(hi[s:i+1]); ll = min(lo[s:i+1])
        ks.append(100*(cl[i]-ll)/(hh-ll) if hh!=ll else 50)
    ds = ema(ks, d)
    return ks, ds

def supertrend(hi, lo, cl, n=10, m=3.0):
    atr = atr_s(hi, lo, cl, n)
    up  = [0.0]*len(cl)
    dn  = [0.0]*len(cl)
    tr  = [1]*len(cl)
    for i in range(1, len(cl)):
        mid = (hi[i]+lo[i])/2
        bu  = mid - m*atr[i]; bd = mid + m*atr[i]
        up[i] = bu if bu > up[i-1] or cl[i-1] < up[i-1] else up[i-1]
        dn[i] = bd if bd < dn[i-1] or cl[i-1] > dn[i-1] else dn[i-1]
        if tr[i-1]==(-1) and cl[i]>dn[i]: tr[i]=1
        elif tr[i-1]==1  and cl[i]<up[i]: tr[i]=-1
        else: tr[i] = tr[i-1]
    return tr

def ichimoku(hi, lo, n1=9, n2=26, n3=52):
    def midp(s, e_): return (max(hi[s:e_])+min(lo[s:e_]))/2 if e_>s else 0
    ten=[]; kij=[]; spa=[]; spb=[]; chi=[]
    for i in range(len(hi)):
        ten.append(midp(max(0,i-n1+1),i+1))
        kij.append(midp(max(0,i-n2+1),i+1))
        spa.append((ten[i]+kij[i])/2)
        spb.append(midp(max(0,i-n3+1),i+1))
        chi.append(hi[i] if hi[i]>0 else 0)
    return ten, kij, spa, spb

# ─── Pre-compute indicators ───────────────────────────────────────────────────
e5   = ema(c,5);   e8 = ema(c,8)
e20  = ema(c,20);  e21= ema(c,21);  e50 = ema(c,50); e100= ema(c,100); e200= ema(c,200)
atr  = atr_s(h,l,c,14)
rsi  = rsi_s(c,14)
macd_l, macd_sig, macd_h = macd_s(c)
stk, stk_d = stoch_s(h,l,c)
st   = supertrend(h,l,c,10,3.0)
ten, kij, spa, spb = ichimoku(h,l)

# ─── D1/H4 trend ─────────────────────────────────────────────────────────────
d1e50  = ema(d1_c, 50); d1e200 = ema(d1_c, min(200,len(d1_c)-1))
h4e50  = ema(h4_c, 50); h4e200 = ema(h4_c, min(200,len(h4_c)-1))
d1_bull_v = [d1_c[i]>d1e50[i]>d1e200[i] for i in range(len(d1_c))]
d1_bear_v = [d1_c[i]<d1e50[i]<d1e200[i] for i in range(len(d1_c))]
h4_bull_v = [h4_c[i]>h4e50[i]>h4e200[i] for i in range(len(h4_c))]
h4_bear_v = [h4_c[i]<h4e50[i]<h4e200[i] for i in range(len(h4_c))]

def gtd(ts):
    for j in range(len(d1_t)-1,-1,-1):
        if d1_t[j]<=ts: return d1_bull_v[j], d1_bear_v[j]
    return False, False

def gth(ts):
    for j in range(len(h4_t)-1,-1,-1):
        if h4_t[j]<=ts: return h4_bull_v[j], h4_bear_v[j]
    return False, False

# ─── Trade simulator ──────────────────────────────────────────────────────────
def sim(entries):
    """entries = list of (i, sig, sl_mult, tp_mult)"""
    trades = []
    for (i, sig, sl_m, tp_m) in entries:
        sl_d = atr[i]*sl_m; tp_d = atr[i]*tp_m
        sl   = c[i]-sl_d if sig==1 else c[i]+sl_d
        tp   = c[i]+tp_d if sig==1 else c[i]-tp_d
        res  = None
        for j in range(i+1, min(i+60, N)):
            if sig==1:
                if l[j]<=sl: res=-sl_d; break
                if h[j]>=tp: res=+tp_d; break
            else:
                if h[j]>=sl: res=-sl_d; break
                if l[j]<=tp: res=+tp_d; break
        if res is None:
            pnl = c[min(i+60,N-1)]-c[i] if sig==1 else c[i]-c[min(i+60,N-1)]
            res = pnl
        trades.append(res)
    return trades

def stats(label, trades):
    if not trades:
        return label, 0, 0.0, 0.0, 0.0, False
    wins = [x for x in trades if x>0]
    loss = [x for x in trades if x<=0]
    wr   = len(wins)/len(trades)*100
    pf   = sum(wins)/abs(sum(loss)) if loss and sum(loss)!=0 else 999
    net  = sum(trades)
    ok   = wr >= 50
    gr   = 'KEEP' if wr>=50 else 'DROP'
    marker = '>>>' if wr>=55 else ('OK ' if wr>=50 else '   ')
    print(f"{marker} {label:<36} {len(trades):>4} trades | WR:{wr:>5.1f}% [{gr}] | PF:{pf:>4.2f} | Net:${net:>8.2f}")
    return label, len(trades), wr, pf, net, ok

print("\n=== FarhanFX FULL STRATEGY BACKTEST — XAUUSD H1 (5000 bars) ===")
print(f"{'':>4} {'Strategy':<36} {'Trades':>5} | Win Rate       | PF     | Net P&L")
print("-"*80)

results = []

# ─────────────────────────────────────────────────────────────────────────────
# 1. EMA TREND (EMA20/100 cross + RSI 40-60)
# ─────────────────────────────────────────────────────────────────────────────
entries = []
for i in range(101, N-1):
    if atr[i]<3: continue
    hr = bar_hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    db, dbe = gtd(t[i]); hb, hbe = gth(t[i])
    sig = 0
    if e20[i]>e100[i] and e20[i-1]<=e100[i-1] and 40<rsi[i]<65 and db and hb: sig=1
    elif e20[i]<e100[i] and e20[i-1]>=e100[i-1] and 35<rsi[i]<60 and dbe and hbe: sig=-1
    if sig: entries.append((i, sig, 1.5, 3.0))
results.append(stats("EMA Trend (cross + RSI + D1H4)", sim(entries)))

# ─────────────────────────────────────────────────────────────────────────────
# 2. EMA TREND V2 (EMA trending + RSI zone, no strict cross)
# ─────────────────────────────────────────────────────────────────────────────
entries = []
for i in range(101, N-1):
    if atr[i]<3: continue
    hr = bar_hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    db, dbe = gtd(t[i]); hb, hbe = gth(t[i])
    sig = 0
    rising  = e20[i]>e20[i-5]>e20[i-10] and e100[i]>e100[i-10]
    falling = e20[i]<e20[i-5]<e20[i-10] and e100[i]<e100[i-10]
    if rising  and c[i]>e20[i] and 45<rsi[i]<65 and db and hb:  sig=1
    if falling and c[i]<e20[i] and 35<rsi[i]<55 and dbe and hbe: sig=-1
    if sig: entries.append((i, sig, 1.5, 3.0))
results.append(stats("EMA Trend V2 (trending+zone)", sim(entries)))

# ─────────────────────────────────────────────────────────────────────────────
# 3. SCALPER (EMA5/8 fast cross + RSI momentum)
# ─────────────────────────────────────────────────────────────────────────────
entries = []
for i in range(10, N-1):
    if atr[i]<3: continue
    hr = bar_hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    sig = 0
    if e5[i]>e8[i] and e5[i-1]<=e8[i-1] and rsi[i]>55 and c[i]>e20[i]: sig=1
    elif e5[i]<e8[i] and e5[i-1]>=e8[i-1] and rsi[i]<45 and c[i]<e20[i]: sig=-1
    if sig: entries.append((i, sig, 1.0, 2.0))
results.append(stats("Scalper (EMA5/8 cross+RSI)", sim(entries)))

# ─────────────────────────────────────────────────────────────────────────────
# 4. SUPERTREND FLIP
# ─────────────────────────────────────────────────────────────────────────────
entries = []
for i in range(12, N-1):
    if atr[i]<3: continue
    hr = bar_hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    db, dbe = gtd(t[i]); hb, hbe = gth(t[i])
    sig = 0
    if st[i-1]==-1 and st[i]==1 and db and hb:  sig=1
    elif st[i-1]==1 and st[i]==-1 and dbe and hbe: sig=-1
    if sig: entries.append((i, sig, 1.5, 3.0))
results.append(stats("Supertrend Flip (D1+H4)", sim(entries)))

# ─────────────────────────────────────────────────────────────────────────────
# 5. TRIPLE FILTER (200EMA + Supertrend + RSI 45-55 + session)
# ─────────────────────────────────────────────────────────────────────────────
entries = []
for i in range(201, N-1):
    if atr[i]<3: continue
    hr = bar_hrs[i]
    if not ((8<=hr<11)or(13<=hr<16)): continue
    sig = 0
    if c[i]>e200[i] and st[i]==1 and 45<rsi[i]<62 and st[i-1]==1: sig=1
    elif c[i]<e200[i] and st[i]==-1 and 38<rsi[i]<55 and st[i-1]==-1: sig=-1
    if sig: entries.append((i, sig, 1.5, 3.0))
results.append(stats("Triple Filter (200EMA+ST+RSI)", sim(entries)))

# ─────────────────────────────────────────────────────────────────────────────
# 6. ICHIMOKU (price vs cloud + TK cross)
# ─────────────────────────────────────────────────────────────────────────────
entries = []
for i in range(55, N-1):
    if atr[i]<3: continue
    hr = bar_hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    cld_top = max(spa[i], spb[i]); cld_bot = min(spa[i], spb[i])
    sig = 0
    # Bullish: price above cloud + TK cross bullish
    if c[i]>cld_top and ten[i]>kij[i] and ten[i-1]<=kij[i-1]: sig=1
    elif c[i]<cld_bot and ten[i]<kij[i] and ten[i-1]>=kij[i-1]: sig=-1
    if sig: entries.append((i, sig, 1.5, 3.0))
results.append(stats("Ichimoku (TK cross + cloud)", sim(entries)))

# ─────────────────────────────────────────────────────────────────────────────
# 7. AI CONFLUENCE proxy (EMA+RSI+MACD+Stoch 4/4 agree)
# ─────────────────────────────────────────────────────────────────────────────
entries = []
for i in range(35, N-1):
    if atr[i]<3: continue
    hr = bar_hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    db, dbe = gtd(t[i]); hb, hbe = gth(t[i])
    ema_b = c[i]>e20[i] and e20[i]>e50[i]
    rsi_b = 50<rsi[i]<70
    mac_b = macd_l[i]>macd_sig[i]
    stk_b = stk[i]>50 and stk[i]>stk_d[i]
    ema_s = c[i]<e20[i] and e20[i]<e50[i]
    rsi_s2= 30<rsi[i]<50
    mac_s = macd_l[i]<macd_sig[i]
    stk_s = stk[i]<50 and stk[i]<stk_d[i]
    sig = 0
    if ema_b and rsi_b and mac_b and stk_b and db and hb:   sig=1
    elif ema_s and rsi_s2 and mac_s and stk_s and dbe and hbe: sig=-1
    if sig: entries.append((i, sig, 1.5, 3.0))
results.append(stats("AI Confluence (4/4: EMA+RSI+MACD+Stoch)", sim(entries)))

# ─────────────────────────────────────────────────────────────────────────────
# 8. PIN BAR SR (Pin bar at EMA21 or pivot level)
# ─────────────────────────────────────────────────────────────────────────────
entries = []
ph_levels = []; pl_levels = []
for i in range(5, N-5):
    if all(h[i]>=h[j] for j in range(i-5,i+6) if j!=i): ph_levels.append((i,h[i]))
    if all(l[i]<=l[j] for j in range(i-5,i+6) if j!=i): pl_levels.append((i,l[i]))

def near_sr(i, sig_type, tol=0.7):
    # check near EMA21
    if sig_type==1 and abs(l[i]-e21[i])<atr[i]*tol: return True
    if sig_type==-1 and abs(h[i]-e21[i])<atr[i]*tol: return True
    # check near recent pivot
    if sig_type==1:
        for (pi,pv) in reversed(pl_levels):
            if pi<i-5 and i-pi<60: return abs(l[i]-pv)<atr[i]*tol
    else:
        for (pi,pv) in reversed(ph_levels):
            if pi<i-5 and i-pi<60: return abs(h[i]-pv)<atr[i]*tol
    return False

for i in range(55, N-1):
    if atr[i]<3: continue
    hr = bar_hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    db, dbe = gtd(t[i]); hb, hbe = gth(t[i])
    body   = abs(c[i]-o[i]); rng = h[i]-l[i]
    dw     = min(c[i],o[i])-l[i]; uw = h[i]-max(c[i],o[i])
    sig = 0
    bull_pin = dw>=body*2.2 and dw>uw*2.0 and body>atr[i]*0.04
    bear_pin = uw>=body*2.2 and uw>dw*2.0 and body>atr[i]*0.04
    if bull_pin and near_sr(i,1) and db and hb:  sig=1
    elif bear_pin and near_sr(i,-1) and dbe and hbe: sig=-1
    if sig: entries.append((i, sig, 1.5, 3.0))
results.append(stats("Pin Bar SR (at EMA21/pivot)", sim(entries)))

# ─────────────────────────────────────────────────────────────────────────────
# 9. ENGULFING TREND (engulfing at EMA level + trend)
# ─────────────────────────────────────────────────────────────────────────────
entries = []
for i in range(55, N-1):
    if atr[i]<3: continue
    hr = bar_hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    db, dbe = gtd(t[i]); hb, hbe = gth(t[i])
    body   = abs(c[i]-o[i])
    bull_eng = c[i-1]<o[i-1] and c[i]>o[i] and c[i]>o[i-1] and o[i]<c[i-1] and body>atr[i]*0.3
    bear_eng = c[i-1]>o[i-1] and c[i]<o[i] and c[i]<o[i-1] and o[i]>c[i-1] and body>atr[i]*0.3
    at_lvl   = abs(c[i]-e21[i]) < atr[i]*0.7
    sig = 0
    if bull_eng and at_lvl and c[i]>e50[i] and db and hb:   sig=1
    elif bear_eng and at_lvl and c[i]<e50[i] and dbe and hbe: sig=-1
    if sig: entries.append((i, sig, 1.5, 3.0))
results.append(stats("Engulfing Trend (at EMA21+D1+H4)", sim(entries)))

# ─────────────────────────────────────────────────────────────────────────────
# 10. INSIDE BAR BREAKOUT
# ─────────────────────────────────────────────────────────────────────────────
entries = []
for i in range(55, N-1):
    if atr[i]<3: continue
    hr = bar_hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    db, dbe = gtd(t[i]); hb, hbe = gth(t[i])
    # inside bar formed at [i-1] inside [i-2]
    ib = h[i-1]<h[i-2] and l[i-1]>l[i-2]
    sig = 0
    if ib and c[i]>h[i-2] and c[i]>o[i] and db and hb:   sig=1
    elif ib and c[i]<l[i-2] and c[i]<o[i] and dbe and hbe: sig=-1
    if sig: entries.append((i, sig, 1.5, 3.0))
results.append(stats("Inside Bar Breakout (D1+H4)", sim(entries)))

# ─────────────────────────────────────────────────────────────────────────────
# 11. FALSE BREAKOUT / IB FBO (inside bar false breakout trap)
# ─────────────────────────────────────────────────────────────────────────────
entries = []
for i in range(55, N-1):
    if atr[i]<3: continue
    hr = bar_hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    ib = h[i-1]<h[i-2] and l[i-1]>l[i-2]
    sig = 0
    # Bullish FBO: broke below mother low, closed back above
    if ib and l[i]<l[i-2] and c[i]>l[i-2] and c[i]>o[i]: sig=1
    # Bearish FBO: broke above mother high, closed back below
    elif ib and h[i]>h[i-2] and c[i]<h[i-2] and c[i]<o[i]: sig=-1
    if sig: entries.append((i, sig, 1.5, 3.0))
results.append(stats("False Breakout IB FBO (no trend filter)", sim(entries)))

# IB FBO with trend filter
entries = []
for i in range(55, N-1):
    if atr[i]<3: continue
    hr = bar_hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    ib = h[i-1]<h[i-2] and l[i-1]>l[i-2]
    sig = 0
    if ib and l[i]<l[i-2] and c[i]>l[i-2] and c[i]>o[i] and c[i]>e50[i]: sig=1
    elif ib and h[i]>h[i-2] and c[i]<h[i-2] and c[i]<o[i] and c[i]<e50[i]: sig=-1
    if sig: entries.append((i, sig, 1.5, 3.0))
results.append(stats("False Breakout IB FBO + EMA50 trend", sim(entries)))

# ─────────────────────────────────────────────────────────────────────────────
# 12. 3-BAR REVERSAL
# ─────────────────────────────────────────────────────────────────────────────
entries = []
for i in range(55, N-1):
    if atr[i]<3: continue
    hr = bar_hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    db, dbe = gtd(t[i]); hb, hbe = gth(t[i])
    body = abs(c[i]-o[i])
    no_ob = not (h[i-1]>h[i-2] and l[i-1]<l[i-2])
    sig = 0
    if c[i-2]<o[i-2] and l[i-1]<l[i-2] and no_ob and c[i]>h[i-2] and body>atr[i]*0.2 and db and hb: sig=1
    elif c[i-2]>o[i-2] and h[i-1]>h[i-2] and no_ob and c[i]<l[i-2] and body>atr[i]*0.2 and dbe and hbe: sig=-1
    if sig: entries.append((i, sig, 1.5, 3.0))
results.append(stats("3-Bar Reversal (D1+H4)", sim(entries)))

# ─────────────────────────────────────────────────────────────────────────────
# 13. PA CONFLUENCE (trend + EMA level + pin/engulf pattern)
# ─────────────────────────────────────────────────────────────────────────────
entries = []
for i in range(55, N-1):
    if atr[i]<3: continue
    hr = bar_hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    db, dbe = gtd(t[i]); hb, hbe = gth(t[i])
    body = abs(c[i]-o[i]); dw = min(c[i],o[i])-l[i]; uw = h[i]-max(c[i],o[i])
    bull_pin = dw>=body*2.2 and dw>uw*1.5 and body>atr[i]*0.04
    bear_pin = uw>=body*2.2 and uw>dw*1.5 and body>atr[i]*0.04
    bull_eng = c[i-1]<o[i-1] and c[i]>o[i] and c[i]>o[i-1] and o[i]<c[i-1] and body>atr[i]*0.25
    bear_eng = c[i-1]>o[i-1] and c[i]<o[i] and c[i]<o[i-1] and o[i]>c[i-1] and body>atr[i]*0.25
    bull_pa = bull_pin or bull_eng
    bear_pa = bear_pin or bear_eng
    at_ema  = abs(c[i]-e21[i]) < atr[i]*0.8
    trend_b = c[i]>e50[i] and e50[i]>e200[i]
    trend_s = c[i]<e50[i] and e50[i]<e200[i]
    sig = 0
    if bull_pa and at_ema and trend_b and db and hb:   sig=1
    elif bear_pa and at_ema and trend_s and dbe and hbe: sig=-1
    if sig: entries.append((i, sig, 1.5, 3.0))
results.append(stats("PA Confluence (Trend+Level+Signal)", sim(entries)))

# ─────────────────────────────────────────────────────────────────────────────
# 14. M2B / M2S (already optimized — Variant 7: RSI + EMA stack)
# ─────────────────────────────────────────────────────────────────────────────
entries = []
for i in range(20, N-1):
    if atr[i]<3: continue
    hr = bar_hrs[i]
    if not ((7<=hr<11)or(13<=hr<16)): continue
    if hr in (12, 20): continue
    rising  = e20[i] > e20[i-8] > e20[i-16]
    falling = e20[i] < e20[i-8] < e20[i-16]
    tb = min(l[max(0,i-10):i]) <= e20[i-5] + atr[i]*0.6
    ts2 = max(h[max(0,i-10):i]) >= e20[i-5] - atr[i]*0.6
    sig = 0
    # Optimized: RSI 45-62 + EMA20>EMA50
    if rising and c[i]>e20[i] and c[i]>c[i-1] and tb and c[i]>o[i] and 45<rsi[i]<62 and e20[i]>e50[i]: sig=1
    if falling and c[i]<e20[i] and c[i]<c[i-1] and ts2 and c[i]<o[i] and 38<rsi[i]<55 and e20[i]<e50[i]: sig=-1
    if sig==0: continue
    db, dbe = gtd(t[i]); hb, hbe = gth(t[i])
    if sig==1 and (not db or not hb): continue
    if sig==-1 and (not dbe or not hbe): continue
    entries.append((i, sig, 1.5, 3.0))
results.append(stats("M2B/M2S OPTIMIZED (RSI+EMA stack)", sim(entries)))

# ─────────────────────────────────────────────────────────────────────────────
# 15. ORDER BLOCK (simple: last bearish/bullish OB before impulse move)
# ─────────────────────────────────────────────────────────────────────────────
entries = []
for i in range(10, N-1):
    if atr[i]<3: continue
    hr = bar_hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    db, dbe = gtd(t[i]); hb, hbe = gth(t[i])
    # Bullish OB: last bearish candle before a strong bullish impulse (2+ consecutive bull bars)
    # Price retraces back to that OB zone
    sig = 0
    for k in range(2, 8):
        # last bearish candle at i-k, followed by strong bullish move
        if c[i-k]<o[i-k]:  # bearish OB candle
            ob_h = o[i-k]; ob_l = c[i-k]
            impulse = all(c[i-j]>o[i-j] for j in range(1,k)) and (c[i-1]-o[i-k])>atr[i]*1.5
            if impulse and ob_l<=c[i]<=ob_h+atr[i]*0.3 and db and hb:
                sig=1; break
    if not sig:
        for k in range(2, 8):
            if c[i-k]>o[i-k]:  # bearish impulse OB candle
                ob_h = c[i-k]; ob_l = o[i-k]
                impulse = all(c[i-j]<o[i-j] for j in range(1,k)) and (o[i-k]-c[i-1])>atr[i]*1.5
                if impulse and ob_l-atr[i]*0.3<=c[i]<=ob_h and dbe and hbe:
                    sig=-1; break
    if sig: entries.append((i, sig, 1.5, 3.0))
results.append(stats("Order Block (OB retest D1+H4)", sim(entries)))

# ─────────────────────────────────────────────────────────────────────────────
# 16. DOUBLE TOP / BOTTOM (reversal)
# ─────────────────────────────────────────────────────────────────────────────
entries = []
for i in range(30, N-1):
    if atr[i]<3: continue
    hr = bar_hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    sig = 0
    # Double top: 2 highs within 0.5 ATR, second slightly lower
    w = 30
    recent_h = h[i-w:i]
    # find local max
    peaks = [j for j in range(2, w-2) if recent_h[j]>=recent_h[j-1] and recent_h[j]>=recent_h[j+1] and recent_h[j]>=recent_h[j-2] and recent_h[j]>=recent_h[j+2]]
    if len(peaks)>=2:
        p1 = peaks[-2]; p2 = peaks[-1]
        h1 = recent_h[p1]; h2 = recent_h[p2]
        if abs(h1-h2)<atr[i]*0.5 and h2<=h1+atr[i]*0.1 and c[i]<c[i-1]<c[i-2]: sig=-1
    recent_l = l[i-w:i]
    troughs = [j for j in range(2,w-2) if recent_l[j]<=recent_l[j-1] and recent_l[j]<=recent_l[j+1] and recent_l[j]<=recent_l[j-2] and recent_l[j]<=recent_l[j+2]]
    if len(troughs)>=2:
        t1 = troughs[-2]; t2 = troughs[-1]
        l1 = recent_l[t1]; l2 = recent_l[t2]
        if abs(l1-l2)<atr[i]*0.5 and l2>=l1-atr[i]*0.1 and c[i]>c[i-1]>c[i-2]: sig=1
    if sig: entries.append((i, sig, 1.5, 3.0))
results.append(stats("Double Top/Bottom (reversal)", sim(entries)))

# ─────────────────────────────────────────────────────────────────────────────
# 17. TREND CONTINUATION (EMA200 + ST + price pullback to EMA50)
# ─────────────────────────────────────────────────────────────────────────────
entries = []
for i in range(201, N-1):
    if atr[i]<3: continue
    hr = bar_hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    db, dbe = gtd(t[i]); hb, hbe = gth(t[i])
    near50 = abs(c[i]-e50[i]) < atr[i]*0.8
    sig = 0
    if c[i]>e200[i] and st[i]==1 and near50 and c[i]>o[i] and 45<rsi[i]<65 and db and hb: sig=1
    elif c[i]<e200[i] and st[i]==-1 and near50 and c[i]<o[i] and 35<rsi[i]<55 and dbe and hbe: sig=-1
    if sig: entries.append((i, sig, 1.5, 3.0))
results.append(stats("Trend Continuation (EMA200+ST+EMA50)", sim(entries)))

# ─────────────────────────────────────────────────────────────────────────────
# 18. BOS (Break of Structure) simple proxy
# ─────────────────────────────────────────────────────────────────────────────
entries = []
for i in range(20, N-1):
    if atr[i]<3: continue
    hr = bar_hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    db, dbe = gtd(t[i]); hb, hbe = gth(t[i])
    # BOS: price breaks above last swing high (from 10-20 bars back) with strong candle
    sig = 0
    sw_h = max(h[i-20:i-3])
    sw_l = min(l[i-20:i-3])
    body = abs(c[i]-o[i])
    if c[i]>sw_h and c[i]>o[i] and body>atr[i]*0.4 and e20[i]>e50[i] and db and hb: sig=1
    elif c[i]<sw_l and c[i]<o[i] and body>atr[i]*0.4 and e20[i]<e50[i] and dbe and hbe: sig=-1
    if sig: entries.append((i, sig, 1.5, 3.0))
results.append(stats("BOS Break of Structure", sim(entries)))

# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY TABLE
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "="*80)
print("DECISION TABLE:")
print(f"  {'Strategy':<40} {'WR':>6}  {'Decision'}")
print("-"*60)
keep = []; drop = []
for (label, ntrades, wr, pf, net, ok) in results:
    if ntrades == 0:
        print(f"  {label:<40} {'N/A':>6}  DROP (no trades)")
        drop.append(label)
        continue
    decision = "KEEP" if ok else "DROP"
    flag = "*** HIGH WR ***" if wr>=55 else ""
    print(f"  {label:<40} {wr:>5.1f}%  {decision} {flag}")
    if ok: keep.append((label, wr, pf))
    else: drop.append(label)

print(f"\nKEEP ({len(keep)}): " + ", ".join([f"{x[0]} ({x[1]:.1f}%)" for x in sorted(keep, key=lambda x:-x[1])]))
print(f"DROP ({len(drop)}): " + ", ".join(drop))
print("\n[DONE]")
