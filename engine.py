"""
FarhanFX trade engine — the shared maths every bot uses, whether it is a
backtest, a demo (paper) bot or a live Binance bot.

Keeping it in one place is the point: a backtest only means something if the
live bot exits, sizes and pays fees the same way the backtest assumed.

Rules every trade follows
  * Signals are read on CLOSED candles only (the forming candle repaints).
  * Every position gets a stop loss and a take profit the moment it opens.
      stop  = sl_atr x ATR away   (or trailing_atr x ATR for trailing bots)
      target= tp_atr x ATR away   (or stop distance x reward_r when tp_atr = 0)
  * Size comes from the stop: lose at most risk_pct of the capital the risk
    manager hands the bot, capped by that capital x leverage.
  * One position per bot; no averaging in.
  * Fees: Binance USDT-M taker 0.05% per side, plus 0.03% slippage on market
    fills (entries, stop fills, signal-flip exits). Take profits are treated
    as filled at the target with taker fee, no extra slippage.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

TAKER_FEE      = 0.0005   # Binance USDT-M futures taker, per side
SLIPPAGE       = 0.0003   # market order slippage, per fill
DEFAULT_SL_ATR = 2.0
DEFAULT_RR     = 2.0      # take profit = stop distance x 2 when no tp_atr is set


# ── Indicators (self-contained so the engine has no server import) ────────────
def atr(highs, lows, closes, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    # Wilder smoothing
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


# ── Protective levels and sizing ──────────────────────────────────────────────
def stop_distance(bot: dict, atr_value: float) -> float:
    trail = float(bot.get("trailing_atr") or 0)
    sl_atr = float(bot.get("sl_atr") or DEFAULT_SL_ATR)
    mult = trail if trail > 0 else sl_atr
    return mult * atr_value


def protective_levels(bot: dict, side: str, entry: float, atr_value: float):
    """Return (stop_loss, take_profit) for a fresh position."""
    dist = stop_distance(bot, atr_value)
    tp_atr = float(bot.get("tp_atr") or 0)
    reward_r = float(bot.get("reward_r") or DEFAULT_RR)
    tp_dist = tp_atr * atr_value if tp_atr > 0 else dist * reward_r
    if side == "BUY":
        return entry - dist, entry + tp_dist
    return entry + dist, entry - tp_dist


def position_size(capital: float, risk_pct: float, entry: float, stop: float,
                  leverage: float) -> float:
    """Coins to trade so a stop-out loses risk_pct of capital (fees included),
    never more notional than capital x leverage."""
    dist = abs(entry - stop)
    if capital <= 0 or entry <= 0 or dist <= 0:
        return 0.0
    # Round-trip fees + slippage are part of the loss at the stop.
    per_coin_loss = dist + entry * (2 * TAKER_FEE + 2 * SLIPPAGE)
    qty = capital * risk_pct / 100.0 / per_coin_loss
    max_qty = capital * leverage / entry
    return max(0.0, min(qty, max_qty))


def exit_on_bar(side: str, stop: float, target: float, high: float, low: float):
    """Did this candle hit the stop or the target? Stop is assumed first when
    both are inside one candle (the conservative choice).
    Returns (fill_price, reason) or None."""
    if side == "BUY":
        if low <= stop:
            return stop, "stop_loss"
        if target and high >= target:
            return target, "take_profit"
    else:
        if high >= stop:
            return stop, "stop_loss"
        if target and low <= target:
            return target, "take_profit"
    return None


def trail(side: str, stop: float, extreme: float, trail_dist: float) -> float:
    """Move a trailing stop with the best price seen; never loosen it."""
    if trail_dist <= 0:
        return stop
    if side == "BUY":
        return max(stop, extreme - trail_dist)
    return min(stop, extreme + trail_dist)


def fill_price(side_of_fill: str, price: float, market: bool = True) -> float:
    """Apply slippage to a market fill. side_of_fill is 'buy' or 'sell'."""
    if not market:
        return price
    return price * (1 + SLIPPAGE) if side_of_fill == "buy" else price * (1 - SLIPPAGE)


def trade_pnl(side: str, entry: float, exit_: float, qty: float) -> float:
    """Net P/L after taker fees on both sides."""
    gross = (exit_ - entry) * qty if side == "BUY" else (entry - exit_) * qty
    fees = (entry + exit_) * qty * TAKER_FEE
    return gross - fees


# ── Backtest ──────────────────────────────────────────────────────────────────
def backtest(signal_fn, bot: dict, ohlcv: list, window: int,
             capital: float = 1000.0, risk_pct: float = 1.0,
             leverage: float = 10.0) -> dict:
    """Bar-by-bar replay with the live rules above.

    signal_fn(bot, window_of_closed_bars) -> "BUY" | "SELL" | None
    Bar i is treated as closed when its signal is read; the entry fills at
    its close (≈ the next bar's open) with slippage. Exits are checked on
    every later bar's high/low."""
    equity = capital
    peak_equity = capital
    max_dd = 0.0
    pos = None          # dict(side, entry, qty, stop, target, extreme, entry_i, risk_usd)
    trades = []
    errors = 0
    last_close_i = -1
    n = len(ohlcv)

    def close(i, price, reason, market):
        nonlocal equity, pos, last_close_i
        side = pos["side"]
        px = fill_price("sell" if side == "BUY" else "buy", price, market)
        pnl = trade_pnl(side, pos["entry"], px, pos["qty"])
        equity += pnl
        trades.append({
            "time": _ts(ohlcv[pos["entry_i"]][0]),
            "exit_time": _ts(ohlcv[i][0]),
            "signal": side, "price": round(pos["entry"], 6),
            "exit_price": round(px, 6), "amount": pos["qty"],
            "pnl": round(pnl, 4), "r": round(pnl / pos["risk_usd"], 3) if pos["risk_usd"] else 0,
            "exit_reason": reason, "status": "closed",
        })
        pos = None
        last_close_i = i

    for i in range(window - 1, n):
        o, h, l, c = ohlcv[i][1], ohlcv[i][2], ohlcv[i][3], ohlcv[i][4]

        # 1) exits inside this candle for a position opened on an earlier bar
        if pos and i > pos["entry_i"]:
            hit = exit_on_bar(pos["side"], pos["stop"], pos["target"], h, l)
            if hit:
                close(i, hit[0], hit[1], market=(hit[1] == "stop_loss"))
            elif pos["trail_dist"] > 0:
                pos["extreme"] = max(pos["extreme"], h) if pos["side"] == "BUY" else min(pos["extreme"], l)
                pos["stop"] = trail(pos["side"], pos["stop"], pos["extreme"], pos["trail_dist"])

        # 2) signal on the now-closed bar i
        win = ohlcv[i - window + 1:i + 1]
        try:
            signal = signal_fn(bot, win)
        except Exception:
            signal = None
            errors += 1

        if pos and signal and signal != pos["side"]:
            close(i, c, "signal_flip", market=True)
        elif not pos and signal and i != last_close_i:
            highs = [x[2] for x in win]; lows = [x[3] for x in win]; closes = [x[4] for x in win]
            a = atr(highs, lows, closes, 14)
            if a > 0:
                entry = fill_price("buy" if signal == "BUY" else "sell", c)
                stop, target = protective_levels(bot, signal, entry, a)
                qty = position_size(equity, risk_pct, entry, stop, leverage)
                if qty > 0:
                    pos = {"side": signal, "entry": entry, "qty": qty, "stop": stop,
                           "target": target, "extreme": entry, "entry_i": i,
                           "trail_dist": float(bot.get("trailing_atr") or 0) * a,
                           "risk_usd": equity * risk_pct / 100}

        # 3) mark-to-market drawdown
        mtm = equity
        if pos:
            mtm += trade_pnl(pos["side"], pos["entry"], c, pos["qty"])
        if mtm > peak_equity:
            peak_equity = mtm
        elif peak_equity > 0:
            max_dd = max(max_dd, (peak_equity - mtm) / peak_equity * 100)

    if pos:  # close anything still open at the last price
        close(n - 1, ohlcv[-1][4], "end_of_data", market=True)

    return summarize(trades, capital, equity, max_dd, errors)


def summarize(trades, capital, equity, max_dd, errors=0) -> dict:
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    n = len(trades)
    return {
        "total_trades": n,
        "win_rate": round(len(wins) / n * 100, 1) if n else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (None if not wins else 99.0),
        "net_pnl": round(equity - capital, 4),
        "net_pnl_pct": round((equity - capital) / capital * 100, 2) if capital else 0,
        "final_equity": round(equity, 4),
        "max_drawdown_pct": round(max_dd, 2),
        "avg_r": round(sum(t.get("r", 0) for t in trades) / n, 3) if n else None,
        "stop_outs": sum(1 for t in trades if t["exit_reason"] == "stop_loss"),
        "signal_errors": errors,
        "trades": trades,
    }


def _ts(ms) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def candle_close_delay(timeframe_seconds: int, now: float, grace: float = 5.0) -> float:
    """Seconds until just after the next candle closes, so a bot reads the
    finished candle instead of a half-built one."""
    nxt = (math.floor(now / timeframe_seconds) + 1) * timeframe_seconds
    return max(1.0, nxt - now + grace)
