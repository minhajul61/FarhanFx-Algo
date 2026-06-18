"""
Forex Bot — standalone Grid Recovery strategy runner.

Runs as its OWN process with its OWN MT5 terminal connection, completely
separate from server.py's main connection (which drives the 7 live
strategies on the primary account). This isolation is intentional: the
MetaTrader5 Python module only supports one logged-in account per process,
so mixing this into server.py would risk switching the main account's
connection mid-trade.

Config comes from forex_bot_config.json (written by server.py's
/api/forexbot/connect + /api/forexbot/start). Status is written continuously
to forex_bot_status.json for the dashboard to poll. A forex_bot_stop.flag
file triggers graceful shutdown.

Strategy — Grid Recovery (modeled on observed "Nemesis EA / GoldTrap" behavior):
  - Opens a small base-lot position when an EMA/RSI signal fires.
  - If price moves against it, adds another same-direction position at the
    next step, with lot size scaled by MULTIPLIER (~1.68x, matching the
    observed pattern), up to MAX_STEPS.
  - Once the whole basket's floating profit reaches the target, closes
    every position in the basket at once and resets to step 0.
  - At MAX_STEPS, stops adding new grid orders — caps exposure instead of
    scaling indefinitely. This is a stricter cap than the EA we studied
    (which appeared to allow ~14 steps); ours stops at 12 by default.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5

_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_FILE = os.path.join(_DIR, "forex_bot_config.json")
_STATUS_FILE = os.path.join(_DIR, "forex_bot_status.json")
_STOP_FLAG   = os.path.join(_DIR, "forex_bot_stop.flag")

# Hard safety guard: this bot must NEVER end up connected to the main
# live-trading account, even if the user mistypes a terminal path that
# happens to resolve to it.
_PROTECTED_LOGIN = 698085
_PROTECTED_TERMINAL_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"

MAGIC = 777001


def _load_config() -> dict:
    with open(_CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def _write_status(**kwargs):
    try:
        with open(_STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump({"updated": datetime.now().isoformat(), **kwargs}, f, indent=2, default=str)
    except Exception:
        pass


def _log(logs: list, msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    logs.append(f"[{ts}] {msg}")
    if len(logs) > 200:
        logs.pop(0)
    print(f"[{ts}] {msg}", flush=True)


def _ema(prices, period):
    k = 2 / (period + 1)
    out = [prices[0]]
    for p in prices[1:]:
        out.append(p * k + out[-1] * (1 - k))
    return out


def _rsi(prices, period=14):
    gains = [0.0]
    losses = [0.0]
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period
    out = [50.0] * period
    for i in range(period, len(prices)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out.append(100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss))
    return out


def _get_signal(symbol, timeframe):
    """Simple EMA20/EMA50 + RSI trend signal for picking grid direction."""
    tf_map = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
              "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1}
    rates = mt5.copy_rates_from_pos(symbol, tf_map.get(timeframe, mt5.TIMEFRAME_M15), 0, 100)
    if rates is None or len(rates) < 60:
        return None
    closes = [float(r["close"]) for r in rates]
    e20 = _ema(closes, 20)
    e50 = _ema(closes, 50)
    rsi = _rsi(closes, 14)[-1]
    if closes[-1] > e20[-1] > e50[-1] and 45 < rsi < 70:
        return "BUY"
    if closes[-1] < e20[-1] < e50[-1] and 30 < rsi < 55:
        return "SELL"
    return None


def _send_order(symbol, side, volume, comment):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    info = mt5.symbol_info(symbol)
    if info and not info.visible:
        mt5.symbol_select(symbol, True)
    is_buy = side == "BUY"
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": round(volume, 2),
        "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
        "price": tick.ask if is_buy else tick.bid,
        "deviation": 30,
        "magic": MAGIC,
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
    }
    for filling in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
        req["type_filling"] = filling
        result = mt5.order_send(req)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            return result
        if result and result.retcode != 10038:
            return result
    return None


def _close_position(ticket):
    pos = next((p for p in (mt5.positions_get(ticket=ticket) or [])), None)
    if pos is None:
        return False
    tick = mt5.symbol_info_tick(pos.symbol)
    if tick is None:
        return False
    is_buy = pos.type == 0
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": pos.symbol,
        "volume": pos.volume,
        "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
        "position": pos.ticket,
        "price": tick.bid if is_buy else tick.ask,
        "deviation": 30,
        "magic": MAGIC,
        "comment": "ForexBot-Close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(req)
    return bool(result and result.retcode == mt5.TRADE_RETCODE_DONE)


def main():
    cfg = _load_config()
    login        = int(cfg["login"])
    password     = cfg["password"]
    server       = cfg["server"]
    terminal     = cfg.get("terminal_path") or None
    symbol       = cfg.get("symbol", "XAUUSD")
    timeframe    = cfg.get("timeframe", "M15")
    base_lot     = float(cfg.get("base_lot", 0.01))
    max_steps    = int(cfg.get("max_steps", 12))
    multiplier   = float(cfg.get("multiplier", 1.68))
    tp_usd       = float(cfg.get("tp_usd", 0.0)) or None  # 0/None = auto (per-step heuristic)

    logs = []

    if login == _PROTECTED_LOGIN:
        _log(logs, f"REFUSED — login {login} is the protected main trading account. Aborting.")
        _write_status(status="error", error="refused: protected main account", logs=logs)
        return

    if terminal and os.path.normcase(os.path.abspath(terminal)) == os.path.normcase(os.path.abspath(_PROTECTED_TERMINAL_PATH)):
        _log(logs, f"REFUSED — terminal path is the main account's terminal. Logging in here would hijack live trading. Aborting.")
        _write_status(status="error", error="refused: protected main terminal path", logs=logs)
        return

    init_kwargs = {"timeout": 20000}
    if terminal:
        init_kwargs["path"] = terminal
    if not mt5.initialize(**init_kwargs):
        _log(logs, f"MT5 initialize failed: {mt5.last_error()}")
        _write_status(status="error", error=str(mt5.last_error()), logs=logs)
        return

    if not mt5.login(login=login, password=password, server=server, timeout=25000):
        _log(logs, f"MT5 login failed: {mt5.last_error()}")
        _write_status(status="error", error=str(mt5.last_error()), logs=logs)
        mt5.shutdown()
        return

    info = mt5.account_info()
    if info is None:
        _log(logs, "No account info after login")
        _write_status(status="error", error="no account info", logs=logs)
        mt5.shutdown()
        return

    if info.login == _PROTECTED_LOGIN:
        _log(logs, f"REFUSED — connected account is the protected main account ({_PROTECTED_LOGIN}). Shutting down.")
        _write_status(status="error", error="refused: connected to protected main account", logs=logs)
        mt5.shutdown()
        return

    real_symbol = symbol
    if mt5.symbol_info(symbol) is None:
        for s in (mt5.symbols_get() or []):
            if s.name.lower().startswith(symbol.lower()):
                real_symbol = s.name
                break
    mt5.symbol_select(real_symbol, True)

    _log(logs, f"Connected: {info.login} | {info.name} | {info.server} | balance {info.balance} {info.currency}")
    _log(logs, f"Grid Recovery starting on {real_symbol} {timeframe} | base_lot={base_lot} max_steps={max_steps} mult={multiplier}")

    step = 0
    direction = None
    pip = mt5.symbol_info(real_symbol).point * 10

    _write_status(status="running", login=info.login, name=info.name, server=info.server,
                  balance=info.balance, symbol=real_symbol, step=step, max_steps=max_steps,
                  basket_pnl=0.0, logs=logs)

    while True:
        if os.path.exists(_STOP_FLAG):
            _log(logs, "Stop flag detected — shutting down (open positions left as-is).")
            _write_status(status="stopped", login=info.login, logs=logs)
            os.remove(_STOP_FLAG)
            break

        try:
            positions = [p for p in (mt5.positions_get(symbol=real_symbol) or []) if p.magic == MAGIC]
            basket_pnl = round(sum(p.profit for p in positions), 2)

            if not positions:
                step = 0
                direction = None
                sig = _get_signal(real_symbol, timeframe)
                if sig:
                    result = _send_order(real_symbol, sig, base_lot, f"ForexBot-Grid-s0")
                    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                        direction = sig
                        step = 1
                        _log(logs, f"Step 1: {sig} {base_lot} lot @ {result.price}")
            else:
                direction = "BUY" if positions[0].type == 0 else "SELL"
                step = len(positions)
                target = tp_usd if tp_usd else max(1.0, base_lot * 100 * (1.5 ** step))
                if basket_pnl >= target:
                    closed = 0
                    for p in positions:
                        if _close_position(p.ticket):
                            closed += 1
                    _log(logs, f"Basket TP hit (+${basket_pnl:.2f}) — closed {closed}/{len(positions)} positions. Resetting.")
                    step = 0
                    direction = None
                elif basket_pnl < 0 and step < max_steps:
                    next_lot = round(base_lot * (multiplier ** step), 2)
                    result = _send_order(real_symbol, direction, next_lot, f"ForexBot-Grid-s{step}")
                    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                        _log(logs, f"Step {step+1}: added {direction} {next_lot} lot @ {result.price} (basket pnl ${basket_pnl:.2f})")
                elif step >= max_steps:
                    _log(logs, f"Max steps ({max_steps}) reached — holding, no new orders (basket pnl ${basket_pnl:.2f})")

            info = mt5.account_info()
            _write_status(status="running", login=info.login, name=info.name, server=info.server,
                          balance=info.balance, equity=info.equity, symbol=real_symbol,
                          step=step, max_steps=max_steps, direction=direction,
                          basket_pnl=basket_pnl, open_positions=len(positions), logs=logs)
        except Exception as e:
            _log(logs, f"Error in loop: {e}")
            _write_status(status="running", error=str(e), logs=logs)

        time.sleep(5)

    mt5.shutdown()


if __name__ == "__main__":
    main()
