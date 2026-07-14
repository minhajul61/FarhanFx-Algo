"""
FarhanFX AI Trading Brain  v2
------------------------------
Self-learning AI that monitors all bots, analyzes trade performance,
learns from patterns, and autonomously fixes underperforming strategies.

Runs every BRAIN_INTERVAL_HOURS (default 4h).
Uses Groq API (llama-3.3-70b) — 100% FREE, no billing needed, 14,400 req/day.
Get free API key: https://console.groq.com  → API Keys → Create

v2 improvements:
- More tunable parameters: risk_pct, tp_atr, trailing_atr, bb_std, slow_ema, fast_ema
- Anti-oscillation: blocks flip-flopping on same param within 3 runs
- Parameter bounds: prevents extreme values
- Winner rewards: good bots get risk_pct or max_open_trades increase
- Consecutive loss detection: flags 4+ loss streaks for immediate action
- Better prompt: shows current param values + streak data to AI
- Interval 4h (was 6h), max_tokens 3500 (was 2000)
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
BRAIN_FILE            = "brain_state.json"
BRAIN_CONFIG_FILE     = "brain_config.json"
BOTS_FILE             = "bots.json"
BRAIN_INTERVAL_HOURS  = 4      # was 6
BRAIN_MODEL           = "llama-3.3-70b-versatile"

MIN_TRADES_TO_JUDGE   = 10
PAUSE_WR_THRESHOLD    = 35     # was 40 — more aggressive
RESUME_WR_THRESHOLD   = 52
REWARD_WR_THRESHOLD   = 65     # WR above this → reward with more risk
REWARD_PF_THRESHOLD   = 1.8    # PF above this → reward

# Parameter bounds — AI cannot push beyond these
PARAM_BOUNDS = {
    "adx_min":        (0,   80),
    "rsi_ob":         (60,  95),
    "rsi_os":         (5,   40),
    "max_open_trades":(1,    5),
    "risk_pct":       (0.5,  5.0),
    "tp_atr":         (0.5,  6.0),
    "trailing_atr":   (0.5,  5.0),
    "bb_std":         (1.5,  3.5),
    "slow_ema":       (10,   50),
    "fast_ema":       (3,    20),
}

_brain_lock    = threading.Lock()
_brain_thread  = None
_api_key_cache = ""
_GROQ_API_URL  = "https://api.groq.com/openai/v1/chat/completions"


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
        "param_history": {},   # {strategy: {param: [last3 values]}}
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
    """Return True if new_val is same as 2 runs ago (flip-flopping)."""
    hist = param_history.get(strat, {}).get(param, [])
    if len(hist) >= 2 and hist[-2] == new_val:
        return True
    return False


def _record_param_change(param_history, strat, param, new_val):
    if strat not in param_history:
        param_history[strat] = {}
    hist = param_history[strat].get(param, [])
    hist.append(new_val)
    param_history[strat][param] = hist[-4:]  # keep last 4


# ── Metrics Calculator ────────────────────────────────────────────────────────

def calculate_metrics(bots):
    metrics = {}
    for bot in bots.values():
        strat = bot.get("strategy")
        if not strat:
            continue
        trades  = bot.get("trades", [])
        closed  = [t for t in trades if t.get("status") == "closed"]
        open_t  = [t for t in trades if t.get("status") == "open"]
        wins    = [t for t in closed if t.get("pnl", 0) > 0]
        losses  = [t for t in closed if t.get("pnl", 0) < 0]
        pnl_sum = sum(t.get("pnl", 0) for t in closed)
        win_sum = sum(t.get("pnl", 0) for t in wins)
        los_sum = abs(sum(t.get("pnl", 0) for t in losses))
        pf      = round(win_sum / los_sum, 2) if los_sum > 0 else (9.99 if win_sum > 0 else 0)
        wr      = round(len(wins) / len(closed) * 100, 1) if closed else 0
        equity  = bot.get("demo_equity", bot.get("demo_balance", 5000))
        balance = bot.get("demo_balance", 5000)

        # Consecutive loss streak (last 8 trades)
        recent8 = sorted(closed, key=lambda x: x.get("time", ""), reverse=True)[:8]
        streak  = 0
        for t in recent8:
            if t.get("pnl", 0) < 0:
                streak += 1
            else:
                break

        if strat not in metrics:
            metrics[strat] = {
                "strategy": strat,
                "symbol":   bot.get("symbol", ""),
                "timeframe": bot.get("timeframe", ""),
                "total_closed": 0, "total_open": 0,
                "wins": 0, "losses": 0,
                "win_rate": 0, "profit_factor": 0,
                "total_pnl": 0, "equity": equity, "balance": balance,
                "recent_trades": [],
                "bot_status": bot.get("status", "active"),
                "loss_streak": streak,
                "current_params": {},
            }

        m = metrics[strat]
        m["total_closed"] += len(closed)
        m["total_open"]   += len(open_t)
        m["wins"]         += len(wins)
        m["losses"]       += len(losses)
        m["total_pnl"]     = round(m["total_pnl"] + pnl_sum, 2)
        m["equity"]        = round(equity, 2)
        m["profit_factor"] = pf
        m["loss_streak"]   = max(m["loss_streak"], streak)
        if m["total_closed"] > 0:
            m["win_rate"] = round(m["wins"] / m["total_closed"] * 100, 1)

        # Capture current tunable params
        for p in PARAM_BOUNDS:
            if p in bot:
                m["current_params"][p] = bot[p]

        recent = sorted(closed, key=lambda x: x.get("time", ""), reverse=True)[:6]
        m["recent_trades"] = [
            {"time": t.get("time"), "side": t.get("signal"),
             "pnl": round(t.get("pnl", 0), 2), "exit": t.get("exit_reason", "")}
            for t in recent
        ]

    return metrics


# ── Prompt Builder ────────────────────────────────────────────────────────────

def _build_prompt(metrics, state):
    lines = []
    for strat, m in sorted(metrics.items(), key=lambda x: x[1]["total_pnl"], reverse=True):
        if m["total_closed"] == 0:
            tag = "NEW"
        elif m["win_rate"] >= 60:
            tag = "GOOD"
        elif m["win_rate"] >= 45:
            tag = "WATCH"
        else:
            tag = "POOR"

        streak_note = f" ⚠️ LOSS_STREAK={m['loss_streak']}" if m["loss_streak"] >= 3 else ""
        lines.append(
            f"• {strat} [{tag}]{streak_note} | {m['timeframe']} | status={m['bot_status']}"
            f" | closed={m['total_closed']} | WR={m['win_rate']}% | PF={m['profit_factor']}"
            f" | PnL=${m['total_pnl']:+.2f}"
        )
        # Show current tunable params
        if m["current_params"]:
            param_str = " | ".join(f"{k}={v}" for k, v in m["current_params"].items()
                                   if k in ["adx_min","rsi_ob","rsi_os","max_open_trades","risk_pct","tp_atr","trailing_atr"])
            if param_str:
                lines.append(f"  params: {param_str}")
        if m["recent_trades"]:
            rec = ", ".join(
                f"{'W' if t['pnl']>0 else 'L'}${t['pnl']:+.2f}"
                for t in m["recent_trades"]
            )
            lines.append(f"  last trades: {rec}")

    prev_decisions = "None yet."
    if state.get("decisions"):
        prev_decisions = "\n".join(
            f"- {d['time']}: {d['action']} on {d['strategy']} — {d['reason'][:100]}"
            for d in state["decisions"][-8:]
        )

    prev_learning = "None yet."
    if state.get("journal"):
        last = state["journal"][-1]
        prev_learning = (
            f"[{last.get('time','')}] {last.get('overall','')[:200]}\n"
            f"Learnings: {'; '.join(last.get('key_learnings',[]))}"
        )

    bounds_info = "\n".join(
        f"  {p}: min={b[0]}, max={b[1]}"
        for p, b in PARAM_BOUNDS.items()
    )

    return f"""You are FarhanFX AI Trading Brain v2 — an expert algorithmic trading analyst.
Your job: analyze ALL bot performance deeply, identify WHY trades win or lose, and make SMART autonomous parameter adjustments.

DATE: {datetime.now().strftime('%Y-%m-%d %H:%M')}

=== STRATEGY PERFORMANCE (sorted by PnL, best first) ===
{chr(10).join(lines)}

=== PREVIOUS DECISIONS (last 8) ===
{prev_decisions}

=== PREVIOUS LEARNINGS ===
{prev_learning}

=== TUNABLE PARAMETERS & BOUNDS ===
{bounds_info}
Parameter meanings:
- adx_min: minimum ADX trend strength required to take trade (higher = fewer but stronger signals)
- rsi_ob/rsi_os: RSI overbought/oversold thresholds (tighter = fewer trades)
- max_open_trades: max concurrent positions per strategy
- risk_pct: % of balance risked per trade (lower = smaller positions)
- tp_atr: take-profit in ATR multiples (higher = bigger TP targets)
- trailing_atr: trailing stop in ATR multiples
- bb_std: Bollinger Band standard deviation width
- slow_ema / fast_ema: EMA crossover periods

=== YOUR DECISION RULES ===
1. PAUSE: closed >= {MIN_TRADES_TO_JUDGE} AND WR < {PAUSE_WR_THRESHOLD}% AND PnL < -$20 AND loss_streak >= 3
2. RESUME: status=paused AND recent data shows improvement
3. REWARD: WR >= {REWARD_WR_THRESHOLD}% AND PF >= {REWARD_PF_THRESHOLD} → increase risk_pct by 0.5 or max_open_trades by 1
4. ADJUST: make 1-2 specific param changes per strategy max — do NOT oscillate values
5. NEW bots (0 trades): verdict="new", action="none" — never touch them
6. < 8 trades: verdict="watch", action="none" — too early
7. LOSS_STREAK >= 4: must take action (pause or tighten params significantly)
8. Avoid reversing a param change you made 1-2 runs ago — check previous decisions

Respond ONLY with valid JSON, no extra text:
{{
  "overall_assessment": "3-4 sentence deep portfolio health analysis",
  "market_insight": "What the win/loss patterns reveal about current market regime",
  "strategy_analysis": [
    {{
      "strategy": "name",
      "verdict": "good|watch|poor|new",
      "insight": "Specific analytical reason — WHY is it winning or losing?",
      "action": "none|pause|adjust_param|resume|reward",
      "action_detail": "e.g. risk_pct=2.0 adx_min=30 OR reason for pause/reward",
      "learning": "One key insight from this strategy's data"
    }}
  ],
  "top_performers": ["strategy1", "strategy2"],
  "underperformers": ["strategy2"],
  "key_learnings": ["learning1", "learning2", "learning3"],
  "next_check_focus": "Specific thing to monitor in next 4-hour check"
}}"""


# ── Decision Executor ─────────────────────────────────────────────────────────

def _execute(analysis, bots, state, now):
    """Apply brain decisions to bots. Returns list of decision records."""
    made         = []
    param_history = state.setdefault("param_history", {})

    adjustable = {
        "max_open_trades": int,
        "adx_min":         int,
        "rsi_ob":          int,
        "rsi_os":          int,
        "risk_pct":        float,
        "tp_atr":          float,
        "trailing_atr":    float,
        "bb_std":          float,
        "slow_ema":        int,
        "fast_ema":        int,
    }

    for sa in analysis.get("strategy_analysis", []):
        strat  = sa.get("strategy")
        action = sa.get("action", "none")
        detail = sa.get("action_detail", "")
        reason = sa.get("insight", "")[:200]

        if action == "pause":
            for bot in bots.values():
                if bot.get("strategy") == strat and bot.get("status") == "active":
                    bot["status"] = "paused"
                    bot["last_error"] = f"🧠 Brain paused: {detail[:120]}"
                    made.append({"time": now, "strategy": strat,
                                 "action": "paused", "reason": reason,
                                 "ai_detail": detail[:200]})

        elif action == "resume":
            for bot in bots.values():
                if bot.get("strategy") == strat and bot.get("status") == "paused":
                    bot["status"] = "active"
                    bot["last_error"] = None
                    made.append({"time": now, "strategy": strat,
                                 "action": "resumed", "reason": reason,
                                 "ai_detail": detail[:200]})

        elif action in ("adjust_param", "reward"):
            for param, cast in adjustable.items():
                pattern = rf"{param}[^\d\.\-]*([0-9]+\.?[0-9]*)"
                m = re.search(pattern, detail, re.IGNORECASE)
                if not m:
                    continue
                try:
                    new_val = cast(m.group(1))
                except Exception:
                    continue

                # Enforce bounds
                if param in PARAM_BOUNDS:
                    lo, hi = PARAM_BOUNDS[param]
                    new_val = cast(max(lo, min(hi, new_val)))

                # Anti-oscillation check
                if _is_oscillating(param_history, strat, param, new_val):
                    print(f"[Brain] Skipping oscillating change: {strat}.{param} → {new_val} (flip-flop detected)")
                    continue

                changed = False
                for bot in bots.values():
                    if bot.get("strategy") == strat:
                        old_val = bot.get(param, "?")
                        if old_val != new_val:
                            bot[param] = new_val
                            changed = True

                if changed:
                    _record_param_change(param_history, strat, param, new_val)
                    made.append({"time": now, "strategy": strat,
                                 "action": f"adjust_{param}",
                                 "reason": f"{param}: {old_val} → {new_val}",
                                 "ai_detail": detail[:200]})

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
            state   = _load_state()
            bots    = _load_bots()
            if not bots:
                return {"error": "No bots found in bots.json"}

            metrics = calculate_metrics(bots)
            prompt  = _build_prompt(metrics, state)

            resp = _requests.post(
                _GROQ_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":       BRAIN_MODEL,
                    "messages":    [{"role": "user", "content": prompt}],
                    "max_tokens":  3500,
                    "temperature": 0.25,
                },
                timeout=90,
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

            state["last_run"]        = now
            state["total_analyses"]  = state.get("total_analyses", 0) + 1
            state["decisions"]       = (state.get("decisions", []) + decisions)[-150:]
            state["journal"]         = (state.get("journal", []) + [{
                "time":            now,
                "overall":         analysis.get("overall_assessment", ""),
                "market_insight":  analysis.get("market_insight", ""),
                "key_learnings":   analysis.get("key_learnings", []),
                "top_performers":  analysis.get("top_performers", []),
                "underperformers": analysis.get("underperformers", []),
                "decisions_count": len(decisions),
                "next_focus":      analysis.get("next_check_focus", ""),
                "insight_summary": analysis.get("overall_assessment", ""),
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
                "top_performers":    analysis.get("top_performers", []),
                "underperformers":   analysis.get("underperformers", []),
                "key_learnings":     analysis.get("key_learnings", []),
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
                print(f"[Brain] Analysis done — {n} decision(s) made")
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
    print(f"[Brain] Started v2 (Groq/{BRAIN_MODEL}) — first analysis in 2 min, then every {BRAIN_INTERVAL_HOURS}h")


def get_status():
    state = _load_state()
    key   = _get_api_key()
    return {
        "enabled":          bool(key and _GROQ_OK),
        "key_configured":   bool(key),
        "groq_ok":          _GROQ_OK,
        "last_run":         state.get("last_run"),
        "total_analyses":   state.get("total_analyses", 0),
        "latest_journal":   state["journal"][-1] if state.get("journal") else None,
        "recent_decisions": state.get("decisions", [])[-10:],
        "strategy_scores":  state.get("strategy_scores", {}),
        "all_journal":      state.get("journal", []),
    }
