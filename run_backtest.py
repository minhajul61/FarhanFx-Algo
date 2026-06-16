"""
FarhanFX Strategy Backtester
MT5 থেকে real data নিয়ে সব strategy test করে WR বের করে
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import MetaTrader5 as mt5
from datetime import datetime, timedelta
import statistics

# ─── MT5 CONNECT ─────────────────────────────────────────────────────────────
if not mt5.initialize(timeout=8000):
    print("MT5 initialize failed")
    sys.exit(1)

SYMBOL   = "XAUUSDc"
TF       = mt5.TIMEFRAME_H1
BARS     = 5000   # ~208 days H1
SL_ATR   = 1.5
TP_ATR   = 3.0
MIN_ATR  = 3.0    # minimum ATR in USD (ignore low-vol bars)

# ─── FETCH DATA ───────────────────────────────────────────────────────────────
print(f"\nFetching {BARS} H1 bars for {SYMBOL}...")
mt5.symbol_select(SYMBOL, True)
rates = mt5.copy_rates_from_pos(SYMBOL, TF, 0, BARS)

# Also fetch D1 and H4 for trend
rates_d1 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_D1, 0, 500)
rates_h4 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H4, 0, 1500)
mt5.shutdown()

if rates is None or len(rates) < 200:
    print("Not enough data"); sys.exit(1)

print(f"Got {len(rates)} H1 bars  |  {len(rates_d1)} D1  |  {len(rates_h4)} H4")
print(f"From: {datetime.fromtimestamp(rates[0]['time'])}  To: {datetime.fromtimestamp(rates[-1]['time'])}")

# Convert to lists
o  = [float(r['open'])  for r in rates]
h  = [float(r['high'])  for r in rates]
l  = [float(r['low'])   for r in rates]
c  = [float(r['close']) for r in rates]
t  = [int(r['time'])    for r in rates]

d1_c = [float(r['close']) for r in rates_d1]
h4_c = [float(r['close']) for r in rates_h4]
h4_t = [int(r['time'])    for r in rates_h4]
d1_t = [int(r['time'])    for r in rates_d1]

# ─── INDICATOR FUNCTIONS ──────────────────────────────────────────────────────
def ema(prices, period):
    if len(prices) < period: return [prices[0]] * len(prices)
    k = 2 / (period + 1)
    e = [prices[0]]
    for p in prices[1:]:
        e.append(p * k + e[-1] * (1 - k))
    return e

def atr_series(highs, lows, closes, period=14):
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    atr = [sum(trs[:period]) / period]
    for i in range(period, len(trs)):
        atr.append((atr[-1] * (period-1) + trs[i]) / period)
    # pad front
    return [atr[0]] * (period - 1) + atr

def rsi_series(closes, period=14):
    gains, losses = [0], [0]
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    avg_g = sum(gains[1:period+1]) / period
    avg_l = sum(losses[1:period+1]) / period
    rsi = [50] * period
    for i in range(period, len(closes)):
        avg_g = (avg_g * (period-1) + gains[i]) / period
        avg_l = (avg_l * (period-1) + losses[i]) / period
        if avg_l == 0: rsi.append(100)
        else: rsi.append(100 - 100 / (1 + avg_g / avg_l))
    return [50] * (period - rsi.count(50) if rsi.count(50) < period else 0) + rsi

def supertrend(highs, lows, closes, period=10, mult=3.0):
    atr_v = atr_series(highs, lows, closes, period)
    n = len(closes)
    up = [0.0] * n; dn = [0.0] * n; trend = [1] * n
    for i in range(period, n):
        hl2 = (highs[i] + lows[i]) / 2
        up[i] = hl2 + mult * atr_v[i]
        dn[i] = hl2 - mult * atr_v[i]
        if i > period:
            if closes[i-1] > up[i-1]: up[i] = min(up[i], up[i-1])
            if closes[i-1] < dn[i-1]: dn[i] = max(dn[i], dn[i-1])
            if closes[i] > up[i-1]:   trend[i] = 1
            elif closes[i] < dn[i-1]: trend[i] = -1
            else:                      trend[i] = trend[i-1]
    return trend

# ─── PRE-COMPUTE INDICATORS ───────────────────────────────────────────────────
print("\nComputing indicators...")
N = len(c)
ema20_v  = ema(c, 20)
ema21_v  = ema(c, 21)
ema50_v  = ema(c, 50)
ema200_v = ema(c, 200)
atr_v    = atr_series(h, l, c, 14)
rsi_v    = rsi_series(c, 14)
st_dir   = supertrend(h, l, c, 10, 3.0)

# D1 trend for each H1 bar
d1_e50  = ema(d1_c, min(50, len(d1_c)-1))
d1_e200 = ema(d1_c, min(200, len(d1_c)-1))
d1_bull_arr = [d1_c[i] > d1_e50[i] > d1_e200[i] for i in range(len(d1_c))]
d1_bear_arr = [d1_c[i] < d1_e50[i] < d1_e200[i] for i in range(len(d1_c))]

def get_d1_trend(ts):
    # find the D1 bar for this H1 timestamp
    for j in range(len(d1_t)-1, -1, -1):
        if d1_t[j] <= ts:
            return d1_bull_arr[j], d1_bear_arr[j]
    return False, False

# H4 trend
h4_e50  = ema(h4_c, min(50, len(h4_c)-1))
h4_e200 = ema(h4_c, min(200, len(h4_c)-1))
h4_bull_arr = [h4_c[i] > h4_e50[i] > h4_e200[i] for i in range(len(h4_c))]
h4_bear_arr = [h4_c[i] < h4_e50[i] < h4_e200[i] for i in range(len(h4_c))]

def get_h4_trend(ts):
    for j in range(len(h4_t)-1, -1, -1):
        if h4_t[j] <= ts:
            return h4_bull_arr[j], h4_bear_arr[j]
    return False, False

# Pivot highs/lows
def pivot_levels(i, lb=5):
    ph = pl = None
    for j in range(lb, i-lb):
        if all(h[j] >= h[k] for k in range(j-lb, j+lb+1) if k != j): ph = h[j]
        if all(l[j] <= l[k] for k in range(j-lb, j+lb+1) if k != j): pl = l[j]
    return ph, pl

print("Pre-computing pivot levels...")
pivots = [(None, None)] * N
last_ph = last_pl = None
for i in range(10, N):
    if i >= 10:
        for j in range(max(0,i-5), i-4):
            if j >= 5 and all(h[j] >= h[k] for k in range(j-5, j+6) if k != j and k < N): last_ph = h[j]
            if j >= 5 and all(l[j] <= l[k] for k in range(j-5, j+6) if k != j and k < N): last_pl = l[j]
    pivots[i] = (last_ph, last_pl)

# ─── BACKTEST ENGINE ──────────────────────────────────────────────────────────
def run_backtest(name, signal_fn, req_d1_trend=True, req_h4_trend=True, session_filter=True):
    trades = []
    i = 50  # start after warmup

    while i < N - 1:
        if atr_v[i] < MIN_ATR:
            i += 1; continue

        # Session filter (UTC hour)
        from datetime import timezone
        bar_hour = datetime.fromtimestamp(t[i], tz=timezone.utc).hour
        if session_filter and not ((7 <= bar_hour < 11) or (12 <= bar_hour < 16)):
            i += 1; continue
        if bar_hour in (12, 20):
            i += 1; continue

        # Trend filter
        d1_b, d1_be = get_d1_trend(t[i])
        h4_b, h4_be = get_h4_trend(t[i])

        sig = signal_fn(i)
        if sig == 0:
            i += 1; continue

        # Trend check
        if sig == 1:  # BUY
            if req_d1_trend and not d1_b:  i += 1; continue
            if req_h4_trend and not h4_b:  i += 1; continue
        else:         # SELL
            if req_d1_trend and not d1_be: i += 1; continue
            if req_h4_trend and not h4_be: i += 1; continue

        # Entry
        entry = c[i]
        sl_d  = atr_v[i] * SL_ATR
        tp_d  = atr_v[i] * TP_ATR
        sl    = entry - sl_d if sig == 1 else entry + sl_d
        tp    = entry + tp_d if sig == 1 else entry - tp_d

        # Simulate next bars
        result = None
        for j in range(i+1, min(i+50, N)):
            if sig == 1:
                if l[j] <= sl: result = ("SL", -(sl_d));  break
                if h[j] >= tp: result = ("TP", +(tp_d));  break
            else:
                if h[j] >= sl: result = ("SL", -(sl_d)); break
                if l[j] <= tp: result = ("TP", +(tp_d)); break
        if result is None:
            result = ("MAX", c[min(i+50, N-1)] - entry if sig==1 else entry - c[min(i+50, N-1)])

        trades.append({
            "time": datetime.fromtimestamp(t[i]).strftime("%Y-%m-%d %H:%M"),
            "dir":  "BUY" if sig==1 else "SELL",
            "exit": result[0],
            "pnl":  result[1],
        })
        i += 2  # skip 1 bar after entry to avoid re-entry

    if not trades:
        return {"name": name, "trades": 0, "wr": 0, "pf": 0, "net": 0, "avg": 0}

    wins  = [r for r in trades if r["pnl"] > 0]
    loss  = [r for r in trades if r["pnl"] <= 0]
    net   = sum(r["pnl"] for r in trades)
    gross_w = sum(r["pnl"] for r in wins)
    gross_l = abs(sum(r["pnl"] for r in loss))
    pf    = gross_w / gross_l if gross_l > 0 else 999
    wr    = len(wins) / len(trades) * 100
    avg   = net / len(trades)
    return {"name": name, "trades": len(trades), "wr": round(wr,1),
            "pf": round(pf,2), "net": round(net,2), "avg": round(avg,2)}

# ─── STRATEGY SIGNAL FUNCTIONS ────────────────────────────────────────────────

def sig_m2b_m2s(i):
    if i < 20: return 0
    rising  = ema20_v[i] > ema20_v[i-8] > ema20_v[i-16]
    falling = ema20_v[i] < ema20_v[i-8] < ema20_v[i-16]
    touched_bull = min(l[max(0,i-10):i]) <= ema20_v[i-5] + atr_v[i] * 0.6
    touched_bear = max(h[max(0,i-10):i]) >= ema20_v[i-5] - atr_v[i] * 0.6
    if rising  and c[i] > ema20_v[i] and c[i] > c[i-1] and touched_bull and c[i] > o[i] and 40 < rsi_v[i] < 65:
        return 1
    if falling and c[i] < ema20_v[i] and c[i] < c[i-1] and touched_bear and c[i] < o[i] and 35 < rsi_v[i] < 60:
        return -1
    return 0

def sig_pin_bar(i):
    if i < 10: return 0
    body   = abs(c[i] - o[i])
    up_w   = h[i] - max(c[i], o[i])
    dn_w   = min(c[i], o[i]) - l[i]
    if body < atr_v[i] * 0.04: return 0
    ph, pl = pivots[i]
    at_sup = pl is not None and abs(l[i] - pl) < atr_v[i] * 0.4
    at_ema = abs(l[i] - ema21_v[i]) < atr_v[i] * 0.5
    at_res = ph is not None and abs(h[i] - ph) < atr_v[i] * 0.4
    at_ema_bear = abs(h[i] - ema21_v[i]) < atr_v[i] * 0.5
    bull_pin = dn_w > body * 2.2 and dn_w > up_w * 2.0
    bear_pin = up_w > body * 2.2 and up_w > dn_w * 2.0
    if bull_pin and (at_sup or at_ema): return 1
    if bear_pin and (at_res or at_ema_bear): return -1
    return 0

def sig_ib_fbo(i):
    if i < 3: return 0
    ib = h[i-1] < h[i-2] and l[i-1] > l[i-2]  # inside bar at i-1
    if not ib: return 0
    bull = l[i] < l[i-2] and c[i] > l[i-2] and c[i] > o[i]
    bear = h[i] > h[i-2] and c[i] < h[i-2] and c[i] < o[i]
    if bull: return 1
    if bear: return -1
    return 0

def sig_engulfing(i):
    if i < 5: return 0
    bull = c[i-1] < o[i-1] and c[i] > o[i] and c[i] > o[i-1] and o[i] < c[i-1]
    bear = c[i-1] > o[i-1] and c[i] < o[i] and c[i] < o[i-1] and o[i] > c[i-1]
    body = abs(c[i] - o[i])
    if body < atr_v[i] * 0.3: return 0
    ph, pl = pivots[i]
    at_sup = pl is not None and abs(l[i] - pl) < atr_v[i] * 0.5
    at_ema = abs(c[i] - ema21_v[i]) < atr_v[i] * 0.6
    at_res = ph is not None and abs(h[i] - ph) < atr_v[i] * 0.5
    if bull and c[i] > ema50_v[i] and (at_sup or at_ema): return 1
    if bear and c[i] < ema50_v[i] and (at_res or at_ema): return -1
    return 0

def sig_3bar_reversal(i):
    if i < 4: return 0
    no_ob = not (h[i-1] > h[i-2] and l[i-1] < l[i-2])
    body = abs(c[i] - o[i])
    if body < atr_v[i] * 0.2: return 0
    bull = c[i-2] < o[i-2] and l[i-1] < l[i-2] and no_ob and c[i] > h[i-2]
    bear = c[i-2] > o[i-2] and h[i-1] > h[i-2] and no_ob and c[i] < l[i-2]
    if bull: return 1
    if bear: return -1
    return 0

def sig_supertrend(i):
    if i < 15: return 0
    if st_dir[i-1] == -1 and st_dir[i] == 1:  # flipped bullish
        return 1 if c[i] > ema200_v[i] else 0
    if st_dir[i-1] == 1 and st_dir[i] == -1:  # flipped bearish
        return -1 if c[i] < ema200_v[i] else 0
    return 0

def sig_combined(i):
    # Use the best 3 strategies together — only trade if 2+ agree
    signals = [
        sig_m2b_m2s(i),
        sig_pin_bar(i),
        sig_ib_fbo(i),
        sig_engulfing(i),
    ]
    buys  = signals.count(1)
    sells = signals.count(-1)
    if buys >= 1 and sells == 0:  return 1
    if sells >= 1 and buys == 0:  return -1
    return 0

# ─── RUN ALL TESTS ────────────────────────────────────────────────────────────
print("\n" + "="*70)
print("BACKTESTING ALL STRATEGIES  (D1+H4 trend filter ON, Session filter ON)")
print("="*70)
print(f"{'Strategy':<25} {'Trades':>7} {'Win Rate':>9} {'Prof.Factor':>12} {'Net P&L':>10} {'Avg/Trade':>10}")
print("-"*70)

strategies = [
    ("M2B / M2S",          sig_m2b_m2s),
    ("Pin Bar at EMA/SR",  sig_pin_bar),
    ("IB False Breakout",  sig_ib_fbo),
    ("Engulfing at Level", sig_engulfing),
    ("3-Bar Reversal",     sig_3bar_reversal),
    ("Supertrend Flip",    sig_supertrend),
    ("Combined (best 3)",  sig_combined),
]

results = []
for name, fn in strategies:
    r = run_backtest(name, fn)
    results.append(r)
    wr_color = "GOOD" if r["wr"] >= 55 else "OK  " if r["wr"] >= 45 else "BAD "
    pf_color = "GOOD" if r["pf"] >= 1.5 else "OK  " if r["pf"] >= 1.2 else "BAD "
    print(f"{r['name']:<25} {r['trades']:>7} {r['wr']:>8.1f}% [{wr_color}]  {r['pf']:>8.2f} [{pf_color}]  ${r['net']:>9.2f}  ${r['avg']:>7.2f}")

# ─── ALSO TEST WITHOUT FILTERS ────────────────────────────────────────────────
print("\n" + "-"*70)
print("WITHOUT TREND FILTER (signal only, no D1/H4 requirement):")
print("-"*70)
for name, fn in strategies[:4]:
    r = run_backtest(name, fn, req_d1_trend=False, req_h4_trend=False)
    wr_c = "GOOD" if r["wr"] >= 55 else "OK  " if r["wr"] >= 45 else "BAD "
    print(f"{r['name']:<25} {r['trades']:>7} {r['wr']:>8.1f}% [{wr_c}]  PF:{r['pf']:>5.2f}  Net:${r['net']:>8.2f}")

# ─── BEST STRATEGY RECOMMENDATION ───────────────────────────────────────────
print("\n" + "="*70)
best = max(results, key=lambda x: x["wr"] * x["pf"] if x["trades"] > 5 else 0)
print(f"BEST: {best['name']}  WR:{best['wr']}%  PF:{best['pf']}  Net:${best['net']}")
if best["wr"] >= 55 and best["pf"] >= 1.4:
    print("[READY] WR and Profit Factor are good — safe to implement")
elif best["wr"] >= 45 and best["pf"] >= 1.2:
    print("[OK] Acceptable — can implement with caution, needs monitoring")
else:
    print("[FAIL] Not good enough — need different approach")
print("="*70)
