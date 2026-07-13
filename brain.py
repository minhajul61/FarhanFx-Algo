"""
FarhanFX AI Trading Brain
--------------------------
Self-learning AI that monitors all bots, analyzes trade performance,
learns from patterns, and autonomously fixes underperforming strategies.

Runs every BRAIN_INTERVAL_HOURS (default 6h).
Uses Google Gemini API (gemini-2.0-flash) — 100% FREE, 1500 req/day.
Get free API key: https://aistudio.google.com/app/apikey
Set env var: GEMINI_API_KEY=your_key_here
"""

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    import google.generativeai as genai
    _GEMINI_OK = True
except ImportError:
    _GEMINI_OK = False

# ── Config ────────────────────────────────────────────────────────────────────
BRAIN_FILE            = "brain_state.json"
BRAIN_CONFIG_FILE     = "brain_config.json"
BOTS_FILE             = "bots.json"
BRAIN_INTERVAL_HOURS  = 6
BRAIN_MODEL           = "gemini-2.0-flash"

# Thresholds for autonomous decisions
MIN_TRADES_TO_JUDGE   = 10   # need at least this many trades before pausing
PAUSE_WR_THRESHOLD    = 40   # pause if win rate below this %
RESUME_WR_THRESHOLD   = 55   # resume a paused strategy if recent WR recovers

_brain_lock    = threading.Lock()
_brain_thread  = None
_api_key_cache = ""   # updated at runtime via set_api_key()


def _get_api_key() -> str:
    """Read key: in-memory cache → env var → brain_config.json."""
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
    """Save key to brain_config.json and (re)start the brain thread."""
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
    start()  # safe to call multiple times — guards with _brain_thread check


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


# ── Metrics Calculator ────────────────────────────────────────────────────────

def calculate_metrics(bots):
    """Aggregate per-strategy performance from all bot trade histories."""
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
            }

        m = metrics[strat]
        m["total_closed"] += len(closed)
        m["total_open"]   += len(open_t)
        m["wins"]         += len(wins)
        m["losses"]       += len(losses)
        m["total_pnl"]     = round(m["total_pnl"] + pnl_sum, 2)
        m["equity"]        = round(equity, 2)
        m["profit_factor"] = pf
        if m["total_closed"] > 0:
            m["win_rate"] = round(m["wins"] / m["total_closed"] * 100, 1)

        recent = sorted(closed, key=lambda x: x.get("time", ""), reverse=True)[:5]
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
            tag = "NEW-no trades yet"
        elif m["win_rate"] >= 60:
            tag = "GOOD"
        elif m["win_rate"] >= 45:
            tag = "WATCH"
        else:
            tag = "POOR"
        lines.append(
            f"• {strat} | {m['timeframe']} | closed={m['total_closed']} "
            f"| WR={m['win_rate']}% | PF={m['profit_factor']} "
            f"| PnL=${m['total_pnl']:+.2f} | equity=${m['equity']} | [{tag}]"
        )
        if m["recent_trades"]:
            rec = ", ".join(
                f"{t['side']}({'W' if t['pnl']>0 else 'L'} ${t['pnl']:+.2f})"
                for t in m["recent_trades"]
            )
            lines.append(f"  last trades: {rec}")

    prev_decisions = "None yet."
    if state.get("decisions"):
        prev_decisions = "\n".join(
            f"- {d['time']}: {d['action']} on {d['strategy']} — {d['reason'][:120]}"
            for d in state["decisions"][-6:]
        )

    prev_learning = "None yet."
    if state.get("journal"):
        last = state["journal"][-1]
        prev_learning = (
            f"[{last.get('time','')}] {last.get('overall','')}\n"
            f"Learnings: {'; '.join(last.get('key_learnings',[]))}"
        )

    return f"""You are the AI Brain of an algorithmic crypto trading system called FarhanFX Algo.
Your job: analyze bot performance, find WHY trades win or lose, and make smart autonomous decisions.

DATE: {datetime.now().strftime('%Y-%m-%d %H:%M')}

=== STRATEGY PERFORMANCE ===
{chr(10).join(lines)}

=== PREVIOUS DECISIONS ===
{prev_decisions}

=== PREVIOUS LEARNINGS ===
{prev_learning}

=== YOUR RULES ===
- PAUSE only if: closed trades >= {MIN_TRADES_TO_JUDGE} AND win_rate < {PAUSE_WR_THRESHOLD}% AND total_pnl < 0
- NEW bots (0 trades, especially 4h): verdict="new", action="none" — they need time
- < 5 trades: verdict="watch", action="none" — too early to judge
- Be SPECIFIC about WHY a strategy is failing (market regime? noise? wrong timeframe?)
- Suggest concrete parameter fixes (e.g. "reduce max_open_trades to 1", "raise rsi_ob to 80")
- Parameters you CAN adjust: max_open_trades, adx_min, rsi_ob, rsi_os

Respond ONLY with valid JSON, no extra text:
{{
  "overall_assessment": "2-3 sentence portfolio health summary",
  "market_insight": "What trade patterns reveal about current market regime",
  "strategy_analysis": [
    {{
      "strategy": "name",
      "verdict": "good|watch|poor|new",
      "insight": "Specific reason for performance — be analytical",
      "action": "none|pause|adjust_param|resume",
      "action_detail": "e.g. max_open_trades=1 or reason for pause",
      "learning": "One key takeaway from this strategy data"
    }}
  ],
  "top_performers": ["strategy1"],
  "underperformers": ["strategy2"],
  "key_learnings": ["learning1", "learning2", "learning3"],
  "next_check_focus": "What to watch in next analysis"
}}"""


# ── Decision Executor ─────────────────────────────────────────────────────────

def _execute(analysis, bots, now):
    """Apply the brain's decisions to bots dict. Returns list of decision records."""
    made = []
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

        elif action == "adjust_param":
            adjustable = {"max_open_trades": int, "adx_min": int,
                          "rsi_ob": int, "rsi_os": int}
            for param, cast in adjustable.items():
                m = re.search(rf"{param}[^\d]*(\d+)", detail, re.IGNORECASE)
                if m:
                    new_val = cast(m.group(1))
                    for bot in bots.values():
                        if bot.get("strategy") == strat:
                            old_val = bot.get(param, "?")
                            bot[param] = new_val
                    made.append({"time": now, "strategy": strat,
                                 "action": f"adjust_{param}",
                                 "reason": f"{param}: {old_val} → {new_val}",
                                 "ai_detail": detail[:200]})
    return made


# ── Main Analysis Function ────────────────────────────────────────────────────

def run_analysis():
    """
    Run one full brain analysis cycle.
    Returns a result dict. Called by the background loop and the manual API endpoint.
    """
    if not _GEMINI_OK:
        return {"error": "google-generativeai not installed — run: pip install google-generativeai"}
    api_key = _get_api_key()
    if not api_key:
        return {"error": "Gemini API key not set — enter it in the AI BRAIN tab of the dashboard"}

    with _brain_lock:
        try:
            state   = _load_state()
            bots    = _load_bots()
            if not bots:
                return {"error": "No bots found in bots.json"}

            metrics = calculate_metrics(bots)
            prompt  = _build_prompt(metrics, state)

            genai.configure(api_key=api_key)
            model    = genai.GenerativeModel(BRAIN_MODEL)
            response = model.generate_content(prompt)
            raw      = response.text.strip()

            # Extract JSON from response
            json_match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not json_match:
                return {"error": f"Could not parse AI response: {raw[:200]}"}
            analysis = json.loads(json_match.group())

            now       = datetime.now().strftime("%Y-%m-%d %H:%M")
            decisions = _execute(analysis, bots, now)
            _save_bots(bots)

            # Update state
            state["last_run"]        = now
            state["total_analyses"]  = state.get("total_analyses", 0) + 1
            state["decisions"]       = (state.get("decisions", []) + decisions)[-100:]
            state["journal"]         = (state.get("journal", []) + [{
                "time":            now,
                "overall":         analysis.get("overall_assessment", ""),
                "market_insight":  analysis.get("market_insight", ""),
                "key_learnings":   analysis.get("key_learnings", []),
                "top_performers":  analysis.get("top_performers", []),
                "underperformers": analysis.get("underperformers", []),
                "decisions_count": len(decisions),
                "next_focus":      analysis.get("next_check_focus", ""),
                "insight_summary": analysis.get("overall_assessment", "") + " | " + analysis.get("market_insight", ""),
            }])[-50:]

            for strat, m in metrics.items():
                state["strategy_scores"][strat] = {
                    "last_updated":  now,
                    "total_closed":  m["total_closed"],
                    "win_rate":      m["win_rate"],
                    "profit_factor": m["profit_factor"],
                    "total_pnl":     m["total_pnl"],
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
    time.sleep(120)  # wait 2 min after server start before first run
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
    """Start the brain background thread. Safe to call multiple times."""
    global _brain_thread
    if not _GEMINI_OK:
        print("[Brain] google-generativeai not installed — run: pip install google-generativeai")
        return
    if not _get_api_key():
        print("[Brain] API key not set — enter it in the AI BRAIN dashboard tab")
        return
    if _brain_thread and _brain_thread.is_alive():
        return  # already running
    _brain_thread = threading.Thread(target=_loop, daemon=True)
    _brain_thread.start()
    print(f"[Brain] Started — first analysis in 2 min, then every {BRAIN_INTERVAL_HOURS}h")


def get_status():
    """Return current brain state for the /api/brain/status endpoint."""
    state = _load_state()
    key   = _get_api_key()
    return {
        "enabled":          bool(key and _GEMINI_OK),
        "key_configured":   bool(key),
        "gemini_ok":        _GEMINI_OK,
        "last_run":         state.get("last_run"),
        "total_analyses":   state.get("total_analyses", 0),
        "latest_journal":   state["journal"][-1] if state.get("journal") else None,
        "recent_decisions": state.get("decisions", [])[-10:],
        "strategy_scores":  state.get("strategy_scores", {}),
        "all_journal":      state.get("journal", []),
    }
