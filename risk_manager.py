"""
Risk Manager — the one place every bot gets its trading balance from.

Bots never size themselves. Before any entry a bot asks approve(); the risk
manager answers with yes/no, the reason, and the capital/risk/leverage to
size with. Live capital is the Binance USDT-M wallet (optionally capped);
demo capital is a virtual wallet kept here.

Rules (all editable from the dashboard, saved in risk_manager.json):
  risk_pct            % of capital lost if a trade hits its stop      (1.0)
  daily_loss_pct      stop opening trades for the rest of the UTC day
                      once equity is this % below the day's start      (3.0)
  max_positions       open positions at once across all bots          (3)
  max_leverage        hard cap on leverage                             (10)
  capital_cap_usd     0 = use the whole wallet; >0 = never size from
                      more than this many USDT                         (0)
  demo_capital        starting balance of the demo wallet              (1000)
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

FILE = "risk_manager.json"

DEFAULTS = {
    "risk_pct": 1.0,
    "daily_loss_pct": 3.0,
    "max_positions": 3,
    "max_leverage": 10,
    "capital_cap_usd": 0.0,
    "demo_capital": 1000.0,
}
LIMITS = {   # sane bounds the dashboard cannot exceed
    "risk_pct": (0.1, 3.0),
    "daily_loss_pct": (0.5, 10.0),
    "max_positions": (1, 10),
    "max_leverage": (1, 20),
    "capital_cap_usd": (0.0, 1e9),
    "demo_capital": (10.0, 1e9),
}

_lock = threading.RLock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _blank_state(cfg):
    return {
        "demo_wallet": cfg["demo_capital"],
        "day": _today(),
        "day_start": {"live": None, "demo": cfg["demo_capital"]},
        "realized_today": {"live": 0.0, "demo": 0.0},
        "trades_today": {"live": 0, "demo": 0},
        "halted": {"live": False, "demo": False},
        "halt_reason": {"live": "", "demo": ""},
        "manual_halt": False,
        "events": [],
    }


class RiskManager:
    def __init__(self, path: str = FILE):
        self.path = path
        self.config = dict(DEFAULTS)
        self.state = _blank_state(self.config)
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.config.update({k: v for k, v in data.get("config", {}).items() if k in DEFAULTS})
            st = data.get("state") or {}
            base = _blank_state(self.config)
            base.update(st)
            self.state = base
        except Exception:
            pass

    def save(self):
        with _lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"config": self.config, "state": self.state}, f, indent=2)
            os.replace(tmp, self.path)

    def update_config(self, changes: dict) -> dict:
        with _lock:
            for k, v in changes.items():
                if k not in DEFAULTS or v is None:
                    continue
                lo, hi = LIMITS[k]
                v = type(DEFAULTS[k])(v)
                self.config[k] = min(hi, max(lo, v))
            if "demo_capital" in changes and not self._has_demo_history():
                self.state["demo_wallet"] = self.config["demo_capital"]
                self.state["day_start"]["demo"] = self.config["demo_capital"]
            self._log("config", f"Rules updated: {self._rules_text()}")
            self.save()
            return self.config

    def reset_demo_wallet(self):
        with _lock:
            self.state["demo_wallet"] = self.config["demo_capital"]
            self.state["day_start"]["demo"] = self.config["demo_capital"]
            self.state["realized_today"]["demo"] = 0.0
            self.state["halted"]["demo"] = False
            self.state["halt_reason"]["demo"] = ""
            self._log("demo", f"Demo wallet reset to ${self.config['demo_capital']:.2f}")
            self.save()

    def _has_demo_history(self):
        return abs(self.state["demo_wallet"] - self.config["demo_capital"]) > 1e-9

    # ── day roll / halts ─────────────────────────────────────────────────────
    def _roll_day(self, equity_now: dict):
        if self.state["day"] == _today():
            return
        self.state["day"] = _today()
        for m in ("live", "demo"):
            self.state["realized_today"][m] = 0.0
            self.state["trades_today"][m] = 0
            self.state["halted"][m] = False
            self.state["halt_reason"][m] = ""
            self.state["day_start"][m] = equity_now.get(m)
        self._log("day", "New UTC day — daily loss counters reset")

    def set_manual_halt(self, on: bool):
        with _lock:
            self.state["manual_halt"] = bool(on)
            self._log("halt", "Kill switch ON — no new trades" if on else "Kill switch OFF — trading allowed")
            self.save()

    def capital(self, mode: str, wallet_equity: float | None) -> float:
        if mode == "demo":
            return max(0.0, self.state["demo_wallet"])
        if wallet_equity is None:
            return 0.0
        cap = self.config["capital_cap_usd"]
        return min(wallet_equity, cap) if cap > 0 else wallet_equity

    def check_daily(self, mode: str, equity_now: float | None) -> bool:
        """Update the day's high-water start and halt if the loss limit is hit.
        Returns True when trading is halted for this mode."""
        with _lock:
            self._roll_day({mode: equity_now})
            if equity_now is None:
                return self.state["halted"][mode]
            if self.state["day_start"].get(mode) is None:
                self.state["day_start"][mode] = equity_now
                self.save()
            start = self.state["day_start"][mode] or equity_now
            if start > 0 and not self.state["halted"][mode]:
                loss_pct = (start - equity_now) / start * 100
                if loss_pct >= self.config["daily_loss_pct"]:
                    self.state["halted"][mode] = True
                    self.state["halt_reason"][mode] = (
                        f"Daily loss limit hit: -{loss_pct:.2f}% (limit {self.config['daily_loss_pct']}%). "
                        f"No new {mode} trades until 00:00 UTC.")
                    self._log("halt", self.state["halt_reason"][mode])
                    self.save()
            return self.state["halted"][mode]

    # ── the gate every entry goes through ────────────────────────────────────
    def approve(self, mode: str, open_positions: int, equity_now: float | None,
                requested_leverage: int) -> dict:
        with _lock:
            if self.state["manual_halt"]:
                return {"ok": False, "reason": "🛑 Kill switch is ON"}
            if self.check_daily(mode, equity_now):
                return {"ok": False, "reason": "🛑 " + self.state["halt_reason"][mode]}
            if open_positions >= self.config["max_positions"]:
                return {"ok": False, "reason": f"⏸ Risk manager: {open_positions}/{self.config['max_positions']} positions already open"}
            cap = self.capital(mode, equity_now)
            if cap <= 0:
                return {"ok": False, "reason": "⏸ Risk manager: no capital available"}
            lev = max(1, min(int(requested_leverage or 1), int(self.config["max_leverage"])))
            # Each slot may use at most its share of capital as margin.
            slot_capital = cap / self.config["max_positions"]
            return {
                "ok": True, "reason": "",
                "capital": cap,                     # risk is a % of the whole pot
                "risk_pct": self.config["risk_pct"],
                "leverage": lev,
                "max_notional": slot_capital * lev,
            }

    def record_open(self, mode: str, bot_label: str, side: str, qty: float, price: float, risk_usd: float):
        with _lock:
            self.state["trades_today"][mode] += 1
            self._log("open", f"{mode.upper()} {bot_label}: {side} {qty:g} @ {price:g} — risking ${risk_usd:.2f}")
            self.save()

    def record_close(self, mode: str, bot_label: str, pnl: float, reason: str):
        with _lock:
            self.state["realized_today"][mode] += pnl
            if mode == "demo":
                self.state["demo_wallet"] = round(self.state["demo_wallet"] + pnl, 6)
            self._log("close", f"{mode.upper()} {bot_label}: {reason} {'+' if pnl >= 0 else ''}{pnl:.2f} USDT")
            self.save()

    # ── reporting ────────────────────────────────────────────────────────────
    def snapshot(self, live_equity: float | None = None) -> dict:
        with _lock:
            self._roll_day({"live": live_equity, "demo": self.state["demo_wallet"]})
            out = {"config": dict(self.config), "limits": LIMITS, "day": self.state["day"],
                   "manual_halt": self.state["manual_halt"], "modes": {}}
            for m, eq in (("live", live_equity), ("demo", self.state["demo_wallet"])):
                start = self.state["day_start"].get(m)
                pnl_pct = ((eq - start) / start * 100) if (eq is not None and start) else 0.0
                out["modes"][m] = {
                    "equity": eq,
                    "capital": self.capital(m, eq),
                    "day_start": start,
                    "day_pnl_pct": round(pnl_pct, 3),
                    "realized_today": round(self.state["realized_today"][m], 4),
                    "trades_today": self.state["trades_today"][m],
                    "halted": self.state["halted"][m],
                    "halt_reason": self.state["halt_reason"][m],
                    "loss_budget_used_pct": round(max(0.0, -pnl_pct) / self.config["daily_loss_pct"] * 100, 1),
                }
            out["events"] = self.state["events"][-40:]
            return out

    def _rules_text(self):
        c = self.config
        return (f"risk {c['risk_pct']}%/trade, daily stop {c['daily_loss_pct']}%, "
                f"max {c['max_positions']} positions, max {c['max_leverage']}x")

    def _log(self, kind: str, msg: str):
        self.state["events"].append({"time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                                     "kind": kind, "msg": msg})
        self.state["events"] = self.state["events"][-200:]
