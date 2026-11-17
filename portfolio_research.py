"""Portfolio replay of the AI Signal Hunter (source of RISK_LEVELS in autopilot.py).

    python portfolio_research.py build     # backtest 40 coins x approved strategies over 2 years

1. Every (coin, strategy) is backtested once over 2 years with the tested
   stop/target -> a list of trades (entry/exit time, R).
2. The hunter is replayed: trades are taken in entry order, only when that
   strategy had an edge on that coin in the previous 180 days (>= 6 closed
   trades, avg R > 0, known at entry time = walk-forward), one per coin, at
   most SLOTS at once. Each trade risks RISK % of equity at entry.
"""
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
OUT = os.path.join(REPO, ".bt_cache")
TRADES_FILE = os.path.join(OUT, "pf_trades.json")

RESEARCH = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "SUI",
            "LTC", "DOT", "TRX", "NEAR", "UNI", "APT", "ARB", "OP", "INJ", "BCH"]
EXTRA = ["ZEC", "1000PEPE", "ENA", "AAVE", "WLD", "XMR", "ICP", "TAO", "FIL", "JUP",
         "XLM", "ONDO", "HBAR", "FET", "GALA", "TIA", "CRV", "ETC", "ATOM", "1000BONK"]
DAY = 86_400_000


def _ms(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp() * 1000)


def job(args):
    coin, strat, tf, params = args
    import strategy_research as sr
    import server
    import engine
    sr.DAYS = 730
    rows = sr._rows(coin, tf)
    sym = f"{coin}/USDT:USDT"
    bot = server._strategy_bot_template(strat, sym, tf, params)
    res = engine.backtest(server._get_bot_signal, bot, rows, server._bot_lookback_bars(bot), capital=1000, risk_pct=1.0)
    # ATR % of price at each entry (volatility filter)
    idx = {r[0]: i for i, r in enumerate(rows)}
    out = []
    for t in res["trades"]:
        e = _ms(t["time"])
        i = idx.get(e)
        atrp = None
        if i and i > 15:
            tr = [max(rows[k][2] - rows[k][3], abs(rows[k][2] - rows[k - 1][4]), abs(rows[k][3] - rows[k - 1][4]))
                  for k in range(i - 13, i + 1)]
            atrp = sum(tr) / 14 / rows[i][4] * 100
        tf_ms = 14_400_000 if tf == "4h" else DAY
        out.append({"coin": coin, "strategy": strat, "tf": tf, "entry": e + tf_ms, "exit": _ms(t["exit_time"]) + tf_ms,
                    "r": t["r"], "side": t["signal"], "atrp": atrp, "reason": t["exit_reason"]})
    return out


def build():
    import autopilot as apm
    jobs = [(c, st, p["tf"], p["params"]) for c in RESEARCH + EXTRA for st, p in apm.APPROVED_PROFILES.items()]
    import strategy_research as sr
    sr.DAYS = 730
    for c in RESEARCH + EXTRA:            # fill the cache first (single process)
        for tf in ("4h", "1d"):
            sr._rows(c, tf)
    trades = []
    with ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 2) - 1)) as pool:
        for res in pool.map(job, jobs):
            trades += res
    json.dump(trades, open(TRADES_FILE, "w"))
    return trades


def replay(trades, coins=None, strategies=None, slots=5, risk=1.0, edge_days=180, min_trades=6,
           use_edge=True, atr_cap=None, start=None, end=None, capital=1000.0):
    ts = [t for t in trades if (coins is None or t["coin"] in coins) and (strategies is None or t["strategy"] in strategies)]
    by_key = {}
    for t in ts:
        by_key.setdefault((t["coin"], t["strategy"]), []).append(t)
    first = min(t["entry"] for t in ts)
    start = start or first + edge_days * DAY
    cand = sorted([t for t in ts if t["entry"] >= start and (end is None or t["entry"] < end)], key=lambda t: t["entry"])
    equity, peak, max_dd = capital, capital, 0.0
    open_pos, taken, skipped_slot = [], [], 0
    daily = {}

    def settle(until):
        nonlocal equity, peak, max_dd
        open_pos.sort(key=lambda p: p["exit"])
        while open_pos and open_pos[0]["exit"] <= until:
            p = open_pos.pop(0)
            pnl = p["risk_usd"] * p["r"]
            equity += pnl
            d = datetime.fromtimestamp(p["exit"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            daily[d] = daily.get(d, 0) + pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak * 100)

    for t in cand:
        settle(t["entry"])
        if use_edge:
            hist = [x for x in by_key[(t["coin"], t["strategy"])] if t["entry"] - edge_days * DAY <= x["exit"] <= t["entry"]]
            if len(hist) < min_trades or sum(x["r"] for x in hist) / len(hist) <= 0:
                continue
        if atr_cap and (t["atrp"] is None or t["atrp"] > atr_cap):
            continue
        if any(p["coin"] == t["coin"] for p in open_pos):
            continue
        if len(open_pos) >= slots:
            skipped_slot += 1
            continue
        p = dict(t, risk_usd=equity * risk / 100)
        open_pos.append(p)
        taken.append(p)
    settle(10 ** 15)
    days = ((end or max(t["exit"] for t in ts)) - start) / DAY
    n = len(taken)
    rs = [t["r"] for t in taken]
    months = {}
    for d, v in daily.items():
        months[d[:7]] = months.get(d[:7], 0) + v
    return {"trades": n, "per_day": round(n / days, 2), "avg_r": round(sum(rs) / n, 3) if n else 0,
            "total_r": round(sum(rs), 1), "r_per_day": round(sum(rs) / days, 3),
            "win": round(100 * sum(1 for r in rs if r > 0) / n, 1) if n else 0,
            "final": round(equity, 0), "ret_pct": round((equity / capital - 1) * 100, 1), "max_dd": round(max_dd, 1),
            "days": round(days), "slot_skips": skipped_slot,
            "worst_month": round(min(months.values()), 0) if months else 0,
            "best_month": round(max(months.values()), 0) if months else 0,
            "months_up": f"{sum(1 for v in months.values() if v > 0)}/{len(months)}"}


if __name__ == "__main__":
    t0 = time.time()
    os.makedirs(OUT, exist_ok=True)
    trades = build() if (len(sys.argv) > 1 and sys.argv[1] == "build") or not os.path.exists(TRADES_FILE) \
        else json.load(open(TRADES_FILE))
    print(f"{len(trades)} single-strategy trades ({time.time() - t0:.0f}s)")
