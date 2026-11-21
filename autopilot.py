"""
AI Autopilot — picks WHICH coin and WHICH strategy the bots trade.

Every scan (default every 4h, right after the 4h candle closes):
  1. Universe: the most liquid Binance USDT-M perps (24h volume >= min),
     stablecoins / tokenized stocks / commodities removed.
  2. Momentum score 0-100 for each coin from 4h candles:
       7-day and 24h rate of change, trend strength (ADX), EMA20/50
       alignment, and volume surge. Up or down momentum both count —
       futures can short.
  3. The top-momentum coins are re-tested on their own last 180 days with
     each approved strategy (the live engine's exact rules). A strategy is
     eligible on that coin only if it made money there (avg R > 0, enough
     trades).
  4. Final rank = momentum + the strategy's recent edge on that coin.
     The best N coins (N = risk manager's max positions) become the slots.
  5. Slots are handed to the server: new bots are started, bots on coins
     that dropped out stop taking new trades and are removed once flat.
     A bot holding a position is never switched to another coin.

Only strategies that passed the full out-of-sample backtest are allowed
(see APPROVED_PROFILES). Everything is logged so the dashboard can show why
each pick was made.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime, timezone

import engine

FILE = "autopilot_state.json"

# Strategies that made money in BOTH halves of a 1-year, 10-coin backtest
# with fees, slippage and these exact stop/target settings (full_backtest.py
# + stop/target sweep chosen on the first 8 months, confirmed on the last 4).
# Deep research (strategy_research.py, Nov 2026): 2 years × 20 coins, 9 stop/
# target settings, best setting chosen on the first 16 months (train) and then
# judged on the last 8 months it never saw (test). Kept only when profitable
# in both halves, on 8+ coins, and at 5+ of the 9 settings (not one lucky one).
def _p(sl, rr):
    return {"sl_atr": sl, "reward_r": rr, "tp_atr": 0.0, "trailing_atr": 0.0}


APPROVED_PROFILES = {
    "rsi_trend": {
        "tf": "4h", "params": _p(3.0, 3.0),
        "evidence": "2y/20 coins: train +0.35R, test +0.19R/trade (203 trades), 13/20 coins up in both halves, 9/9 settings",
    },
    "ema_trend": {
        "tf": "4h", "params": _p(2.0, 3.0),
        "evidence": "2y/20 coins: train +0.22R, test +0.12R/trade (280 trades), 11/20 coins, 8/9 settings",
    },
    "fibonacci_retracement": {
        "tf": "4h", "params": _p(3.0, 3.0),
        "evidence": "2y/20 coins: train +0.06R, test +0.06R/trade (514 trades), 11/20 coins, 6/9 settings",
    },
    "trend_breakout": {
        "tf": "1d", "params": _p(1.5, 2.0),
        "evidence": "2y/20 coins daily: train +0.25R, test +0.28R/trade (115 trades), 10/20 coins, 7/9 settings",
    },
}
# smc_sweep passed the 20-coin research but lost money in the portfolio replay
# on 20 coins it had never seen (-0.28R per trade), so it was removed.
TF_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}

# One-click start: the user picks a balance and one of these; the AI does the
# rest. Numbers are from the portfolio replay (portfolio.py): the hunter with
# these strategies on 40 coins, Mar 2025 - Nov 2026, compounded, fees included.
RISK_LEVELS = {
    "safe":       {"risk_pct": 0.5, "max_positions": 5, "label": "Safe",
                   "per_1000_day": 0.53, "trades_day": 0.7, "max_dd": 13, "months_up": "11/18"},
    "balanced":   {"risk_pct": 1.0, "max_positions": 8, "label": "Balanced",
                   "per_1000_day": 1.41, "trades_day": 1.1, "max_dd": 34, "months_up": "10/18"},
    "aggressive": {"risk_pct": 2.0, "max_positions": 10, "label": "Aggressive",
                   "per_1000_day": 2.39, "trades_day": 1.3, "max_dd": 62, "months_up": "8/18"},
}

EXCLUDE_BASES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1", "RLUSD",
    "XAU", "XAG", "PAXG", "XAUT", "CL", "UKOIL", "USOIL",
    "SOXL", "SKHYNIX", "MU", "NVDA", "TSLA", "MSFT", "AAPL", "SNDK", "SPCX", "COIN", "MSTR",
}

DEFAULT_CONFIG = {
    "enabled": False,
    "mode": "demo",              # demo | live
    "scan_hours": 4,
    "universe": 40,              # liquid crypto coins scanned every 4h candle
    "min_volume_usd": 50_000_000,
    "candidates": 8,             # top-momentum coins that get strategy-tested
    "validate_days": 180,
    "min_trades": 6,
    "strategies": list(APPROVED_PROFILES.keys()),
    "max_atr_pct": 6.0,          # skip coins that move more per 4h candle than any tested coin
    "level": "balanced",
    "balance": 1000.0,
}

_lock = threading.RLock()


# ── small indicator helpers ───────────────────────────────────────────────────
def _ema(vals, n):
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def _adx(h, l, c, n=14):
    if len(c) < 2 * n + 1:
        return 0.0
    pdm, mdm, tr = [], [], []
    for i in range(1, len(c)):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    def smooth(x):
        s = sum(x[:n]); out = [s]
        for v in x[n:]:
            s = s - s / n + v; out.append(s)
        return out
    str_, sp, sm = smooth(tr), smooth(pdm), smooth(mdm)
    dx = []
    for a, b, t in zip(sp, sm, str_):
        if t <= 0:
            continue
        pdi, mdi = 100 * a / t, 100 * b / t
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0)
    if len(dx) < n:
        return 0.0
    adx = sum(dx[:n]) / n
    for v in dx[n:]:
        adx = (adx * (n - 1) + v) / n
    return adx


def atr_pct(ohlcv: list, n: int = 14) -> float | None:
    """Average true range of the last n candles as % of the last close."""
    if len(ohlcv) < n + 2 or not ohlcv[-1][4]:
        return None
    tr = [max(ohlcv[k][2] - ohlcv[k][3], abs(ohlcv[k][2] - ohlcv[k - 1][4]), abs(ohlcv[k][3] - ohlcv[k - 1][4]))
          for k in range(len(ohlcv) - n, len(ohlcv))]
    return round(sum(tr) / n / ohlcv[-1][4] * 100, 2)


def momentum_score(ohlcv_4h: list) -> dict | None:
    """Momentum from 4h candles (needs ~60+ bars). Returns metrics + score."""
    if len(ohlcv_4h) < 60:
        return None
    c = [x[4] for x in ohlcv_4h]; h = [x[2] for x in ohlcv_4h]; l = [x[3] for x in ohlcv_4h]
    v = [x[5] for x in ohlcv_4h]
    roc_24h = c[-1] / c[-7] - 1            # 6 x 4h = 24h
    roc_7d = c[-1] / c[-43] - 1            # 42 x 4h = 7d
    ema20, ema50 = _ema(c, 20), _ema(c, 50)
    adx = _adx(h, l, c, 14)
    vol_24h = sum(v[-6:]); vol_avg = sum(v[-48:-6]) / 7 if len(v) >= 48 else vol_24h
    vol_ratio = vol_24h / vol_avg if vol_avg else 1.0
    atr_pct = engine.atr(h, l, c, 14) / c[-1] * 100

    direction = 1 if roc_7d >= 0 else -1
    aligned = (direction == 1 and c[-1] > ema20 > ema50) or (direction == -1 and c[-1] < ema20 < ema50)
    same_way = (roc_24h >= 0) == (roc_7d >= 0)
    # Normalise each piece to 0..1 by volatility so a 5% move on BTC counts
    # like a 15% move on a meme coin.
    vol_unit = max(atr_pct, 0.3)
    s_7d = min(1.0, abs(roc_7d) * 100 / (vol_unit * 8))
    s_24h = min(1.0, abs(roc_24h) * 100 / (vol_unit * 3)) if same_way else 0.0
    s_adx = min(1.0, max(0.0, (adx - 15) / 30))
    s_vol = min(1.0, max(0.0, (vol_ratio - 0.8) / 1.2))
    score = 100 * (0.35 * s_7d + 0.15 * s_24h + 0.25 * s_adx + 0.10 * s_vol + (0.15 if aligned else 0.0))
    return {
        "score": round(score, 1), "direction": "UP" if direction == 1 else "DOWN",
        "roc_24h": round(roc_24h * 100, 2), "roc_7d": round(roc_7d * 100, 2),
        "adx": round(adx, 1), "vol_ratio": round(vol_ratio, 2), "atr_pct": round(atr_pct, 2),
        "trend_aligned": aligned, "price": c[-1],
    }


# ── the autopilot ─────────────────────────────────────────────────────────────
class Autopilot:
    def __init__(self, hooks: dict, path: str = FILE):
        """hooks: tickers() -> dict, ohlcv(sym, tf, limit) -> list,
        history(sym, tf, days) -> list, backtest(strategy, ohlcv, sym, tf, params) -> dict,
        apply(picks, config) -> list[str], can_go_live() -> (bool, reason)"""
        self.hooks = hooks
        self.path = path
        self.config = dict(DEFAULT_CONFIG)
        self.state = {"last_scan": None, "next_scan": None, "scanning": False, "leaderboard": [],
                      "picks": [], "log": [], "last_error": None}
        self._thread = None
        self._wake = threading.Event()
        self._load()

    # persistence
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.config.update({k: v for k, v in d.get("config", {}).items() if k in DEFAULT_CONFIG})
            # the hunter always uses every strategy that passed the research
            self.config["strategies"] = list(APPROVED_PROFILES)
            if self.config.get("universe") == 25:      # old default → the hunter scans 40
                self.config["universe"] = 40
            st = d.get("state", {})
            st["scanning"] = False
            self.state.update(st)
        except Exception:
            pass

    def save(self):
        with _lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"config": self.config, "state": self.state}, f, indent=1, default=str)
            os.replace(tmp, self.path)

    def _log(self, msg):
        self.state["log"].append({"time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), "msg": msg})
        self.state["log"] = self.state["log"][-80:]

    # control
    def set_enabled(self, on: bool, mode: str | None = None) -> tuple[bool, str]:
        with _lock:
            if mode in ("demo", "live"):
                if mode == "live":
                    ok, why = self.hooks["can_go_live"]()
                    if not ok:
                        return False, why
                self.config["mode"] = mode
            self.config["enabled"] = bool(on)
            self._log(f"Autopilot {'ON' if on else 'OFF'} ({self.config['mode'].upper()})")
            self.save()
        if on:
            self.scan_now()
        return True, ""

    def update_config(self, changes: dict):
        with _lock:
            for k in ("scan_hours", "universe", "min_volume_usd", "candidates", "validate_days", "min_trades",
                      "max_atr_pct", "balance"):
                if k in changes and changes[k] is not None:
                    self.config[k] = type(DEFAULT_CONFIG[k])(changes[k])
            if "strategies" in changes:
                s = [x for x in changes["strategies"] if x in APPROVED_PROFILES]
                if s:
                    self.config["strategies"] = s
            self.config["scan_hours"] = min(24, max(1, self.config["scan_hours"]))
            self.config["candidates"] = min(15, max(3, self.config["candidates"]))
            if changes.get("level") in RISK_LEVELS:
                self.config["level"] = changes["level"]
            self.config["max_atr_pct"] = min(50.0, max(1.0, self.config["max_atr_pct"]))
            self.save()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="autopilot")
        self._thread.start()

    def scan_now(self):
        self._wake.set()

    def _followers(self) -> list:
        """Client accounts whose own autopilot is on: they trade the same scan."""
        try:
            return list((self.hooks.get("followers") or (lambda: []))())
        except Exception:
            return []

    def _loop(self):
        time.sleep(20)
        while True:
            self._wake.clear()          # a scan request that arrived meanwhile is served by this scan
            # With nobody trading it still reads the market (nothing is opened),
            # so the public page and the dashboards always show a live scan.
            acting = bool(self.config["enabled"] or self._followers())
            try:
                self.hunt(apply=acting)
            except Exception as e:
                self.state["last_error"] = str(e)[:300]
                self._log(f"Scan failed: {str(e)[:200]}")
                self.save()
            period = self.config["scan_hours"] * 3600
            wait = engine.candle_close_delay(min(period, 14400), time.time(), grace=60)
            self.state["next_scan"] = datetime.fromtimestamp(time.time() + wait, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            self._wake.wait(wait)
            self._wake.clear()

    # the scan
    def run_scan(self, apply: bool = True) -> dict:
        with _lock:
            if self.state.get("scanning"):
                return {"busy": True}
            self.state["scanning"] = True
            self.state["last_error"] = None
        t0 = time.time()
        try:
            cfg = dict(self.config)
            slots = self.hooks["slots"]()
            universe = self._universe(cfg)
            board = []
            for sym, vol in universe:
                try:
                    m = momentum_score(self.hooks["ohlcv"](sym, "4h", 120))
                except Exception:
                    m = None
                if m:
                    board.append({"symbol": sym, "volume_24h": round(vol), **m})
            board.sort(key=lambda x: -x["score"])
            candidates = board[:cfg["candidates"]]

            picks = []
            for coin in candidates:
                best = None
                tests = []
                for strat in cfg["strategies"]:
                    prof = APPROVED_PROFILES[strat]
                    try:
                        hist = self.hooks["history"](coin["symbol"], prof["tf"], cfg["validate_days"])
                        r = self.hooks["backtest"](strat, hist, coin["symbol"], prof["tf"], prof["params"])
                    except Exception as e:
                        tests.append({"strategy": strat, "error": str(e)[:80]})
                        continue
                    t = {"strategy": strat, "tf": prof["tf"], "trades": r["total_trades"],
                         "avg_r": r["avg_r"], "pf": r["profit_factor"], "net_pct": r["net_pnl_pct"],
                         "max_dd": r["max_drawdown_pct"], "win_rate": r["win_rate"]}
                    t["eligible"] = bool(r["total_trades"] >= cfg["min_trades"] and (r["avg_r"] or 0) > 0)
                    tests.append(t)
                    if t["eligible"] and (best is None or t["avg_r"] > best["avg_r"]):
                        best = t
                coin["tests"] = tests
                if best:
                    fit = min(1.0, best["avg_r"] / 0.5)        # 0.5R/trade = full marks
                    rank = round(0.6 * coin["score"] + 40 * fit, 1)
                    picks.append({"symbol": coin["symbol"], "strategy": best["strategy"], "tf": best["tf"],
                                  "params": APPROVED_PROFILES[best["strategy"]]["params"],
                                  "momentum": coin["score"], "direction": coin["direction"],
                                  "rank": rank, "backtest": best,
                                  "why": (f"momentum {coin['score']:.0f} ({coin['direction']}, 7d {coin['roc_7d']:+.1f}%, "
                                          f"ADX {coin['adx']:.0f}); {best['strategy']} on this coin, last "
                                          f"{cfg['validate_days']}d: {best['trades']} trades, {best['avg_r']:+.2f}R avg, "
                                          f"PF {best['pf']}")})
            picks.sort(key=lambda p: -p["rank"])
            chosen = picks[:slots]

            actions = []
            if apply and cfg["enabled"]:
                actions = self.hooks["apply"](chosen, cfg)
            with _lock:
                self.state["leaderboard"] = board[:20]
                self.state["candidates_tested"] = [{k: c[k] for k in ("symbol", "score", "direction", "tests")}
                                                   for c in candidates]
                self.state["picks"] = chosen
                self.state["last_scan"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                self.state["scan_seconds"] = round(time.time() - t0, 1)
                if chosen:
                    self._log("Picked: " + ", ".join(f"{p['symbol'].split('/')[0]}→{p['strategy']}" for p in chosen))
                else:
                    self._log("No coin passed the strategy test this scan — no new bots")
                for a in actions:
                    self._log(a)
                self.save()
            return {"picks": chosen, "actions": actions}
        finally:
            with _lock:
                self.state["scanning"] = False
                self.save()

    # ── AI Signal Hunter: scan the whole liquid market, trade fresh signals ──
    HUNT_BARS = 300          # 4h candles per coin: enough for EMA(200) + box warm-up
    EDGE_TTL = 12 * 3600     # re-check a coin+strategy backtest twice a day
    MAX_SIGNAL_AGE_MIN = 30  # only enter within 30 min of the candle close

    def _edge(self, symbol, strategy, tf, params):
        """Has this strategy made money on THIS coin recently? Cached."""
        cache = self.__dict__.setdefault("_edge_cache", {})
        key = (symbol, strategy, tf)
        hit = cache.get(key)
        if hit and time.time() - hit[0] < self.EDGE_TTL:
            return hit[1]
        days = self.PICK_DAYS.get(tf, 180)
        r = self.hooks["backtest"](strategy, self.hooks["history"](symbol, tf, days), symbol, tf, params)
        res = {"trades": r.get("total_trades") or 0, "avg_r": r.get("avg_r") or 0, "pf": r.get("profit_factor"),
               "win_rate": r.get("win_rate"), "days": days}
        res["ok"] = bool(res["trades"] >= self.config["min_trades"] and res["avg_r"] > 0)
        cache[key] = (time.time(), res)
        return res

    def hunt(self, apply: bool = True) -> dict:
        """Every closed 4h candle: look at the top liquid coins with every tested
        strategy. A coin with a FRESH buy/sell signal on the candle that just
        closed, whose strategy also made money on that coin's own recent
        history, becomes a trade (best first, while risk slots are free).
        Each candle is acted on once — a restart never enters it twice."""
        with _lock:
            if self.state.get("scanning"):
                return {"busy": True}
            self.state["scanning"] = True
            self.state["last_error"] = None
        t0 = time.time()
        try:
            cfg = dict(self.config)
            board, signals, last_bar = [], [], {}
            tfs = sorted({APPROVED_PROFILES[st]["tf"] for st in cfg["strategies"]} | {"4h"})
            for sym, vol in self._universe(cfg):
                rows_by = {}
                for tf in tfs:
                    try:
                        rows_by[tf] = self.hooks["ohlcv"](sym, tf, self.HUNT_BARS)
                    except Exception:
                        rows_by[tf] = []
                rows = rows_by["4h"]
                if len(rows) < 120:
                    continue
                for tf, r in rows_by.items():
                    if r:
                        last_bar[tf] = max(last_bar.get(tf, 0), r[-1][0])
                m = momentum_score(rows[-120:])
                if m:
                    board.append({"symbol": sym, "volume_24h": round(vol), **m})
                atrp = atr_pct(rows)
                for strat in cfg["strategies"]:
                    prof = APPROVED_PROFILES[strat]
                    r = rows_by.get(prof["tf"]) or []
                    if len(r) < 60:
                        continue
                    try:
                        sig = self.hooks["signal"](strat, sym, prof["tf"], prof["params"], r)
                    except Exception:
                        sig = None
                    if sig in ("BUY", "SELL"):
                        signals.append({"symbol": sym, "strategy": strat, "tf": prof["tf"], "side": sig,
                                        "params": prof["params"], "bar": r[-1][0], "atr_pct": atrp,
                                        "momentum": (m or {}).get("score", 0), "trend": (m or {}).get("direction")})
            board.sort(key=lambda x: -x["score"])
            fresh = [s for s in signals if s["bar"] == last_bar.get(s["tf"])]
            cap = float(cfg.get("max_atr_pct") or 0)
            for s in fresh:
                wild = bool(cap and s.get("atr_pct") and s["atr_pct"] > cap)
                try:
                    e = {"ok": False, "trades": 0, "avg_r": 0} if wild else \
                        self._edge(s["symbol"], s["strategy"], s["tf"], s["params"])
                except Exception as ex:
                    e = {"ok": False, "trades": 0, "avg_r": 0, "error": str(ex)[:80]}
                s["edge"] = e
                aligned = (s["side"] == "BUY" and s["trend"] == "UP") or (s["side"] == "SELL" and s["trend"] == "DOWN")
                s["rank"] = round(40 * min(1.0, max(-1.0, (e.get("avg_r") or 0) / 0.5)) + 0.4 * s["momentum"]
                                  + (15 if aligned else 0), 1)
                s["direction"] = "UP" if s["side"] == "BUY" else "DOWN"
                s["why"] = (f"{s['side']} signal, but {s['symbol'].split('/')[0]} moves {s['atr_pct']:.1f}% per 4h candle — "
                            f"tested coins move up to {cap:g}%, so its stop and target would be unrealistic; skipped") if wild else \
                           (f"{s['side']} signal on the {datetime.fromtimestamp(s['bar'] / 1000, tz=timezone.utc).strftime('%H:%M')} UTC "
                            f"candle; {s['strategy'].replace('_', ' ')} on this coin, last {e.get('days', 180)}d: "
                            f"{e.get('trades', 0)} trades, {(e.get('avg_r') or 0):+.2f}R avg"
                            + ("" if e.get("ok") else " — no edge here, skipped")
                            + (f"; momentum {s['momentum']:.0f} {s['trend']}" if s.get("trend") else ""))
            tradeable = sorted([s for s in fresh if s["edge"].get("ok")], key=lambda s: -s["rank"])
            # Each timeframe's candle is acted on once, and only right after it
            # closes — the backtest enters there, so an old signal is never chased.
            hunted = dict(self.state.get("hunted") or {})
            if self.state.get("hunted_bar"):
                hunted["4h"] = max(int(hunted.get("4h") or 0), int(self.state["hunted_bar"]))
            now_ms = time.time() * 1000
            new_tfs, late_tfs = set(), set()
            for tf, bar in last_bar.items():
                if bar > int(hunted.get(tf) or 0):
                    age = (now_ms - (bar + TF_SECONDS.get(tf, 14400) * 1000)) / 60000
                    (late_tfs if age > self.MAX_SIGNAL_AGE_MIN else new_tfs).add(tf)
            new_candle = "4h" in new_tfs or "4h" in late_tfs
            actions, active = [], []
            followers = self._followers() if apply else []
            acting = apply and (cfg["enabled"] or bool(followers))
            go = [s for s in tradeable if s["tf"] in new_tfs]
            late = [s for s in tradeable if s["tf"] in late_tfs]
            late_note = (f"{', '.join(sorted({s['tf'] for s in late}))} candle closed more than "
                         f"{self.MAX_SIGNAL_AGE_MIN} min ago — too late to enter its signals, waiting for the next candle") if late else None
            if apply and cfg["enabled"]:
                actions, active = self.hooks["open"](go, cfg)
                if late_note:
                    actions.insert(0, late_note)
            if followers and self.hooks.get("follow"):
                summary = f"Scanned {len(board)} coins: {len(fresh)} fresh signal(s), {len(tradeable)} with an edge"
                try:
                    self.hooks["follow"](followers, go if acting else [], summary, late_note)
                except Exception as e:
                    self._log(f"Client autopilots failed: {str(e)[:150]}")
            with _lock:
                if acting:
                    for tf in new_tfs | late_tfs:
                        hunted[tf] = last_bar[tf]
                    self.state["hunted"] = hunted
                    self.state["hunted_bar"] = int(hunted.get("4h") or 0)
                self.state["leaderboard"] = board[:20]
                self.state["signals"] = [{k: v for k, v in s.items() if k != "params"} for s in
                                         sorted(fresh, key=lambda s: -s.get("rank", 0))][:30]
                self.state["picks"] = active if (apply and cfg["enabled"]) else \
                    [{k: v for k, v in s.items() if k != "params"} for s in tradeable[:6]]
                self.state["coins_scanned"] = len(board)
                self.state["last_scan"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                self.state["scan_seconds"] = round(time.time() - t0, 1)
                self._log(f"Scanned {len(board)} coins × {len(cfg['strategies'])} strategies: "
                          f"{len(fresh)} fresh signal(s), {len(tradeable)} with an edge"
                          + ("" if new_candle or not (apply and cfg["enabled"]) else " — this candle was already handled"))
                for a in actions:
                    self._log(a)
                self.save()
            return {"signals": fresh, "tradeable": tradeable, "actions": actions}
        finally:
            with _lock:
                self.state["scanning"] = False
                self.save()

    # ── coin picker for one strategy (ALGO BOTS "AI picks the pair") ─────────
    PICK_DAYS = {"15m": 45, "1h": 120, "4h": 180, "1d": 730}

    def _board(self, max_age=1800):
        """Momentum leaderboard of the liquid universe, cached 30 min."""
        cache = getattr(self, "_board_cache", None)
        if cache and time.time() - cache[0] < max_age:
            return cache[1]
        board = []
        for sym, vol in self._universe(self.config):
            try:
                m = momentum_score(self.hooks["ohlcv"](sym, "4h", 120))
            except Exception:
                m = None
            if m:
                board.append({"symbol": sym, "volume_24h": round(vol), **m})
        board.sort(key=lambda x: -x["score"])
        self._board_cache = (time.time(), board)
        return board

    AI_COINS = 5     # coins an algo bot watches at once

    def pick_for_strategy(self, strategy, tf, params, exclude=(), keep=None):
        """Best coin for this strategy right now: the top-momentum coins are
        each backtested with this strategy/timeframe/stop settings, ranked by
        momentum + backtest edge. `keep` (the bot's current coin) is kept while
        it is still one of the coins with an edge, so bots do not churn."""
        results = self.rank_for_strategy(strategy, tf, params, exclude, keep)
        if not results:
            return None
        kept = next((x for x in results if x["symbol"] == keep and x["edge"]), None)
        best = kept or next((x for x in results if x["edge"]), None)
        if not best:
            # never hand a bot a coin where this strategy has been losing money
            return {"symbol": None, "kept": False, "tested": len(results), "alternatives": [],
                    "why": (f"none of the {len(results)} coins tested made money with {strategy} {tf} "
                            f"recently (best {results[0]['symbol'].split('/')[0]}: {results[0]['avg_r']:+.2f}R avg)")}
        edge = [x["symbol"] for x in results if x["edge"]]
        coins = edge[:self.AI_COINS]
        if best["symbol"] not in coins:          # the kept coin always stays on the bot's list
            coins = [best["symbol"]] + coins[:self.AI_COINS - 1]
        return {**best, "kept": bool(kept), "tested": len(results),
                "alternatives": [x for x in edge if x != best["symbol"]][:3],
                # the bot's coin list: every coin where this strategy has an edge, best first
                "coins": coins}

    def rank_for_strategy(self, strategy, tf, params, exclude=(), keep=None, candidates=None):
        """Every candidate coin with its momentum and this strategy's backtest, best first."""
        cfg = dict(self.config)
        if candidates:
            cfg["candidates"] = candidates
        board = [c for c in self._board() if c["symbol"] not in set(exclude) or c["symbol"] == keep]
        candidates = board[:cfg["candidates"]]
        if keep and keep not in [c["symbol"] for c in candidates]:
            candidates += [c for c in board if c["symbol"] == keep][:1]
        days = self.PICK_DAYS.get(tf, 180)
        results = []
        for coin in candidates:
            try:
                hist = self.hooks["history"](coin["symbol"], tf, days)
                r = self.hooks["backtest"](strategy, hist, coin["symbol"], tf, params)
            except Exception:
                continue
            avg_r = r.get("avg_r") or 0
            trades = r.get("total_trades") or 0
            edge = trades >= cfg["min_trades"] and avg_r > 0
            fit = max(-1.0, min(1.0, avg_r / 0.5))
            results.append({
                "symbol": coin["symbol"], "momentum": coin["score"], "direction": coin["direction"],
                "trades": trades, "avg_r": avg_r, "pf": r.get("profit_factor"), "edge": edge,
                "roc_7d": coin.get("roc_7d"), "volume_24h": coin.get("volume_24h"),
                "win_rate": r.get("win_rate"), "net_pct": r.get("net_pnl_pct"), "max_dd": r.get("max_drawdown_pct"),
                "rank": round(0.6 * coin["score"] + 40 * fit + (25 if edge else 0), 1),
                "why": (f"momentum {coin['score']:.0f} ({coin['direction']}, 7d {coin['roc_7d']:+.1f}%); "
                        f"{strategy} {tf} on this coin, last {days}d: {trades} trades, {avg_r:+.2f}R avg"
                        + ("" if edge else " — no clear edge")),
            })
        results.sort(key=lambda x: -x["rank"])
        return results

    def _universe(self, cfg):
        tickers = self.hooks["tickers"]()
        # Binance tags every contract: COIN = crypto; EQUITY / COMMODITY /
        # PREMARKET / FX / INDEX are stock, gold, pre-IPO... perps — not ours.
        markets = {}
        if self.hooks.get("markets"):
            try:
                markets = self.hooks["markets"]() or {}
            except Exception:
                markets = {}
        rows = []
        for sym, t in tickers.items():
            if not sym.endswith("/USDT:USDT"):
                continue
            base = sym.split("/")[0]
            if base in EXCLUDE_BASES or base.startswith("1000000"):
                continue
            info = (markets.get(sym) or {}).get("info") or {}
            if info.get("underlyingType") and info["underlyingType"] != "COIN":
                continue
            if "RWA" in (info.get("underlyingSubType") or []):
                continue
            vol = float(t.get("quoteVolume") or 0)
            if vol >= cfg["min_volume_usd"]:
                rows.append((sym, vol))
        rows.sort(key=lambda x: -x[1])
        return rows[:cfg["universe"]]

    def snapshot(self) -> dict:
        with _lock:
            return {"config": dict(self.config), "state": dict(self.state),
                    "profiles": APPROVED_PROFILES, "levels": RISK_LEVELS}
