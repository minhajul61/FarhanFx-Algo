"""
Backtest: "Buy Sell V1" Pine Script logic — ZigZag-style reversal signal.
Re-implemented NON-REPAINTING (repaint=false equivalent): a pivot only
fires a signal once confirmed by a Deviation-sized reversal, same as the
real-time/closed-bar behavior. The repainting version is not backtestable
honestly since it shows signals that get deleted/moved after the fact.

Params from the script: Depth=30, Deviation=5, Backstep=5
Deviation is in "points" in the original (instrument-agnostic) — for
XAUUSD that's ambiguous, so this tests several $ values to be fair.
"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import MetaTrader5 as mt5
from datetime import datetime, timezone

mt5.initialize(timeout=15000)
mt5.symbol_select('XAUUSDc', True)
rates    = mt5.copy_rates_from_pos('XAUUSDc', mt5.TIMEFRAME_H1, 0, 5000)
rates_d1 = mt5.copy_rates_from_pos('XAUUSDc', mt5.TIMEFRAME_D1, 0, 500)
rates_h4 = mt5.copy_rates_from_pos('XAUUSDc', mt5.TIMEFRAME_H4, 0, 1500)
mt5.shutdown()

o=[float(r['open'])  for r in rates]; h=[float(r['high'])  for r in rates]
l=[float(r['low'])   for r in rates]; c=[float(r['close']) for r in rates]
t=[int(r['time'])    for r in rates]; N=len(c)
bar_hrs=[datetime.fromtimestamp(ts,tz=timezone.utc).hour for ts in t]

d1_c=[float(r['close']) for r in rates_d1]; d1_t=[int(r['time']) for r in rates_d1]
h4_c=[float(r['close']) for r in rates_h4]; h4_t=[int(r['time']) for r in rates_h4]

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

atr = atr_s(h,l,c,14)
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

# ─── Non-repainting ZigZag confirmation ───────────────────────────────────────
def zigzag_signals(highs, lows, depth, deviation, backstep):
    """
    Confirmed-only ZigZag pivots (repaint=false equivalent).
    Checks confirmation BEFORE updating the candidate so the reversal
    bar is always strictly after the candidate's own bar.
    Returns list of (confirm_idx, signal, pivot_idx, pivot_price)
    signal: 'BUY' (confirmed low) or 'SELL' (confirmed high)
    """
    n = len(highs)
    signals = []
    searching = 'low'
    cand_idx = depth
    cand_price = lows[depth]
    last_confirm_idx = -10**9

    for i in range(depth+1, n):
        if searching == 'low':
            if highs[i] >= cand_price + deviation and (cand_idx - last_confirm_idx) >= backstep:
                signals.append((i, 'BUY', cand_idx, cand_price))
                last_confirm_idx = cand_idx
                searching = 'high'
                cand_price = highs[i]; cand_idx = i
                continue
            if lows[i] < cand_price:
                cand_price = lows[i]; cand_idx = i
        else:
            if lows[i] <= cand_price - deviation and (cand_idx - last_confirm_idx) >= backstep:
                signals.append((i, 'SELL', cand_idx, cand_price))
                last_confirm_idx = cand_idx
                searching = 'low'
                cand_price = lows[i]; cand_idx = i
                continue
            if highs[i] > cand_price:
                cand_price = highs[i]; cand_idx = i
    return signals

def sim(entries, sl_m=1.5, tp_m=3.0, mx=80):
    trades=[]
    for (i,sig) in entries:
        sl_d=atr[i]*sl_m; tp_d=atr[i]*tp_m
        sl=c[i]-sl_d if sig=='BUY' else c[i]+sl_d
        tp=c[i]+tp_d if sig=='BUY' else c[i]-tp_d
        res=None
        for j in range(i+1,min(i+mx,N)):
            if sig=='BUY':
                if l[j]<=sl: res=-sl_d; break
                if h[j]>=tp: res=+tp_d; break
            else:
                if h[j]>=sl: res=-sl_d; break
                if l[j]<=tp: res=+tp_d; break
        if res is None:
            pnl=c[min(i+mx,N-1)]-c[i] if sig=='BUY' else c[i]-c[min(i+mx,N-1)]
            res=pnl
        trades.append(res)
    return trades

def show(label, trades):
    if len(trades)<5:
        print(f"  {label:<48} -- {len(trades)} trades (too few)")
        return
    wins=[x for x in trades if x>0]; loss=[x for x in trades if x<=0]
    wr=len(wins)/len(trades)*100
    pf=sum(wins)/abs(sum(loss)) if loss and sum(loss)!=0 else 999
    net=sum(trades)
    mk=">>>" if wr>=55 else (">> " if wr>=50 else "   ")
    print(f"  {mk} {label:<48} {len(trades):>4} | WR:{wr:>5.1f}% | PF:{pf:>4.2f} | Net:${net:>8.2f}")

print("=== ZigZag 'Buy Sell V1' Backtest (non-repainting) — XAUUSDc H1 ===\n")
print("Script params: Depth=30, Backstep=5 (fixed). Testing several Deviation ($) values\n")
print("Deviation is ambiguous across instruments in the original script, so testing a range:\n")

DEPTH = 30
BACKSTEP = 5

for deviation in [1.0, 2.0, 3.0, 5.0, 8.0, 12.0]:
    sigs = zigzag_signals(h, l, DEPTH, deviation, BACKSTEP)
    print(f"--- Deviation=${deviation} ---  ({len(sigs)} raw pivots confirmed)")

    # 1. Raw signal, no filter
    ents = [(i,sig) for (i,sig,pi,pp) in sigs if i < N-1]
    show(f"Raw (no filter)", sim(ents))

    # 2. + D1/H4 trend filter + session filter (same framework as other strategies)
    ents_f = []
    for (i,sig,pi,pp) in sigs:
        if i >= N-1: continue
        hr = bar_hrs[i]
        if not ((7<=hr<11)or(12<=hr<16)): continue
        db,dbs = gtd(t[i]); hb,hbs = gth(t[i])
        if sig=='BUY' and db and hb: ents_f.append((i,sig))
        elif sig=='SELL' and dbs and hbs: ents_f.append((i,sig))
    show(f"+ D1/H4 trend + session filter", sim(ents_f))
    print()

print("[DONE]")
