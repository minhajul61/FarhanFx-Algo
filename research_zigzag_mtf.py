import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import itertools
import server

SYMBOL = "BTC/USDT"
MIN_TRADES_FRAC = 20  # minimum trades scales with timeframe below

# Fetch genuinely ~1 year of data per timeframe, bypassing the 20000-bar
# safety cap on the production backtest endpoint (fine for a one-off
# research run, not fine for a synchronous HTTP request).
def fetch_1y(symbol, timeframe):
    import time as _t
    from datetime import datetime, timedelta
    ex = server._ccxt.binance()
    since = ex.parse8601((datetime.utcnow() - timedelta(days=370)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    rows = []
    while True:
        batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
        if not batch:
            break
        rows += batch
        if len(batch) < 1000:
            break
        since = batch[-1][0] + 1
        if len(rows) > 40000:
            break
    return rows

grid_depth     = [10, 15, 20, 30]
grid_deviation = [1.0, 2.0, 3.0, 5.0]
grid_backstep  = [3, 5, 8]

def run(data, depth, deviation, backstep, tf):
    params = {"slow_ema": depth * 4 + 50, "zz_depth": depth, "zz_deviation": deviation, "zz_backstep": backstep}
    return server._run_backtest_strategy("zigzag_reversal", data, SYMBOL, tf, param_overrides=params)

results_summary = []

for TF in ["15m", "1h", "4h"]:
    print(f"\n{'='*70}", flush=True)
    print(f"=== TIMEFRAME: {TF} ===", flush=True)
    ohlcv = fetch_1y(SYMBOL, TF)
    n = len(ohlcv)
    print(f"Fetched {n} bars", flush=True)
    print(f"Range: {server.datetime.fromtimestamp(ohlcv[0][0]/1000, tz=server.timezone.utc)} -> "
          f"{server.datetime.fromtimestamp(ohlcv[-1][0]/1000, tz=server.timezone.utc)}", flush=True)

    half = n // 2
    train, val = ohlcv[:half], ohlcv[half:]
    min_trades = MIN_TRADES_FRAC if TF != "4h" else 8
    print(f"Train: {len(train)} bars | Validation: {len(val)} bars | min_trades={min_trades}", flush=True)

    candidates = []
    for depth, deviation, backstep in itertools.product(grid_depth, grid_deviation, grid_backstep):
        r = run(train, depth, deviation, backstep, TF)
        if r["total_trades"] >= min_trades:
            candidates.append(((depth, deviation, backstep), r))
    candidates.sort(key=lambda x: x[1]["profit_factor"] or 0, reverse=True)
    print(f"{len(candidates)} combos cleared >= {min_trades} trades on train half", flush=True)
    for (depth, deviation, backstep), r in candidates[:6]:
        print(f"  train: depth={depth} dev={deviation}% backstep={backstep}  trades={r['total_trades']:4d} "
              f"wr={r['win_rate']}% pf={r['profit_factor']} net=${r['net_pnl']:.2f}", flush=True)

    best = None
    best_pf = 0
    for (depth, deviation, backstep), train_r in candidates[:20]:
        val_r = run(val, depth, deviation, backstep, TF)
        if val_r["total_trades"] >= max(3, min_trades // 2) and (val_r["profit_factor"] or 0) > best_pf:
            best_pf = val_r["profit_factor"] or 0
            best = ((depth, deviation, backstep), train_r, val_r)

    if best and best_pf > 1.0:
        (depth, deviation, backstep), train_r, val_r = best
        print(f">>> BEST VALIDATED ({TF}): depth={depth} deviation={deviation}% backstep={backstep}", flush=True)
        print(f"    train: trades={train_r['total_trades']} wr={train_r['win_rate']}% pf={train_r['profit_factor']} net=${train_r['net_pnl']:.2f}", flush=True)
        print(f"    val:   trades={val_r['total_trades']} wr={val_r['win_rate']}% pf={val_r['profit_factor']} net=${val_r['net_pnl']:.2f}", flush=True)
        results_summary.append((TF, depth, deviation, backstep, train_r, val_r))
    else:
        print(f">>> NO COMBO VALIDATED OUT-OF-SAMPLE ON {TF}", flush=True)
        results_summary.append((TF, None, None, None, None, None))

print(f"\n{'='*70}", flush=True)
print("=== FINAL SUMMARY (best validated per timeframe) ===", flush=True)
for TF, depth, deviation, backstep, train_r, val_r in results_summary:
    if depth is None:
        print(f"{TF}: no validated edge", flush=True)
    else:
        print(f"{TF}: depth={depth} dev={deviation}% backstep={backstep} | "
              f"train PF={train_r['profit_factor']} val PF={val_r['profit_factor']} "
              f"val net=${val_r['net_pnl']:.2f} val trades={val_r['total_trades']}", flush=True)
