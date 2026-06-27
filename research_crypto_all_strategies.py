"""
Research pass over every crypto algo-bot strategy: grid-search the ones with
tunable parameters (out-of-sample validated on a train/validation split of
~2.3 years of real Binance 1h data), and give the fixed-logic price-action
strategies a longer, fairer run on the full dataset. Used to decide which
strategies get better default parameters and which get removed because they
show no real edge even after tuning.
"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import itertools
import server

SYMBOL, TF = "BTC/USDT", "1h"
ohlcv = server._fetch_backtest_ohlcv(SYMBOL, TF, 900)
n = len(ohlcv)
print(f"Fetched {n} bars of {SYMBOL} {TF}")
print(f"Range: {server.datetime.utcfromtimestamp(ohlcv[0][0]/1000)} -> {server.datetime.utcfromtimestamp(ohlcv[-1][0]/1000)}")

half = n // 2
train, val = ohlcv[:half], ohlcv[half:]
print(f"Train: {len(train)} bars | Validation: {len(val)} bars\n")

MIN_TRADES = 15

def run(strategy, data, params=None):
    return server._run_backtest_strategy(strategy, data, SYMBOL, TF, param_overrides=params)


def grid_search(strategy, grid_keys, grid_values):
    print(f"=== {strategy} — grid search ({len(list(itertools.product(*grid_values)))} combos) ===")
    candidates = []
    for combo in itertools.product(*grid_values):
        params = dict(zip(grid_keys, combo))
        r = run(strategy, train, params)
        if r["total_trades"] >= MIN_TRADES:
            candidates.append((params, r))
    candidates.sort(key=lambda x: x[1]["profit_factor"] or 0, reverse=True)
    print(f"  {len(candidates)} combos cleared >= {MIN_TRADES} trades on train half")
    for params, r in candidates[:5]:
        print(f"    train: {params}  trades={r['total_trades']:4d} wr={r['win_rate']}% pf={r['profit_factor']} net=${r['net_pnl']:.2f}")

    best = None
    for params, train_r in candidates[:8]:
        val_r = run(strategy, val, params)
        if val_r["total_trades"] >= MIN_TRADES // 2 and (val_r["profit_factor"] or 0) > 1.0:
            best = (params, train_r, val_r)
            break
    if best:
        params, train_r, val_r = best
        print(f"  >>> VALIDATED: {params}")
        print(f"      train: trades={train_r['total_trades']} wr={train_r['win_rate']}% pf={train_r['profit_factor']} net=${train_r['net_pnl']:.2f}")
        print(f"      val:   trades={val_r['total_trades']} wr={val_r['win_rate']}% pf={val_r['profit_factor']} net=${val_r['net_pnl']:.2f}")
    else:
        print("  >>> NO COMBO VALIDATED OUT-OF-SAMPLE")
    print()
    return best


results = {}

results["ema_cross"] = grid_search(
    "ema_cross", ["fast_ema", "slow_ema"],
    [[5, 8, 9, 12, 15, 20], [21, 26, 34, 50, 100]],
)

results["rsi"] = grid_search(
    "rsi", ["rsi_period", "rsi_ob", "rsi_os"],
    [[7, 10, 14, 21], [65, 70, 75, 80], [20, 25, 30, 35]],
)

results["macd_cross"] = grid_search(
    "macd_cross", ["macd_fast", "macd_slow", "macd_signal"],
    [[5, 8, 12], [21, 26, 35], [5, 9]],
)

results["bb_squeeze"] = grid_search(
    "bb_squeeze", ["bb_period", "bb_std"],
    [[14, 20, 30], [1.5, 2.0, 2.5]],
)

results["supertrend"] = grid_search(
    "supertrend", ["atr_period", "st_multiplier"],
    [[7, 10, 14, 20], [1.5, 2.0, 2.5, 3.0]],
)

results["ai_score"] = grid_search(
    "ai_score", ["ai_min_score"],
    [[50, 55, 60, 65, 70, 75]],
)

results["breakout"] = grid_search(
    "breakout", ["bo_lookback"],
    [[10, 15, 20, 30, 40, 55]],
)

print("=" * 70)
print("FIXED-LOGIC STRATEGIES — full-period run, no tunable params exposed")
print("=" * 70)
for strategy in ["scalp", "btc_momentum_breakout", "pin_bar_sr", "engulfing_trend",
                 "false_breakout", "pa_confluence"]:
    r = run(strategy, ohlcv)
    print(f"{strategy:25s} trades={r['total_trades']:4d} wr={r['win_rate']}% pf={r['profit_factor']} "
          f"net=${r['net_pnl']:.2f} ({r['net_pnl_pct']}%) maxDD={r['max_drawdown_pct']}%")
