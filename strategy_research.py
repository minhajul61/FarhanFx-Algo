"""
Strategy research: which strategies have a real edge after fees?

For each strategy × timeframe, a stop/target grid is run on the first two
thirds of a year of data (TRAIN) for 10 coins; the best grid point on TRAIN
is then checked on the last third (TEST), which it never saw. A strategy
passes only if it makes money in both halves on several coins.

    python strategy_research.py            # writes strategy_research.json
"""
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "SUI"]
GRID = [(sl, rr) for sl in (1.5, 2.0, 3.0) for rr in (1.5, 2.0, 3.0)]
PLAN = {
    "4h": ["ema_trend", "rsi_trend", "smc_sweep", "pa_engulf", "macd_trend",
           "rsi", "liquidity_sweep", "bos_choch", "ob_fvg", "fvg", "ifvg", "false_breakout",
           "vwap_bands", "rsi_divergence"],
    "1h": ["ema_trend", "rsi_trend", "smc_sweep", "pa_engulf", "macd_trend"],
}
MIN_TRAIN_TRADES = 40       # pooled over the 10 coins
MIN_TEST_TRADES = 20
MIN_COINS_BOTH = 4
MIN_TEST_R = 0.05
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bt_cache")


def _server():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import server  # noqa
    return server


def _rows(coin, tf):
    path = os.path.join(CACHE, f"{coin}_{tf}_365.json")
    if not os.path.exists(path) or time.time() - os.path.getmtime(path) > 24 * 3600:
        os.makedirs(CACHE, exist_ok=True)
        rows = _server()._fetch_backtest_ohlcv(f"{coin}/USDT:USDT", tf, 365)
        with open(path, "w") as f:
            json.dump(rows, f)
    with open(path) as f:
        return json.load(f)


def job(args):
    strategy, tf, coin = args
    srv = _server()
    rows = _rows(coin, tf)
    sym = f"{coin}/USDT:USDT"
    cut = int(len(rows) * 2 / 3)
    window = srv._bot_lookback_bars(srv._strategy_bot_template(strategy, sym, tf))
    out = []
    for sl, rr in GRID:
        params = {"sl_atr": sl, "reward_r": rr, "tp_atr": 0.0, "trailing_atr": 0.0}
        res = {}
        for half, part in (("train", rows[:cut]), ("test", rows[max(0, cut - window):])):
            r = srv._run_backtest_strategy(strategy, part, sym, tf, param_overrides=params)
            res[half] = (r["total_trades"], r["avg_r"] or 0.0)
        out.append({"strategy": strategy, "tf": tf, "coin": coin, "sl": sl, "rr": rr, **res})
    return out


def main():
    for tf, strategies in PLAN.items():
        for coin in COINS:
            _rows(coin, tf)
    jobs = [(st, tf, coin) for tf, sts in PLAN.items() for st in sts for coin in COINS]
    t0 = time.time()
    rows = []
    with ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 2) - 1)) as pool:
        for k, res in enumerate(pool.map(job, jobs), 1):
            rows += res
            if k % 20 == 0:
                print(f"  {k}/{len(jobs)} ({time.time() - t0:.0f}s)", flush=True)
    report = []
    for tf, strategies in PLAN.items():
        for st in strategies:
            best = None
            for sl, rr in GRID:
                pts = [r for r in rows if r["strategy"] == st and r["tf"] == tf and r["sl"] == sl and r["rr"] == rr]
                tr_n = sum(p["train"][0] for p in pts)
                te_n = sum(p["test"][0] for p in pts)
                tr_r = sum(p["train"][0] * p["train"][1] for p in pts) / tr_n if tr_n else 0
                te_r = sum(p["test"][0] * p["test"][1] for p in pts) / te_n if te_n else 0
                both = sum(1 for p in pts if p["train"][0] >= 3 and p["test"][0] >= 2 and p["train"][1] > 0 and p["test"][1] > 0)
                cand = {"strategy": st, "tf": tf, "sl_atr": sl, "reward_r": rr, "train_trades": tr_n, "train_r": round(tr_r, 3),
                        "test_trades": te_n, "test_r": round(te_r, 3), "coins_both": both,
                        "coins": sorted(p["coin"] for p in pts if p["train"][1] > 0 and p["test"][1] > 0)}
                if tr_n >= MIN_TRAIN_TRADES and (best is None or tr_r > best["train_r"]):
                    best = cand
            if not best:
                report.append({"strategy": st, "tf": tf, "pass": False, "why": "too few trades"})
                continue
            best["pass"] = bool(best["train_r"] > 0 and best["test_r"] >= MIN_TEST_R and best["test_trades"] >= MIN_TEST_TRADES
                                and best["coins_both"] >= MIN_COINS_BOTH)
            report.append(best)
    report.sort(key=lambda r: (not r.get("pass"), -(r.get("test_r") or -9)))
    with open("strategy_research.json", "w") as f:
        json.dump({"generated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "rules": {
            "grid": GRID, "min_train_trades": MIN_TRAIN_TRADES, "min_test_trades": MIN_TEST_TRADES,
            "min_coins_both": MIN_COINS_BOTH, "min_test_r": MIN_TEST_R}, "results": report}, f, indent=1)
    print(f"\ndone in {time.time() - t0:.0f}s")
    print(f"{'strategy':18}{'tf':4}{'SL':>5}{'RR':>5}{'trainR':>8}{'n':>5}{'testR':>8}{'n':>5}{'coins':>7}  pass")
    for r in report:
        if "train_r" not in r:
            print(f"{r['strategy']:18}{r['tf']:4}  {r['why']}")
            continue
        print(f"{r['strategy']:18}{r['tf']:4}{r['sl_atr']:>5}{r['reward_r']:>5}{r['train_r']:>8}{r['train_trades']:>5}"
              f"{r['test_r']:>8}{r['test_trades']:>5}{r['coins_both']:>5}/10  {'YES' if r['pass'] else ''}")


if __name__ == "__main__":
    main()
