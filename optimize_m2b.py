"""M2B/M2S optimization — find best filter combination"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import MetaTrader5 as mt5
from datetime import datetime, timezone

mt5.initialize(timeout=8000)
mt5.symbol_select('XAUUSDc', True)
rates    = mt5.copy_rates_from_pos('XAUUSDc', mt5.TIMEFRAME_H1, 0, 5000)
rates_d1 = mt5.copy_rates_from_pos('XAUUSDc', mt5.TIMEFRAME_D1, 0, 500)
rates_h4 = mt5.copy_rates_from_pos('XAUUSDc', mt5.TIMEFRAME_H4, 0, 1500)
mt5.shutdown()

o=[float(r['open']) for r in rates]; h=[float(r['high']) for r in rates]
l=[float(r['low']) for r in rates];  c=[float(r['close']) for r in rates]
t=[int(r['time']) for r in rates]
d1_c=[float(r['close']) for r in rates_d1]; d1_t=[int(r['time']) for r in rates_d1]
h4_c=[float(r['close']) for r in rates_h4]; h4_t=[int(r['time']) for r in rates_h4]

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
        ag = (ag*(n-1)+g[i])/n; al = (al*(n-1)+lo2[i])/n
        r.append(100 if al==0 else 100-100/(1+ag/al))
    return r

N   = len(c)
e20 = ema(c,20); e21 = ema(c,21); e50 = ema(c,50); e200 = ema(c,200)
atr = atr_s(h,l,c,14); rsi = rsi_s(c,14)

d1_e50  = ema(d1_c, min(50,  len(d1_c)-1))
d1_e200 = ema(d1_c, min(200, len(d1_c)-1))
h4_e50  = ema(h4_c, min(50,  len(h4_c)-1))
h4_e200 = ema(h4_c, min(200, len(h4_c)-1))
d1_bull = [d1_c[i]>d1_e50[i]>d1_e200[i] for i in range(len(d1_c))]
d1_bear = [d1_c[i]<d1_e50[i]<d1_e200[i] for i in range(len(d1_c))]
h4_bull = [h4_c[i]>h4_e50[i]>h4_e200[i] for i in range(len(h4_c))]
h4_bear = [h4_c[i]<h4_e50[i]<h4_e200[i] for i in range(len(h4_c))]

def gtd(ts):
    for j in range(len(d1_t)-1, -1, -1):
        if d1_t[j] <= ts: return d1_bull[j], d1_bear[j]
    return False, False

def gth(ts):
    for j in range(len(h4_t)-1, -1, -1):
        if h4_t[j] <= ts: return h4_bull[j], h4_bear[j]
    return False, False

# Precompute hours for each bar
bar_hrs = [datetime.fromtimestamp(ts, tz=timezone.utc).hour for ts in t]

def backtest(label, extra_b=None, extra_s=None, sl_atr=1.5, tp_atr=3.0):
    trades = []
    for i in range(20, N-1):
        if atr[i] < 3.0: continue
        hr = bar_hrs[i]
        if not ((7<=hr<11) or (12<=hr<16)): continue
        if hr in (12, 20): continue

        rising  = e20[i] > e20[i-8] > e20[i-16]
        falling = e20[i] < e20[i-8] < e20[i-16]
        tb = min(l[max(0,i-10):i]) <= e20[i-5] + atr[i]*0.6
        ts2 = max(h[max(0,i-10):i]) >= e20[i-5] - atr[i]*0.6

        sig = 0
        if rising  and c[i]>e20[i] and c[i]>c[i-1] and tb  and c[i]>o[i] and 40<rsi[i]<65: sig=1
        if falling and c[i]<e20[i] and c[i]<c[i-1] and ts2 and c[i]<o[i] and 35<rsi[i]<60: sig=-1
        if sig == 0: continue

        db, dbe = gtd(t[i])
        hb, hbe = gth(t[i])
        if sig == 1:
            if not db or not hb: continue
            if extra_b and not extra_b(i): continue
        else:
            if not dbe or not hbe: continue
            if extra_s and not extra_s(i): continue

        entry = c[i]; sl_d = atr[i]*sl_atr; tp_d = atr[i]*tp_atr
        sl = entry-sl_d if sig==1 else entry+sl_d
        tp = entry+tp_d if sig==1 else entry-tp_d
        res = None
        for j in range(i+1, min(i+60, N)):
            if sig==1:
                if l[j]<=sl: res=('SL', -sl_d); break
                if h[j]>=tp: res=('TP', +tp_d); break
            else:
                if h[j]>=sl: res=('SL', -sl_d); break
                if l[j]<=tp: res=('TP', +tp_d); break
        if res is None:
            pnl = c[min(i+60,N-1)]-entry if sig==1 else entry-c[min(i+60,N-1)]
            res = ('MAX', pnl)
        trades.append(res[1])

    if not trades:
        print(f"{label:<42} -- no trades --")
        return
    wins  = [x for x in trades if x > 0]
    loss  = [x for x in trades if x <= 0]
    wr    = len(wins)/len(trades)*100
    pf    = sum(wins)/abs(sum(loss)) if loss else 999
    net   = sum(trades)
    grade = 'GOOD' if wr>=55 and pf>=1.4 else ('OK  ' if wr>=50 else 'BAD ')
    print(f"{label:<42} {len(trades):>5} | WR:{wr:>5.1f}% [{grade}] | PF:{pf:>4.2f} | Net:${net:>8.2f}")

print("\n=== M2B/M2S OPTIMIZATION — XAUUSD H1 ===")
print(f"{'Variant':<42} {'Trades':>6} | Win Rate        | Prof Fct | Net PnL")
print("-"*80)

# Base
backtest("1. Base (D1+H4+Session filter)")

# RSI filters
backtest("2. + RSI strict 45-60",
    extra_b=lambda i: 45<rsi[i]<60,
    extra_s=lambda i: 40<rsi[i]<55)

# EMA21 proximity
backtest("3. + Near EMA21 (within 0.5 ATR)",
    extra_b=lambda i: abs(c[i]-e21[i]) < atr[i]*0.5,
    extra_s=lambda i: abs(c[i]-e21[i]) < atr[i]*0.5)

# EMA stack (stronger trend)
backtest("4. + EMA20 > EMA50 (trend aligned)",
    extra_b=lambda i: e20[i] > e50[i],
    extra_s=lambda i: e20[i] < e50[i])

# London only
backtest("5. + London ONLY (7-10 UTC)",
    extra_b=lambda i: 7<=bar_hrs[i]<10,
    extra_s=lambda i: 7<=bar_hrs[i]<10)

# NY only
backtest("6. + NY ONLY (13-15 UTC)",
    extra_b=lambda i: 13<=bar_hrs[i]<15,
    extra_s=lambda i: 13<=bar_hrs[i]<15)

# Combine best
backtest("7. + RSI + EMA stack",
    extra_b=lambda i: 45<rsi[i]<62 and e20[i]>e50[i],
    extra_s=lambda i: 38<rsi[i]<55 and e20[i]<e50[i])

# Near EMA21 + EMA stack
backtest("8. + EMA21 + EMA stack",
    extra_b=lambda i: abs(c[i]-e21[i])<atr[i]*0.6 and e20[i]>e50[i],
    extra_s=lambda i: abs(c[i]-e21[i])<atr[i]*0.6 and e20[i]<e50[i])

# Different TP ratios
print()
backtest("9. SL=1.0x ATR, TP=2.0x ATR", sl_atr=1.0, tp_atr=2.0)
backtest("10. SL=1.5x ATR, TP=4.0x ATR", sl_atr=1.5, tp_atr=4.0)
backtest("11. SL=1.2x ATR, TP=2.5x ATR", sl_atr=1.2, tp_atr=2.5)
backtest("12. SL=1.0x ATR, TP=3.0x ATR", sl_atr=1.0, tp_atr=3.0)

# Best combo
print()
backtest("13. BEST COMBO: RSI+EMA stack+London",
    extra_b=lambda i: 45<rsi[i]<62 and e20[i]>e50[i] and 7<=bar_hrs[i]<11,
    extra_s=lambda i: 38<rsi[i]<55 and e20[i]<e50[i] and 7<=bar_hrs[i]<11)

backtest("14. BEST COMBO: RSI+EMA21+any session",
    extra_b=lambda i: 45<rsi[i]<62 and abs(c[i]-e21[i])<atr[i]*0.7 and e20[i]>e50[i],
    extra_s=lambda i: 38<rsi[i]<55 and abs(c[i]-e21[i])<atr[i]*0.7 and e20[i]<e50[i])

print("\n[DONE]")
