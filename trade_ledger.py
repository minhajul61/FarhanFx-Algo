"""
Trade ledger — one permanent record of every trade, whoever placed it.

Bots keep only their last 200 trades and lose them when deleted; the ledger
keeps everything so the Reports page can be built like an exchange
statement. Sources:
    autopilot   AI Autopilot bots
    bot         ALGO BOTS created by hand
    manual      Trade tab orders
    telegram    Telegram signal trades (live)
Telegram demo trades live in the Telegram bot's own state and are merged in
at report time.

Record (all times UTC "YYYY-MM-DD HH:MM:SS"):
    id, account, source, mode (live|demo), status (open|closed), bot_id,
    strategy, symbol, side (LONG|SHORT), qty, entry, exit, sl, tp, risk_usd,
    pnl (net of fees), fees, r (pnl / risk), open_time, close_time,
    duration_min, exit_reason
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

FILE = "trade_ledger.json"
TAKER_FEE = 0.0005


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse(ts):
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(str(ts)[:19], fmt)
        except ValueError:
            continue
    return None


class TradeLedger:
    def __init__(self, path: str = FILE):
        self.path = path
        self._lock = threading.RLock()
        self.rows: list[dict] = []
        self._index: dict[str, dict] = {}
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                self.rows = json.load(f)
        except Exception:
            self.rows = []
        self._index = {r["id"]: r for r in self.rows if r.get("id")}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.rows, f, separators=(",", ":"))
        os.replace(tmp, self.path)

    # ── writes ──────────────────────────────────────────────────────────────
    def open(self, rec_id: str, **fields) -> dict:
        with self._lock:
            if rec_id in self._index:
                return self._index[rec_id]
            rec = {"id": rec_id, "status": "open", "open_time": now_utc(), "exit": None, "pnl": None,
                   "fees": None, "r": None, "close_time": None, "duration_min": None, "exit_reason": None}
            rec.update({k: v for k, v in fields.items() if v is not None})
            if rec.get("side") in ("BUY", "buy", "long"):
                rec["side"] = "LONG"
            elif rec.get("side") in ("SELL", "sell", "short"):
                rec["side"] = "SHORT"
            self.rows.append(rec)
            self._index[rec_id] = rec
            self._save()
            return rec

    def close(self, rec_id: str, exit_price, pnl, reason, fees=None, close_time=None) -> dict | None:
        with self._lock:
            rec = self._index.get(rec_id)
            if not rec or rec.get("status") == "closed":
                return rec
            rec["status"] = "closed"
            rec["exit"] = round(float(exit_price), 8) if exit_price else None
            rec["pnl"] = round(float(pnl or 0), 4)
            rec["exit_reason"] = reason
            rec["close_time"] = close_time or now_utc()
            qty, entry = float(rec.get("qty") or 0), float(rec.get("entry") or 0)
            rec["fees"] = round(float(fees), 4) if fees is not None else \
                round(qty * (entry + float(exit_price or entry)) * TAKER_FEE, 4)
            risk = float(rec.get("risk_usd") or 0)
            rec["r"] = round(rec["pnl"] / risk, 3) if risk > 0 else None
            a, b = _parse(rec.get("open_time")), _parse(rec["close_time"])
            rec["duration_min"] = round((b - a).total_seconds() / 60, 1) if a and b else None
            self._save()
            return rec

    def add_closed(self, rec_id: str, **fields) -> bool:
        """Insert an already-closed trade (backfill). False if it exists."""
        with self._lock:
            if rec_id in self._index:
                return False
            rec = {"id": rec_id, "status": "closed"}
            rec.update(fields)
            if rec.get("side") in ("BUY", "buy"):
                rec["side"] = "LONG"
            elif rec.get("side") in ("SELL", "sell"):
                rec["side"] = "SHORT"
            risk = float(rec.get("risk_usd") or 0)
            if rec.get("r") is None and risk > 0 and rec.get("pnl") is not None:
                rec["r"] = round(float(rec["pnl"]) / risk, 3)
            a, b = _parse(rec.get("open_time")), _parse(rec.get("close_time"))
            if rec.get("duration_min") is None and a and b:
                rec["duration_min"] = round((b - a).total_seconds() / 60, 1)
            self.rows.append(rec)
            self._index[rec_id] = rec
            return True

    def flush(self):
        with self._lock:
            self._save()

    # ── reads ───────────────────────────────────────────────────────────────
    def get(self, rec_id):
        return self._index.get(rec_id)

    def open_records(self, **match):
        with self._lock:
            return [dict(r) for r in self.rows if r.get("status") == "open"
                    and all(r.get(k) == v for k, v in match.items())]

    def all(self):
        with self._lock:
            return [dict(r) for r in self.rows]


# ── analytics ─────────────────────────────────────────────────────────────────
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _group(trades, key):
    out = {}
    for t in trades:
        k = key(t)
        g = out.setdefault(k, {"key": k, "trades": 0, "wins": 0, "pnl": 0.0, "gross_win": 0.0, "gross_loss": 0.0, "rs": []})
        p = float(t.get("pnl") or 0)
        g["trades"] += 1
        g["pnl"] += p
        if p > 0:
            g["wins"] += 1
            g["gross_win"] += p
        elif p < 0:
            g["gross_loss"] += -p
        if t.get("r") is not None:
            g["rs"].append(float(t["r"]))
    rows = []
    for g in out.values():
        rows.append({
            "key": g["key"], "trades": g["trades"], "wins": g["wins"],
            "win_rate": round(g["wins"] / g["trades"] * 100, 1) if g["trades"] else None,
            "pnl": round(g["pnl"], 2),
            "profit_factor": round(g["gross_win"] / g["gross_loss"], 2) if g["gross_loss"] > 0 else (None if not g["gross_win"] else 99.0),
            "avg_r": round(sum(g["rs"]) / len(g["rs"]), 3) if g["rs"] else None,
        })
    rows.sort(key=lambda r: -r["pnl"])
    return rows


def analyse(trades: list) -> dict:
    """Exchange-style statistics for a list of CLOSED ledger records."""
    trades = sorted(trades, key=lambda t: t.get("close_time") or "")
    n = len(trades)
    pnls = [float(t.get("pnl") or 0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    rs = [float(t["r"]) for t in trades if t.get("r") is not None]
    durs = []
    for t in trades:
        if t.get("duration_min") is not None:
            durs.append(float(t["duration_min"]))
        else:
            a, b = _parse(t.get("open_time")), _parse(t.get("close_time"))
            if a and b:
                durs.append((b - a).total_seconds() / 60)

    curve, cum, peak, max_dd, max_dd_at = [], 0.0, 0.0, 0.0, None
    for t, p in zip(trades, pnls):
        cum += p
        peak = max(peak, cum)
        if peak - cum > max_dd:
            max_dd, max_dd_at = peak - cum, t.get("close_time")
        curve.append({"time": t.get("close_time"), "cum": round(cum, 2), "pnl": round(p, 2)})

    streak_w = streak_l = cur_w = cur_l = 0
    for p in pnls:
        if p > 0:
            cur_w, cur_l = cur_w + 1, 0
        elif p < 0:
            cur_l, cur_w = cur_l + 1, 0
        streak_w, streak_l = max(streak_w, cur_w), max(streak_l, cur_l)

    def side(s):
        ts = [t for t in trades if t.get("side") == s]
        ps = [float(t.get("pnl") or 0) for t in ts]
        return {"trades": len(ts), "wins": sum(1 for p in ps if p > 0), "pnl": round(sum(ps), 2),
                "win_rate": round(sum(1 for p in ps if p > 0) / len(ts) * 100, 1) if ts else None}

    daily = {}
    for t, p in zip(trades, pnls):
        d = str(t.get("close_time") or "")[:10]
        x = daily.setdefault(d, {"date": d, "pnl": 0.0, "trades": 0, "wins": 0})
        x["pnl"] += p
        x["trades"] += 1
        x["wins"] += 1 if p > 0 else 0
    daily_rows = [{**v, "pnl": round(v["pnl"], 2)} for v in sorted(daily.values(), key=lambda v: v["date"])]
    monthly = {}
    for d in daily_rows:
        m = monthly.setdefault(d["date"][:7], {"month": d["date"][:7], "pnl": 0.0, "trades": 0})
        m["pnl"] += d["pnl"]
        m["trades"] += d["trades"]
    best = max(trades, key=lambda t: float(t.get("pnl") or 0)) if trades else None
    worst = min(trades, key=lambda t: float(t.get("pnl") or 0)) if trades else None

    def wd(t):
        dt = _parse(t.get("close_time"))
        return WEEKDAYS[dt.weekday()] if dt else "?"

    def hr(t):
        dt = _parse(t.get("open_time")) or _parse(t.get("close_time"))
        return f"{dt.hour:02d}" if dt else "?"

    by_weekday = {r["key"]: r for r in _group(trades, wd)}
    by_hour = {r["key"]: r for r in _group(trades, hr)}
    return {
        "summary": {
            "trades": n, "wins": len(wins), "losses": len(losses), "breakeven": n - len(wins) - len(losses),
            "win_rate": round(len(wins) / n * 100, 1) if n else None,
            "net_pnl": round(sum(pnls), 2), "gross_profit": round(gross_win, 2), "gross_loss": round(gross_loss, 2),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (None if not gross_win else 99.0),
            "avg_win": round(gross_win / len(wins), 2) if wins else None,
            "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
            "payoff": round((gross_win / len(wins)) / (gross_loss / len(losses)), 2) if wins and losses else None,
            "expectancy": round(sum(pnls) / n, 2) if n else None,
            "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
            "fees": round(sum(float(t.get("fees") or 0) for t in trades), 2),
            "max_drawdown": round(max_dd, 2), "max_drawdown_at": max_dd_at,
            "best_trade": best, "worst_trade": worst,
            "max_win_streak": streak_w, "max_loss_streak": streak_l,
            "avg_duration_min": round(sum(durs) / len(durs), 1) if durs else None,
            "long": side("LONG"), "short": side("SHORT"),
            "first": trades[0].get("close_time") if trades else None,
            "last": trades[-1].get("close_time") if trades else None,
        },
        "equity_curve": curve,
        "daily": daily_rows,
        "monthly": [{**m, "pnl": round(m["pnl"], 2)} for m in sorted(monthly.values(), key=lambda m: m["month"])],
        "by_symbol": _group(trades, lambda t: str(t.get("symbol") or "?").split("/")[0]),
        "by_strategy": _group(trades, lambda t: t.get("strategy") or "—"),
        "by_source": _group(trades, lambda t: t.get("source") or "—"),
        "by_mode": _group(trades, lambda t: t.get("mode") or "—"),
        "by_exit": _group(trades, lambda t: t.get("exit_reason") or "—"),
        "by_weekday": [by_weekday.get(d, {"key": d, "trades": 0, "pnl": 0, "win_rate": None}) for d in WEEKDAYS],
        "by_hour": [by_hour.get(f"{h:02d}", {"key": f"{h:02d}", "trades": 0, "pnl": 0, "win_rate": None}) for h in range(24)],
    }
