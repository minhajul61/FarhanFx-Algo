"""
AI Market Guard — the AI Brain's hands on the risk manager.

It watches the whole market every few minutes and can only make trading
SAFER, never riskier:

  shock pause     BTC moves >= 4% in 1h or >= 7% in 4h -> no new entries for
                  2 hours (stops slip badly in liquidation cascades)
  extreme mood    Fear & Greed <= 10 or >= 90 -> risk per trade x0.5
  crowded trade   funding >= +0.10% and the trade is a LONG (or <= -0.10%
                  and SHORT) -> that entry is skipped
  AI Brain view   the language model (when a key is set) may lower risk to
                  x0.5-x1.0 and name up to 5 coins to avoid, for 8 hours

If market data cannot be fetched the guard simply has no opinion — it never
blocks trading because a website was down, and it never raises risk.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone

FILE = "market_guard.json"

SHOCK_1H_PCT = 4.0
SHOCK_4H_PCT = 7.0
SHOCK_PAUSE_SECONDS = 2 * 3600
FG_EXTREME_LOW = 10
FG_EXTREME_HIGH = 90
FUNDING_CROWDED_PCT = 0.10
REFRESH_SECONDS = 300
STALE_SECONDS = 3600
LLM_TTL_SECONDS = 8 * 3600
LLM_MIN_MULT = 0.5
MAX_AVOID = 5


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M UTC")


def _base(sym: str) -> str:
    return str(sym).split("/")[0].replace("USDT", "").upper()


class MarketGuard:
    """hooks: btc_ohlcv() -> 1h candles, fear_greed() -> {score,label},
    funding() -> {"BTC": pct, ...}, now() -> unix time (tests)."""

    def __init__(self, hooks: dict, path: str = FILE):
        self.h = hooks
        self.path = path
        self._lock = threading.RLock()
        self._thread = None
        self.state = {
            "updated": 0.0, "btc": None, "fear_greed": None, "funding": {},
            "shock_until": 0.0, "shock_reason": "", "llm": None, "log": [], "errors": [],
        }
        self._load()

    # ── persistence ─────────────────────────────────────────────────────────
    def _now(self):
        return self.h.get("now", time.time)()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                saved = json.load(f)
            for k in ("shock_until", "shock_reason", "llm", "log"):
                if k in saved:
                    self.state[k] = saved[k]
        except Exception:
            pass

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({k: self.state[k] for k in ("shock_until", "shock_reason", "llm", "log")}, f, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def _log(self, msg: str):
        self.state["log"].append({"time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), "msg": msg})
        self.state["log"] = self.state["log"][-60:]

    # ── data refresh ────────────────────────────────────────────────────────
    def refresh(self):
        errors = []
        btc = fg = funding = None
        try:
            c = self.h["btc_ohlcv"]()
            if c and len(c) >= 26:
                closes = [float(x[4]) for x in c]
                last = closes[-1]
                btc = {
                    "price": last,
                    "chg_1h": round((last / closes[-2] - 1) * 100, 2),
                    "chg_4h": round((last / closes[-5] - 1) * 100, 2),
                    "chg_24h": round((last / closes[-25] - 1) * 100, 2),
                }
        except Exception as e:
            errors.append(f"BTC data: {str(e)[:80]}")
        try:
            fg = self.h["fear_greed"]() or None
            if fg and "score" not in fg:
                fg = None
        except Exception as e:
            errors.append(f"Fear & Greed: {str(e)[:80]}")
        try:
            funding = self.h["funding"]() or {}
        except Exception as e:
            errors.append(f"Funding: {str(e)[:80]}")
        with self._lock:
            now = self._now()
            if btc is not None:
                self.state["btc"] = btc
                shock = ""
                if abs(btc["chg_1h"]) >= SHOCK_1H_PCT:
                    shock = f"BTC {btc['chg_1h']:+.1f}% in 1h"
                elif abs(btc["chg_4h"]) >= SHOCK_4H_PCT:
                    shock = f"BTC {btc['chg_4h']:+.1f}% in 4h"
                if shock:
                    started = now >= self.state["shock_until"]
                    # stays paused for 2h after the LAST extreme reading
                    self.state["shock_until"] = now + SHOCK_PAUSE_SECONDS
                    self.state["shock_reason"] = shock
                    if started:
                        self._log(f"⚡ Shock: {shock} — new entries paused until {_utc(self.state['shock_until'])}")
            if fg is not None:
                self.state["fear_greed"] = fg
            if funding is not None:
                self.state["funding"] = funding
            if btc is not None or fg is not None:
                self.state["updated"] = now
            self.state["errors"] = errors
            self._save()

    def start(self):
        if self._thread and self._thread.is_alive():
            return

        def loop():
            while True:
                try:
                    self.refresh()
                except Exception as e:
                    print(f"[guard] refresh failed: {e}")
                time.sleep(REFRESH_SECONDS)

        self._thread = threading.Thread(target=loop, daemon=True, name="market-guard")
        self._thread.start()

    # ── AI Brain overlay ────────────────────────────────────────────────────
    def apply_llm(self, view: dict | None, source: str = "AI Brain"):
        """view = {"risk_multiplier": 0.5-1.0, "avoid_symbols": [...], "reason": "..."}.
        Anything that would raise risk is clamped away."""
        if not isinstance(view, dict):
            return None
        try:
            mult = float(view.get("risk_multiplier", 1.0))
        except (TypeError, ValueError):
            mult = 1.0
        mult = max(LLM_MIN_MULT, min(1.0, mult))
        avoid = [_base(s) for s in (view.get("avoid_symbols") or []) if str(s).strip()][:MAX_AVOID]
        reason = str(view.get("reason") or "")[:300]
        with self._lock:
            self.state["llm"] = {"risk_multiplier": round(mult, 2), "avoid": avoid, "reason": reason,
                                 "source": source, "expires": self._now() + LLM_TTL_SECONDS}
            self._log(f"🧠 {source}: risk x{mult:.2f}" + (f", avoid {', '.join(avoid)}" if avoid else "")
                      + (f" — {reason}" if reason else ""))
            self._save()
        return self.state["llm"]

    def _llm_active(self):
        llm = self.state.get("llm")
        if llm and llm.get("expires", 0) > self._now():
            return llm
        return None

    # ── decisions ───────────────────────────────────────────────────────────
    def _fresh(self):
        return self._now() - self.state.get("updated", 0) < STALE_SECONDS

    def risk_multiplier(self) -> tuple[float, list]:
        mult, why = 1.0, []
        if self._fresh():
            fg = self.state.get("fear_greed") or {}
            score = fg.get("score")
            if isinstance(score, (int, float)) and (score <= FG_EXTREME_LOW or score >= FG_EXTREME_HIGH):
                mult = min(mult, 0.5)
                why.append(f"Fear & Greed {score} ({fg.get('label', '')}) — risk halved")
        llm = self._llm_active()
        if llm and llm["risk_multiplier"] < 1.0:
            mult = min(mult, llm["risk_multiplier"])
            why.append(f"AI Brain: risk x{llm['risk_multiplier']:.2f}")
        return mult, why

    def entry_check(self, symbol: str, side: str) -> dict:
        """side is BUY/SELL (or buy/sell). Returns {ok, reason, risk_mult}."""
        with self._lock:
            now = self._now()
            if now < self.state.get("shock_until", 0):
                return {"ok": False, "risk_mult": 0.0,
                        "reason": f"⚡ AI Guard: {self.state['shock_reason']} — new entries paused until {_utc(self.state['shock_until'])}"}
            base = _base(symbol)
            llm = self._llm_active()
            if llm and base in llm.get("avoid", []):
                return {"ok": False, "risk_mult": 0.0,
                        "reason": f"🧠 AI Brain: avoiding {base} — {llm.get('reason') or 'flagged in the latest analysis'}"}
            long = str(side).lower() in ("buy", "long")
            f = (self.state.get("funding") or {}).get(base) if self._fresh() else None
            if isinstance(f, (int, float)):
                if long and f >= FUNDING_CROWDED_PCT:
                    return {"ok": False, "risk_mult": 0.0,
                            "reason": f"⚖ AI Guard: {base} longs are crowded (funding {f:+.3f}%) — skipping this LONG"}
                if not long and f <= -FUNDING_CROWDED_PCT:
                    return {"ok": False, "risk_mult": 0.0,
                            "reason": f"⚖ AI Guard: {base} shorts are crowded (funding {f:+.3f}%) — skipping this SHORT"}
            mult, why = self.risk_multiplier()
            return {"ok": True, "risk_mult": mult, "reason": "; ".join(why)}

    def snapshot(self, symbols=None) -> dict:
        with self._lock:
            now = self._now()
            mult, why = self.risk_multiplier()
            paused = now < self.state.get("shock_until", 0)
            llm = self._llm_active()
            fund = self.state.get("funding") or {}
            watch = sorted({_base(s) for s in (symbols or [])} | {"BTC", "ETH"})
            if paused:
                level, headline = "pause", f"Paused: {self.state['shock_reason']} — resumes {_utc(self.state['shock_until'])}"
            elif mult < 1.0:
                level, headline = "caution", "Caution: " + "; ".join(why)
            elif not self._fresh():
                level, headline = "unknown", "Waiting for market data"
            else:
                level, headline = "clear", "Market clear — normal risk"
            return {
                "level": level, "headline": headline, "paused": paused,
                "pause_until": self.state.get("shock_until") if paused else None,
                "risk_multiplier": mult, "reasons": why,
                "btc": self.state.get("btc"), "fear_greed": self.state.get("fear_greed"),
                "funding": {b: fund.get(b) for b in watch if b in fund},
                "crowded": [b for b, v in fund.items() if isinstance(v, (int, float)) and abs(v) >= FUNDING_CROWDED_PCT][:10],
                "llm": llm, "updated": self.state.get("updated"), "fresh": self._fresh(),
                "errors": self.state.get("errors", []), "log": self.state.get("log", [])[-20:],
                "rules": {"shock_1h_pct": SHOCK_1H_PCT, "shock_4h_pct": SHOCK_4H_PCT,
                          "fg_extreme": [FG_EXTREME_LOW, FG_EXTREME_HIGH], "funding_crowded_pct": FUNDING_CROWDED_PCT},
            }
