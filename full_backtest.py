"""
Full backtest: every crypto strategy x the most liquid Binance USDT-M perps x
15m / 1h / 4h, over the last year, with the live engine's rules (stop loss,
take profit, stop-based sizing, fees, slippage, closed candles).

Each run is split in two: the first two thirds ("train") and the last third
("test"). A strategy/timeframe only counts as approved when it makes money in
BOTH halves on enough coins — one lucky stretch is not an edge.

Usage:
    python full_backtest.py                 # full run, writes backtest_report.json
    python full_backtest.py --days 180      # shorter history
    python full_backtest.py --cache DIR     # reuse downloaded candles
Takes 5-20 minutes depending on CPU.
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "SUI"]
TIMEFRAMES = ["15m", "1h", "4h"]
SKIP = {"funding_rate"}       # needs live funding-rate data, cannot be replayed

# Approval rules for a strategy+timeframe
MIN_TRADES_PER_COIN = 8       # per half, for a coin to count
MIN_COINS_PROFITABLE = 0.6    # share of tested coins that must be profitable in both halves
MIN_PF = 1.15                 # pooled profit factor in each half


def _server():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import server  # noqa: E402  (import has no bot side effects)
    return server


def fetch_all(days, cache_dir):
    srv = _server()
    os.makedirs(cache_dir, exist_ok=True)
    data = {}
    for coin in COINS:
        sym = f"{coin}/USDT:USDT"
        for tf in TIMEFRAMES:
            path = os.path.join(cache_dir, f"{coin}_{tf}_{days}.json")
            if os.path.exists(path) and time.time() - os.path.getmtime(path) < 6 * 3600:
                continue
            for attempt in range(3):
                try:
                    rows = srv._fetch_backtest_ohlcv(sym, tf, days)
                    with open(path, "w") as f:
                        json.dump(rows, f)
                    print(f"  fetched {sym} {tf}: {len(rows)} bars", flush=True)
                    break
                except Exception as e:
                    print(f"  fetch {sym} {tf} failed ({e}), retry", flush=True)
                    time.sleep(3)
            data[(coin, tf)] = path
    return data


def run_one(args):
    strategy, coin, tf, path = args
    srv = _server()
    with open(path) as f:
        rows = json.load(f)
    sym = f"{coin}/USDT:USDT"
    cut = int(len(rows) * 2 / 3)
    window = srv._bot_lookback_bars(srv._strategy_bot_template(strategy, sym, tf))
    out = {"strategy": strategy, "coin": coin, "tf": tf}
    # test half gets the warm-up bars from the end of train so it starts trading at once
    for name, part in (("train", rows[:cut]), ("test", rows[max(0, cut - window):])):
        r = srv._run_backtest_strategy(strategy, part, sym, tf)
        out[name] = {k: r[k] for k in ("total_trades", "win_rate", "profit_factor", "net_pnl",
                                         "net_pnl_pct", "max_drawdown_pct", "avg_r", "signal_errors")}
    return out


def aggregate(results):
    by = {}
    for r in results:
        by.setdefault((r["strategy"], r["tf"]), []).append(r)
    table = []
    for (strategy, tf), rows in by.items():
        row = {"strategy": strategy, "tf": tf, "coins": len(rows)}
        good = 0
        for half in ("train", "test"):
            pnl = sum(x[half]["net_pnl"] for x in rows)
            trades = sum(x[half]["total_trades"] for x in rows)
            # pooled PF from per-coin net pnl signs is crude; use avg R instead
            rs = [x[half]["avg_r"] for x in rows if x[half]["avg_r"] is not None]
            pfs = [x[half]["profit_factor"] for x in rows if x[half]["profit_factor"] is not None]
            row[half] = {
                "net_pnl_pct_avg": round(pnl / len(rows) / 10, 2),   # capital 1000 → %
                "trades": trades,
                "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
                "median_pf": round(sorted(pfs)[len(pfs) // 2], 2) if pfs else None,
                "max_dd_worst": max(x[half]["max_drawdown_pct"] for x in rows),
                "errors": sum(x[half]["signal_errors"] for x in rows),
            }
        for x in rows:
            if (x["train"]["total_trades"] >= MIN_TRADES_PER_COIN and x["test"]["total_trades"] >= MIN_TRADES_PER_COIN // 2
                    and x["train"]["net_pnl"] > 0 and x["test"]["net_pnl"] > 0):
                good += 1
        row["coins_profitable_both"] = good
        row["profitable_coins"] = sorted(x["coin"] for x in rows
                                         if x["train"]["net_pnl"] > 0 and x["test"]["net_pnl"] > 0
                                         and x["train"]["total_trades"] >= MIN_TRADES_PER_COIN)
        row["approved"] = (
            good >= MIN_COINS_PROFITABLE * len(rows)
            and (row["train"]["median_pf"] or 0) >= MIN_PF
            and (row["test"]["median_pf"] or 0) >= MIN_PF
        )
        table.append(row)
    table.sort(key=lambda r: ((r["test"]["avg_r"] or -9) + (r["train"]["avg_r"] or -9)), reverse=True)
    return table


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--cache", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bt_cache"))
    ap.add_argument("--out", default="backtest_report.json")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    a = ap.parse_args()

    srv = _server()
    strategies = [s for s in srv._ALL_CRYPTO_STRATEGIES if s not in SKIP]
    print(f"Downloading candles ({len(COINS)} coins x {len(TIMEFRAMES)} timeframes, {a.days} days)...", flush=True)
    fetch_all(a.days, a.cache)
    jobs = []
    for coin in COINS:
        for tf in TIMEFRAMES:
            path = os.path.join(a.cache, f"{coin}_{tf}_{a.days}.json")
            if os.path.exists(path):
                for st in strategies:
                    jobs.append((st, coin, tf, path))
    print(f"Running {len(jobs)} backtests on {a.workers} workers...", flush=True)
    t0 = time.time()
    results, failures = [], []
    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        futs = {pool.submit(run_one, j): j for j in jobs}
        for k, fut in enumerate(as_completed(futs), 1):
            try:
                results.append(fut.result())
            except Exception as e:
                failures.append({"job": futs[fut][:3], "error": str(e)[:300]})
            if k % 50 == 0:
                print(f"  {k}/{len(jobs)} done ({time.time() - t0:.0f}s)", flush=True)
    table = aggregate(results)
    report = {
        "generated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "days": a.days, "coins": COINS, "timeframes": TIMEFRAMES,
        "rules": {"min_trades_per_coin": MIN_TRADES_PER_COIN, "min_coins_profitable": MIN_COINS_PROFITABLE,
                  "min_pf": MIN_PF, "fees": "0.05% taker/side + 0.03% slippage", "sl": "2 ATR (or trailing)",
                  "tp": "tp_atr or 2R", "risk_per_trade": "1%"},
        "approved": [{"strategy": r["strategy"], "tf": r["tf"], "coins": r["profitable_coins"]}
                     for r in table if r["approved"]],
        "table": table,
        "per_coin": results,
        "failures": failures,
    }
    with open(a.out, "w") as f:
        json.dump(report, f, indent=1)
    print(f"\nDone in {time.time() - t0:.0f}s. {len(failures)} failures. Report: {a.out}")
    print(f"{'strategy':26}{'tf':5}{'trainR':>8}{'testR':>8}{'trainPF':>8}{'testPF':>8}{'coins+':>7}{'worstDD':>8}  ok")
    for r in table:
        print(f"{r['strategy']:26}{r['tf']:5}{str(r['train']['avg_r']):>8}{str(r['test']['avg_r']):>8}"
              f"{str(r['train']['median_pf']):>8}{str(r['test']['median_pf']):>8}"
              f"{r['coins_profitable_both']:>4}/{r['coins']:<2}{max(r['train']['max_dd_worst'], r['test']['max_dd_worst']):>8}"
              f"  {'YES' if r['approved'] else ''}")


if __name__ == "__main__":
    main()
