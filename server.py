import asyncio
import json
import queue
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import List, Optional

import MetaTrader5 as mt5
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel


# ── MT5 SINGLE-THREAD WORKER ────────────────────────────────────────────────────
# MT5 Python API must be called from a single dedicated thread only.

_cmd_queue: queue.Queue = queue.Queue()


def _mt5_worker():
    """Runs forever on its own thread, executing MT5 calls."""
    mt5.initialize()
    info = mt5.account_info()
    if info:
        print(f"MT5 connected — {info.login} | {info.balance} {info.currency}")
    else:
        print("MT5 initialized — no account logged in yet")

    while True:
        fn, result_event, result_box = _cmd_queue.get()
        if fn is None:
            break
        try:
            result_box.append(fn())
        except Exception as e:
            result_box.append({"error": str(e)})
        finally:
            result_event.set()

    mt5.shutdown()


def _mt5_call(fn, timeout=10):
    """Call fn() on the MT5 worker thread, blocking until result is ready."""
    result_box = []
    ev = threading.Event()
    _cmd_queue.put((fn, ev, result_box))
    if not ev.wait(timeout=timeout):
        return {"error": "MT5 call timed out"}
    return result_box[0] if result_box else {"error": "No result"}


# ── FASTAPI APP ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    t = threading.Thread(target=_mt5_worker, daemon=True)
    t.start()
    await asyncio.sleep(1)
    print("FarhanFX Algo API — http://127.0.0.1:8000")
    yield
    _cmd_queue.put((None, None, None))


app = FastAPI(title="FarhanFX Algo API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── ACCOUNT ─────────────────────────────────────────────────────────────────────

@app.get("/api/account")
def get_account():
    def fn():
        info = mt5.account_info()
        if info is None:
            return {"error": "MT5 not connected", "code": str(mt5.last_error())}
        return {
            "login":       info.login,
            "name":        info.name,
            "server":      info.server,
            "balance":     info.balance,
            "equity":      info.equity,
            "margin":      info.margin,
            "free_margin": info.margin_free,
            "margin_level": round(info.margin_level, 2) if info.margin_level else 0,
            "profit":      round(info.profit, 2),
            "currency":    info.currency,
            "leverage":    info.leverage,
        }
    result = _mt5_call(fn)
    if "error" in result:
        return JSONResponse(result, status_code=503)
    return result


# ── MT5 CREDENTIAL LOGIN ────────────────────────────────────────────────────────

class ConnectRequest(BaseModel):
    login:    int
    password: str
    server:   str

@app.post("/api/connect")
def connect_mt5(req: ConnectRequest):
    def fn():
        ok = mt5.login(login=req.login, password=req.password, server=req.server)
        if not ok:
            err = mt5.last_error()
            return {"error": f"Login failed: {err[1]} (code {err[0]})"}
        info = mt5.account_info()
        if info is None:
            return {"error": "Logged in but could not get account info"}
        return {
            "success":  True,
            "login":    info.login,
            "name":     info.name,
            "server":   info.server,
            "balance":  info.balance,
            "currency": info.currency,
            "leverage": info.leverage,
        }
    result = _mt5_call(fn, timeout=15)
    if "error" in result:
        return JSONResponse(result, status_code=401)
    return result

@app.post("/api/disconnect")
def disconnect_mt5():
    def fn():
        mt5.shutdown()
        mt5.initialize()
        return {"success": True}
    return _mt5_call(fn)


# ── OPEN POSITIONS ──────────────────────────────────────────────────────────────

@app.get("/api/positions")
def get_positions():
    def fn():
        positions = mt5.positions_get()
        if positions is None:
            return []
        return [
            {
                "ticket":        p.ticket,
                "symbol":        p.symbol,
                "type":          "BUY" if p.type == 0 else "SELL",
                "volume":        p.volume,
                "open_price":    p.price_open,
                "current_price": p.price_current,
                "sl":            p.sl,
                "tp":            p.tp,
                "profit":        round(p.profit, 2),
                "swap":          round(p.swap, 2),
                "open_time":     datetime.fromtimestamp(p.time).strftime("%Y-%m-%d %H:%M"),
                "comment":       p.comment,
            }
            for p in positions
        ]
    return _mt5_call(fn)


# ── DEAL HISTORY ────────────────────────────────────────────────────────────────

@app.get("/api/deals")
def get_deals(days: int = 30, limit: int = 100):
    def fn():
        date_from = datetime.now() - timedelta(days=days)
        deals = mt5.history_deals_get(date_from, datetime.now())
        if deals is None:
            return []
        result = []
        for d in deals:
            if d.type not in (0, 1):
                continue
            result.append({
                "ticket":     d.ticket,
                "order":      d.order,
                "symbol":     d.symbol,
                "type":       "BUY" if d.type == 0 else "SELL",
                "volume":     d.volume,
                "price":      d.price,
                "profit":     round(d.profit, 2),
                "commission": round(d.commission, 2),
                "swap":       round(d.swap, 2),
                "time":       datetime.fromtimestamp(d.time).strftime("%Y-%m-%d %H:%M"),
                "comment":    d.comment,
            })
        return result[-limit:]
    return _mt5_call(fn)


# ── SYMBOLS LIST ────────────────────────────────────────────────────────────────

@app.get("/api/symbols")
def get_symbols():
    def fn():
        all_syms = mt5.symbols_get()
        if not all_syms:
            return {"error": "Cannot get symbols"}
        result = []
        for s in all_syms:
            result.append({
                "name": s.name,
                "description": s.description,
                "path": s.path,
            })
        return result
    return _mt5_call(fn)


# ── PRICE ───────────────────────────────────────────────────────────────────────

@app.get("/api/price/{symbol}")
def get_price(symbol: str):
    def fn():
        real = _resolve_symbol(symbol)
        mt5.symbol_select(real, True)
        tick = mt5.symbol_info_tick(real)
        if tick is None:
            return {"error": f"Symbol '{symbol}' not found"}
        return {
            "symbol": real,
            "bid":    tick.bid,
            "ask":    tick.ask,
            "spread": round((tick.ask - tick.bid) * 100000, 1),
            "time":   datetime.fromtimestamp(tick.time).strftime("%H:%M:%S"),
        }
    result = _mt5_call(fn)
    if "error" in result:
        return JSONResponse(result, status_code=404)
    return result


# ── PLACE ORDER ─────────────────────────────────────────────────────────────────

class OrderRequest(BaseModel):
    symbol:     str
    order_type: str
    volume:     float
    sl:         Optional[float] = 0.0
    tp:         Optional[float] = 0.0
    comment:    str = "FarhanFX Algo"

@app.post("/api/order")
def place_order(req: OrderRequest):
    def fn():
        real = _resolve_symbol(req.symbol)
        sym = mt5.symbol_info(real)
        if sym is None:
            return {"error": f"Symbol '{req.symbol}' not found"}
        if not sym.visible:
            mt5.symbol_select(real, True)
        req.symbol = real

        tick = mt5.symbol_info_tick(req.symbol)
        if tick is None:
            return {"error": "Cannot get tick data"}

        is_buy = req.order_type.upper() == "BUY"
        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       req.symbol,
            "volume":       req.volume,
            "type":         mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
            "price":        tick.ask if is_buy else tick.bid,
            "sl":           req.sl,
            "tp":           req.tp,
            "deviation":    20,
            "magic":        234000,
            "comment":      req.comment,
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        # Try all filling modes — CXM Direct requirements vary by instrument
        result = None
        for filling in [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]:
            request["type_filling"] = filling
            result = mt5.order_send(request)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                break
            if result.retcode != 10038:  # 10038 = invalid filling — try next
                break
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return {"error": f"{result.comment} (retcode {result.retcode})"}
        return {"success": True, "ticket": result.order, "price": result.price}

    result = _mt5_call(fn, timeout=15)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


# ── CLOSE POSITION ──────────────────────────────────────────────────────────────

@app.post("/api/close/{ticket}")
def close_position(ticket: int):
    def fn():
        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return {"error": "Position not found"}
        p = pos[0]
        tick = mt5.symbol_info_tick(p.symbol)
        if tick is None:
            return {"error": "Cannot get tick"}
        is_buy = p.type == 0
        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       p.symbol,
            "volume":       p.volume,
            "type":         mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "position":     ticket,
            "price":        tick.bid if is_buy else tick.ask,
            "deviation":    20,
            "magic":        234000,
            "comment":      "FarhanFX Close",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return {"error": f"{result.comment} (retcode {result.retcode})"}
        return {"success": True}

    result = _mt5_call(fn, timeout=15)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


# ── WEBSOCKET — live account + positions ────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    loop = asyncio.get_event_loop()
    try:
        while True:
            def get_live():
                info = mt5.account_info()
                positions = mt5.positions_get()
                return {
                    "account": {
                        "balance":     info.balance,
                        "equity":      info.equity,
                        "profit":      round(info.profit, 2),
                        "margin":      info.margin,
                        "free_margin": info.margin_free,
                    } if info else None,
                    "open_count": len(positions) if positions else 0,
                }
            data = await loop.run_in_executor(None, lambda: _mt5_call(get_live))
            await websocket.send_text(json.dumps(data))
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


# ── ALGO STRATEGY ENGINE ────────────────────────────────────────────────────────

import time as _time

# Active strategies: id -> {config, thread, stop_event, log}
_strategies: dict = {}

TF_MAP = {
    "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}

def _resolve_symbol(symbol: str) -> str:
    """Find exact MT5 symbol name case-insensitively."""
    if mt5.symbol_info(symbol):
        return symbol
    all_syms = mt5.symbols_get()
    if all_syms:
        low = symbol.lower()
        for s in all_syms:
            if s.name.lower() == low:
                return s.name
    return symbol

def _get_rates(symbol, tf, count=100):
    def fn():
        real = _resolve_symbol(symbol)
        mt5.symbol_select(real, True)
        return mt5.copy_rates_from_pos(real, TF_MAP.get(tf, mt5.TIMEFRAME_H1), 0, count), real
    result = _mt5_call(fn)
    if isinstance(result, dict):
        return None, symbol
    return result  # (rates, real_symbol)

def _ema(prices, period):
    k = 2 / (period + 1)
    ema = [prices[0]]
    for p in prices[1:]:
        ema.append(p * k + ema[-1] * (1 - k))
    return ema

def _rsi(prices, period=14):
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period-1) + gains[i]) / period
        avg_l = (avg_l * (period-1) + losses[i]) / period
    if avg_l == 0:
        return 100
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 2)

def _bollinger(prices, period=20, dev=2):
    import math
    sma = sum(prices[-period:]) / period
    std = math.sqrt(sum((p - sma)**2 for p in prices[-period:]) / period)
    return sma + dev*std, sma, sma - dev*std

def _macd(prices, fast=12, slow=26, signal=9):
    ef = _ema(prices, fast)
    es = _ema(prices, slow)
    macd = [f - s for f, s in zip(ef, es)]
    sig  = _ema(macd, signal)
    return macd, sig

def _stochastic(highs, lows, closes, k=14, d=3):
    kv = []
    for i in range(len(closes)):
        if i < k - 1:
            kv.append(50.0); continue
        hh = max(highs[i-k+1:i+1])
        ll = min(lows[i-k+1:i+1])
        kv.append((closes[i]-ll)/(hh-ll)*100 if hh != ll else 50.0)
    dv = []
    for i in range(len(kv)):
        dv.append(sum(kv[max(0,i-d+1):i+1]) / min(d, i+1))
    return kv, dv

def _atr(highs, lows, closes, period=14):
    tr = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, len(closes))]
    if len(tr) < period:
        return 0
    atr = sum(tr[:period]) / period
    for t in tr[period:]:
        atr = (atr * (period-1) + t) / period
    return atr

def _cci(highs, lows, closes, period=20):
    tp = [(h+l+c)/3 for h,l,c in zip(highs, lows, closes)]
    if len(tp) < period:
        return 0
    sl = tp[-period:]
    sma = sum(sl) / period
    mad = sum(abs(t - sma) for t in sl) / period
    return (tp[-1] - sma) / (0.015 * mad) if mad else 0

def _williams_r(highs, lows, closes, period=14):
    if len(closes) < period:
        return -50
    hh = max(highs[-period:])
    ll = min(lows[-period:])
    return (hh - closes[-1]) / (hh - ll) * -100 if hh != ll else -50

def _sma(prices, period):
    return sum(prices[-period:]) / period if len(prices) >= period else prices[-1]


# ── SMC (Smart Money Concepts) Helpers ──────────────────────────────────────────

def _swing_high(highs, i, left=5, right=2):
    """True if highs[i] is a swing high (highest in left+right window, no lookahead — uses right bars already passed)"""
    if i < left or i + right >= len(highs):
        return False
    peak = highs[i]
    return all(highs[i-j] <= peak for j in range(1, left+1)) and \
           all(highs[i+j] <= peak for j in range(1, right+1))

def _swing_low(lows, i, left=5, right=2):
    """True if lows[i] is a swing low"""
    if i < left or i + right >= len(lows):
        return False
    trough = lows[i]
    return all(lows[i-j] >= trough for j in range(1, left+1)) and \
           all(lows[i+j] >= trough for j in range(1, right+1))

def _get_recent_swing_highs(highs, lows, up_to, lookback=100, left=5, right=2):
    """Return list of (idx, price) swing highs found up to index"""
    result = []
    start = max(left, up_to - lookback)
    end   = up_to - right
    for i in range(start, end):
        if _swing_high(highs, i, left, right):
            result.append((i, highs[i]))
    return result

def _get_recent_swing_lows(highs, lows, up_to, lookback=100, left=5, right=2):
    result = []
    start = max(left, up_to - lookback)
    end   = up_to - right
    for i in range(start, end):
        if _swing_low(lows, i, left, right):
            result.append((i, lows[i]))
    return result

def _find_fvg(highs, lows, idx):
    """Fair Value Gap check at idx (needs idx >= 2)"""
    if idx < 2:
        return None, None, None
    # Bullish FVG: gap between [idx-2].high and [idx].low (price ran up fast)
    if lows[idx] > highs[idx-2]:
        return "bullish", highs[idx-2], lows[idx]
    # Bearish FVG: gap between [idx-2].low and [idx].high (price fell fast)
    if highs[idx] < lows[idx-2]:
        return "bearish", highs[idx], lows[idx-2]
    return None, None, None

def _last_ob(opens, closes, highs, lows, from_idx, direction, lookback=20):
    """Find last Order Block before index.
       Bullish OB = last bearish candle before bullish impulse.
       Bearish OB = last bullish candle before bearish impulse."""
    start = max(0, from_idx - lookback)
    if direction == "bullish":
        for j in range(from_idx, start - 1, -1):
            if closes[j] < opens[j]:          # red/bearish candle
                return lows[j], highs[j]
    else:
        for j in range(from_idx, start - 1, -1):
            if closes[j] > opens[j]:          # green/bullish candle
                return lows[j], highs[j]
    return None, None


def _compute_smc_ob_signals(opens, closes, highs, lows, n, swing_lb=5, expiry=60):
    """SMC: BOS + Order Block retest entry"""
    left = swing_lb; right = max(2, swing_lb // 2)
    signals    = [None] * n
    sh_list    = []
    sl_list    = []
    active_ob  = None

    for i in range(left + right + 2, n):
        check = i - right
        if check >= left:
            if _swing_high(highs, check, left, right):
                sh_list.append((check, highs[check]))
            if _swing_low(lows, check, left, right):
                sl_list.append((check, lows[check]))

        if sl_list and closes[i] < sl_list[-1][1]:
            sl_idx, _ = sl_list[-1]
            ob_lo, ob_hi = _last_ob(opens, closes, highs, lows, sl_idx - 1, "bearish")
            if ob_lo is not None:
                active_ob = {'dir': 'sell', 'low': ob_lo, 'high': ob_hi, 'valid_until': i + expiry}
            sl_list = [s for s in sl_list if s[1] > closes[i]]

        if sh_list and closes[i] > sh_list[-1][1]:
            sh_idx, _ = sh_list[-1]
            ob_lo, ob_hi = _last_ob(opens, closes, highs, lows, sh_idx - 1, "bullish")
            if ob_lo is not None:
                active_ob = {'dir': 'buy', 'low': ob_lo, 'high': ob_hi, 'valid_until': i + expiry}
            sh_list = [s for s in sh_list if s[1] < closes[i]]  # remove broken highs

        # Entry: price retraces into OB zone
        if active_ob and i < active_ob['valid_until']:
            ob_lo, ob_hi = active_ob['low'], active_ob['high']
            if active_ob['dir'] == 'buy' and lows[i] <= ob_hi and closes[i] >= ob_lo:
                signals[i] = 'BUY'
                active_ob  = None
            elif active_ob['dir'] == 'sell' and highs[i] >= ob_lo and closes[i] <= ob_hi:
                signals[i] = 'SELL'
                active_ob  = None
        elif active_ob and i >= active_ob['valid_until']:
            active_ob = None

    return signals


def _compute_smc_fvg_signals(opens, closes, highs, lows, n, expiry=60):
    """SMC: Fair Value Gap — enter when price returns to fill the FVG"""
    signals  = [None] * n
    fvg_list = []   # [{'dir','lo','hi','created'}]

    for i in range(2, n):
        # Detect new FVGs
        ftype, flo, fhi = _find_fvg(highs, lows, i)
        if ftype:
            fvg_list.append({'dir': ftype, 'lo': flo, 'hi': fhi, 'created': i})

        # Expire old FVGs (older than 80 candles)
        fvg_list = [f for f in fvg_list if i - f['created'] < expiry]

        # Check if price returns to fill any FVG
        for f in fvg_list:
            if f['dir'] == 'bullish' and lows[i] <= f['hi'] and closes[i] >= f['lo']:
                signals[i] = 'BUY'
                fvg_list.remove(f)
                break
            elif f['dir'] == 'bearish' and highs[i] >= f['lo'] and closes[i] <= f['hi']:
                signals[i] = 'SELL'
                fvg_list.remove(f)
                break

    return signals


def _compute_smc_choch_signals(opens, closes, highs, lows, n, swing_lb=5):
    """SMC: Change of Character (ChoCH) — first BOS against prevailing trend"""
    signals   = [None] * n
    trend     = None   # 'bull' or 'bear'
    last_hh   = None
    last_ll   = None
    sh_list   = []
    sl_list   = []

    right = max(2, swing_lb // 2)
    for i in range(swing_lb + right + 2, n):
        check = i - right
        if check >= swing_lb:
            if _swing_high(highs, check, swing_lb, right):
                sh_list.append((check, highs[check]))
            if _swing_low(lows, check, swing_lb, right):
                sl_list.append((check, lows[check]))

        # Uptrend: HH + HL
        if sh_list and sl_list:
            if trend == 'bull':
                # ChoCH: price breaks below last HL (swing low) → reversal to bear
                if sl_list and closes[i] < sl_list[-1][1]:
                    signals[i] = 'SELL'
                    trend = 'bear'
                    sl_list = []
            elif trend == 'bear':
                # ChoCH: price breaks above last LH (swing high) → reversal to bull
                if sh_list and closes[i] > sh_list[-1][1]:
                    signals[i] = 'BUY'
                    trend = 'bull'
                    sh_list = []
            else:
                # Determine initial trend
                if len(sh_list) >= 2 and sh_list[-1][1] > sh_list[-2][1]:
                    trend = 'bull'
                elif len(sl_list) >= 2 and sl_list[-1][1] < sl_list[-2][1]:
                    trend = 'bear'

    return signals


def _compute_smc_liquidity_signals(opens, closes, highs, lows, n, swing_lb=5):
    """SMC: Liquidity Sweep — price sweeps above swing high / below swing low then reverses"""
    signals = [None] * n
    sh_list = []
    sl_list = []

    right = max(2, swing_lb // 2)
    for i in range(swing_lb + right + 2, n):
        check = i - right
        if check >= swing_lb:
            if _swing_high(highs, check, swing_lb, right):
                sh_list.append((check, highs[check]))
            if _swing_low(lows, check, swing_lb, right):
                sl_list.append((check, lows[check]))

        # Liquidity grab above swing high → price wicks above then closes below (fake breakout) → SELL
        if sh_list:
            prev_sh = sh_list[-1][1]
            if highs[i] > prev_sh and closes[i] < prev_sh:
                signals[i] = 'SELL'
                sh_list.pop()

        # Liquidity grab below swing low → price wicks below then closes above (stop hunt) → BUY
        if sl_list:
            prev_sl = sl_list[-1][1]
            if lows[i] < prev_sl and closes[i] > prev_sl:
                signals[i] = 'BUY'
                sl_list.pop()

    return signals

def _has_open_position(symbol, magic):
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return False
    return any(p.magic == magic for p in positions)

def _send_order(symbol, side, volume, sl, tp, magic, comment):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    sym = mt5.symbol_info(symbol)
    if sym and not sym.visible:
        mt5.symbol_select(symbol, True)
    is_buy = side == "BUY"
    req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       volume,
        "type":         mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
        "price":        tick.ask if is_buy else tick.bid,
        "sl":           sl,
        "tp":           tp,
        "deviation":    20,
        "magic":        magic,
        "comment":      comment,
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    return mt5.order_send(req)


def _strategy_runner(sid: str, cfg: dict, stop_ev: threading.Event, log: list):
    symbol   = cfg["symbol"]
    tf       = cfg["timeframe"]
    volume   = cfg["volume"]
    sl_pips  = cfg["sl"]
    tp_pips  = cfg["tp"]
    strategy = cfg["strategy"]
    magic    = 234000 + hash(sid) % 1000

    def add_log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        log.append(f"[{ts}] {msg}")
        if len(log) > 100:
            log.pop(0)

    # Resolve correct symbol name once at start
    real_symbol = _mt5_call(lambda: _resolve_symbol(symbol))
    if isinstance(real_symbol, dict):
        real_symbol = symbol
    if real_symbol != symbol:
        add_log(f"Symbol '{symbol}' resolved to '{real_symbol}'")
    symbol = real_symbol

    add_log(f"Strategy '{strategy}' started on {symbol} {tf}")

    def do_trade(side, entry, sl_val, tp_val):
        def fn():
            if _has_open_position(symbol, magic):
                return {"skip": True}
            return _send_order(symbol, side, volume, sl_val, tp_val, magic, f"FarhanFX-{strategy}")
        result = _mt5_call(fn)
        if result and isinstance(result, dict) and result.get("skip"):
            return
        if result and hasattr(result, "retcode") and result.retcode == mt5.TRADE_RETCODE_DONE:
            add_log(f"✅ {side} #{result.order} @ {result.price:.5f}  SL:{sl_val:.5f}  TP:{tp_val:.5f}")
            _strategies[sid]["trades"] += 1
        elif result:
            err = result.comment if hasattr(result, "comment") else str(result)
            add_log(f"❌ Order failed: {err}")

    while not stop_ev.is_set():
        try:
            rates, symbol = _get_rates(symbol, tf, 100)
            if rates is None or (isinstance(rates, dict) and "error" in rates):
                add_log(f"⚠️ Cannot get rates for '{symbol}'")
                _time.sleep(30)
                continue

            closes = [r["close"] for r in rates]

            def get_tick_pip():
                t = mt5.symbol_info_tick(symbol)
                s = mt5.symbol_info(symbol)
                return t, s
            tick_data = _mt5_call(get_tick_pip)
            if isinstance(tick_data, dict):
                _time.sleep(5)
                continue
            tick, sym_info = tick_data
            if tick is None or sym_info is None:
                _time.sleep(5)
                continue

            pip = sym_info.point * 10
            signal = None

            if strategy == "MA Cross":
                fast = _ema(closes, 20)
                slow = _ema(closes, 50)
                if fast[-2] < slow[-2] and fast[-1] > slow[-1]:
                    signal = "BUY"
                elif fast[-2] > slow[-2] and fast[-1] < slow[-1]:
                    signal = "SELL"

            elif strategy == "RSI":
                rsi = _rsi(closes)
                _strategies[sid]["indicator"] = f"RSI: {rsi}"
                if rsi < 30:
                    signal = "BUY"
                elif rsi > 70:
                    signal = "SELL"

            elif strategy == "Bollinger Bands":
                upper, mid, lower = _bollinger(closes)
                price = closes[-1]
                _strategies[sid]["indicator"] = f"BB: {lower:.5f} / {mid:.5f} / {upper:.5f}"
                if price < lower:
                    signal = "BUY"
                elif price > upper:
                    signal = "SELL"

            elif strategy == "EMA Trend":
                ema20 = _ema(closes, 20)
                ema100 = _ema(closes, 100)
                price = closes[-1]
                _strategies[sid]["indicator"] = f"EMA20:{ema20[-1]:.5f} EMA100:{ema100[-1]:.5f}"
                if price > ema20[-1] > ema100[-1]:
                    signal = "BUY"
                elif price < ema20[-1] < ema100[-1]:
                    signal = "SELL"

            elif strategy == "Scalper":
                fast = _ema(closes, 5)
                slow = _ema(closes, 13)
                rsi = _rsi(closes, 7)
                _strategies[sid]["indicator"] = f"EMA5:{fast[-1]:.5f} RSI7:{rsi}"
                if fast[-2] < slow[-2] and fast[-1] > slow[-1] and rsi < 60:
                    signal = "BUY"
                elif fast[-2] > slow[-2] and fast[-1] < slow[-1] and rsi > 40:
                    signal = "SELL"

            if signal:
                is_buy = signal == "BUY"
                entry = tick.ask if is_buy else tick.bid
                sl_val = entry - sl_pips * pip if is_buy else entry + sl_pips * pip
                tp_val = entry + tp_pips * pip if is_buy else entry - tp_pips * pip
                add_log(f"📊 Signal: {signal} @ {entry:.5f}")
                do_trade(signal, entry, sl_val, tp_val)

        except Exception as e:
            add_log(f"⚠️ Error: {e}")

        _time.sleep(30)

    add_log(f"Strategy '{strategy}' stopped")


class StrategyRequest(BaseModel):
    strategy:  str   # "MA Cross" | "RSI" | "Bollinger Bands" | "EMA Trend" | "Scalper"
    symbol:    str
    timeframe: str
    volume:    float
    sl:        float = 20.0
    tp:        float = 40.0

@app.post("/api/strategy/start")
def start_strategy(req: StrategyRequest):
    import uuid
    sid = str(uuid.uuid4())[:8]
    stop_ev = threading.Event()
    log = []
    cfg = req.model_dump()
    _strategies[sid] = {
        "id":        sid,
        "config":    cfg,
        "status":    "running",
        "trades":    0,
        "indicator": "",
        "log":       log,
        "started":   datetime.now().strftime("%H:%M:%S"),
    }
    t = threading.Thread(target=_strategy_runner, args=(sid, cfg, stop_ev, log), daemon=True)
    _strategies[sid]["_stop"] = stop_ev
    t.start()
    return {"success": True, "id": sid}

@app.post("/api/strategy/stop/{sid}")
def stop_strategy(sid: str):
    if sid not in _strategies:
        return JSONResponse({"error": "Strategy not found"}, status_code=404)
    _strategies[sid]["_stop"].set()
    _strategies[sid]["status"] = "stopped"
    return {"success": True}

@app.get("/api/strategy/list")
def list_strategies():
    result = []
    for sid, s in _strategies.items():
        result.append({
            "id":        sid,
            "strategy":  s["config"]["strategy"],
            "symbol":    s["config"]["symbol"],
            "timeframe": s["config"]["timeframe"],
            "volume":    s["config"]["volume"],
            "status":    s["status"],
            "trades":    s["trades"],
            "indicator": s.get("indicator", ""),
            "started":   s["started"],
            "log":       s["log"][-5:],
        })
    return result

@app.delete("/api/strategy/{sid}")
def delete_strategy(sid: str):
    if sid in _strategies:
        _strategies[sid]["_stop"].set()
        del _strategies[sid]
    return {"success": True}


# ── REAL MT5 BACKTEST ENGINE ────────────────────────────────────────────────────

import math as _math

class RuleCondition(BaseModel):
    ind:        str            # RSI, EMA, SMA, CLOSE, MACD, MACD_SIG, BB_UP, BB_MID, BB_LOW, STOCH_K, STOCH_D, CCI, WR, ATR
    period:     int   = 14
    op:         str            # gt, lt, gte, lte, cross_up, cross_dn
    cmp_type:   str   = "value"   # "value" or "indicator"
    value:      float = 0.0
    cmp_ind:    str   = ""
    cmp_period: int   = 14

class BacktestRequest(BaseModel):
    strategy:     str
    symbol:       str
    timeframe:    str
    date_from:    str
    date_to:      str
    capital:      float = 10000.0
    risk_pct:     float = 1.0
    sl_pips:      float = 20.0
    tp_pips:      float = 40.0
    custom_buy:   List[RuleCondition] = []
    custom_sell:  List[RuleCondition] = []
    smc_swing_lb: int   = 5       # bars for swing detection
    smc_expiry:   int   = 60      # candles before OB/FVG expires
    smc_entry:    str   = "close" # touch | close | midpoint
    smc_session:  str   = "all"   # all | london | newyork | lnny | asian

@app.post("/api/backtest")
def run_backtest(req: BacktestRequest):
    def fn():
        symbol = _resolve_symbol(req.symbol)
        mt5.symbol_select(symbol, True)

        tf = TF_MAP.get(req.timeframe, mt5.TIMEFRAME_H1)
        dt_from = datetime.strptime(req.date_from, "%Y-%m-%d")
        dt_to   = datetime.strptime(req.date_to,   "%Y-%m-%d")

        rates = mt5.copy_rates_range(symbol, tf, dt_from, dt_to)
        if rates is None or len(rates) == 0:
            return {"error": f"No data for {symbol} {req.timeframe} in selected range"}

        sym_info = mt5.symbol_info(symbol)
        pip = sym_info.point * 10 if sym_info else 0.0001

        return rates, pip, symbol

    result = _mt5_call(fn, timeout=30)
    if isinstance(result, dict) and "error" in result:
        return JSONResponse(result, status_code=400)

    rates, pip, symbol = result
    closes  = [r["close"] for r in rates]
    highs   = [r["high"]  for r in rates]
    lows    = [r["low"]   for r in rates]
    times   = [datetime.fromtimestamp(r["time"]).strftime("%Y-%m-%d %H:%M") for r in rates]
    n       = len(closes)

    if n < 110:
        return JSONResponse({"error": f"Not enough data — only {n} candles. Try a wider date range or lower timeframe."}, status_code=400)

    opens      = [r["open"]  for r in rates]
    timestamps = [r["time"]  for r in rates]   # Unix UTC timestamps

    # ── Session mask ───────────────────────────────────────────────────────────
    def _in_session(ts: int, session: str) -> bool:
        if session == "all":
            return True
        from datetime import timezone
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        h = dt.hour
        if session == "london":   return 7 <= h < 16
        if session == "newyork":  return 13 <= h < 21
        if session == "lnny":     return 7 <= h < 21
        if session == "asian":    return 0 <= h < 9
        return True

    sess = req.smc_session

    # ── Signal computation ─────────────────────────────────────────────────────
    strat = req.strategy

    # SMC strategies use full-pass pre-computation (need lookahead for swing detection)
    lb  = req.smc_swing_lb
    exp = req.smc_expiry
    if strat == "SMC - Order Block":
        signals = _compute_smc_ob_signals(opens, closes, highs, lows, n, swing_lb=lb, expiry=exp)
    elif strat == "SMC - Fair Value Gap":
        signals = _compute_smc_fvg_signals(opens, closes, highs, lows, n, expiry=exp)
    elif strat == "SMC - ChoCH":
        signals = _compute_smc_choch_signals(opens, closes, highs, lows, n, swing_lb=lb)
    elif strat == "SMC - Liquidity Sweep":
        signals = _compute_smc_liquidity_signals(opens, closes, highs, lows, n, swing_lb=lb)

    # Apply session filter to SMC signals
    if strat.startswith("SMC"):
        signals = [
            sig if (sig and _in_session(timestamps[i], sess)) else None
            for i, sig in enumerate(signals)
        ]
    else:
        # All other strategies: candle-by-candle (no lookahead)
        signals = [None] * n
        for i in range(100, n):
            c   = closes[:i+1]
            sig = None

            if strat == "MA Cross":
                fast = _ema(c, 20); slow = _ema(c, 50)
                if fast[-2] < slow[-2] and fast[-1] > slow[-1]: sig = "BUY"
                elif fast[-2] > slow[-2] and fast[-1] < slow[-1]: sig = "SELL"

            elif strat == "RSI":
                rsi = _rsi(c)
                if rsi < 30: sig = "BUY"
                elif rsi > 70: sig = "SELL"

            elif strat == "Bollinger Bands":
                upper, mid, lower = _bollinger(c)
                if c[-1] < lower: sig = "BUY"
                elif c[-1] > upper: sig = "SELL"

            elif strat == "EMA Trend":
                ema20 = _ema(c, 20); ema100 = _ema(c, 100)
                if c[-1] > ema20[-1] > ema100[-1]: sig = "BUY"
                elif c[-1] < ema20[-1] < ema100[-1]: sig = "SELL"

            elif strat == "Scalper":
                fast = _ema(c, 5); slow = _ema(c, 13); rsi = _rsi(c, 7)
                if fast[-2] < slow[-2] and fast[-1] > slow[-1] and rsi < 60: sig = "BUY"
                elif fast[-2] > slow[-2] and fast[-1] < slow[-1] and rsi > 40: sig = "SELL"

            elif strat == "MACD":
                macd, sig_line = _macd(c)
                if macd[-2] < sig_line[-2] and macd[-1] > sig_line[-1]: sig = "BUY"
                elif macd[-2] > sig_line[-2] and macd[-1] < sig_line[-1]: sig = "SELL"

            elif strat == "MACD + EMA Trend":
                ema200 = _ema(c, 200) if len(c) >= 200 else None
                if ema200:
                    macd, sig_line = _macd(c)
                    if c[-1] > ema200[-1] and macd[-2] < sig_line[-2] and macd[-1] > sig_line[-1]: sig = "BUY"
                    elif c[-1] < ema200[-1] and macd[-2] > sig_line[-2] and macd[-1] < sig_line[-1]: sig = "SELL"

            elif strat == "Stochastic":
                h = highs[:i+1]; l = lows[:i+1]
                kv, dv = _stochastic(h, l, c)
                if kv[-2] < 20 and dv[-2] < 20 and kv[-1] > dv[-1] and kv[-2] <= dv[-2]: sig = "BUY"
                elif kv[-2] > 80 and dv[-2] > 80 and kv[-1] < dv[-1] and kv[-2] >= dv[-2]: sig = "SELL"

            elif strat == "Stochastic + RSI":
                h = highs[:i+1]; l = lows[:i+1]
                kv, dv = _stochastic(h, l, c); rsi = _rsi(c)
                if kv[-1] < 25 and rsi < 35: sig = "BUY"
                elif kv[-1] > 75 and rsi > 65: sig = "SELL"

            elif strat == "ATR Breakout":
                h = highs[:i+1]; l = lows[:i+1]
                atr = _atr(h, l, c)
                if c[-1] > max(highs[i-20:i]) and atr > 0: sig = "BUY"
                elif c[-1] < min(lows[i-20:i]) and atr > 0: sig = "SELL"

            elif strat == "CCI":
                h = highs[:i+1]; l = lows[:i+1]
                cci_now = _cci(h, l, c); cci_prev = _cci(h[:-1], l[:-1], c[:-1])
                if cci_prev < -100 and cci_now >= -100: sig = "BUY"
                elif cci_prev > 100 and cci_now <= 100: sig = "SELL"

            elif strat == "Williams %R":
                h = highs[:i+1]; l = lows[:i+1]
                wr = _williams_r(h, l, c); wr_prev = _williams_r(h[:-1], l[:-1], c[:-1])
                if wr_prev < -80 and wr >= -80: sig = "BUY"
                elif wr_prev > -20 and wr <= -20: sig = "SELL"

            elif strat == "Trend + RSI":
                ema200 = _ema(c, 200) if len(c) >= 200 else None
                if ema200:
                    rsi = _rsi(c); rsi_prev = _rsi(c[:-1])
                    if c[-1] > ema200[-1] and rsi_prev < 50 and rsi >= 50: sig = "BUY"
                    elif c[-1] < ema200[-1] and rsi_prev > 50 and rsi <= 50: sig = "SELL"

            elif strat == "Triple EMA":
                ema5 = _ema(c, 5); ema20 = _ema(c, 20); ema50 = _ema(c, 50)
                if ema5[-2] < ema20[-2] and ema5[-1] > ema20[-1] and ema20[-1] > ema50[-1]: sig = "BUY"
                elif ema5[-2] > ema20[-2] and ema5[-1] < ema20[-1] and ema20[-1] < ema50[-1]: sig = "SELL"

            elif strat == "Bollinger + RSI":
                upper, mid, lower = _bollinger(c); rsi = _rsi(c)
                if c[-1] < lower and rsi < 35: sig = "BUY"
                elif c[-1] > upper and rsi > 65: sig = "SELL"

            elif strat == "SMA Cross":
                s20_p = _sma(closes[:i], 20); s50_p = _sma(closes[:i], 50)
                s20   = _sma(c, 20);           s50   = _sma(c, 50)
                if s20_p < s50_p and s20 > s50: sig = "BUY"
                elif s20_p > s50_p and s20 < s50: sig = "SELL"

            elif strat == "Custom Strategy":
                h = highs[:i+1]; l = lows[:i+1]
                hp = highs[:i];   lp = lows[:i]
                def _iv(ind, period, cv, hv, lv):
                    if ind == "RSI":      return _rsi(cv, period)
                    if ind == "EMA":      return _ema(cv, period)[-1]
                    if ind == "SMA":      return _sma(cv, period)
                    if ind == "CLOSE":    return cv[-1]
                    if ind == "MACD":     return _macd(cv)[0][-1]
                    if ind == "MACD_SIG": return _macd(cv)[1][-1]
                    if ind == "BB_UP":    return _bollinger(cv)[0]
                    if ind == "BB_MID":   return _bollinger(cv)[1]
                    if ind == "BB_LOW":   return _bollinger(cv)[2]
                    if ind == "STOCH_K":  return _stochastic(hv, lv, cv)[0][-1]
                    if ind == "STOCH_D":  return _stochastic(hv, lv, cv)[1][-1]
                    if ind == "CCI":      return _cci(hv, lv, cv, period)
                    if ind == "WR":       return _williams_r(hv, lv, cv, period)
                    if ind == "ATR":      return _atr(hv, lv, cv, period)
                    return 0
                def _cr(rule, cv, hv, lv, cv_p, hv_p, lv_p):
                    now = _iv(rule.ind, rule.period, cv, hv, lv)
                    prev = _iv(rule.ind, rule.period, cv_p, hv_p, lv_p)
                    cn = rule.value if rule.cmp_type == "value" else _iv(rule.cmp_ind, rule.cmp_period, cv, hv, lv)
                    cp = rule.value if rule.cmp_type == "value" else _iv(rule.cmp_ind, rule.cmp_period, cv_p, hv_p, lv_p)
                    if rule.op == "gt":       return now > cn
                    if rule.op == "lt":       return now < cn
                    if rule.op == "gte":      return now >= cn
                    if rule.op == "lte":      return now <= cn
                    if rule.op == "cross_up": return prev <= cp and now > cn
                    if rule.op == "cross_dn": return prev >= cp and now < cn
                    return False
                if req.custom_buy and all(_cr(r, c, h, l, closes[:i], hp, lp) for r in req.custom_buy):
                    sig = "BUY"
                elif req.custom_sell and all(_cr(r, c, h, l, closes[:i], hp, lp) for r in req.custom_sell):
                    sig = "SELL"

            signals[i] = sig

    # ── Simulate trades ────────────────────────────────────────────────────────
    capital   = req.capital
    equity    = capital
    sl_p      = req.sl_pips * pip
    tp_p      = req.tp_pips * pip
    risk_amt  = capital * req.risk_pct / 100

    trades    = []
    equity_curve = [capital]
    position  = None   # {"side","entry","sl","tp","open_time","open_idx"}

    for i in range(100, n):
        c_high = highs[i]
        c_low  = lows[i]
        c_close = closes[i]

        # Check if open position hit SL or TP
        if position:
            hit_tp = hit_sl = False
            if position["side"] == "BUY":
                if c_high >= position["tp"]:
                    hit_tp = True
                elif c_low <= position["sl"]:
                    hit_sl = True
            else:
                if c_low <= position["tp"]:
                    hit_tp = True
                elif c_high >= position["sl"]:
                    hit_sl = True

            if hit_tp or hit_sl:
                pnl = risk_amt * req.tp_pips / req.sl_pips if hit_tp else -risk_amt
                equity += pnl
                trades.append({
                    "num":       len(trades) + 1,
                    "side":      position["side"],
                    "entry":     round(position["entry"], 5),
                    "exit":      round(position["tp"] if hit_tp else position["sl"], 5),
                    "result":    "WIN" if hit_tp else "LOSS",
                    "pnl":       round(pnl, 2),
                    "open_time": position["open_time"],
                    "close_time":times[i],
                    "duration":  f"{i - position['open_idx']} bars",
                })
                equity_curve.append(round(equity, 2))
                position = None

        # Open new trade on signal (only if no open position)
        if not position and signals[i]:
            entry = c_close
            if signals[i] == "BUY":
                sl = entry - sl_p
                tp = entry + tp_p
            else:
                sl = entry + sl_p
                tp = entry - tp_p
            position = {
                "side":      signals[i],
                "entry":     entry,
                "sl":        sl,
                "tp":        tp,
                "open_time": times[i],
                "open_idx":  i,
            }

    # Close any open position at last price
    if position:
        pnl = (closes[-1] - position["entry"]) * (1 if position["side"] == "BUY" else -1) / pip * risk_amt / req.sl_pips
        equity += pnl
        equity_curve.append(round(equity, 2))

    # ── Metrics ────────────────────────────────────────────────────────────────
    if not trades:
        return JSONResponse({"error": "No trades generated — try different date range or strategy"}, status_code=400)

    wins    = [t for t in trades if t["result"] == "WIN"]
    losses  = [t for t in trades if t["result"] == "LOSS"]
    win_rate = len(wins) / len(trades) * 100
    net_pnl  = round(equity - capital, 2)
    ret_pct  = round(net_pnl / capital * 100, 2)

    gross_win  = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses)) or 1
    pf = round(gross_win / gross_loss, 2)

    # Max drawdown
    peak = capital
    max_dd = 0.0
    dd_curve = [0.0]
    running = capital
    for t in trades:
        running += t["pnl"]
        peak = max(peak, running)
        dd = (peak - running) / peak * 100
        max_dd = max(max_dd, dd)
        dd_curve.append(round(-dd, 2))

    # Sharpe (simplified daily returns)
    pnls = [t["pnl"] for t in trades]
    avg_pnl = sum(pnls) / len(pnls)
    std_pnl = _math.sqrt(sum((p - avg_pnl)**2 for p in pnls) / len(pnls)) or 1
    sharpe = round(avg_pnl / std_pnl * _math.sqrt(len(pnls)), 2)

    calmar = round(abs(ret_pct / max_dd), 2) if max_dd > 0 else 0

    return {
        "symbol":      symbol,
        "strategy":    strat,
        "timeframe":   req.timeframe,
        "date_from":   req.date_from,
        "date_to":     req.date_to,
        "candles":     n,
        "capital":     capital,
        "net_pnl":     net_pnl,
        "ret_pct":     ret_pct,
        "win_rate":    round(win_rate, 1),
        "total_trades":len(trades),
        "wins":        len(wins),
        "losses":      len(losses),
        "profit_factor": pf,
        "max_dd":      round(max_dd, 2),
        "sharpe":      sharpe,
        "calmar":      calmar,
        "avg_win":     round(gross_win / len(wins), 2) if wins else 0,
        "avg_loss":    round(-gross_loss / len(losses), 2) if losses else 0,
        "equity_curve": equity_curve,
        "dd_curve":     dd_curve,
        "trades":      trades[-50:],   # last 50 trades for table
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)
