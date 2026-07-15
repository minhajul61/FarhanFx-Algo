"""
FarhanFX AI Trading Brain  v4 — Internet Research + Auto-Implementation
------------------------------------------------------------------------
v4 upgrades over v3:
  • Live internet research via DuckDuckGo (no API key, free) — BTC/ETH/Gold news
  • Auto-implement learned rules: block_hours (bad UTC hours) + set_direction (BUY/SELL bias)
  • Server trading loop enforces blocked_hours + direction_bias — real rule enforcement
  • All prior v3 features: deep trade analysis, anti-oscillation, param bounds, Telegram learning

Runs every 4h. Groq llama-3.3-70b, 4000 tokens output.
"""

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    import requests as _requests
    _GROQ_OK = True
except ImportError:
    _GROQ_OK = False

# ── Config ────────────────────────────────────────────────────────────────────
BRAIN_FILE           = "brain_state.json"
BRAIN_CONFIG_FILE    = "brain_config.json"
BOTS_FILE            = "bots.json"
TG_DEMO_FILE         = "telegram_demo_state.json"
TG_SIGNALS_FILE      = "telegram_signals.json"
BRAIN_INTERVAL_HOURS = 4
BRAIN_MODEL          = "llama-3.3-70b-versatile"

MIN_TRADES_TO_JUDGE  = 8
PAUSE_WR_THRESHOLD   = 35
RESUME_WR_THRESHOLD  = 52
REWARD_WR_THRESHOLD  = 65
REWARD_PF_THRESHOLD  = 1.8

PARAM_BOUNDS = {
    "adx_min":         (0,   80),
    "rsi_ob":          (60,  95),
    "rsi_os":          (5,   40),
    "max_open_trades": (1,    5),
    "risk_pct":        (0.5,  5.0),
    "tp_atr":          (0.5,  6.0),
    "trailing_atr":    (0.5,  5.0),
    "bb_std":          (1.5,  3.5),
    "slow_ema":        (10,   50),
    "fast_ema":        (3,    20),
}

_brain_lock    = threading.Lock()
_brain_thread  = None
_api_key_cache = ""
_GROQ_API_URL  = "https://api.groq.com/openai/v1/chat/completions"


# ── API Key ───────────────────────────────────────────────────────────────────

def _get_api_key() -> str:
    if _api_key_cache:
        return _api_key_cache
    key = os.environ.get("GEMINI_API_KEY", "")
    if key:
        return key
    cfg_path = Path(BRAIN_CONFIG_FILE)
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            return cfg.get("gemini_api_key", "")
        except Exception:
            pass
    return ""


def set_api_key(key: str):
    global _api_key_cache
    _api_key_cache = key.strip()
    cfg = {}
    cfg_path = Path(BRAIN_CONFIG_FILE)
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    cfg["gemini_api_key"] = _api_key_cache
    with open(BRAIN_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    start()


# ── State I/O ─────────────────────────────────────────────────────────────────

def _load_state():
    if Path(BRAIN_FILE).exists():
        try:
            with open(BRAIN_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "last_run": None,
        "total_analyses": 0,
        "strategy_scores": {},
        "decisions": [],
        "journal": [],
        "param_history": {},
        "research_insights": [],   # cumulative cross-run learnings
    }


def _save_state(state):
    with open(BRAIN_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _load_bots():
    if Path(BOTS_FILE).exists():
        with open(BOTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_bots(bots):
    with open(BOTS_FILE, "w", encoding="utf-8") as f:
        json.dump(bots, f, indent=2, default=str)


# ── Anti-oscillation ──────────────────────────────────────────────────────────

def _is_oscillating(param_history, strat, param, new_val):
    hist = param_history.get(strat, {}).get(param, [])
    if len(hist) >= 2 and hist[-2] == new_val:
        return True
    return False


def _record_param_change(param_history, strat, param, new_val):
    if strat not in param_history:
        param_history[strat] = {}
    hist = param_history[strat].get(param, [])
    hist.append(new_val)
    param_history[strat][param] = hist[-4:]


# ── Deep Trade Analysis Helpers ───────────────────────────────────────────────

def _time_of_day_analysis(closed_trades):
    """Win rate by 6-hour time buckets."""
    buckets = {"00-06 UTC": [], "06-12 UTC": [], "12-18 UTC": [], "18-24 UTC": []}
    for t in closed_trades:
        ts = t.get("time", "")
        try:
            hour = int(str(ts).split(" ")[1].split(":")[0])
        except Exception:
            continue
        if hour < 6:    buckets["00-06 UTC"].append(t.get("pnl", 0))
        elif hour < 12: buckets["06-12 UTC"].append(t.get("pnl", 0))
        elif hour < 18: buckets["12-18 UTC"].append(t.get("pnl", 0))
        else:           buckets["18-24 UTC"].append(t.get("pnl", 0))
    result = {}
    for k, pnls in buckets.items():
        if pnls:
            wins = sum(1 for p in pnls if p > 0)
            result[k] = {
                "trades": len(pnls),
                "wr":     round(wins / len(pnls) * 100, 1),
                "pnl":    round(sum(pnls), 2),
            }
    return result


def _exit_reason_analysis(closed_trades):
    """Break down performance by exit reason (tp/sl/signal/etc.)."""
    reasons = {}
    for t in closed_trades:
        r = (t.get("exit_reason") or "unknown").lower()
        if r not in reasons:
            reasons[r] = {"count": 0, "wins": 0, "pnl": 0.0}
        reasons[r]["count"] += 1
        if t.get("pnl", 0) > 0:
            reasons[r]["wins"] += 1
        reasons[r]["pnl"] += t.get("pnl", 0)
    return {
        k: {
            "count": v["count"],
            "wr":    round(v["wins"] / v["count"] * 100, 1) if v["count"] else 0,
            "pnl":   round(v["pnl"], 2),
        }
        for k, v in reasons.items()
    }


def _direction_analysis(closed_trades):
    """Win rate split by BUY vs SELL signals."""
    dirs = {"BUY": {"count": 0, "wins": 0, "pnl": 0.0},
            "SELL": {"count": 0, "wins": 0, "pnl": 0.0}}
    for t in closed_trades:
        d = (t.get("signal") or t.get("side") or "").upper()
        if d not in dirs:
            continue
        dirs[d]["count"] += 1
        if t.get("pnl", 0) > 0:
            dirs[d]["wins"] += 1
        dirs[d]["pnl"] += t.get("pnl", 0)
    return {
        k: {
            "count": v["count"],
            "wr":    round(v["wins"] / v["count"] * 100, 1) if v["count"] else 0,
            "pnl":   round(v["pnl"], 2),
        }
        for k, v in dirs.items() if v["count"] > 0
    }


def _load_telegram_trades():
    """Load closed trades from Telegram Signal Bot demo state."""
    path = Path(TG_DEMO_FILE)
    if not path.exists():
        return [], {}
    try:
        st     = json.loads(path.read_text(encoding="utf-8"))
        trades = st.get("trades", [])
        equity = st.get("equity", 1000)
        open_p = len(st.get("positions", {}))
        return trades, {"equity": equity, "open_positions": open_p}
    except Exception:
        return [], {}


def _fetch_market_context():
    """Pull live Binance futures data — 100% free, no API key needed.
    Returns funding rate, long/short ratio, open interest for BTC/ETH/SOL."""
    base    = "https://fapi.binance.com"
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    ctx     = {}
    for sym in symbols:
        short = sym.replace("USDT", "")
        try:
            # Funding rate + mark price in one call
            pr = _requests.get(
                f"{base}/fapi/v1/premiumIndex?symbol={sym}", timeout=8
            ).json()
            funding_pct = round(float(pr.get("lastFundingRate", 0)) * 100, 5)
            price       = round(float(pr.get("markPrice", 0)), 2)

            # Open Interest (current)
            oi_r  = _requests.get(
                f"{base}/fapi/v1/openInterest?symbol={sym}", timeout=8
            ).json()
            oi_now = round(float(oi_r.get("openInterest", 0)), 2)

            # OI history — 5h trend (5 × 1h buckets)
            oi_h = _requests.get(
                f"{base}/futures/data/openInterestHist"
                f"?symbol={sym}&period=1h&limit=6", timeout=8
            ).json()
            oi_change_pct = 0.0
            if isinstance(oi_h, list) and len(oi_h) >= 2:
                old = float(oi_h[0]["sumOpenInterest"])
                new = float(oi_h[-1]["sumOpenInterest"])
                oi_change_pct = round((new - old) / old * 100, 2) if old else 0

            # Global long/short account ratio
            ls = _requests.get(
                f"{base}/futures/data/globalLongShortAccountRatio"
                f"?symbol={sym}&period=1h&limit=1", timeout=8
            ).json()
            if isinstance(ls, list) and ls:
                ls_ratio  = round(float(ls[0].get("longShortRatio", 1.0)), 3)
                long_pct  = round(float(ls[0].get("longAccount",  0.5)) * 100, 1)
            else:
                ls_ratio, long_pct = 1.0, 50.0

            # Derive a simple signal from funding + L/S
            if funding_pct >  0.03:
                signal = "⚠️ OVER-LONG  (short squeeze risk)"
            elif funding_pct < -0.01:
                signal = "⚠️ OVER-SHORT (long squeeze risk)"
            elif ls_ratio > 1.5:
                signal = "⚡ HEAVY LONG  bias"
            elif ls_ratio < 0.75:
                signal = "⚡ HEAVY SHORT bias"
            else:
                signal = "✅ BALANCED"

            ctx[short] = {
                "price":           price,
                "funding_pct":     funding_pct,
                "oi":              oi_now,
                "oi_5h_chg":       oi_change_pct,
                "ls_ratio":        ls_ratio,
                "long_pct":        long_pct,
                "signal":          signal,
            }
        except Exception as exc:
            ctx[short] = {"error": str(exc)[:80]}

    return ctx


def _fetch_pair_volumes():
    """Fetch 24h volume + volatility for major Binance USDT perp pairs."""
    CANDIDATES = [
        "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
        "DOGEUSDT","ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT",
        "LTCUSDT","MATICUSDT","ATOMUSDT","UNIUSDT","APTUSDT",
        "OPUSDT","ARBUSDT","SUIUSDT","NEARUSDT","FILUSDT",
        "TIAUSDT","INJUSDT","STXUSDT","LDOUSDT","JUPUSDT",
    ]
    try:
        tickers = _requests.get(
            "https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=10
        ).json()
        result = {}
        for t in tickers:
            sym = t.get("symbol", "")
            if sym not in CANDIDATES:
                continue
            price  = float(t.get("lastPrice", 0))
            high   = float(t.get("highPrice", 0))
            low    = float(t.get("lowPrice",  0))
            vol_m  = round(float(t.get("quoteVolume", 0)) / 1_000_000, 1)
            chg    = round(float(t.get("priceChangePercent", 0)), 2)
            volat  = round((high - low) / price * 100, 2) if price > 0 else 0
            pair   = sym.replace("USDT", "/USDT:USDT")
            result[pair] = {"vol_m": vol_m, "volatility_pct": volat, "chg_24h": chg}
        return dict(sorted(result.items(), key=lambda x: x[1]["vol_m"], reverse=True))
    except Exception as exc:
        return {"error": str(exc)[:80]}


def _format_pair_volumes(pairs: dict) -> str:
    if not pairs or "error" in pairs:
        return "[PAIRS] No data."
    lines = ["Pair                   | Vol(M$) | Volatility | 24h Chg"]
    lines.append("-" * 58)
    for pair, d in list(pairs.items())[:20]:
        lines.append(
            f"{pair:<22} | ${d['vol_m']:>6,.0f}M | {d['volatility_pct']:>5.1f}%     | {d['chg_24h']:+.2f}%"
        )
    return "\n".join(lines)


def _format_market_ctx(ctx: dict) -> str:
    """Format market context dict into a readable prompt string."""
    if not ctx:
        return "[MARKET CTX] No data fetched."
    lines = []
    for sym, d in ctx.items():
        if "error" in d:
            lines.append(f"{sym}: ERROR — {d['error']}")
            continue
        lines.append(
            f"{sym} @ ${d['price']:,.2f} | "
            f"Funding={d['funding_pct']:+.4f}% | "
            f"OI={d['oi']:,.0f} ({d['oi_5h_chg']:+.1f}% 5h) | "
            f"L/S ratio={d['ls_ratio']} (long={d['long_pct']}%) | "
            f"{d['signal']}"
        )
    return "\n".join(lines)


def _web_research():
    """Live market intelligence from DuckDuckGo — no API key, completely free."""
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        return "[WEB] duckduckgo_search not installed — run: pip install duckduckgo-search"

    month_year = datetime.now().strftime("%B %Y")
    queries = [
        f"BTC ETH crypto market trend analysis {month_year}",
        f"gold XAU USD technical analysis forecast {month_year}",
        "crypto trading trending ranging market regime scalping strategy",
    ]
    snippets = []
    try:
        with DDGS() as ddgs:
            for q in queries:
                try:
                    results = list(ddgs.text(q, max_results=2))
                    for r in results:
                        body  = (r.get("body")  or "")[:200]
                        title = (r.get("title") or "")[:70]
                        if body:
                            snippets.append(f"• [{title}] {body}")
                except Exception:
                    continue
    except Exception as e:
        return f"[WEB] Search failed: {e}"

    return "\n".join(snippets[:6]) if snippets else "[WEB] No results returned."


# ── Full Metrics Calculator ───────────────────────────────────────────────────

def calculate_metrics(bots):
    """Deep per-strategy metrics including time/direction/exit analysis."""
    metrics = {}
    for bot in bots.values():
        strat = bot.get("strategy")
        if not strat:
            continue
        trades = bot.get("trades", [])
        closed = [t for t in trades if t.get("status") == "closed"]
        open_t = [t for t in trades if t.get("status") == "open"]
        wins   = [t for t in closed if t.get("pnl", 0) > 0]
        losses = [t for t in closed if t.get("pnl", 0) < 0]
        pnl_sum = sum(t.get("pnl", 0) for t in closed)
        win_sum = sum(t.get("pnl", 0) for t in wins)
        los_sum = abs(sum(t.get("pnl", 0) for t in losses))
        pf      = round(win_sum / los_sum, 2) if los_sum > 0 else (9.99 if win_sum > 0 else 0)
        wr      = round(len(wins) / len(closed) * 100, 1) if closed else 0
        equity  = bot.get("demo_equity", bot.get("demo_balance", 5000))

        # Consecutive loss streak
        recent8 = sorted(closed, key=lambda x: x.get("time", ""), reverse=True)[:8]
        streak  = 0
        for t in recent8:
            if t.get("pnl", 0) < 0:
                streak += 1
            else:
                break

        # Best and worst single trades
        best  = max(closed, key=lambda x: x.get("pnl", 0), default=None)
        worst = min(closed, key=lambda x: x.get("pnl", 0), default=None)

        if strat not in metrics:
            metrics[strat] = {
                "strategy": strat, "symbol": bot.get("symbol", ""),
                "timeframe": bot.get("timeframe", ""),
                "total_closed": 0, "total_open": 0,
                "wins": 0, "losses": 0,
                "win_rate": 0, "profit_factor": 0,
                "total_pnl": 0, "equity": equity,
                "recent_trades": [],
                "all_closed_trades": [],
                "bot_status": bot.get("status", "active"),
                "loss_streak": streak,
                "current_params": {},
                "best_trade": None,
                "worst_trade": None,
                "time_analysis": {},
                "exit_analysis": {},
                "direction_analysis": {},
                "blocked_hours":  bot.get("blocked_hours", []),
                "direction_bias": bot.get("direction_bias", "both"),
            }

        m = metrics[strat]
        m["total_closed"]     += len(closed)
        m["total_open"]       += len(open_t)
        m["wins"]             += len(wins)
        m["losses"]           += len(losses)
        m["total_pnl"]         = round(m["total_pnl"] + pnl_sum, 2)
        m["equity"]            = round(equity, 2)
        m["profit_factor"]     = pf
        m["loss_streak"]       = max(m["loss_streak"], streak)
        m["all_closed_trades"] += closed
        if m["total_closed"] > 0:
            m["win_rate"] = round(m["wins"] / m["total_closed"] * 100, 1)
        if best  and (m["best_trade"]  is None or best["pnl"]  > m["best_trade"]["pnl"]):
            m["best_trade"]  = best
        if worst and (m["worst_trade"] is None or worst["pnl"] < m["worst_trade"]["pnl"]):
            m["worst_trade"] = worst

        for p in PARAM_BOUNDS:
            if p in bot:
                m["current_params"][p] = bot[p]

        recent = sorted(closed, key=lambda x: x.get("time", ""), reverse=True)[:8]
        m["recent_trades"] = [
            {"time": t.get("time"), "side": t.get("signal") or t.get("side"),
             "pnl": round(t.get("pnl", 0), 4),
             "exit": t.get("exit_reason", "?")}
            for t in recent
        ]

    # Post-process deep analyses
    for strat, m in metrics.items():
        all_c = m.pop("all_closed_trades", [])
        if all_c:
            m["time_analysis"]      = _time_of_day_analysis(all_c)
            m["exit_analysis"]      = _exit_reason_analysis(all_c)
            m["direction_analysis"] = _direction_analysis(all_c)

    return metrics


# ── Prompt Builder ────────────────────────────────────────────────────────────

def _build_prompt(metrics, state, tg_trades, tg_stats, web_research="", market_ctx=None, pair_volumes=None):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── Strategy blocks ──
    strat_lines = []
    for strat, m in sorted(metrics.items(), key=lambda x: x[1]["total_pnl"], reverse=True):
        if m["total_closed"] == 0:
            tag = "NEW"
        elif m["win_rate"] >= 60:
            tag = "GOOD"
        elif m["win_rate"] >= 45:
            tag = "WATCH"
        else:
            tag = "POOR"

        streak_flag = f" ⚠️STREAK={m['loss_streak']}" if m["loss_streak"] >= 3 else ""
        block = [
            f"▶ {strat} [{tag}]{streak_flag} | {m['timeframe']} | status={m['bot_status']}"
            f" | trades={m['total_closed']} | WR={m['win_rate']}% | PF={m['profit_factor']}"
            f" | PnL=${m['total_pnl']:+.2f}"
        ]

        # Current params
        pkeys = ["adx_min","rsi_ob","rsi_os","max_open_trades","risk_pct","tp_atr","trailing_atr"]
        pstr  = " | ".join(f"{k}={v}" for k, v in m["current_params"].items() if k in pkeys)
        if pstr:
            block.append(f"  params: {pstr}")

        # Brain-enforced rules (already implemented — do NOT re-implement)
        if m.get("blocked_hours"):
            block.append(f"  ✅ BRAIN RULE: blocked_hours={m['blocked_hours']} UTC (already set)")
        if m.get("direction_bias", "both") != "both":
            block.append(f"  ✅ BRAIN RULE: direction_bias={m['direction_bias']} (already set)")

        # Recent trades (W/L sequence)
        if m["recent_trades"]:
            seq = " ".join(
                f"{'W' if t['pnl'] > 0 else 'L'}${t['pnl']:+.2f}[{t['exit'] or '?'}]"
                for t in m["recent_trades"]
            )
            block.append(f"  recent: {seq}")

        # Time analysis
        if m["time_analysis"]:
            t_str = " | ".join(
                f"{k}: {v['trades']}trades WR={v['wr']}% PnL=${v['pnl']:+.2f}"
                for k, v in m["time_analysis"].items()
            )
            block.append(f"  time:   {t_str}")

        # Direction analysis
        if m["direction_analysis"]:
            d_str = " | ".join(
                f"{k}: {v['count']}t WR={v['wr']}% PnL=${v['pnl']:+.2f}"
                for k, v in m["direction_analysis"].items()
            )
            block.append(f"  dir:    {d_str}")

        # Exit analysis
        if m["exit_analysis"]:
            e_str = " | ".join(
                f"{k}: {v['count']}t WR={v['wr']}%"
                for k, v in m["exit_analysis"].items()
            )
            block.append(f"  exits:  {e_str}")

        # Best/worst trade
        if m["best_trade"]:
            block.append(f"  best trade:  +${m['best_trade'].get('pnl',0):.2f} @ {m['best_trade'].get('time','?')}")
        if m["worst_trade"]:
            block.append(f"  worst trade: ${m['worst_trade'].get('pnl',0):+.2f} @ {m['worst_trade'].get('time','?')}")

        strat_lines.append("\n".join(block))

    # ── Telegram section ──
    tg_section = "No Telegram trades yet."
    if tg_trades:
        tg_closed = [t for t in tg_trades if t.get("status") == "closed" or "exit" in t]
        if tg_closed:
            tg_wins = [t for t in tg_closed if t.get("pnl", 0) > 0]
            tg_pnl  = round(sum(t.get("pnl", 0) for t in tg_closed), 2)
            tg_wr   = round(len(tg_wins) / len(tg_closed) * 100, 1) if tg_closed else 0
            tg_time = _time_of_day_analysis(tg_closed)
            tg_dir  = _direction_analysis(tg_closed)
            tg_seq  = " ".join(
                f"{'W' if t.get('pnl',0)>0 else 'L'}${t.get('pnl',0):+.2f}[{t.get('symbol','?').split('/')[0]}]"
                for t in sorted(tg_closed, key=lambda x: x.get("time",""), reverse=True)[:10]
            )
            t_str = " | ".join(
                f"{k}: {v['trades']}t WR={v['wr']}%"
                for k, v in tg_time.items()
            ) if tg_time else "insufficient data"
            d_str = " | ".join(
                f"{k}: WR={v['wr']}% PnL=${v['pnl']:+.2f}"
                for k, v in tg_dir.items()
            ) if tg_dir else "n/a"
            tg_section = (
                f"Closed: {len(tg_closed)} | WR: {tg_wr}% | Net PnL: ${tg_pnl:+.2f}"
                f" | Open positions: {tg_stats.get('open_positions', 0)}"
                f"\nRecent: {tg_seq}"
                f"\nTime:   {t_str}"
                f"\nDir:    {d_str}"
            )
        else:
            tg_section = f"Open positions: {tg_stats.get('open_positions', 0)} | No closed trades yet."

    # ── Previous decisions ──
    prev_decisions = "None yet."
    if state.get("decisions"):
        prev_decisions = "\n".join(
            f"- {d['time']}: {d['action']} on {d['strategy']} — {d['reason'][:100]}"
            for d in state["decisions"][-10:]
        )

    # ── Research insights ──
    insights = state.get("research_insights", [])
    insights_text = "None yet." if not insights else "\n".join(
        f"[{i.get('time','')}] {i.get('insight','')}"
        for i in insights[-8:]
    )

    # ── Previous journal ──
    prev_learning = "None yet."
    if state.get("journal"):
        last = state["journal"][-1]
        prev_learning = f"[{last.get('time','')}] {last.get('overall','')[:200]}"

    # ── Parameter bounds info ──
    bounds_info = " | ".join(f"{p}:[{b[0]},{b[1]}]" for p, b in PARAM_BOUNDS.items())

    web_section  = web_research if web_research else "[WEB] No research available this run."
    mkt_section  = _format_market_ctx(market_ctx) if market_ctx else "[MARKET] No data fetched."
    pair_section = _format_pair_volumes(pair_volumes) if pair_volumes else "[PAIRS] No data fetched."

    return f"""You are FarhanFX AI Trading Brain v4 — expert self-learning algorithmic trading analyst with internet research capability.
Your mission: deeply analyze ALL trade data + live market intel, find patterns, learn, and auto-implement smart fixes.

DATE: {now_str}

════════════════════════════════════════════
CRYPTO ALGO BOT PERFORMANCE (deep analysis)
════════════════════════════════════════════
{chr(10).join(strat_lines)}

════════════════════════════════════════════
BINANCE PERP PAIRS — 24H VOLUME + VOLATILITY
════════════════════════════════════════════
{pair_section}

PAIR SELECTION GUIDE (for assign_pair action):
• ICT/Smart Money (bos_choch, ob_fvg, liquidity_sweep, silver_bullet, fvg, ifvg, bpr): HIGH volatility + HIGH volume (BTC, ETH, SOL, BNB)
• Oscillators (rsi, rsi_divergence, macd_cross, vwap_rsi, bb_rsi_strict, vwap_bands): MEDIUM-HIGH volume, steady price action
• Breakout (super_breakout, false_breakout, orb, trend_breakout): HIGH volatility + momentum (AVAX, LINK, APT, etc.)
• funding_rate: needs active futures market with meaningful funding (ETH, BNB, SOL all valid)
• RULE: Each strategy MUST get a UNIQUE pair. No two strategies on same pair. Pick from the list above.

════════════════════════════════════════════
LIVE MARKET CONTEXT (Binance real-time data)
════════════════════════════════════════════
{mkt_section}

Rules for market context:
• Funding > +0.03%  → market over-leveraged LONG → caution adding longs; consider set_direction=sell_only for trend bots
• Funding < -0.01%  → market over-leveraged SHORT → short squeeze risk; consider set_direction=buy_only
• L/S ratio > 1.5   → retail longs crowded → mean-reversion opportunity, avoid pure trend-follow longs
• L/S ratio < 0.75  → retail shorts crowded → short squeeze coming, avoid trend-follow shorts
• OI 5h change >+5% → hot market, volatility spiking → tighten risk_pct or max_open_trades
• OI 5h change <-5% → mass liquidation/exits → extra caution, may be false signal period

════════════════════════════════════════════
LIVE MARKET INTELLIGENCE (internet research)
════════════════════════════════════════════
{web_section}

════════════════════════════════════════════
TELEGRAM SIGNAL BOT PERFORMANCE
════════════════════════════════════════════
{tg_section}

════════════════════════════════════════════
PREVIOUS DECISIONS (last 10)
════════════════════════════════════════════
{prev_decisions}

════════════════════════════════════════════
ACCUMULATED RESEARCH INSIGHTS (cross-run learning)
════════════════════════════════════════════
{insights_text}

════════════════════════════════════════════
PREVIOUS RUN SUMMARY
════════════════════════════════════════════
{prev_learning}

════════════════════════════════════════════
TUNABLE PARAMETER BOUNDS
════════════════════════════════════════════
{bounds_info}
Key parameters:
adx_min=trend strength filter | rsi_ob/os=RSI threshold | max_open_trades=concurrent positions
risk_pct=% balance per trade | tp_atr=TP in ATR units | trailing_atr=trailing stop ATR
bb_std=BB width | slow_ema/fast_ema=EMA crossover periods

════════════════════════════════════════════
YOUR ANALYSIS RULES
════════════════════════════════════════════
1. DEEP PATTERN ANALYSIS: Look at time analysis (best/worst hours), direction (BUY vs SELL bias), exit reasons (TP vs SL hit rate), trade sequences (W/L streaks)
2. PAUSE: trades>={MIN_TRADES_TO_JUDGE} AND WR<{PAUSE_WR_THRESHOLD}% AND PnL<-$20 AND streak>=3 → pause
3. REWARD: WR>={REWARD_WR_THRESHOLD}% AND PF>={REWARD_PF_THRESHOLD} → increase risk_pct +0.5 OR max_open_trades +1
4. RESEARCH: Note key patterns/insights for future runs (e.g. "rsi wins in 06-12 UTC but loses in 18-24 UTC")
5. ADJUST: 1-2 params max per strategy. Never reverse a decision from last 2 runs (check previous decisions)
6. NEW bots (0 trades): action="none" always
7. LOSS_STREAK>=4: must act immediately
8. Telegram: if channel has pattern (BUY bias, specific hours work), note as research insight
9. Anti-oscillation: if you adjusted a param 2 runs ago to X, do NOT set it back to the old value
10. BLOCK HOURS (v4 new!): if time_analysis shows WR<35% in a UTC bucket, action="block_hours" with action_detail="hours=[11,12] reason=..." — bot will skip signals in those hours
11. SET DIRECTION (v4 new!): if direction_analysis shows one side dominates (gap >30% WR), action="set_direction" with action_detail="direction=buy_only reason=..." — valid values: buy_only, sell_only, both
12. NEVER re-implement rules already shown as ✅ BRAIN RULE above. Only add new rules or expand existing ones.
13. USE INTERNET DATA: Cross-reference live market intelligence (above) with trade patterns. If web shows BTC is ranging → adjust strategies accordingly. Note web-derived insights in research_insights.
14. ASSIGN_PAIR (MANDATORY every run): For EVERY strategy, pick the optimal unique pair from the BINANCE PAIRS table above.
    Use action="assign_pair" with action_detail="pair=SOL/USDT:USDT reason=high volatility suits ICT kill zone"
    RULES for assign_pair:
    - Each strategy gets a DIFFERENT pair — no duplicates across all strategies
    - Pick pairs with highest daily volume ($500M+) for ICT strategies
    - ICT (bos_choch, ob_fvg, liquidity_sweep, silver_bullet) → BTC/ETH/SOL/BNB only
    - Breakout/trend (super_breakout, orb, trend_breakout) → AVAX/LINK/APT/OP/ARB
    - Oscillators (rsi, macd_cross, vwap*) → mid-cap coins LINK/DOT/LTC/ATOM/UNI
    - If strategy already has a good pair with no issues, still include it with current pair (no unnecessary change)
    - TLM and DRAM bots: leave them as-is (they have fixed pairs)

Respond ONLY with valid JSON:
{{
  "overall_assessment": "4-5 sentence deep portfolio analysis with specific pattern findings",
  "market_insight": "What the collective win/loss patterns reveal about current market regime",
  "strategy_analysis": [
    {{
      "strategy": "name",
      "verdict": "good|watch|poor|new",
      "deep_insight": "Specific findings from time/direction/exit analysis — be analytical and precise",
      "action": "none|pause|adjust_param|resume|reward|research|block_hours|unblock_hours|set_direction|assign_pair",
      "action_detail": "e.g. adx_min=35 | hours=[11,12] | direction=buy_only | pair=SOL/USDT:USDT reason=...",
      "learning": "One specific learnable pattern from this strategy's data"
    }}
  ],
  "telegram_analysis": {{
    "signal_quality": "good|poor|insufficient_data",
    "key_finding": "What the Telegram signal data reveals",
    "recommendation": "What to do with TG signal bot settings"
  }},
  "research_insights": [
    "Specific insight to remember for future runs — e.g. hour patterns, direction bias, market regime observations"
  ],
  "top_performers": ["strategy1"],
  "underperformers": ["strategy2"],
  "key_learnings": ["learning1", "learning2", "learning3"],
  "next_check_focus": "Specific thing to monitor in next 4h check"
}}"""


# ── Decision Executor ─────────────────────────────────────────────────────────

def _execute(analysis, bots, state, now):
    made          = []
    param_history = state.setdefault("param_history", {})
    research_list = state.setdefault("research_insights", [])

    adjustable = {
        "max_open_trades": int,   "adx_min": int,
        "rsi_ob":          int,   "rsi_os":  int,
        "risk_pct":        float, "tp_atr":  float,
        "trailing_atr":    float, "bb_std":  float,
        "slow_ema":        int,   "fast_ema": int,
    }

    for sa in analysis.get("strategy_analysis", []):
        strat  = sa.get("strategy")
        action = sa.get("action", "none")
        detail = sa.get("action_detail", "")
        reason = sa.get("deep_insight", sa.get("insight", ""))[:200]

        if action == "pause":
            for bot in bots.values():
                if bot.get("strategy") == strat and bot.get("status") == "active":
                    bot["status"]     = "paused"
                    bot["last_error"] = f"🧠 Brain paused: {detail[:120]}"
                    made.append({"time": now, "strategy": strat,
                                 "action": "paused", "reason": reason,
                                 "ai_detail": detail[:200]})

        elif action == "resume":
            for bot in bots.values():
                if bot.get("strategy") == strat and bot.get("status") == "paused":
                    bot["status"]     = "active"
                    bot["last_error"] = None
                    made.append({"time": now, "strategy": strat,
                                 "action": "resumed", "reason": reason,
                                 "ai_detail": detail[:200]})

        elif action in ("adjust_param", "reward"):
            for param, cast in adjustable.items():
                m = re.search(rf"{param}[^\d\.\-]*([0-9]+\.?[0-9]*)", detail, re.IGNORECASE)
                if not m:
                    continue
                try:
                    new_val = cast(m.group(1))
                except Exception:
                    continue

                if param in PARAM_BOUNDS:
                    lo, hi  = PARAM_BOUNDS[param]
                    new_val = cast(max(lo, min(hi, new_val)))

                if _is_oscillating(param_history, strat, param, new_val):
                    print(f"[Brain] Skip oscillate: {strat}.{param} → {new_val}")
                    continue

                changed  = False
                old_val  = "?"
                for bot in bots.values():
                    if bot.get("strategy") == strat:
                        old_val = bot.get(param, "?")
                        if old_val != new_val:
                            bot[param] = new_val
                            changed    = True

                if changed:
                    _record_param_change(param_history, strat, param, new_val)
                    made.append({"time": now, "strategy": strat,
                                 "action": f"adjust_{param}",
                                 "reason": f"{param}: {old_val} → {new_val}",
                                 "ai_detail": detail[:200]})

        elif action == "research":
            insight_text = sa.get("learning", detail)
            if insight_text:
                research_list.append({
                    "time":     now,
                    "strategy": strat,
                    "insight":  insight_text[:300],
                })
                made.append({"time": now, "strategy": strat,
                             "action": "research_noted",
                             "reason": insight_text[:120],
                             "ai_detail": insight_text[:200]})

        elif action == "block_hours":
            # Parse hours list from action_detail, e.g. "hours=[11,12]" or "hours=11,12"
            m = re.search(r"hours[=:\s]*\[?([\d,\s]+)\]?", detail, re.IGNORECASE)
            if m:
                try:
                    hours = [int(h.strip()) for h in m.group(1).split(",") if h.strip().isdigit()]
                    if hours:
                        for bot in bots.values():
                            if bot.get("strategy") == strat:
                                existing  = bot.get("blocked_hours", [])
                                new_hours = sorted(set(existing + hours))
                                bot["blocked_hours"] = new_hours
                        made.append({"time": now, "strategy": strat,
                                     "action": f"block_hours:{hours}",
                                     "reason": f"UTC hours {hours} blocked — low WR detected",
                                     "ai_detail": detail[:200]})
                except Exception:
                    pass

        elif action == "unblock_hours":
            for bot in bots.values():
                if bot.get("strategy") == strat:
                    bot["blocked_hours"] = []
            made.append({"time": now, "strategy": strat,
                         "action": "unblock_hours",
                         "reason": "All hour blocks cleared",
                         "ai_detail": detail[:200]})

        elif action == "set_direction":
            m = re.search(r"direction[=:\s]*(\w+)", detail, re.IGNORECASE)
            if m:
                direction = m.group(1).lower()
                if direction in ("buy_only", "sell_only", "both"):
                    for bot in bots.values():
                        if bot.get("strategy") == strat:
                            bot["direction_bias"] = direction
                    made.append({"time": now, "strategy": strat,
                                 "action": f"set_direction:{direction}",
                                 "reason": f"Direction bias → {direction}",
                                 "ai_detail": detail[:200]})

        elif action == "assign_pair":
            m = re.search(r"pair[=:\s]*([A-Z0-9]+/USDT:USDT)", detail, re.IGNORECASE)
            if m:
                new_pair = m.group(1).upper()
                # Skip TLM/DRAM fixed bots
                if any(b.get("strategy") == strat and b.get("symbol") in ("TLM/USDT:USDT", "DRAM/USDT:USDT")
                       for b in bots.values()):
                    continue
                # Enforce uniqueness: if another strategy already claimed this pair this run, skip
                already_claimed = any(
                    d.get("action", "").startswith("assign_pair") and new_pair in d.get("reason", "")
                    for d in made
                )
                if already_claimed:
                    print(f"[Brain] Skip assign_pair {strat}: {new_pair} already claimed this run")
                    continue
                changed = False
                old_pair = "?"
                for bot in bots.values():
                    if bot.get("strategy") == strat and bot.get("symbol") not in ("TLM/USDT:USDT", "DRAM/USDT:USDT"):
                        old_pair = bot.get("symbol", "?")
                        if old_pair == new_pair:
                            # Already correct pair — still mark claimed so uniqueness check works
                            made.append({"time": now, "strategy": strat,
                                         "action": f"assign_pair:{new_pair}",
                                         "reason": f"Pair: {new_pair} (no change)",
                                         "ai_detail": detail[:200]})
                            break
                        bot["symbol"]           = new_pair
                        bot["open_side"]        = None
                        bot["open_entry_price"] = None
                        bot["open_trade_count"] = 0
                        bot["open_amount"]      = 0
                        bot["open_peak"]        = None
                        bot["open_trough"]      = None
                        bot["last_close_bar"]   = None
                        bot["last_error"]       = None
                        changed = True
                if changed:
                    made.append({"time": now, "strategy": strat,
                                 "action": f"assign_pair:{new_pair}",
                                 "reason": f"Pair: {old_pair} → {new_pair}",
                                 "ai_detail": detail[:200]})
                    print(f"[Brain] assign_pair: {strat} → {new_pair}")

    # Store global research insights from the AI response
    for insight in analysis.get("research_insights", []):
        if insight and len(insight) > 10:
            research_list.append({
                "time":     now,
                "strategy": "global",
                "insight":  str(insight)[:300],
            })

    # Keep only last 30 insights
    state["research_insights"] = research_list[-30:]

    return made


# ── Main Analysis Function ────────────────────────────────────────────────────

def run_analysis():
    if not _GROQ_OK:
        return {"error": "requests library not installed — run: pip install requests"}
    api_key = _get_api_key()
    if not api_key:
        return {"error": "Groq API key not set — enter it in the AI BRAIN tab"}

    with _brain_lock:
        try:
            state      = _load_state()
            bots       = _load_bots()
            if not bots:
                return {"error": "No bots found in bots.json"}

            metrics    = calculate_metrics(bots)
            tg_trades, tg_stats = _load_telegram_trades()
            print("[Brain] Fetching Binance market context...")
            market_ctx   = _fetch_market_context()
            print("[Brain] Fetching pair volumes...")
            pair_volumes = _fetch_pair_volumes()
            print("[Brain] Running live web research...")
            web_research = _web_research()
            prompt       = _build_prompt(metrics, state, tg_trades, tg_stats, web_research, market_ctx, pair_volumes)
            state["last_market_ctx"] = market_ctx

            resp = _requests.post(
                _GROQ_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":       BRAIN_MODEL,
                    "messages":    [{"role": "user", "content": prompt}],
                    "max_tokens":  4000,
                    "temperature": 0.2,
                },
                timeout=120,
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"].strip()

            json_match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not json_match:
                return {"error": f"Could not parse AI response: {raw[:200]}"}
            analysis = json.loads(json_match.group())

            now       = datetime.now().strftime("%Y-%m-%d %H:%M")
            decisions = _execute(analysis, bots, state, now)
            _save_bots(bots)

            state["last_run"]       = now
            state["total_analyses"] = state.get("total_analyses", 0) + 1
            state["decisions"]      = (state.get("decisions", []) + decisions)[-150:]
            state["journal"]        = (state.get("journal", []) + [{
                "time":             now,
                "overall":          analysis.get("overall_assessment", ""),
                "market_insight":   analysis.get("market_insight", ""),
                "key_learnings":    analysis.get("key_learnings", []),
                "top_performers":   analysis.get("top_performers", []),
                "underperformers":  analysis.get("underperformers", []),
                "telegram_finding": analysis.get("telegram_analysis", {}).get("key_finding", ""),
                "decisions_count":  len(decisions),
                "next_focus":       analysis.get("next_check_focus", ""),
                "insight_summary":  analysis.get("overall_assessment", ""),
            }])[-60:]

            for strat, m in metrics.items():
                state["strategy_scores"][strat] = {
                    "last_updated":  now,
                    "total_closed":  m["total_closed"],
                    "win_rate":      m["win_rate"],
                    "profit_factor": m["profit_factor"],
                    "total_pnl":     m["total_pnl"],
                    "loss_streak":   m["loss_streak"],
                }
            _save_state(state)

            return {
                "success":           True,
                "time":              now,
                "overall":           analysis.get("overall_assessment"),
                "market_insight":    analysis.get("market_insight"),
                "strategy_analysis": analysis.get("strategy_analysis", []),
                "telegram_analysis": analysis.get("telegram_analysis", {}),
                "top_performers":    analysis.get("top_performers", []),
                "underperformers":   analysis.get("underperformers", []),
                "key_learnings":     analysis.get("key_learnings", []),
                "research_insights": analysis.get("research_insights", []),
                "decisions":         decisions,
                "next_focus":        analysis.get("next_check_focus", ""),
            }

        except Exception as e:
            import traceback
            return {"error": str(e), "trace": traceback.format_exc()[-500:]}


# ── Background Loop ───────────────────────────────────────────────────────────

def _loop():
    time.sleep(120)
    while True:
        try:
            result = run_analysis()
            if result.get("error"):
                print(f"[Brain] Error: {result['error']}")
            else:
                n = len(result.get("decisions", []))
                r = len(result.get("research_insights", []))
                print(f"[Brain] v3 analysis done — {n} decision(s), {r} new insight(s)")
        except Exception as e:
            print(f"[Brain] Loop exception: {e}")
        time.sleep(BRAIN_INTERVAL_HOURS * 3600)


def start():
    global _brain_thread
    if not _GROQ_OK:
        print("[Brain] requests library not installed")
        return
    if not _get_api_key():
        print("[Brain] Groq API key not set — enter it in the AI BRAIN dashboard tab")
        return
    if _brain_thread and _brain_thread.is_alive():
        return
    _brain_thread = threading.Thread(target=_loop, daemon=True)
    _brain_thread.start()
    print(f"[Brain] Started v4 — internet research + auto-implement | {BRAIN_INTERVAL_HOURS}h interval | Groq/{BRAIN_MODEL}")


def get_status():
    state = _load_state()
    key   = _get_api_key()
    return {
        "enabled":           bool(key and _GROQ_OK),
        "key_configured":    bool(key),
        "groq_ok":           _GROQ_OK,
        "last_run":          state.get("last_run"),
        "total_analyses":    state.get("total_analyses", 0),
        "latest_journal":    state["journal"][-1] if state.get("journal") else None,
        "recent_decisions":  state.get("decisions", [])[-10:],
        "strategy_scores":   state.get("strategy_scores", {}),
        "all_journal":       state.get("journal", []),
        "research_insights": state.get("research_insights", [])[-20:],
        "market_ctx":        state.get("last_market_ctx", {}),
    }
