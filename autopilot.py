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
APPROVED_PROFILES = {
    "trend_breakout": {
        "tf": "4h", "params": {"sl_atr": 2.0, "reward_r": 3.0, "tp_atr": 0.0, "trailing_atr": 0.0},
        "evidence": "test +0.31R/trade, profitable on 8/10 coins in both halves, 330 trades",
    },
    "bb_squeeze": {
        "tf": "4h", "params": {"sl_atr": 1.5, "reward_r": 1.5, "tp_atr": 0.0, "trailing_atr": 0.0},
        "evidence": "test +0.29R/trade, 6/10 coins profitable out of sample, 190 trades",
    },
    "fibonacci_retracement": {
        "tf": "4h", "params": {"sl_atr": 1.5, "reward_r": 3.0, "tp_atr": 0.0, "trailing_atr": 0.0},
        "evidence": "test +0.11R/trade, 5/10 coins profitable out of sample, 594 trades",
    },
    "super_breakout": {
        "tf": "4h", "params": {"sl_atr": 3.0, "reward_r": 3.0, "tp_atr": 0.0, "trailing_atr": 0.0},
        "evidence": "test +0.25R/trade, 147 trades (fewer signals)",
    },
    # strategy_research.py: positive on the unseen test half at all 9 stop/target settings
    "smc_sweep": {
        "tf": "4h", "params": {"sl_atr": 1.5, "reward_r": 2.0, "tp_atr": 0.0, "trailing_atr": 0.0},
        "evidence": "test +0.35R/trade, profitable on 8/10 coins in both halves, 187 trades",
    },
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
            for k in ("scan_hours", "universe", "min_volume_usd", "candidates", "validate_days", "min_trades"):
                if k in changes and changes[k] is not None:
                    self.config[k] = type(DEFAULT_CONFIG[k])(changes[k])
            if "strategies" in changes:
                s = [x for x in changes["strategies"] if x in APPROVED_PROFILES]
                if s:
                    self.config["strategies"] = s
            self.config["scan_hours"] = min(24, max(1, self.config["scan_hours"]))
            self.config["candidates"] = min(15, max(3, self.config["candidates"]))
            self.save()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="autopilot")
        self._thread.start()

    def scan_now(self):
        self._wake.set()

    def _loop(self):
        time.sleep(20)
        while True:
            self._wake.clear()          # a scan request that arrived meanwhile is served by this scan
            if self.config["enabled"]:
                try:
                    self.hunt(apply=True)
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
            board, signals, last_bar = [], [], 0
            for sym, vol in self._universe(cfg):
                try:
                    rows = self.hooks["ohlcv"](sym, "4h", self.HUNT_BARS)
                except Exception:
                    continue
                if len(rows) < 120:
                    continue
                last_bar = max(last_bar, rows[-1][0])
                m = momentum_score(rows[-120:])
                if m:
                    board.append({"symbol": sym, "volume_24h": round(vol), **m})
                for strat in cfg["strategies"]:
                    prof = APPROVED_PROFILES[strat]
                    try:
                        sig = self.hooks["signal"](strat, sym, prof["tf"], prof["params"], rows)
                    except Exception:
                        sig = None
                    if sig in ("BUY", "SELL"):
                        signals.append({"symbol": sym, "strategy": strat, "tf": prof["tf"], "side": sig,
                                        "params": prof["params"], "bar": rows[-1][0],
                                        "momentum": (m or {}).get("score", 0), "trend": (m or {}).get("direction")})
            board.sort(key=lambda x: -x["score"])
            fresh = [s for s in signals if s["bar"] == last_bar]
            for s in fresh:
                try:
                    e = self._edge(s["symbol"], s["strategy"], s["tf"], s["params"])
                except Exception as ex:
                    e = {"ok": False, "trades": 0, "avg_r": 0, "error": str(ex)[:80]}
                s["edge"] = e
                aligned = (s["side"] == "BUY" and s["trend"] == "UP") or (s["side"] == "SELL" and s["trend"] == "DOWN")
                s["rank"] = round(40 * min(1.0, max(-1.0, (e.get("avg_r") or 0) / 0.5)) + 0.4 * s["momentum"]
                                  + (15 if aligned else 0), 1)
                s["direction"] = "UP" if s["side"] == "BUY" else "DOWN"
                s["why"] = (f"{s['side']} signal on the {datetime.fromtimestamp(s['bar'] / 1000, tz=timezone.utc).strftime('%H:%M')} UTC "
                            f"candle; {s['strategy'].replace('_', ' ')} on this coin, last {e.get('days', 180)}d: "
                            f"{e.get('trades', 0)} trades, {(e.get('avg_r') or 0):+.2f}R avg"
                            + ("" if e.get("ok") else " — no edge here, skipped")
                            + (f"; momentum {s['momentum']:.0f} {s['trend']}" if s.get("trend") else ""))
            tradeable = sorted([s for s in fresh if s["edge"].get("ok")], key=lambda s: -s["rank"])
            new_candle = last_bar > int(self.state.get("hunted_bar") or 0)
            # the backtest enters right after the candle closes — never chase an old signal
            age_min = (time.time() * 1000 - (last_bar + 4 * 3600 * 1000)) / 60000 if last_bar else 0
            too_late = age_min > self.MAX_SIGNAL_AGE_MIN
            actions, active = [], []
            if apply and cfg["enabled"]:
                actions, active = self.hooks["open"](tradeable if (new_candle and not too_late) else [], cfg)
                if new_candle and too_late and tradeable:
                    actions.insert(0, f"Candle closed {age_min:.0f} min ago — too late to enter its signals, waiting for the next candle")
            with _lock:
                if apply and cfg["enabled"] and new_candle:
                    self.state["hunted_bar"] = last_bar
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
                          + ("" if new_candle or not (apply and cfg["enabled"]) else " — this candle was already traded"))
                for a in actions:
                    self._log(a)
                self.save()
            return {"signals": fresh, "tradeable": tradeable, "actions": actions}
        finally:
            with _lock:
                self.state["scanning"] = False
                self.save()

    # ── coin picker for one strategy (ALGO BOTS "AI picks the pair") ─────────
    PICK_DAYS = {"15m": 45, "1h": 120, "4h": 180, "1d": 365}

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

    def pick_for_strategy(self, strategy, tf, params, exclude=(), keep=None):
        """Best coin for this strategy right now: the top-momentum coins are
        each backtested with this strategy/timeframe/stop settings, ranked by
        momentum + backtest edge. `keep` (the bot's current coin) is kept while
        it is still one of the coins with an edge, so bots do not churn."""
        results = self.rank_for_strategy(strategy, tf, params, exclude, keep)
        if not results:
            return None
        kept = next((x for x in results if x["symbol"] == keep and x["edge"]), None)
        best = kept or results[0]
        return {**best, "kept": bool(kept), "tested": len(results),
                "alternatives": [x["symbol"] for x in results[1:4]]}

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
                    "profiles": APPROVED_PROFILES}
