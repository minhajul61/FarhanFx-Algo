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
    "universe": 25,              # coins considered (by 24h volume)
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
            self.config["strategies"] = [s for s in self.config["strategies"] if s in APPROVED_PROFILES] \
                or list(APPROVED_PROFILES)
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
                    self.run_scan(apply=True)
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

    def _universe(self, cfg):
        tickers = self.hooks["tickers"]()
        rows = []
        for sym, t in tickers.items():
            if not sym.endswith("/USDT:USDT"):
                continue
            base = sym.split("/")[0]
            if base in EXCLUDE_BASES or base.startswith("1000000"):
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
