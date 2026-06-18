import asyncio
import hashlib
import json
import os
import queue
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import MetaTrader5 as mt5
import uvicorn
from fastapi import FastAPI, Header, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel


# ── MT5 SINGLE-THREAD WORKER ────────────────────────────────────────────────────
# MT5 Python API must be called from a single dedicated thread only.

_cmd_queue: queue.Queue = queue.Queue()

# Explicit terminal path — the VPS has a second MT5 terminal installed
# ("CXM Direct MT5 Terminal") logged into a different, unrelated account.
# Without specifying path, mt5.initialize() can attach to whichever terminal
# it finds first, silently connecting to the wrong account. This path is the
# terminal logged into the correct account (Minhajul Hoque, 698085).
# Falls back to None (default lookup) if this exact path doesn't exist —
# keeps local-PC runs (different install path) working unchanged.
_MT5_TERMINAL_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"
if not os.path.exists(_MT5_TERMINAL_PATH):
    _MT5_TERMINAL_PATH = None

def _mt5_init(**kwargs) -> bool:
    """mt5.initialize() wrapper that pins the terminal path when known."""
    if _MT5_TERMINAL_PATH:
        kwargs["path"] = _MT5_TERMINAL_PATH
    return mt5.initialize(**kwargs)


def _mt5_worker():
    """Runs forever on its own thread, executing MT5 calls."""
    # timeout=8000ms — don't hang if MT5 terminal is not running yet
    ok = _mt5_init(timeout=8000)
    if ok:
        info = mt5.account_info()
        if info:
            print(f"MT5 auto-connected — {info.login} | {info.balance} {info.currency}")
        else:
            print("MT5 ready — no account logged in yet")
    else:
        print(f"MT5 not available at startup — connect via UI | {mt5.last_error()}")

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
    # Restore saved crypto exchange connections in background
    threading.Thread(target=_restore_exchanges, daemon=True).start()
    # Restore algo strategies that were running before shutdown
    threading.Thread(target=_restore_strategies_state, daemon=True).start()
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

# ── USER AUTH ───────────────────────────────────────────────────────────────────

_USERS_FILE    = "users.json"
_auth_sessions: dict = {}   # token -> {"username": str, "created": str}

class AuthLoginRequest(BaseModel):
    username: str
    password: str

class AuthChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

def _hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()

def _load_users() -> dict:
    try:
        with open(_USERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"users": []}

def _save_users(data: dict):
    with open(_USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def _ensure_default_user():
    data = _load_users()
    if not data.get("users"):
        salt = secrets.token_hex(16)
        data["users"] = [{
            "username": "admin",
            "salt": salt,
            "password_hash": _hash_pw("admin123", salt),
            "display_name": "Admin"
        }]
        _save_users(data)

_ensure_default_user()

@app.post("/api/auth/login")
def auth_login(req: AuthLoginRequest):
    data  = _load_users()
    user  = next((u for u in data.get("users", []) if u["username"] == req.username), None)
    if not user or _hash_pw(req.password, user["salt"]) != user["password_hash"]:
        return JSONResponse({"error": "Invalid username or password"}, status_code=401)
    token = secrets.token_urlsafe(32)
    _auth_sessions[token] = {"username": req.username, "display_name": user.get("display_name", req.username),
                              "created": datetime.now().isoformat()}
    return {"token": token, "username": req.username, "display_name": user.get("display_name", req.username)}

@app.get("/api/auth/verify")
def auth_verify(authorization: str = Header(default=None)):
    token = (authorization or "").replace("Bearer ", "").strip()
    sess  = _auth_sessions.get(token)
    if not sess:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    return {"username": sess["username"], "display_name": sess.get("display_name", sess["username"])}

@app.post("/api/auth/logout")
def auth_logout(authorization: str = Header(default=None)):
    token = (authorization or "").replace("Bearer ", "").strip()
    _auth_sessions.pop(token, None)
    return {"ok": True}

@app.post("/api/auth/change_password")
def auth_change_password(req: AuthChangePasswordRequest, authorization: str = Header(default=None)):
    token = (authorization or "").replace("Bearer ", "").strip()
    sess  = _auth_sessions.get(token)
    if not sess:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    data  = _load_users()
    user  = next((u for u in data.get("users", []) if u["username"] == sess["username"]), None)
    if not user or _hash_pw(req.old_password, user["salt"]) != user["password_hash"]:
        return JSONResponse({"error": "Old password incorrect"}, status_code=400)
    user["salt"]          = secrets.token_hex(16)
    user["password_hash"] = _hash_pw(req.new_password, user["salt"])
    _save_users(data)
    return {"ok": True}


class ConnectRequest(BaseModel):
    login:    int
    password: str
    server:   str

_MT5_ERR = {
    -10004: "MT5 terminal not found — open MetaTrader 5 first",
    -10003: "MT5 initialization failed — restart MetaTrader 5",
    -10002: "MT5 connection timeout — check internet connection",
    -10001: "MT5 internal error",
        1:  "Connection timeout — check server name and internet",
        2:  "Invalid server — check broker server name",
        5:  "No MT5 terminal connection — open MetaTrader 5",
}

@app.post("/api/connect")
def connect_mt5(req: ConnectRequest):
    def fn():
        # Ensure MT5 is initialized — 10s timeout so we don't block forever
        if not _mt5_init(timeout=10000):
            code, msg = mt5.last_error()
            hint = "Open MetaTrader 5 terminal first, then try again"
            return {"error": hint, "code": code}

        # Login with 25-second internal timeout so wrapper can catch it
        ok = mt5.login(
            login=req.login,
            password=req.password,
            server=req.server,
            timeout=25000        # ms — MT5 gives up after 25s
        )
        if not ok:
            code, msg = mt5.last_error()
            friendly = _MT5_ERR.get(code)
            if not friendly:
                if code in (-2, -10002): friendly = "Wrong server name or no internet"
                elif "password" in msg.lower() or code in (3, 6): friendly = "Wrong account number or password"
                else: friendly = f"{msg} (code {code})"
            return {"error": friendly, "code": code}

        info = mt5.account_info()
        if info is None:
            return {"error": "Logged in but account info unavailable — try again"}

        return {
            "success":  True,
            "login":    info.login,
            "name":     info.name,
            "server":   info.server,
            "balance":  round(info.balance, 2),
            "equity":   round(info.equity, 2),
            "currency": info.currency,
            "leverage": info.leverage,
        }

    result = _mt5_call(fn, timeout=35)   # 35s — larger than mt5.login timeout
    if isinstance(result, dict) and "error" in result:
        return JSONResponse(result, status_code=401)
    return result

@app.post("/api/disconnect")
def disconnect_mt5():
    # Never shut down MT5 while algo strategies are actively running
    running = [s for s in _strategies.values() if s.get("status") == "running"]
    if running:
        return {"success": True, "skipped": True, "reason": f"{len(running)} strategies still running — MT5 kept alive"}
    def fn():
        mt5.shutdown()
        _mt5_init()
        return {"success": True}
    return _mt5_call(fn)


# ── OPEN POSITIONS ──────────────────────────────────────────────────────────────

@app.get("/api/positions")
def get_positions():
    def fn():
        positions = mt5.positions_get()
        if positions is None:
            return []
        now_ts = datetime.now().timestamp()
        result = []
        for p in positions:
            sym = mt5.symbol_info(p.symbol)
            # Pip size
            pip = 0.1 if sym and ("XAU" in p.symbol.upper() or "GOLD" in p.symbol.upper()) \
                else 1.0 if sym and any(x in p.symbol.upper() for x in ["BTC","ETH","NAS","US30","US500","GER","UK1"]) \
                else (sym.point * 10 if sym else 0.0001)
            # Pips profit
            diff = (p.price_current - p.price_open) if p.type == 0 else (p.price_open - p.price_current)
            pips = round(diff / pip, 1) if pip else 0
            # Duration
            secs  = int(now_ts - p.time)
            h, m  = secs // 3600, (secs % 3600) // 60
            dur   = f"{h}h {m}m" if h > 0 else f"{m}m"
            result.append({
                "ticket":        p.ticket,
                "symbol":        p.symbol,
                "type":          "BUY" if p.type == 0 else "SELL",
                "volume":        p.volume,
                "open_price":    round(p.price_open,    sym.digits if sym else 5),
                "current_price": round(p.price_current, sym.digits if sym else 5),
                "sl":            round(p.sl, sym.digits if sym else 5) if p.sl else 0,
                "tp":            round(p.tp, sym.digits if sym else 5) if p.tp else 0,
                "profit":        round(p.profit, 2),
                "swap":          round(p.swap, 2),
                "pips":          pips,
                "duration":      dur,
                "open_time":     datetime.fromtimestamp(p.time).strftime("%Y-%m-%d %H:%M"),
                "comment":       p.comment,
            })
        return result
    return _mt5_call(fn)


# ── DEAL HISTORY ────────────────────────────────────────────────────────────────

@app.get("/api/deals")
def get_deals(days: int = 30):
    """Returns matched complete trades (IN+OUT paired by position_id)."""
    def fn():
        # Use midnight of (today - days + 1) so days=1 = all of today,
        # days=7 = last 7 calendar days, etc. (avoids missing early-AM trades)
        now = datetime.now()
        date_from = datetime(now.year, now.month, now.day) - timedelta(days=days - 1)
        # Fetch wider window to catch opening deals for positions opened before the range
        date_from_wide = date_from - timedelta(days=180)
        # Add 60s buffer so very recently closed trades are never missed
        date_to = now + timedelta(seconds=60)
        deals_all = mt5.history_deals_get(date_from_wide, date_to)
        if deals_all is None:
            return []

        cutoff_ts = date_from.timestamp()

        # Split into IN and OUT maps by position_id
        in_map: dict  = {}   # position_id -> IN deal (last IN wins for partial opens)
        out_list       = []  # list of OUT deals within requested range

        for d in deals_all:
            if d.type not in (0, 1):  # only BUY/SELL deals
                continue
            # Opening deals always have profit=0; closing deals have profit!=0.
            # Using profit check is more reliable than d.entry (varies by broker/account type).
            is_close = not (d.profit == 0.0 and d.commission == 0.0 and d.swap == 0.0)
            if not is_close:           # opening deal → store for matching
                in_map[d.position_id] = d
            else:                      # closing deal
                if d.time >= cutoff_ts:
                    out_list.append(d)

        result = []
        for out in out_list:
            in_d   = in_map.get(out.position_id)
            sym    = mt5.symbol_info(out.symbol)
            digits = sym.digits if sym else 5

            # Pip size
            name = out.symbol.upper()
            if "XAU" in name or "GOLD" in name:   pip = 0.1
            elif any(x in name for x in ["BTC","ETH","NAS","US30","US500","GER","UK1","JP2","AUS","HK","SPX"]): pip = 1.0
            elif sym:                               pip = sym.point * 10
            else:                                   pip = 0.0001

            # Pips (based on entry type)
            if in_d and pip:
                is_buy = in_d.type == 0
                diff   = (out.price - in_d.price) if is_buy else (in_d.price - out.price)
                pips   = round(diff / pip, 1)
            else:
                pips = 0

            # Duration
            if in_d:
                secs = int(out.time - in_d.time)
                h, m = secs // 3600, (secs % 3600) // 60
                duration = f"{h}h {m}m" if h > 0 else f"{m}m"
            else:
                duration = "—"

            net = round(out.profit + out.commission + out.swap, 2)
            result.append({
                "position_id":  out.position_id,
                "symbol":       out.symbol,
                "type":         "BUY" if (in_d.type if in_d else out.type) == 0 else "SELL",
                "volume":       out.volume,
                "entry_price":  round(in_d.price, digits) if in_d else None,
                "exit_price":   round(out.price,  digits),
                "entry_time":   datetime.fromtimestamp(in_d.time).strftime("%Y-%m-%d %H:%M") if in_d else "—",
                "exit_time":    datetime.fromtimestamp(out.time).strftime("%Y-%m-%d %H:%M"),
                "duration":     duration,
                "pips":         pips,
                "gross_profit": round(out.profit, 2),
                "commission":   round(out.commission, 2),
                "swap":         round(out.swap, 2),
                "net_profit":   net,
                "comment":      out.comment or "",
                "win":          net > 0,
            })

        result.sort(key=lambda x: x["exit_time"], reverse=True)
        return result
    return _mt5_call(fn)


@app.get("/api/account/funding")
def get_account_funding(date_from: str = "", date_to: str = ""):
    """Returns deposit/withdrawal history (MT5 deal type 2 = BALANCE) + current balance."""
    def fn():
        info = mt5.account_info()
        if info is None:
            return {"error": "No account info"}
        now = datetime.now()
        if date_from and date_to:
            _df = datetime.strptime(date_from, "%Y-%m-%d")
            _dt = datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
        elif date_from:
            _df = datetime.strptime(date_from, "%Y-%m-%d")
            _dt = now
        elif date_to:
            _df = datetime(2010, 1, 1)
            _dt = datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
        else:
            _df = datetime(2010, 1, 1)
            _dt = now
        deals = mt5.history_deals_get(_df, _dt + timedelta(seconds=60))
        if deals is None:
            deals = []
        entries = []
        total_deposit = 0.0
        total_withdrawal = 0.0
        for d in deals:
            if d.type != 2:  # DEAL_TYPE_BALANCE only
                continue
            amount = round(d.profit, 2)
            kind = "deposit" if amount >= 0 else "withdrawal"
            if kind == "deposit":
                total_deposit += amount
            else:
                total_withdrawal += abs(amount)
            entries.append({
                "ticket": d.ticket,
                "time": datetime.fromtimestamp(d.time).strftime("%Y-%m-%d %H:%M:%S"),
                "amount": amount,
                "kind": kind,
                "comment": d.comment or "",
            })
        entries.sort(key=lambda x: x["time"], reverse=True)
        return {
            "balance": round(info.balance, 2),
            "equity": round(info.equity, 2),
            "currency": info.currency,
            "total_deposit": round(total_deposit, 2),
            "total_withdrawal": round(total_withdrawal, 2),
            "net_funded": round(total_deposit - total_withdrawal, 2),
            "entries": entries,
        }
    return _mt5_call(fn)


@app.get("/api/deals/today")
def get_deals_today():
    """Raw today's deals for debugging TODAY REALIZED discrepancies."""
    def fn():
        now   = datetime.now()
        today = datetime(now.year, now.month, now.day)
        raw   = mt5.history_deals_get(today, now + timedelta(seconds=60))
        if raw is None:
            return {"error": mt5.last_error(), "deals": []}
        out = []
        for d in raw:
            out.append({
                "ticket": d.ticket, "order": d.order,
                "position_id": d.position_id,
                "type": d.type, "entry": d.entry,
                "time": datetime.fromtimestamp(d.time).strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": d.symbol, "volume": d.volume,
                "price": d.price, "profit": d.profit,
                "commission": d.commission, "comment": d.comment,
            })
        return {"count": len(out), "net_profit": round(sum(d["profit"]+d["commission"] for d in out if d["entry"] in (1,2,3) and d["type"] in (0,1)), 2), "deals": out}
    return _mt5_call(fn)


@app.get("/api/debug/raw")
def debug_raw():
    """Debug: raw MT5 positions + account + today deals."""
    def fn():
        info = mt5.account_info()
        positions = mt5.positions_get()
        now = datetime.now()
        today_midnight = datetime(now.year, now.month, now.day)
        deals = mt5.history_deals_get(today_midnight, now + timedelta(seconds=60)) or []
        return {
            "account": {"balance": info.balance if info else 0, "equity": info.equity if info else 0,
                        "profit": info.profit if info else 0, "margin": info.margin if info else 0},
            "open_positions_count": len(positions) if positions else 0,
            "open_positions": [{"ticket": p.ticket, "symbol": p.symbol, "type": p.type,
                                "volume": p.volume, "profit": p.profit} for p in (positions or [])],
            "today_deals_count": len(deals),
            "today_deals_with_profit": [{"ticket": d.ticket, "pos_id": d.position_id,
                                         "type": d.type, "entry": d.entry,
                                         "time": datetime.fromtimestamp(d.time).strftime("%H:%M:%S"),
                                         "profit": d.profit, "commission": d.commission}
                                        for d in deals if d.profit != 0.0],
        }
    return _mt5_call(fn)


import json as _json
_TODAY_FILE = "today_balance.json"

def _load_today_start():
    try:
        with open(_TODAY_FILE, encoding="utf-8-sig") as f:  # utf-8-sig strips BOM
            return _json.load(f)
    except Exception:
        return {"date": None, "balance": None, "login": None}

def _save_today_start(date_str, balance, login):
    try:
        with open(_TODAY_FILE, "w") as f:
            _json.dump({"date": date_str, "balance": balance, "login": login}, f)
    except Exception:
        pass


@app.get("/api/today_realized")
def get_today_realized():
    """Balance-based TODAY REALIZED — accurate even when broker deal history is incomplete."""
    def fn():
        info = mt5.account_info()
        if not info:
            return {"realized": 0.0, "trades": 0, "wins": 0, "losses": 0}

        today_str  = datetime.now().strftime("%Y-%m-%d")
        stored     = _load_today_start()

        # Reset baseline if day rolled over OR the connected account login changed
        # (e.g. server briefly connected to a different MT5 terminal) — comparing
        # balances across two different accounts produces a meaningless number.
        if stored["date"] != today_str or stored["balance"] is None or stored.get("login") != info.login:
            _save_today_start(today_str, info.balance, info.login)
            start_balance = info.balance
        else:
            start_balance = stored["balance"]

        # realized = balance now - balance at start of day (deposits excluded in most cases)
        realized = round(info.balance - start_balance, 2)

        # Best-effort trade count from deal history
        now            = datetime.now()
        today_midnight = datetime(now.year, now.month, now.day)
        deals  = mt5.history_deals_get(today_midnight, now + timedelta(seconds=60)) or []
        wins   = losses = trades = 0
        for d in deals:
            if d.type not in (0, 1):
                continue
            if d.profit == 0.0 and d.commission == 0.0 and d.swap == 0.0:
                continue
            net = d.profit + d.commission + d.swap
            trades += 1
            if net > 0: wins += 1
            else:       losses += 1

        return {"realized": realized, "trades": trades, "wins": wins, "losses": losses,
                "start_balance": start_balance}
    return _mt5_call(fn)


# ── REPORTS — comprehensive analytics ──────────────────────────────────────────

def _session_name(utc_hour: int) -> str:
    if 0 <= utc_hour < 9:   return "Asian"
    if 9 <= utc_hour < 17:  return "London"
    if 17 <= utc_hour < 22: return "New York"
    return "Off-Hours"

@app.get("/api/reports")
def get_reports(months: int = 12, filter: str = "all", date_from: str = "", date_to: str = ""):
    """Full analytics: monthly, daily, symbol, day-of-week, session breakdown.
    filter: all | manual_forex | manual_crypto
    date_from/date_to: YYYY-MM-DD (overrides months when provided)
    """
    _CRYPTO_SYMS = ("BTC","ETH","LTC","XRP","BNB","SOL","ADA","DOGE","XMR","DOT","AVAX","MATIC")
    def _is_crypto(sym): return any(c in (sym or "").upper() for c in _CRYPTO_SYMS)
    def _is_algo(comment): return (comment or "").startswith("FarhanFX-")

    def fn():
        now = datetime.now()
        # Custom date range overrides months preset
        if date_from and date_to:
            _date_from = datetime.strptime(date_from, "%Y-%m-%d")
            _date_to   = datetime.strptime(date_to,   "%Y-%m-%d").replace(hour=23, minute=59, second=59)
        elif date_from:
            _date_from = datetime.strptime(date_from, "%Y-%m-%d")
            _date_to   = now
        elif date_to:
            _date_from = datetime(2010, 1, 1)
            _date_to   = datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
        else:
            _date_from = now - timedelta(days=months * 31)
            _date_to   = now
        # Fetch full history for monthly chart (up to 10 years back)
        date_wide  = datetime(max(now.year - 10, 2010), 1, 1)
        deals_all  = mt5.history_deals_get(date_wide, now + timedelta(seconds=60))
        if deals_all is None:
            return {"monthly": [], "monthly_chart": [], "by_symbol": [], "by_session": [],
                    "daily_pnl": [], "summary": {}}

        cutoff_ts = _date_from.timestamp()
        ceil_ts   = _date_to.timestamp()

        in_map   = {}
        out_list = []
        monthly_chart: dict = {}

        for d in deals_all:
            if d.type not in (0, 1):
                continue
            is_close = not (d.profit == 0.0 and d.commission == 0.0 and d.swap == 0.0)
            if not is_close:
                in_map[d.position_id] = d
            else:
                # Apply filter
                if filter == "manual_forex":
                    if _is_algo(d.comment) or _is_crypto(d.symbol): continue
                elif filter == "manual_crypto":
                    if _is_algo(d.comment) or not _is_crypto(d.symbol): continue
                # Build monthly_chart from ALL closing deals (no cutoff)
                net_c = round(d.profit + d.commission + d.swap, 2)
                dt_c  = datetime.fromtimestamp(d.time)
                mkey  = dt_c.strftime("%Y-%m")
                if mkey not in monthly_chart:
                    monthly_chart[mkey] = {"key": mkey, "label": dt_c.strftime("%b %Y"),
                                           "pnl": 0.0, "trades": 0, "wins": 0, "losses": 0}
                monthly_chart[mkey]["pnl"]    = round(monthly_chart[mkey]["pnl"] + net_c, 2)
                monthly_chart[mkey]["trades"] += 1
                if net_c > 0: monthly_chart[mkey]["wins"]   += 1
                else:         monthly_chart[mkey]["losses"] += 1

                if cutoff_ts <= d.time <= ceil_ts:
                    out_list.append(d)

        # ── Accumulators ──────────────────────────────────────────────────────
        monthly:     dict = {}
        by_symbol:   dict = {}
        by_strategy: dict = {}
        by_dow:     dict = {i: {"day": d, "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
                            for i, d in enumerate(["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"])}
        by_session: dict = {s: {"session": s, "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
                            for s in ["Asian","London","New York","Off-Hours"]}
        daily:      dict = {}

        all_nets   = []
        best_trade = {"pnl": None, "symbol": "", "date": ""}
        worst_trade= {"pnl": None, "symbol": "", "date": ""}
        running_balance = 0.0
        peak    = 0.0
        max_dd  = 0.0

        for out in sorted(out_list, key=lambda x: x.time):
            in_d = in_map.get(out.position_id)
            net  = round(out.profit + out.commission + out.swap, 2)
            dt   = datetime.fromtimestamp(out.time)
            utc_dt = datetime.fromtimestamp(out.time, tz=timezone.utc)

            all_nets.append(net)
            running_balance += net
            if running_balance > peak:
                peak = running_balance
            dd = round(peak - running_balance, 2)
            if dd > max_dd:
                max_dd = dd

            # Best / worst trade
            if best_trade["pnl"] is None or net > best_trade["pnl"]:
                best_trade = {"pnl": net, "symbol": out.symbol, "date": dt.strftime("%Y-%m-%d")}
            if worst_trade["pnl"] is None or net < worst_trade["pnl"]:
                worst_trade = {"pnl": net, "symbol": out.symbol, "date": dt.strftime("%Y-%m-%d")}

            # Monthly
            mkey   = dt.strftime("%Y-%m");  mlabel = dt.strftime("%b %Y")
            if mkey not in monthly:
                monthly[mkey] = {"label": mlabel, "key": mkey, "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
            monthly[mkey]["trades"] += 1
            monthly[mkey]["pnl"]     = round(monthly[mkey]["pnl"] + net, 2)
            if net > 0: monthly[mkey]["wins"]   += 1
            else:       monthly[mkey]["losses"] += 1

            # Daily
            dkey = dt.strftime("%Y-%m-%d")
            if dkey not in daily:
                daily[dkey] = {"date": dkey, "label": dt.strftime("%d %b"), "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
            daily[dkey]["trades"] += 1
            daily[dkey]["pnl"]     = round(daily[dkey]["pnl"] + net, 2)
            if net > 0: daily[dkey]["wins"]   += 1
            else:       daily[dkey]["losses"] += 1

            # By strategy (from comment prefix FarhanFX-{strategy})
            _cmt = (in_d.comment if in_d else "") or ""
            if _cmt.startswith("FarhanFX-") and "Close" not in _cmt:
                _strat_name = _cmt[len("FarhanFX-"):][:20].rstrip()
                if _strat_name not in by_strategy:
                    by_strategy[_strat_name] = {"strategy": _strat_name, "trades": 0, "wins": 0, "losses": 0,
                                                 "pnl": 0.0, "gross_win": 0.0, "gross_loss": 0.0}
                by_strategy[_strat_name]["trades"] += 1
                by_strategy[_strat_name]["pnl"]     = round(by_strategy[_strat_name]["pnl"] + net, 2)
                if net > 0:
                    by_strategy[_strat_name]["wins"]      += 1
                    by_strategy[_strat_name]["gross_win"]  = round(by_strategy[_strat_name]["gross_win"] + net, 2)
                else:
                    by_strategy[_strat_name]["losses"]     += 1
                    by_strategy[_strat_name]["gross_loss"] = round(by_strategy[_strat_name]["gross_loss"] + net, 2)

            # Symbol
            sym = out.symbol
            if sym not in by_symbol:
                by_symbol[sym] = {"symbol": sym, "trades": 0, "wins": 0, "losses": 0,
                                  "pnl": 0.0, "gross_win": 0.0, "gross_loss": 0.0,
                                  "avg_pips": 0.0, "pips_sum": 0.0}
            by_symbol[sym]["trades"] += 1
            by_symbol[sym]["pnl"]    = round(by_symbol[sym]["pnl"] + net, 2)
            if net > 0:
                by_symbol[sym]["wins"]      += 1
                by_symbol[sym]["gross_win"] = round(by_symbol[sym]["gross_win"] + net, 2)
            else:
                by_symbol[sym]["losses"]     += 1
                by_symbol[sym]["gross_loss"] = round(by_symbol[sym]["gross_loss"] + net, 2)

            # Day-of-week (local time)
            dow = dt.weekday()  # 0=Mon … 6=Sun
            by_dow[dow]["trades"] += 1
            by_dow[dow]["pnl"]    = round(by_dow[dow]["pnl"] + net, 2)
            if net > 0: by_dow[dow]["wins"]   += 1
            else:       by_dow[dow]["losses"] += 1

            # Session (UTC hour)
            sess = _session_name(utc_dt.hour)
            by_session[sess]["trades"] += 1
            by_session[sess]["pnl"]    = round(by_session[sess]["pnl"] + net, 2)
            if net > 0: by_session[sess]["wins"]   += 1
            else:       by_session[sess]["losses"] += 1

        # ── Post-process ──────────────────────────────────────────────────────
        def wr(w, t): return round(w / t * 100, 1) if t > 0 else 0
        def pf(gw, gl): return round(abs(gw / gl), 2) if gl != 0 else 0

        monthly_list = sorted(monthly.values(), key=lambda x: x["key"], reverse=True)
        for m in monthly_list:
            m["win_rate"] = wr(m["wins"], m["trades"])

        monthly_chart_list = sorted(monthly_chart.values(), key=lambda x: x["key"])
        for m in monthly_chart_list:
            m["win_rate"] = wr(m["wins"], m["trades"])

        daily_list = sorted(daily.values(), key=lambda x: x["date"], reverse=True)
        for d in daily_list:
            d["win_rate"] = wr(d["wins"], d["trades"])

        sym_list = sorted(by_symbol.values(), key=lambda x: x["trades"], reverse=True)
        for s in sym_list:
            s["win_rate"]    = wr(s["wins"], s["trades"])
            s["profit_factor"]= pf(s["gross_win"], s["gross_loss"])
            s["avg_trade"]   = round(s["pnl"] / s["trades"], 2) if s["trades"] > 0 else 0

        dow_list = [by_dow[i] for i in range(7)]
        for d in dow_list:
            d["win_rate"] = wr(d["wins"], d["trades"])

        sess_list = [by_session[s] for s in ["Asian","London","New York","Off-Hours"]]
        for s in sess_list:
            s["win_rate"] = wr(s["wins"], s["trades"])

        strat_list = sorted(by_strategy.values(), key=lambda x: x["trades"], reverse=True)
        for s in strat_list:
            s["win_rate"]     = wr(s["wins"], s["trades"])
            s["profit_factor"]= pf(s["gross_win"], s["gross_loss"])
            s["avg_trade"]    = round(s["pnl"] / s["trades"], 2) if s["trades"] > 0 else 0

        wins   = [n for n in all_nets if n > 0]
        losses = [n for n in all_nets if n <= 0]
        total  = len(all_nets)
        gross_w = sum(wins)
        gross_l = abs(sum(losses))

        daily_pnl_vals = [d["pnl"] for d in sorted(daily.values(), key=lambda x: x["date"])]
        best_day  = max(daily.values(), key=lambda x: x["pnl"])  if daily else {}
        worst_day = min(daily.values(), key=lambda x: x["pnl"]) if daily else {}

        monthly_pnl = [m["pnl"] for m in monthly_list]
        summary = {
            "total_trades":      total,
            "wins":              len(wins),
            "losses":            len(losses),
            "win_rate":          wr(len(wins), total),
            "net_pnl":           round(sum(all_nets), 2),
            "gross_win":         round(gross_w, 2),
            "gross_loss":        round(gross_l, 2),
            "profit_factor":     pf(gross_w, gross_l),
            "avg_win":           round(gross_w / len(wins),   2) if wins   else 0,
            "avg_loss":          round(sum(losses) / len(losses), 2) if losses else 0,
            "avg_trade":         round(sum(all_nets) / total, 2) if total else 0,
            "best_trade":        best_trade,
            "worst_trade":       worst_trade,
            "best_day":          best_day,
            "worst_day":         worst_day,
            "max_drawdown":      round(max_dd, 2),
            "total_months":      len(monthly_list),
            "profitable_months": sum(1 for p in monthly_pnl if p > 0),
            "avg_monthly":       round(sum(monthly_pnl) / len(monthly_pnl), 2) if monthly_pnl else 0,
            "best_month":        max(monthly_list, key=lambda x: x["pnl"]) if monthly_list else {},
            "worst_month":       min(monthly_list, key=lambda x: x["pnl"]) if monthly_list else {},
            "total_days":        len(daily),
            "profitable_days":   sum(1 for d in daily.values() if d["pnl"] > 0),
            "best_session":      max(sess_list, key=lambda x: x["pnl"]) if sess_list else {},
            "best_pair":         max(sym_list, key=lambda x: x["pnl"]) if sym_list else {},
            "worst_pair":        min(sym_list, key=lambda x: x["pnl"]) if sym_list else {},
            "best_dow":          max(dow_list, key=lambda x: x["pnl"])  if dow_list else {},
            "worst_dow":         min(dow_list, key=lambda x: x["pnl"])  if dow_list else {},
        }
        return {
            "summary":      summary,
            "monthly":      monthly_list,
            "monthly_chart":monthly_chart_list,
            "daily":        daily_list[:60],
            "by_symbol":    sym_list,
            "by_dow":       dow_list,
            "by_session":   sess_list,
            "by_strategy":  strat_list,
        }
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
    import time as _t
    def fn():
        real = _resolve_symbol(symbol)
        mt5.symbol_select(real, True)
        for _ in range(5):            # retry up to 5× after subscribe
            tick = mt5.symbol_info_tick(real)
            if tick is not None and tick.bid > 0:
                return {
                    "symbol": real,
                    "bid":    tick.bid,
                    "ask":    tick.ask,
                    "spread": round((tick.ask - tick.bid) * 100000, 1),
                    "time":   datetime.fromtimestamp(tick.time).strftime("%H:%M:%S"),
                }
            _t.sleep(0.2)
        return {"error": f"No tick data for '{symbol}'"}
    result = _mt5_call(fn)
    if "error" in result:
        return JSONResponse(result, status_code=404)
    return result


def _pip_size(sym_info) -> float:
    """Return pip size for a symbol based on its name."""
    name = sym_info.name.upper()
    if "XAU" in name or "GOLD" in name:           return 0.1
    if "XAG" in name or "SILVER" in name:          return 0.01
    if any(x in name for x in ["BTC","ETH","LTC","XRP","ADA","SOL","BNB","DOT","AVAX","DOGE","MATIC","LINK","UNI"]):
        return 1.0
    if any(x in name for x in ["NAS","US30","US500","SPX","UK1","GER","JP2","AUS","HK","FRA","DAX","CAC"]):
        return 1.0
    if any(x in name for x in ["OIL","WTI","BRENT","GAS"]):
        return 0.01
    return round(10 * sym_info.point, 10)  # standard 5-digit forex


# ── PLACE ORDER ─────────────────────────────────────────────────────────────────

class OrderRequest(BaseModel):
    symbol:     str
    order_type: str
    volume:     float
    sl:         Optional[float] = 0.0
    tp:         Optional[float] = 0.0
    sl_pips:    Optional[float] = 0.0   # alternative: send pips, server converts
    tp_pips:    Optional[float] = 0.0
    comment:    str = "FarhanFX Algo"

@app.post("/api/order")
def place_order(req: OrderRequest):
    import time as _t
    def fn():
        real = _resolve_symbol(req.symbol)
        sym = mt5.symbol_info(real)
        if sym is None:
            return {"error": f"Symbol '{req.symbol}' not found on this broker"}
        mt5.symbol_select(real, True)

        tick = None
        for _ in range(5):
            tick = mt5.symbol_info_tick(real)
            if tick is not None and tick.bid > 0:
                break
            _t.sleep(0.2)
        if tick is None or tick.bid == 0:
            return {"error": f"No price data for '{real}' — check MT5 Market Watch"}

        is_buy = req.order_type.upper() == "BUY"
        price  = tick.ask if is_buy else tick.bid

        # Convert pips → price if client sent pips mode
        sl, tp = req.sl or 0.0, req.tp or 0.0
        if (req.sl_pips and req.sl_pips > 0) or (req.tp_pips and req.tp_pips > 0):
            pip = _pip_size(sym)
            # MT5 rule: BUY SL/TP reference = BID, SELL SL/TP reference = ASK
            # This ensures SL is always on the correct side of current market
            sl_tp_ref = tick.bid if is_buy else tick.ask
            # Broker minimum stop distance
            min_dist = max(sym.trade_stops_level * sym.point, pip)

            if req.sl_pips and req.sl_pips > 0:
                sl_dist = max(req.sl_pips * pip, min_dist + sym.point)
                sl = round(sl_tp_ref - sl_dist, sym.digits) if is_buy \
                     else round(sl_tp_ref + sl_dist, sym.digits)
            if req.tp_pips and req.tp_pips > 0:
                tp_dist = max(req.tp_pips * pip, min_dist + sym.point)
                tp = round(sl_tp_ref + tp_dist, sym.digits) if is_buy \
                     else round(sl_tp_ref - tp_dist, sym.digits)

        request = {
            "action":    mt5.TRADE_ACTION_DEAL,
            "symbol":    real,
            "volume":    req.volume,
            "type":      mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
            "price":     price,
            "sl":        sl,
            "tp":        tp,
            "deviation": 30,
            "magic":     234000,
            "comment":   req.comment,
            "type_time": mt5.ORDER_TIME_GTC,
        }
        print(f"[ORDER] {req.order_type} {real} vol={req.volume} price={price} sl={sl} tp={tp} (sl_pips={req.sl_pips} tp_pips={req.tp_pips} pip={_pip_size(sym)})")

        for filling in [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]:
            request["type_filling"] = filling
            result = mt5.order_send(request)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                return {"success": True, "ticket": result.order, "price": result.price, "sl": sl, "tp": tp}
            if result.retcode != 10038:
                return {"error": f"{result.comment} (retcode {result.retcode})"}
        return {"error": f"{result.comment} (retcode {result.retcode})"}

    result = _mt5_call(fn, timeout=15)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


# ── MODIFY POSITION SL/TP ───────────────────────────────────────────────────────

class ModifyRequest(BaseModel):
    sl:      Optional[float] = 0.0
    tp:      Optional[float] = 0.0
    sl_pips: Optional[float] = 0.0
    tp_pips: Optional[float] = 0.0
    mode:    str = "pips"   # "pips" or "price"

@app.post("/api/modify/{ticket}")
def modify_position(ticket: int, req: ModifyRequest):
    import time as _t
    def fn():
        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return {"error": f"Position #{ticket} not found"}
        p = pos[0]
        sym = mt5.symbol_info(p.symbol)
        if sym is None:
            return {"error": f"Symbol info not found"}

        sl, tp = req.sl or 0.0, req.tp or 0.0

        if req.mode == "pips" and (req.sl_pips > 0 or req.tp_pips > 0):
            tick = None
            for _ in range(5):
                tick = mt5.symbol_info_tick(p.symbol)
                if tick and tick.bid > 0:
                    break
                _t.sleep(0.1)
            if not tick:
                return {"error": "Cannot get price"}

            is_buy = p.type == mt5.ORDER_TYPE_BUY
            pip = _pip_size(sym)
            min_dist = max(sym.trade_stops_level * sym.point, pip)
            sl_tp_ref = tick.bid if is_buy else tick.ask

            if req.sl_pips > 0:
                sl_dist = max(req.sl_pips * pip, min_dist + sym.point)
                sl = round(sl_tp_ref - sl_dist, sym.digits) if is_buy \
                     else round(sl_tp_ref + sl_dist, sym.digits)
            if req.tp_pips > 0:
                tp_dist = max(req.tp_pips * pip, min_dist + sym.point)
                tp = round(sl_tp_ref + tp_dist, sym.digits) if is_buy \
                     else round(sl_tp_ref - tp_dist, sym.digits)

        result = mt5.order_send({
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol":   p.symbol,
            "sl":       sl,
            "tp":       tp,
        })
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return {"error": f"{result.comment} (retcode {result.retcode})"}
        return {"success": True, "sl": sl, "tp": tp}

    result = _mt5_call(fn, timeout=10)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


# ── PENDING ORDERS ──────────────────────────────────────────────────────────────

PENDING_TYPE_MAP = {
    "BUY_LIMIT":   mt5.ORDER_TYPE_BUY_LIMIT,
    "BUY_STOP":    mt5.ORDER_TYPE_BUY_STOP,
    "SELL_LIMIT":  mt5.ORDER_TYPE_SELL_LIMIT,
    "SELL_STOP":   mt5.ORDER_TYPE_SELL_STOP,
}

class PendingRequest(BaseModel):
    symbol:     str
    order_type: str
    volume:     float
    price:      float
    sl:         Optional[float] = 0.0
    tp:         Optional[float] = 0.0
    sl_pips:    Optional[float] = 0.0
    tp_pips:    Optional[float] = 0.0
    comment:    str = "FarhanFX Algo"

@app.post("/api/pending")
def place_pending(req: PendingRequest):
    import time as _t
    def fn():
        otype = PENDING_TYPE_MAP.get(req.order_type.upper())
        if otype is None:
            return {"error": f"Unknown order type '{req.order_type}'"}

        real = _resolve_symbol(req.symbol)
        sym  = mt5.symbol_info(real)
        if sym is None:
            return {"error": f"Symbol '{req.symbol}' not found"}
        mt5.symbol_select(real, True)

        sl, tp = req.sl or 0.0, req.tp or 0.0

        if (req.sl_pips and req.sl_pips > 0) or (req.tp_pips and req.tp_pips > 0):
            pip      = _pip_size(sym)
            min_dist = max(sym.trade_stops_level * sym.point, pip)
            is_buy   = req.order_type.upper().startswith("BUY")
            ref      = req.price   # SL/TP relative to the pending price

            if req.sl_pips and req.sl_pips > 0:
                sl_dist = max(req.sl_pips * pip, min_dist + sym.point)
                sl = round(ref - sl_dist, sym.digits) if is_buy \
                     else round(ref + sl_dist, sym.digits)
            if req.tp_pips and req.tp_pips > 0:
                tp_dist = max(req.tp_pips * pip, min_dist + sym.point)
                tp = round(ref + tp_dist, sym.digits) if is_buy \
                     else round(ref - tp_dist, sym.digits)

        request = {
            "action":    mt5.TRADE_ACTION_PENDING,
            "symbol":    real,
            "volume":    req.volume,
            "type":      otype,
            "price":     req.price,
            "sl":        sl,
            "tp":        tp,
            "deviation": 30,
            "magic":     234000,
            "comment":   req.comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }
        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return {"error": f"{result.comment} (retcode {result.retcode})"}
        return {"success": True, "ticket": result.order}

    result = _mt5_call(fn, timeout=15)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@app.get("/api/pending_orders")
def get_pending_orders():
    def fn():
        orders = mt5.orders_get()
        if not orders:
            return []
        type_names = {
            mt5.ORDER_TYPE_BUY_LIMIT:  "BUY LIMIT",
            mt5.ORDER_TYPE_BUY_STOP:   "BUY STOP",
            mt5.ORDER_TYPE_SELL_LIMIT: "SELL LIMIT",
            mt5.ORDER_TYPE_SELL_STOP:  "SELL STOP",
        }
        return [
            {
                "ticket":  o.ticket,
                "symbol":  o.symbol,
                "type":    type_names.get(o.type, str(o.type)),
                "volume":  o.volume_initial,
                "price":   o.price_open,
                "sl":      o.sl or 0,
                "tp":      o.tp or 0,
                "time":    datetime.fromtimestamp(o.time_setup).strftime("%Y-%m-%d %H:%M"),
                "comment": o.comment,
            }
            for o in orders if o.type in type_names
        ]
    return _mt5_call(fn)


@app.post("/api/cancel/{ticket}")
def cancel_pending(ticket: int):
    def fn():
        orders = mt5.orders_get(ticket=ticket)
        if not orders:
            return {"error": f"Pending order #{ticket} not found"}
        result = mt5.order_send({
            "action": mt5.TRADE_ACTION_REMOVE,
            "order":  ticket,
        })
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return {"error": f"{result.comment} (retcode {result.retcode})"}
        return {"success": True}
    result = _mt5_call(fn, timeout=10)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


# ── CLOSE POSITION ──────────────────────────────────────────────────────────────

def _do_close_position(ticket: int):
    """Close a single position by ticket. Returns dict with success or error."""
    all_pos = mt5.positions_get()
    if all_pos is None:
        all_pos = []
    pos_list = [p for p in all_pos if p.ticket == ticket]
    if not pos_list:
        return {"error": "Position not found"}
    p = pos_list[0]
    tick = mt5.symbol_info_tick(p.symbol)
    if tick is None:
        return {"error": f"Cannot get tick for {p.symbol}"}
    is_buy = p.type == 0
    # Try FOK first, fall back to IOC, then RETURN
    for filling in (mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN):
        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       p.symbol,
            "volume":       p.volume,
            "type":         mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "position":     ticket,
            "price":        tick.bid if is_buy else tick.ask,
            "deviation":    30,
            "magic":        234000,
            "comment":      "FarhanFX Close",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        result = mt5.order_send(request)
        if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
            return {"success": True, "ticket": ticket}
        # retcode 10030 = unsupported filling mode → try next
        if result is not None and result.retcode != 10030:
            return {"error": f"{result.comment} (retcode {result.retcode})"}
    return {"error": f"Order failed: {result.comment} (retcode {result.retcode})"}


@app.post("/api/close/{ticket}")
def close_position(ticket: int):
    result = _mt5_call(lambda: _do_close_position(ticket), timeout=15)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@app.post("/api/close_all")
def close_all_positions():
    def fn():
        all_pos = mt5.positions_get()
        if not all_pos:
            return {"closed": 0, "errors": []}
        closed, errors = 0, []
        for p in all_pos:
            r = _do_close_position(p.ticket)
            if "success" in r:
                closed += 1
            else:
                errors.append({"ticket": p.ticket, "error": r.get("error")})
        return {"closed": closed, "errors": errors}

    result = _mt5_call(fn, timeout=30)
    if isinstance(result, dict) and "error" in result:
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

_STRAT_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategies_state.json")

def _save_strategies_state():
    """Persist running strategy configs to disk so they survive server restarts."""
    try:
        state = [
            {"id": sid, "config": s["config"]}
            for sid, s in _strategies.items()
            if s.get("status") == "running"
        ]
        with open(_STRAT_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass

def _restore_strategies_state():
    """On startup, reload and restart any strategies that were running before restart."""
    if not os.path.exists(_STRAT_STATE_FILE):
        return
    try:
        with open(_STRAT_STATE_FILE) as f:
            state = json.load(f)
        if not state:
            return
        import time as _t; _t.sleep(3)  # let MT5 fully initialize first
        for entry in state:
            sid    = entry["id"]
            cfg    = entry["config"]
            stop_ev = threading.Event()
            log     = []
            _strategies[sid] = {
                "id":        sid,
                "config":    cfg,
                "status":    "running",
                "trades":    0,
                "pnl":       0.0,
                "indicator": "↺ Restored after restart",
                "log":       log,
                "started":   datetime.now().strftime("%H:%M:%S"),
            }
            t = threading.Thread(target=_strategy_runner, args=(sid, cfg, stop_ev, log), daemon=True)
            _strategies[sid]["_stop"] = stop_ev
            t.start()
        print(f"[ALGO] Restored {len(state)} strategies from disk")
    except Exception as e:
        print(f"[ALGO] Failed to restore strategies: {e}")

# Active strategies: id -> {config, thread, stop_event, log}
_strategies: dict = {}

TF_MAP = {
    "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1,
    "MN1": mt5.TIMEFRAME_MN1,
}

def _resolve_symbol(symbol: str) -> str:
    """Find exact MT5 symbol name, case-insensitive, and ensure it's selected."""
    # Try exact match first
    info = mt5.symbol_info(symbol)
    if info:
        mt5.symbol_select(symbol, True)
        return symbol
    # Try case-insensitive match across all broker symbols
    all_syms = mt5.symbols_get()
    if all_syms:
        low = symbol.lower()
        for s in all_syms:
            if s.name.lower() == low:
                mt5.symbol_select(s.name, True)
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

def _supertrend(highs, lows, closes, period=10, mult=3.0):
    """Returns list of direction values: 1=bullish, -1=bearish"""
    tr = []
    for i in range(len(closes)):
        if i == 0:
            tr.append(highs[i] - lows[i])
        else:
            tr.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    # Smooth ATR (Wilder)
    atr = [sum(tr[:period]) / period]
    for i in range(period, len(tr)):
        atr.append((atr[-1] * (period-1) + tr[i]) / period)
    # Pad ATR list to match length
    atr_full = [atr[0]] * (period - 1) + atr

    ub = [(highs[i]+lows[i])/2 + mult*atr_full[i] for i in range(len(closes))]
    lb = [(highs[i]+lows[i])/2 - mult*atr_full[i] for i in range(len(closes))]
    fub, flb = ub[:], lb[:]
    direction = [1] * len(closes)
    for i in range(1, len(closes)):
        flb[i] = max(lb[i], flb[i-1]) if closes[i-1] > flb[i-1] else lb[i]
        fub[i] = min(ub[i], fub[i-1]) if closes[i-1] < fub[i-1] else ub[i]
        if closes[i] > fub[i-1]:
            direction[i] = 1
        elif closes[i] < flb[i-1]:
            direction[i] = -1
        else:
            direction[i] = direction[i-1]
    return direction

def _ichimoku(highs, lows, tenkan=9, kijun=26, senkou=52):
    """Returns (tenkan_sen, kijun_sen, span_a, span_b) lists"""
    def mid(n, i):
        h = max(highs[max(0,i-n+1):i+1])
        l = min(lows[max(0,i-n+1):i+1])
        return (h+l)/2
    n = len(highs)
    tk = [mid(tenkan, i) for i in range(n)]
    kj = [mid(kijun, i) for i in range(n)]
    sa = [(tk[i]+kj[i])/2 for i in range(n)]
    sb = [mid(senkou, i) for i in range(n)]
    return tk, kj, sa, sb


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

# ── ForexFactory News Calendar ──────────────────────────────────────────────────

_NEWS_CACHE: dict = {"data": [], "ts": 0.0}

def _fetch_ff_calendar() -> list:
    now = __import__('time').time()
    if now - _NEWS_CACHE["ts"] < 600 and _NEWS_CACHE["data"]:
        return _NEWS_CACHE["data"]
    try:
        import urllib.request as _ur
        req = _ur.Request(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            headers={"User-Agent": "Mozilla/5.0 (FarhanFX/1.0)"}
        )
        with _ur.urlopen(req, timeout=8) as r:
            raw = json.loads(r.read().decode("utf-8"))
        _NEWS_CACHE["data"] = raw
        _NEWS_CACHE["ts"]   = now
        return raw
    except Exception:
        return _NEWS_CACHE.get("data", [])

def _symbol_currencies(symbol: str) -> list:
    s = symbol.upper().replace("C","").replace("M","")
    if any(x in s for x in ("XAU","GOLD")): return ["USD"]
    if "XAG" in s: return ["USD"]
    if any(x in s for x in ("OIL","WTI","BRENT")): return ["USD"]
    if any(x in s for x in ("US30","US500","SPX","NAS","DOW")): return ["USD"]
    if len(s) >= 6:
        return list({s[:3], s[3:6]})
    return ["USD"]

def _parse_news_dt(date_str: str):
    try:
        if date_str.endswith("Z"):
            return datetime.fromisoformat(date_str.rstrip("Z")).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(date_str)
    except Exception:
        return None

def _get_upcoming_news(symbol: str, minutes_ahead: int = 240, minutes_past: int = 30) -> list:
    currencies = _symbol_currencies(symbol)
    now = datetime.now(timezone.utc)
    events = []
    for item in _fetch_ff_calendar():
        if item.get("impact") not in ("High", "Medium"):
            continue
        if item.get("country", "").upper() not in currencies:
            continue
        ev_dt = _parse_news_dt(item.get("date", ""))
        if ev_dt is None:
            continue
        if ev_dt.tzinfo is None:
            ev_dt = ev_dt.replace(tzinfo=timezone.utc)
        diff_min = (ev_dt - now).total_seconds() / 60
        if -minutes_past <= diff_min <= minutes_ahead:
            actual   = item.get("actual",   "") or ""
            forecast = item.get("forecast", "") or ""
            direction = "NEUTRAL"
            try:
                def _pv(v):
                    v = str(v).replace("%","").replace("K","000").replace("M","000000").replace("B","000000000").strip()
                    return float(v)
                av = _pv(actual) if actual else None
                fv = _pv(forecast) if forecast else None
                if av is not None and fv is not None:
                    if av > fv * 1.02:   direction = "BULLISH"
                    elif av < fv * 0.98: direction = "BEARISH"
            except Exception:
                pass
            events.append({
                "title":        item.get("title", ""),
                "country":      item.get("country", "").upper(),
                "impact":       item.get("impact", ""),
                "date":         item.get("date", ""),
                "actual":       actual,
                "forecast":     forecast,
                "previous":     item.get("previous", "") or "",
                "mins_from_now": round(diff_min, 1),
                "direction":    direction,
            })
    events.sort(key=lambda x: x["mins_from_now"])
    return events

# ── Price Action + ICT + SMC AI Helpers ─────────────────────────────────────────

def _detect_price_action(opens, closes, highs, lows):
    """Detect candlestick patterns (all major patterns from books). Returns list of (name, dir, strength)."""
    patterns = []
    n = len(closes)
    if n < 3:
        return patterns
    o1,c1,h1,l1 = opens[-1],closes[-1],highs[-1],lows[-1]
    o2,c2,h2,l2 = opens[-2],closes[-2],highs[-2],lows[-2]
    o3,c3       = opens[-3],closes[-3]
    body1 = abs(c1-o1); body2 = abs(c2-o2)
    uw1 = h1-max(c1,o1); lw1 = min(c1,o1)-l1
    rng1 = h1-l1
    if rng1 < 1e-10: return patterns

    # ── Pin Bar — highest WR from books (long rejection wick) ─────────────────
    if lw1 >= 2.0*body1 and body1 < rng1*0.35 and lw1 > rng1*0.5:
        patterns.append(("Bullish Pin Bar","BUY",0.90))
    if uw1 >= 2.0*body1 and body1 < rng1*0.35 and uw1 > rng1*0.5:
        patterns.append(("Bearish Pin Bar","SELL",0.90))

    # ── Engulfing (2nd body covers 1st entirely) ───────────────────────────────
    if c2<o2 and c1>o1 and c1>o2 and o1<c2:
        patterns.append(("Bullish Engulfing","BUY",0.85))
    if c2>o2 and c1<o1 and c1<o2 and o1>c2:
        patterns.append(("Bearish Engulfing","SELL",0.85))

    # ── Marubozu (strong momentum, almost no wicks) ────────────────────────────
    if rng1 > 0 and body1/rng1 > 0.88:
        patterns.append(("Bullish Marubozu" if c1>o1 else "Bearish Marubozu",
                         "BUY" if c1>o1 else "SELL", 0.82))

    # ── Inside Bar False Breakout (institutional stop hunt — Candlestick Bible) ─
    # Mother = [-3], Inside Bar = [-2], False breakout candle = [-1]
    if n >= 4:
        h_m,l_m   = highs[-3],lows[-3]
        h_ib,l_ib = highs[-2],lows[-2]
        if h_ib < h_m and l_ib > l_m:          # bar[-2] is inside bar of bar[-3]
            if h1 > h_m and c1 < h_m:          # false break above mother → SELL reversal
                patterns.append(("IB False Breakout","SELL",0.88))
            if l1 < l_m and c1 > l_m:          # false break below mother → BUY reversal
                patterns.append(("IB False Breakout","BUY",0.88))

    # ── Morning Star / Evening Star (3-candle reversal) ───────────────────────
    if c3<o3 and abs(c3-o3)>abs(c2-o2)*2 and c1>o1 and c1>(o3+c3)/2:
        patterns.append(("Morning Star","BUY",0.80))
    if c3>o3 and abs(c3-o3)>abs(c2-o2)*2 and c1<o1 and c1<(o3+c3)/2:
        patterns.append(("Evening Star","SELL",0.80))

    # ── Three White Soldiers / Three Black Crows ───────────────────────────────
    if c1>o1 and c2>o2 and c3>o3 and c1>c2 and c2>c3:
        patterns.append(("Three White Soldiers","BUY",0.80))
    if c1<o1 and c2<o2 and c3<o3 and c1<c2 and c2<c3:
        patterns.append(("Three Black Crows","SELL",0.80))

    # ── Tweezer Tops/Bottoms (same high/low = rejection zone) ─────────────────
    rng_tol = max(rng1, h2-l2) * 0.08
    if abs(h1-h2) < rng_tol and c1 < (h1+l1)/2:
        patterns.append(("Tweezer Top","SELL",0.78))
    if abs(l1-l2) < rng_tol and c1 > (h1+l1)/2:
        patterns.append(("Tweezer Bottom","BUY",0.78))

    # ── Dark Cloud Cover / Piercing Pattern ────────────────────────────────────
    if c2>o2 and c1<o1 and o1>h2 and c1<(o2+c2)/2 and c1>o2:
        patterns.append(("Dark Cloud Cover","SELL",0.76))
    if c2<o2 and c1>o1 and o1<l2 and c1>(o2+c2)/2 and c1<o2:
        patterns.append(("Piercing Pattern","BUY",0.76))

    # ── Hammer / Shooting Star ────────────────────────────────────────────────
    if lw1>2*body1 and uw1<body1*0.5 and body1>0:
        patterns.append(("Hammer","BUY",0.75))
    if uw1>2*body1 and lw1<body1*0.5 and body1>0:
        patterns.append(("Shooting Star","SELL",0.75))

    # ── Harami (small body inside previous large body) ─────────────────────────
    if body2 > 0 and body1 < body2*0.5:
        b1h=max(o1,c1); b1l=min(o1,c1); b2h=max(o2,c2); b2l=min(o2,c2)
        if b1h<=b2h and b1l>=b2l:
            patterns.append(("Bullish Harami" if c2<o2 else "Bearish Harami",
                             "BUY" if c2<o2 else "SELL", 0.72))

    # ── Inside Bar (consolidation — trade breakout in trend direction) ─────────
    if h1<h2 and l1>l2:
        patterns.append(("Inside Bar","BUY" if c2>o2 else "SELL",0.70))

    return patterns


def _check_fibonacci_level(highs, lows, price, atr, lookback=80):
    """Check if price is at 38.2/50/61.8% Fibonacci retracement (key levels from Candlestick Bible)."""
    n = len(highs)
    if n < 20: return "NEUTRAL", "Not enough data"
    window = min(lookback, n)
    recent_high = max(highs[-window:])
    recent_low  = min(lows[-window:])
    swing = recent_high - recent_low
    if swing < atr * 2: return "NEUTRAL", "Swing too small"
    tol = atr * 0.6
    # Pullback levels from swing high (retracement in uptrend — BUY at fib)
    fib618 = recent_high - 0.618 * swing
    fib50  = recent_high - 0.500 * swing
    fib382 = recent_high - 0.382 * swing
    if abs(price - fib618) < tol: return "BUY", f"At 61.8% Fib [{fib618:.4f}] — golden ratio"
    if abs(price - fib50)  < tol: return "BUY", f"At 50.0% Fib [{fib50:.4f}]"
    if abs(price - fib382) < tol: return "BUY", f"At 38.2% Fib [{fib382:.4f}]"
    # Pullback levels from swing low (retracement in downtrend — SELL at fib)
    fib618u = recent_low + 0.618 * swing
    fib50u  = recent_low + 0.500 * swing
    fib382u = recent_low + 0.382 * swing
    if abs(price - fib618u) < tol: return "SELL", f"At 61.8% Fib from low [{fib618u:.4f}]"
    if abs(price - fib50u)  < tol: return "SELL", f"At 50.0% Fib from low [{fib50u:.4f}]"
    if abs(price - fib382u) < tol: return "SELL", f"At 38.2% Fib from low [{fib382u:.4f}]"
    return "NEUTRAL", f"Fib 61.8%={fib618:.4f}  50%={fib50:.4f}  38.2%={fib382:.4f}"


def _check_21ema_bounce(closes, highs, lows, price, atr):
    """Check if price is at/bouncing off 21 EMA (dynamic S/R from Candlestick Bible)."""
    if len(closes) < 25: return "NEUTRAL", "Not enough data"
    ema21v = _ema(closes, 21)
    ema21  = ema21v[-1]
    tol = atr * 0.4
    rising = ema21 > (ema21v[-5] if len(ema21v) >= 5 else ema21)
    # Price touching EMA
    if abs(price - ema21) < tol:
        if rising: return "BUY",  f"At rising 21 EMA [{ema21:.4f}] — dynamic support"
        else:      return "SELL", f"At falling 21 EMA [{ema21:.4f}] — dynamic resistance"
    # Price bounced off EMA last candle
    if len(highs) >= 2 and len(ema21v) >= 2:
        if abs(lows[-2] - ema21v[-2]) < tol and closes[-1] > ema21 and rising:
            return "BUY",  f"Bounced off 21 EMA [{ema21:.4f}]"
        if abs(highs[-2] - ema21v[-2]) < tol and closes[-1] < ema21 and not rising:
            return "SELL", f"Rejected at 21 EMA [{ema21:.4f}]"
    return "NEUTRAL", f"21 EMA: {ema21:.4f}"


def _detect_double_top_bottom(highs, lows, closes, atr, lookback=60):
    """Detect Double Top / Double Bottom chart pattern (reversal setup from cheat sheet)."""
    n = len(closes)
    if n < 20: return "NEUTRAL", "Not enough data"
    window = min(lookback, n)
    h = highs[-window:]; l = lows[-window:]; c = closes[-window:]
    tol = atr * 2.5
    lh = [(i, h[i]) for i in range(3, len(h)-2)
          if h[i] >= max(h[max(0,i-3):i]+[0]) and h[i] >= max(h[i+1:min(len(h),i+4)]+[0])]
    ll = [(i, l[i]) for i in range(3, len(l)-2)
          if l[i] <= min(l[max(0,i-3):i]+[9e9]) and l[i] <= min(l[i+1:min(len(l),i+4)]+[9e9])]
    if len(lh) >= 2:
        p1, p2 = lh[-2][1], lh[-1][1]
        if abs(p1-p2) < tol and c[-1] < min(p1,p2)-tol*0.3:
            return "SELL", f"Double Top ~{(p1+p2)/2:.4f} — confirmed breakdown"
    if len(ll) >= 2:
        p1, p2 = ll[-2][1], ll[-1][1]
        if abs(p1-p2) < tol and c[-1] > max(p1,p2)+tol*0.3:
            return "BUY", f"Double Bottom ~{(p1+p2)/2:.4f} — confirmed breakout"
    return "NEUTRAL", "No Double Top/Bottom detected"


def _check_nr7(highs, lows):
    """NR7: Current bar range = narrowest of last 7 bars (volatility contraction → breakout imminent)."""
    if len(highs) < 7: return False
    ranges = [highs[i]-lows[i] for i in range(-7, 0)]
    return ranges[-1] == min(ranges) and ranges[-1] > 0


def _detect_three_bar_reversal(opens, closes, highs, lows):
    """3-Bar Reversal: bar1 bearish → bar2 makes lower low (no outside bar) → bar3 closes above bar1/2 high.
    Reverse for bearish 3BR. High-probability trapped-trader reversal from PA Trading book."""
    if len(closes) < 4: return "NEUTRAL", "Not enough data"
    o3,c3,h3,l3 = opens[-4],closes[-4],highs[-4],lows[-4]  # oldest
    o2,c2,h2,l2 = opens[-3],closes[-3],highs[-3],lows[-3]
    o1,c1,h1,l1 = opens[-2],closes[-2],highs[-2],lows[-2]
    o0,c0       = opens[-1],closes[-1]

    # Bullish 3BR: bar3 bearish → bar2 lower low (not outside) → bar1 closes above bar3 high
    if c3 < o3 and l2 < l3:
        is_outside = h2 > h3 and l2 < l3
        if not is_outside and c1 > h3:
            return "BUY", f"3-Bar Reversal bullish — trapped sellers, close {c1:.4f} > high {h3:.4f}"
    # Bearish 3BR: bar3 bullish → bar2 higher high (not outside) → bar1 closes below bar3 low
    if c3 > o3 and h2 > h3:
        is_outside = h2 > h3 and l2 < l3
        if not is_outside and c1 < l3:
            return "SELL", f"3-Bar Reversal bearish — trapped buyers, close {c1:.4f} < low {l3:.4f}"
    return "NEUTRAL", "No 3-bar reversal"


def _detect_m2b_m2s(closes, highs, lows, atr):
    """M2B/M2S: Two-legged pullback to rising/falling 20 EMA.
    Highest-probability setup from PA Trading book — price bounces EMA after 2 distinct pullback legs."""
    if len(closes) < 30: return "NEUTRAL", "Not enough data"
    ema20v = _ema(closes, 20)
    ema20  = ema20v[-1]
    price  = closes[-1]
    tol    = atr * 1.0
    ema_rising  = ema20 > ema20v[-8]
    ema_falling = ema20 < ema20v[-8]

    if ema_rising and price > ema20:
        # Look in last 15 bars for two-leg pullback touching EMA zone
        window_l = lows[-15:]
        window_e = ema20v[-15:]
        leg_count = 0
        touched_ema = False
        for i in range(1, len(window_l)-1):
            if window_l[i] < window_l[i-1] and (i+1 >= len(window_l) or window_l[i] < window_l[i+1]):
                leg_count += 1
                if window_l[i] <= window_e[i] + tol:
                    touched_ema = True
        if leg_count >= 2 and touched_ema:
            return "BUY", f"M2B: 2-leg pullback touched 20 EMA [{ema20:.4f}] — trend resumption"

    if ema_falling and price < ema20:
        window_h = highs[-15:]
        window_e = ema20v[-15:]
        leg_count = 0
        touched_ema = False
        for i in range(1, len(window_h)-1):
            if window_h[i] > window_h[i-1] and (i+1 >= len(window_h) or window_h[i] > window_h[i+1]):
                leg_count += 1
                if window_h[i] >= window_e[i] - tol:
                    touched_ema = True
        if leg_count >= 2 and touched_ema:
            return "SELL", f"M2S: 2-leg pullback touched 20 EMA [{ema20:.4f}] — trend resumption"

    return "NEUTRAL", f"No M2B/M2S (EMA20={ema20:.4f})"


def _check_pivot_points(highs, lows, closes, price, atr, lookback=20):
    """Daily pivot points (PP, R1/R2, S1/S2) as key S/R levels.
    Uses last `lookback` bars to compute previous session's range."""
    if len(highs) < lookback + 5: return "NEUTRAL", "Not enough data"
    prev_h = max(highs[-lookback-1:-1])
    prev_l = min(lows[-lookback-1:-1])
    prev_c = closes[-lookback-1]
    pp  = (prev_h + prev_l + prev_c) / 3
    r1  = pp * 2 - prev_l
    r2  = pp + (prev_h - prev_l)
    s1  = pp * 2 - prev_h
    s2  = pp - (prev_h - prev_l)
    tol = atr * 0.7
    levels = [("S2",s2,"BUY"),("S1",s1,"BUY"),("PP",pp,"NEUTRAL"),("R1",r1,"SELL"),("R2",r2,"SELL")]
    for name, lvl, bias in levels:
        if abs(price - lvl) < tol:
            if bias == "BUY":
                return "BUY",  f"At {name}={lvl:.4f} (pivot support)"
            if bias == "SELL":
                return "SELL", f"At {name}={lvl:.4f} (pivot resistance)"
            # PP: direction depends on whether price is bouncing up or down
            if len(closes) >= 3:
                if closes[-1] > closes[-2]: return "BUY",  f"Bounce at PP={lvl:.4f}"
                if closes[-1] < closes[-2]: return "SELL", f"Rejection at PP={lvl:.4f}"
    return "NEUTRAL", f"PP={pp:.4f}  R1={r1:.4f}  S1={s1:.4f}"


def _detect_consecutive_bars_fade(opens, closes, atr):
    """Fade signal: 4+ consecutive bars in same direction — exhaustion likely.
    Counter-signal from PA book: market tends to reverse after 4+ consecutive bars."""
    if len(closes) < 6: return "NEUTRAL", "Not enough data"
    bull_count = 0
    bear_count = 0
    for i in range(-5, 0):
        if closes[i] > opens[i]: bull_count += 1
        else: bull_count = 0
        if closes[i] < opens[i]: bear_count += 1
        else: bear_count = 0
    if bull_count >= 4:
        return "SELL", f"Exhaustion: {bull_count} consecutive bullish bars — fade signal"
    if bear_count >= 4:
        return "BUY",  f"Exhaustion: {bear_count} consecutive bearish bars — fade signal"
    return "NEUTRAL", f"Bull streak: {bull_count}  Bear streak: {bear_count}"


def _get_ict_killzone():
    """Returns (zone_name, level: HIGH/MEDIUM/LOW). All UTC."""
    hm = datetime.now(timezone.utc).hour * 60 + datetime.now(timezone.utc).minute
    if   0   <= hm <  120: return "Asia Open",     "MEDIUM"
    elif 420  <= hm <  600: return "London KZ",     "HIGH"
    elif 600  <= hm <  720: return "London-NY",     "MEDIUM"
    elif 720  <= hm <  900: return "New York KZ",   "HIGH"
    return "Off-Hours", "LOW"

def _detect_bos(closes, highs, lows):
    """Break of Structure: HH+HL=BUY, LL+LH=SELL."""
    lb = 3
    n  = len(closes)
    ph = []; pl = []
    for i in range(lb, n-lb):
        if all(highs[i] >= highs[j] for j in range(i-lb, i+lb+1) if j!=i): ph.append(highs[i])
        if all(lows[i]  <= lows[j]  for j in range(i-lb, i+lb+1) if j!=i): pl.append(lows[i])
    if len(ph)<2 or len(pl)<2:
        return "NEUTRAL","Not enough pivots"
    hh = ph[-1]>ph[-2]; hl = pl[-1]>pl[-2]
    ll = pl[-1]<pl[-2]; lh = ph[-1]<ph[-2]
    if hh and hl: return "BUY",  f"BOS: HH+HL ({ph[-1]:.2f}/{pl[-1]:.2f})"
    if ll and lh: return "SELL", f"BOS: LL+LH ({pl[-1]:.2f}/{ph[-1]:.2f})"
    if hh and ll: return "BUY",  f"CHoCH: HH after LL — reversal UP"
    if ll and hh: return "SELL", f"CHoCH: LL after HH — reversal DOWN"
    return "NEUTRAL","No clear BOS"

def _check_sr_level(closes, highs, lows, price, atr):
    """Check if price is at a key S/R level (pivot high/low)."""
    lb = 5; n = len(closes); tol = atr*0.3
    ph = []; pl = []
    for i in range(lb, n-lb):
        if all(highs[i]>=highs[j] for j in range(i-lb,i+lb+1) if j!=i): ph.append(highs[i])
        if all(lows[i] <=lows[j]  for j in range(i-lb,i+lb+1) if j!=i): pl.append(lows[i])
    for r in ph[-8:]:
        if abs(price-r)<tol: return "SELL",f"At Resistance {r:.2f}"
    for s in pl[-8:]:
        if abs(price-s)<tol: return "BUY", f"At Support {s:.2f}"
    return "NEUTRAL","No key S/R nearby"

def _check_fvg_zone(highs, lows, price):
    """Return if price is inside a recent FVG."""
    n = len(highs)
    for i in range(max(2, n-40), n):
        fd, fl, fh = _find_fvg(highs, lows, i)
        if fd=="bullish" and fl<=price<=fh: return "BUY", f"In Bullish FVG [{fl:.2f}–{fh:.2f}]"
        if fd=="bearish" and fl<=price<=fh: return "SELL",f"In Bearish FVG [{fl:.2f}–{fh:.2f}]"
    return "NEUTRAL","No active FVG"

def _check_ob_zone(opens, closes, highs, lows, price, atr):
    """Check if price is at a fresh Order Block."""
    IMPULSE=3; LOOKBACK=60; n=len(closes); tol=atr*0.5
    avg_size = sum(abs(closes[i]-opens[i]) for i in range(-20,0))/20 if n>=20 else atr*0.1
    bull_ob=bear_ob=None
    for i in range(max(1,n-LOOKBACK), n-4):
        bi = sum(1 for j in range(i+1,min(i+1+IMPULSE,n)) if closes[j]>opens[j] and abs(closes[j]-opens[j])>avg_size*0.5)
        if bi>=IMPULSE and closes[i]<opens[i]:
            oh=max(opens[i],closes[i]); ol=min(opens[i],closes[i])
            if not any(lows[j]<=oh and highs[j]>=ol for j in range(i+IMPULSE+1,n-1)):
                bull_ob=(ol,oh)
        si = sum(1 for j in range(i+1,min(i+1+IMPULSE,n)) if closes[j]<opens[j] and abs(closes[j]-opens[j])>avg_size*0.5)
        if si>=IMPULSE and closes[i]>opens[i]:
            oh=max(opens[i],closes[i]); ol=min(opens[i],closes[i])
            if not any(highs[j]>=ol and lows[j]<=oh for j in range(i+IMPULSE+1,n-1)):
                bear_ob=(ol,oh)
    if bull_ob:
        ol,oh=bull_ob
        if ol-tol<=price<=oh+tol: return "BUY", f"In Bullish OB [{ol:.2f}–{oh:.2f}]"
    if bear_ob:
        ol,oh=bear_ob
        if ol-tol<=price<=oh+tol: return "SELL",f"In Bearish OB [{ol:.2f}–{oh:.2f}]"
    return "NEUTRAL","No fresh OB"

def _check_liq_sweep(highs, lows, closes, atr):
    """Detect recent liquidity sweep with reversal (last 6 bars)."""
    lb=5; n=len(closes)
    for i in range(max(lb+2,n-6), n-1):
        wh=max(highs[i-lb:i]); wl=min(lows[i-lb:i])
        if lows[i]<wl-atr*0.1 and closes[i]>wl:
            return "BUY",  f"Liq Sweep below {wl:.2f} → reversal UP"
        if highs[i]>wh+atr*0.1 and closes[i]<wh:
            return "SELL", f"Liq Sweep above {wh:.2f} → reversal DOWN"
    return "NEUTRAL","No recent sweep"

def _match_position(p, magic, strategy=""):
    """True if position belongs to this strategy — by magic (same session) OR comment (cross-restart).
    hash() is non-deterministic in Python 3.3+, so magic changes each restart.
    Comment prefix survives restarts; broker truncates to 16 chars so we match on [:16]."""
    if p.magic == magic:
        return True
    if strategy:
        prefix = f"FarhanFX-{strategy}"[:16]
        return (p.comment or "").startswith(prefix) and "Close" not in (p.comment or "")
    return False

def _has_open_position(symbol, magic, strategy=""):
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return False
    return any(_match_position(p, magic, strategy) for p in positions)

def _count_open_positions(symbol, magic, strategy=""):
    """Count open positions matching by magic OR comment prefix (survives server restarts)."""
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return 0
    return sum(1 for p in positions if _match_position(p, magic, strategy))

def _get_open_positions_detail(symbol, magic):
    """Return list of dicts for open positions of a strategy."""
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return []
    result = []
    for p in positions:
        if p.magic == magic:
            result.append({
                "ticket":    p.ticket,
                "type":      "BUY" if p.type == 0 else "SELL",
                "volume":    p.volume,
                "price_open": round(p.price_open, 5),
                "price_current": round(p.price_current, 5),
                "profit":    round(p.profit, 2),
                "sl":        round(p.sl, 5),
                "tp":        round(p.tp, 5),
            })
    return result

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
        "deviation":    30,
        "magic":        magic,
        "comment":      comment,
        "type_time":    mt5.ORDER_TIME_GTC,
    }
    for filling in [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]:
        req["type_filling"] = filling
        result = mt5.order_send(req)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            return result
        if result.retcode != 10038:
            return result
    return result


# ── AI SIGNAL GATE ─────────────────────────────────────────────────────────────
# Shared cache: refreshed every 5 min per symbol to avoid repeated MT5 calls
_ai_gate_cache: dict = {}  # symbol → {time, buy_score, sell_score, reason}

def _refresh_ai_gate(symbol: str) -> dict:
    """Fetch H4+D1+H1 data and compute directional AI scores (0-100)."""
    try:
        h4_r, _ = _get_rates(symbol, "H4", 200)
        d1_r, _ = _get_rates(symbol, "D1",  60)
        h1_r, _ = _get_rates(symbol, "H1", 100)

        def _score(direction):
            s = 0
            reasons = []

            # D1 trend (30 pts)
            if d1_r is not None and len(d1_r) >= 30:
                d1c = [float(r["close"]) for r in d1_r]
                d1e50  = _ema(d1c, min(50,  len(d1c)-1))
                d1e200 = _ema(d1c, min(200, len(d1c)-1))
                if d1c[-1] > d1e50[-1] > d1e200[-1]:   d1_dir = "BUY"
                elif d1c[-1] < d1e50[-1] < d1e200[-1]: d1_dir = "SELL"
                else:                                   d1_dir = "NEUTRAL"
                if d1_dir == direction:  s += 30; reasons.append("D1✓")
                elif d1_dir == "NEUTRAL": s += 10; reasons.append("D1~")
                else: reasons.append("D1✗")

            # H4 trend (25 pts)
            if h4_r is not None and len(h4_r) >= 50:
                h4c = [float(r["close"]) for r in h4_r]
                h4e50  = _ema(h4c, 50)
                h4e200 = _ema(h4c, min(200, len(h4c)-1))
                if h4c[-1] > h4e50[-1] > h4e200[-1]:   h4_dir = "BUY"
                elif h4c[-1] < h4e50[-1] < h4e200[-1]: h4_dir = "SELL"
                else:                                   h4_dir = "NEUTRAL"
                if h4_dir == direction:  s += 25; reasons.append("H4✓")
                elif h4_dir == "NEUTRAL": s += 8;  reasons.append("H4~")
                else: reasons.append("H4✗")

            # H1 EMA + RSI (25 pts)
            if h1_r is not None and len(h1_r) >= 50:
                h1c = [float(r["close"]) for r in h1_r]
                h1h = [float(r["high"])  for r in h1_r]
                h1l = [float(r["low"])   for r in h1_r]
                e20 = _ema(h1c, 20); e50 = _ema(h1c, 50)
                rsi = _rsi(h1c, 14)
                atr = _atr(h1h, h1l, h1c, 14)
                price = h1c[-1]

                # EMA alignment (15 pts)
                if direction == "BUY"  and price > e20[-1] > e50[-1]: s += 15; reasons.append("H1EMA✓")
                elif direction == "SELL" and price < e20[-1] < e50[-1]: s += 15; reasons.append("H1EMA✓")
                else: reasons.append("H1EMA✗")

                # RSI not extreme (10 pts)
                if direction == "BUY"  and 35 <= rsi <= 65: s += 10; reasons.append(f"RSI{rsi:.0f}✓")
                elif direction == "SELL" and 35 <= rsi <= 65: s += 10; reasons.append(f"RSI{rsi:.0f}✓")
                elif direction == "BUY"  and rsi > 72: reasons.append(f"RSI{rsi:.0f}OB✗")
                elif direction == "SELL" and rsi < 28: reasons.append(f"RSI{rsi:.0f}OS✗")
                else: s += 5; reasons.append(f"RSI{rsi:.0f}~")

            # Price action patterns (20 pts)
            if h1_r is not None and len(h1_r) >= 10:
                h1o = [float(r["open"])  for r in h1_r]
                h1c2= [float(r["close"]) for r in h1_r]
                h1h2= [float(r["high"])  for r in h1_r]
                h1l2= [float(r["low"])   for r in h1_r]
                pa = _detect_price_action(h1o, h1c2, h1h2, h1l2)
                if pa:
                    best = max(pa, key=lambda x: x[2])
                    if best[1] == direction and best[2] >= 0.6:
                        s += 20; reasons.append(f"PA:{best[0]}✓")
                    elif best[1] == direction:
                        s += 10; reasons.append(f"PA:{best[0]}~")
                    else:
                        reasons.append("PA✗")

            return min(s, 100), " | ".join(reasons)

        buy_score,  buy_reason  = _score("BUY")
        sell_score, sell_reason = _score("SELL")
        result = {
            "time":          datetime.now(),
            "buy_score":     buy_score,
            "sell_score":    sell_score,
            "buy_reason":    buy_reason,
            "sell_reason":   sell_reason,
        }
        _ai_gate_cache[symbol] = result
        return result
    except Exception:
        return {"time": datetime.now(), "buy_score": 50, "sell_score": 50,
                "buy_reason": "error", "sell_reason": "error"}

def _check_ai_gate(symbol: str, direction: str, threshold: int = 55) -> tuple:
    """Return (approved: bool, score: int, reason: str). Cache valid 5 min."""
    cached = _ai_gate_cache.get(symbol)
    age = (datetime.now() - cached["time"]).seconds if cached else 999
    if age > 300:  # refresh every 5 min
        cached = _mt5_call(lambda: _refresh_ai_gate(symbol))
        if isinstance(cached, dict) and "error" in cached:
            return True, 50, "gate-error"
    if not cached:
        return True, 50, "no-cache"
    score  = cached["buy_score"]  if direction == "BUY"  else cached["sell_score"]
    reason = cached["buy_reason"] if direction == "BUY"  else cached["sell_reason"]
    return score >= threshold, score, reason

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

    max_trades  = int(cfg.get("max_trades", 2))
    prev_count  = 0   # track open position count to detect closures

    def do_trade(side, entry, sl_val, tp_val):
        def fn():
            cur_count = _count_open_positions(symbol, magic, strategy)
            if cur_count >= max_trades:
                return {"skip": True, "reason": f"max_trades({max_trades})"}
            return _send_order(symbol, side, volume, sl_val, tp_val, magic, f"FarhanFX-{strategy}")
        result = _mt5_call(fn)
        if result and isinstance(result, dict) and result.get("skip"):
            reason = result.get("reason", "")
            if reason:
                add_log(f"⏸ Signal blocked — {reason} open, waiting for SL/TP")
            return
        if result and hasattr(result, "retcode") and result.retcode == mt5.TRADE_RETCODE_DONE:
            add_log(f"✅ {side} #{result.order} @ {result.price}  SL:{sl_val}  TP:{tp_val}")
            _strategies[sid]["trades"] += 1
            _tg_notify(
                f"<b>FarhanFX — Trade Opened</b>\n"
                f"📊 <b>{strategy}</b> | {symbol}\n"
                f"{'🟢 BUY' if side.upper() == 'BUY' else '🔴 SELL'} @ <code>{result.price}</code>\n"
                f"SL: <code>{sl_val}</code>  TP: <code>{tp_val}</code>\n"
                f"Lot: <code>{volume}</code>"
            )
        elif result:
            err = getattr(result, "comment", str(result))
            code = getattr(result, "retcode", "?")
            add_log(f"❌ Order failed: {err} (retcode {code})")

    while not stop_ev.is_set():
        try:
            rates, symbol = _get_rates(symbol, tf, 100)
            if rates is None or (isinstance(rates, dict) and "error" in rates):
                add_log(f"⚠️ Cannot get rates for '{symbol}'")
                _time.sleep(30)
                continue

            closes = [float(r["close"]) for r in rates]
            highs  = [float(r["high"])  for r in rates]
            lows   = [float(r["low"])   for r in rates]
            opens  = [float(r["open"])  for r in rates]

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

            pip      = _pip_size(sym_info)
            min_dist = max(sym_info.trade_stops_level * sym_info.point, pip)
            signal   = None

            # ── News auto-close: close positions 5 min before high-impact news ──────
            try:
                news_now = _get_upcoming_news(symbol, minutes_ahead=5, minutes_past=0)
                high_news = [e for e in news_now if e["impact"]=="High" and 0<=e["mins_from_now"]<=5]
                if high_news:
                    cur_pos = _mt5_call(lambda: _count_open_positions(symbol, magic, strategy)) or 0
                    if cur_pos > 0:
                        def _close_all_news():
                            positions = mt5.positions_get(symbol=symbol)
                            if not positions: return 0
                            closed = 0
                            for pos in positions:
                                if not _match_position(pos, magic, strategy): continue
                                rtype = mt5.ORDER_TYPE_SELL if pos.type==0 else mt5.ORDER_TYPE_BUY
                                t = mt5.symbol_info_tick(symbol)
                                pclose = t.bid if pos.type==0 else t.ask
                                r = mt5.order_send({
                                    "action": mt5.TRADE_ACTION_DEAL,
                                    "symbol": symbol, "volume": pos.volume,
                                    "type": rtype, "position": pos.ticket,
                                    "price": pclose, "deviation": 30,
                                    "magic": magic, "comment": "FarhanFX-News-Close",
                                    "type_time": mt5.ORDER_TIME_GTC,
                                    "type_filling": mt5.ORDER_FILLING_IOC,
                                })
                                if r and r.retcode==mt5.TRADE_RETCODE_DONE: closed+=1
                            return closed
                        n_cls = _mt5_call(_close_all_news)
                        if n_cls:
                            add_log(f"📰 Closed {n_cls} pos before news: {high_news[0]['title']}")
            except Exception as _ne:
                add_log(f"⚠️ News-close error: {_ne}")

            # ── Session filter: block known low-WR hours (data-driven) ──────────
            _utc_hour = datetime.now(timezone.utc).hour
            # Confirmed bad hours from 30-day live data analysis:
            # 05 UTC: 63.5% WR | 06 UTC: 58.6% WR | 12 UTC: 44% WR | 16 UTC: 51.9% WR | 20 UTC: 22% WR
            _session_block = _utc_hour in (5, 6, 12, 16, 20)

            # ── Minimum TP enforcement: require TP >= 1.5 × SL (data-driven R:R fix) ─
            if tp_pips < sl_pips * 1.5:
                tp_pips = round(sl_pips * 2.0)  # force 2:1 R:R

            # ── News bias: skip/filter signals 30 min around high-impact news ────
            _news_bias  = "NEUTRAL"
            _news_block = False
            try:
                news_soon = _get_upcoming_news(symbol, minutes_ahead=30, minutes_past=5)
                for ev in news_soon:
                    if ev["impact"]=="High":
                        if -5 <= ev["mins_from_now"] <= 30:
                            _news_block = True
                        if ev["direction"] in ("BULLISH","BEARISH"):
                            _news_bias = ev["direction"]
            except Exception:
                pass

            if strategy == "MA Cross":
                # Upgraded to Supertrend for better win rate
                direction = _supertrend(highs, lows, closes, period=10, mult=3.0)
                fast = _ema(closes, 20); slow = _ema(closes, 50)
                _strategies[sid]["indicator"] = f"[Upgraded→Supertrend] ST:{'▲' if direction[-1]==1 else '▼'}"
                if direction[-2]==-1 and direction[-1]==1 and fast[-1]>slow[-1]:
                    signal = "BUY"
                elif direction[-2]==1 and direction[-1]==-1 and fast[-1]<slow[-1]:
                    signal = "SELL"

            elif strategy == "EMA Trend":
                # Upgraded to EMA + RSI filter for better win rate
                ema20 = _ema(closes, 20); ema100 = _ema(closes, 100)
                rsi_v = _rsi(closes, 14)
                price = closes[-1]
                _strategies[sid]["indicator"] = f"[Upgraded] EMA20:{ema20[-1]:.5f} RSI:{rsi_v}"
                if price > ema20[-1] > ema100[-1] and rsi_v > 50:
                    signal = "BUY"
                elif price < ema20[-1] < ema100[-1] and rsi_v < 50:
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

            elif strategy == "Supertrend":
                direction = _supertrend(highs, lows, closes, period=10, mult=3.0)
                trend_txt = "▲ BULLISH" if direction[-1] == 1 else "▼ BEARISH"
                _strategies[sid]["indicator"] = f"Supertrend: {trend_txt}"
                # Direction flip = entry signal
                if direction[-2] == -1 and direction[-1] == 1:
                    signal = "BUY"
                elif direction[-2] == 1 and direction[-1] == -1:
                    signal = "SELL"

            elif strategy == "Triple Filter":
                # 200 EMA (trend) + SuperTrend flip (trigger) + RSI range (momentum) + US session
                from datetime import datetime as _dt
                ema200    = _ema(closes, 200)
                direction = _supertrend(highs, lows, closes, period=10, mult=3.0)
                rsi       = _rsi(closes, 14)
                price     = closes[-1]
                utc_hour  = _dt.utcnow().hour
                in_us     = 12 <= utc_hour < 16   # 12–16 UTC = 18–22 BDT
                st_txt    = "▲" if direction[-1] == 1 else "▼"
                sess_txt  = "🟢US" if in_us else "⏸Off"
                _strategies[sid]["indicator"] = (
                    f"EMA200:{ema200[-1]:.2f} ST:{st_txt} RSI:{rsi:.1f} {sess_txt}"
                )
                if in_us:
                    # BUY: ST flips bullish + price above 200 EMA + RSI 50–65
                    if (direction[-2] == -1 and direction[-1] == 1
                            and price > ema200[-1]
                            and 50 <= rsi <= 65):
                        signal = "BUY"
                    # SELL: ST flips bearish + price below 200 EMA + RSI 35–50
                    elif (direction[-2] == 1 and direction[-1] == -1
                            and price < ema200[-1]
                            and 35 <= rsi <= 50):
                        signal = "SELL"

            elif strategy == "Ichimoku":
                tk, kj, sa, sb = _ichimoku(highs, lows)
                price = closes[-1]
                cloud_top = max(sa[-1], sb[-1])
                cloud_bot = min(sa[-1], sb[-1])
                _strategies[sid]["indicator"] = (
                    f"T:{tk[-1]:.5f}  K:{kj[-1]:.5f}  "
                    f"Cloud:{cloud_bot:.5f}–{cloud_top:.5f}  Price:{price:.5f}"
                )
                # TK cross above cloud = strong BUY
                if tk[-2] < kj[-2] and tk[-1] > kj[-1] and price > cloud_top:
                    signal = "BUY"
                elif tk[-2] > kj[-2] and tk[-1] < kj[-1] and price < cloud_bot:
                    signal = "SELL"

            elif strategy == "AI Confluence":
                # Consensus of 4 independent signals — only trades on 4/4 agreement
                ema50  = _ema(closes, 50)
                ema200 = _ema(closes, 200)
                rsi    = _rsi(closes, 14)
                macd, msig = _macd(closes)
                mhist = [m - s for m, s in zip(macd, msig)]
                kv, dv = _stochastic(highs, lows, closes)
                price  = closes[-1]

                # Individual signals
                trend_bull = price > ema50[-1] > ema200[-1]
                trend_bear = price < ema50[-1] < ema200[-1]
                rsi_bull   = 40 < rsi < 60     # healthy uptrend zone
                rsi_bear   = 40 < rsi < 60     # healthy downtrend zone (symmetric)
                macd_bull  = mhist[-1] > 0 and mhist[-1] > mhist[-2]
                macd_bear  = mhist[-1] < 0 and mhist[-1] < mhist[-2]
                stoch_bull = kv[-1] < 50 and kv[-1] > dv[-1]
                stoch_bear = kv[-1] > 50 and kv[-1] < dv[-1]

                buy_score  = sum([trend_bull, rsi_bull, macd_bull, stoch_bull])
                sell_score = sum([trend_bear, rsi_bear, macd_bear, stoch_bear])

                _strategies[sid]["indicator"] = (
                    f"🤖 AI Score → BUY:{buy_score}/4  SELL:{sell_score}/4  "
                    f"RSI:{rsi}  Stoch:{kv[-1]:.0f}  MACD-H:{mhist[-1]:.5f}"
                )
                if buy_score >= 4:
                    signal = "BUY"
                elif sell_score >= 4:
                    signal = "SELL"

            elif strategy == "Order Block":
                # Smart Money Concept — Order Block detection
                # Bullish OB: last bearish candle before strong impulsive up-move
                # Bearish OB: last bullish candle before strong impulsive down-move
                # Entry: price returns into the OB zone (retrace), confirmed by a close back out
                # Fresh OB only: price hasn't touched the zone since it formed

                IMPULSE_CANDLES = 3   # minimum consecutive same-direction candles = impulsive move
                OB_LOOKBACK     = 50  # candles to scan for OBs

                def _is_bullish(i): return closes[i] > opens[i]
                def _is_bearish(i): return closes[i] < opens[i]
                def _candle_size(i): return abs(closes[i] - opens[i])
                avg_size = sum(_candle_size(i) for i in range(-20, 0)) / 20 if len(closes) >= 20 else pip

                bullish_ob = bearish_ob = None  # (ob_high, ob_low, formed_at_idx)

                n = len(closes)
                for i in range(max(1, n - OB_LOOKBACK), n - 4):
                    # Check for impulsive bullish move starting at i+1
                    bull_impulse = sum(1 for j in range(i + 1, min(i + 1 + IMPULSE_CANDLES, n))
                                       if _is_bullish(j) and _candle_size(j) > avg_size * 0.5)
                    if bull_impulse >= IMPULSE_CANDLES and _is_bearish(i):
                        ob_high = max(opens[i], closes[i])
                        ob_low  = min(opens[i], closes[i])
                        # Check fresh: price hasn't re-entered zone after formation
                        touched = any(lows[j] <= ob_high and highs[j] >= ob_low
                                      for j in range(i + IMPULSE_CANDLES + 1, n - 1))
                        if not touched:
                            bullish_ob = (ob_high, ob_low, i)

                    # Check for impulsive bearish move starting at i+1
                    bear_impulse = sum(1 for j in range(i + 1, min(i + 1 + IMPULSE_CANDLES, n))
                                       if _is_bearish(j) and _candle_size(j) > avg_size * 0.5)
                    if bear_impulse >= IMPULSE_CANDLES and _is_bullish(i):
                        ob_high = max(opens[i], closes[i])
                        ob_low  = min(opens[i], closes[i])
                        touched = any(highs[j] >= ob_low and lows[j] <= ob_high
                                      for j in range(i + IMPULSE_CANDLES + 1, n - 1))
                        if not touched:
                            bearish_ob = (ob_high, ob_low, i)

                price = closes[-1]
                last_high = highs[-1]
                last_low  = lows[-1]

                ob_info = "No fresh OB found"
                if bullish_ob:
                    bh, bl, bi = bullish_ob
                    ob_info = f"🟢 Bullish OB [{bl:.5f}–{bh:.5f}] formed @candle -{n-1-bi}"
                    # Entry: price retraces into OB zone and last candle closes back above OB low
                    if last_low <= bh and price >= bl and closes[-2] < bl and closes[-1] > bl:
                        signal = "BUY"
                if bearish_ob:
                    bh, bl, bi = bearish_ob
                    ob_info += f"  🔴 Bearish OB [{bl:.5f}–{bh:.5f}] formed @candle -{n-1-bi}"
                    if last_high >= bl and price <= bh and closes[-2] > bh and closes[-1] < bh:
                        signal = "SELL"

                _strategies[sid]["indicator"] = f"📦 OB: {ob_info} | Price:{price}"

            elif strategy == "AI Agent":
                # Full scoring via the same engine used in /api/ai/analyze
                ema20  = _ema(closes, 20)
                ema50  = _ema(closes, 50)
                ema200 = _ema(closes, min(200, len(closes)-1))
                rsi_v  = _rsi(closes, 14)
                macd_l, sig_l = _macd(closes)
                mhist  = [m - s for m, s in zip(macd_l, sig_l)]
                st_dir = _supertrend(highs, lows, closes)
                kv, dv = _stochastic(highs, lows, closes)

                # H4 bias (fetch separately)
                h4_rates, _ = _get_rates(symbol, "H4", 100)
                h4_bull = h4_bear = False
                if h4_rates is not None and len(h4_rates) >= 50:
                    h4c    = [float(r["close"]) for r in h4_rates]
                    h4e50  = _ema(h4c, 50)
                    h4e200 = _ema(h4c, min(200, len(h4c)-1))
                    h4_bull = h4c[-1] > h4e50[-1] > h4e200[-1]
                    h4_bear = h4c[-1] < h4e50[-1] < h4e200[-1]

                price = closes[-1]
                buy_pts = sell_pts = 0

                if h4_bull:              buy_pts  += 25
                if h4_bear:              sell_pts += 25
                if price > ema20[-1] > ema50[-1]:  buy_pts  += 20
                if price < ema20[-1] < ema50[-1]:  sell_pts += 20
                if st_dir[-1] == 1:      buy_pts  += 20
                if st_dir[-1] == -1:     sell_pts += 20
                if mhist[-1] > 0 and mhist[-1] >= mhist[-2]: buy_pts  += 15
                if mhist[-1] < 0 and mhist[-1] <= mhist[-2]: sell_pts += 15
                if 50 <= rsi_v <= 68:    buy_pts  += 10
                if 32 <= rsi_v <= 50:    sell_pts += 10
                if kv[-2] < dv[-2] and kv[-1] > dv[-1]: buy_pts  += 10
                if kv[-2] > dv[-2] and kv[-1] < dv[-1]: sell_pts += 10

                _strategies[sid]["indicator"] = (
                    f"🤖 AI Agent → BUY:{buy_pts}% SELL:{sell_pts}% | "
                    f"H4:{'▲' if h4_bull else '▼' if h4_bear else '—'} "
                    f"ST:{'▲' if st_dir[-1]==1 else '▼'} "
                    f"RSI:{rsi_v} MACD:{'↑' if mhist[-1]>mhist[-2] else '↓'}"
                )

                # News bias boost
                if _news_bias == "BULLISH": buy_pts  += 15
                if _news_bias == "BEARISH": sell_pts += 15
                # SMC additions
                atr_v2 = _atr(highs, lows, closes, 14)
                bos2, _ = _detect_bos(closes, highs, lows)
                liq2, _ = _check_liq_sweep(highs, lows, closes, atr_v2)
                ob2, _  = _check_ob_zone(opens, closes, highs, lows, price, atr_v2)
                if bos2=="BUY":  buy_pts  += 15
                if bos2=="SELL": sell_pts += 15
                if liq2=="BUY":  buy_pts  += 15
                if liq2=="SELL": sell_pts += 15
                if ob2=="BUY":   buy_pts  += 10
                if ob2=="SELL":  sell_pts += 10
                # Kill zone bonus
                kz_name, kz_level = _get_ict_killzone()
                if kz_level=="HIGH": buy_pts+=5; sell_pts+=5  # timing bonus (both)
                # Fibonacci level (from Candlestick Bible — 50/61.8% key pullback zones)
                fib3, _ = _check_fibonacci_level(highs, lows, price, atr_v2)
                if fib3=="BUY":  buy_pts  += 12
                if fib3=="SELL": sell_pts += 12
                # 21 EMA dynamic support/resistance (Candlestick Bible)
                ema21_3, _ = _check_21ema_bounce(closes, highs, lows, price, atr_v2)
                if ema21_3=="BUY":  buy_pts  += 10
                if ema21_3=="SELL": sell_pts += 10
                # Double Top/Bottom chart pattern (reversal confirmation)
                dt3, _ = _detect_double_top_bottom(highs, lows, closes, atr_v2)
                if dt3=="BUY":  buy_pts  += 10
                if dt3=="SELL": sell_pts += 10
                # PA pattern bonus (IB FBO 0.88 and high-strength patterns boost score)
                pa3 = _detect_price_action(opens, closes, highs, lows)
                for p3 in pa3:
                    if p3[1]=="BUY":  buy_pts  += int(p3[2] * 12)
                    if p3[1]=="SELL": sell_pts += int(p3[2] * 12)
                max_pts = 207  # 160 base + 12 fib + 10 ema21 + 10 dt + ~15 avg PA
                buy_pct2  = round(buy_pts  / max_pts * 100)
                sell_pct2 = round(sell_pts / max_pts * 100)
                _strategies[sid]["indicator"] = (
                    f"🤖 AI+ BUY:{buy_pct2}% SELL:{sell_pct2}% | "
                    f"H4:{'▲' if h4_bull else '▼' if h4_bear else '—'} "
                    f"BOS:{bos2} Liq:{liq2} KZ:{kz_name} News:{_news_bias}"
                )
                buy_pts  = buy_pct2
                sell_pts = sell_pct2

                if buy_pts >= 60 and buy_pts > sell_pts and not _news_block:
                    signal = "BUY"
                elif sell_pts >= 60 and sell_pts > buy_pts and not _news_block:
                    signal = "SELL"

            elif strategy == "Pin Bar SR":
                # Pin Bar at Key Level: Trend + Level + Signal (3-pillar, Candlestick Bible)
                atr_v = _atr(highs, lows, closes, 14)
                price = closes[-1]
                h4_rates, _ = _get_rates(symbol, "H4", 100)
                h4_bull = h4_bear = False
                if h4_rates is not None and len(h4_rates) >= 50:
                    h4c    = [float(r["close"]) for r in h4_rates]
                    h4e50  = _ema(h4c, 50)
                    h4e200 = _ema(h4c, min(200, len(h4c)-1))
                    h4_bull = h4c[-1] > h4e50[-1] > h4e200[-1]
                    h4_bear = h4c[-1] < h4e50[-1] < h4e200[-1]
                sr_dir,  sr_det  = _check_sr_level(closes, highs, lows, price, atr_v)
                fib_dir, fib_det = _check_fibonacci_level(highs, lows, price, atr_v)
                ema_dir, ema_det = _check_21ema_bounce(closes, highs, lows, price, atr_v)
                level_bull = (sr_dir=="BUY" or fib_dir=="BUY" or ema_dir=="BUY")
                level_bear = (sr_dir=="SELL" or fib_dir=="SELL" or ema_dir=="SELL")
                pa = _detect_price_action(opens, closes, highs, lows)
                pb_bull = any(p[0] in ("Bullish Pin Bar","Hammer","Tweezer Bottom") and p[1]=="BUY"  for p in pa)
                pb_bear = any(p[0] in ("Bearish Pin Bar","Shooting Star","Tweezer Top") and p[1]=="SELL" for p in pa)
                pa_names = [p[0] for p in pa] if pa else ["None"]
                _strategies[sid]["indicator"] = (
                    f"📌 PinBarSR | SR:{sr_dir}|Fib:{fib_dir}|EMA21:{ema_dir} | "
                    f"PA:{','.join(pa_names)} | H4:{'▲' if h4_bull else '▼' if h4_bear else '—'}"
                )
                if h4_bull and level_bull and pb_bull: signal = "BUY"
                elif h4_bear and level_bear and pb_bear: signal = "SELL"

            elif strategy == "Engulfing Trend":
                # Engulfing Bar at key level with trend (Candlestick Bible 3-pillar strategy)
                atr_v = _atr(highs, lows, closes, 14)
                price = closes[-1]
                ema50  = _ema(closes, min(50,  len(closes)-1))
                ema200 = _ema(closes, min(200, len(closes)-1))
                trend_bull = price > ema50[-1] > ema200[-1]
                trend_bear = price < ema50[-1] < ema200[-1]
                sr_dir,  _  = _check_sr_level(closes, highs, lows, price, atr_v)
                ema_dir, _  = _check_21ema_bounce(closes, highs, lows, price, atr_v)
                fib_dir, _  = _check_fibonacci_level(highs, lows, price, atr_v)
                level_bull = (sr_dir=="BUY" or ema_dir=="BUY" or fib_dir=="BUY")
                level_bear = (sr_dir=="SELL" or ema_dir=="SELL" or fib_dir=="SELL")
                pa = _detect_price_action(opens, closes, highs, lows)
                eng_bull = any(p[0] in ("Bullish Engulfing","Morning Star","Piercing Pattern") and p[1]=="BUY"  for p in pa)
                eng_bear = any(p[0] in ("Bearish Engulfing","Evening Star","Dark Cloud Cover") and p[1]=="SELL" for p in pa)
                pa_names = [p[0] for p in pa] if pa else ["None"]
                _strategies[sid]["indicator"] = (
                    f"🕯️ EngulfTrend | Trend:{'▲' if trend_bull else '▼' if trend_bear else '—'} | "
                    f"SR:{sr_dir}|EMA:{ema_dir}|Fib:{fib_dir} | PA:{','.join(pa_names)}"
                )
                if trend_bull and level_bull and eng_bull: signal = "BUY"
                elif trend_bear and level_bear and eng_bear: signal = "SELL"

            elif strategy == "Inside Bar":
                # Inside Bar breakout in trend direction (consolidation → breakout setup)
                atr_v = _atr(highs, lows, closes, 14)
                price = closes[-1]
                ema50  = _ema(closes, min(50,  len(closes)-1))
                ema200 = _ema(closes, min(200, len(closes)-1))
                st_dir = _supertrend(highs, lows, closes)
                trend_bull = closes[-1] > ema50[-1] > ema200[-1] and st_dir[-1] == 1
                trend_bear = closes[-1] < ema50[-1] < ema200[-1] and st_dir[-1] == -1
                pa = _detect_price_action(opens, closes, highs, lows)
                ib_detected = any(p[0] == "Inside Bar" for p in pa)
                n_pa = len(closes)
                mother_high = highs[-3] if n_pa >= 3 else highs[-2]
                mother_low  = lows[-3]  if n_pa >= 3 else lows[-2]
                _strategies[sid]["indicator"] = (
                    f"📦 InsideBar | Trend:{'▲' if trend_bull else '▼' if trend_bear else '—'} | "
                    f"IB:{'Yes' if ib_detected else 'No'} | Mother H={mother_high:.4f} L={mother_low:.4f}"
                )
                if ib_detected and trend_bull and closes[-1] > mother_high: signal = "BUY"
                elif ib_detected and trend_bear and closes[-1] < mother_low: signal = "SELL"

            elif strategy == "False Breakout":
                # Inside Bar False Breakout — institutional stop hunt trap (highest WR pattern)
                atr_v = _atr(highs, lows, closes, 14)
                price = closes[-1]
                pa = _detect_price_action(opens, closes, highs, lows)
                fbo_bull = any(p[0] == "IB False Breakout" and p[1] == "BUY"  for p in pa)
                fbo_bear = any(p[0] == "IB False Breakout" and p[1] == "SELL" for p in pa)
                sr_dir,  _ = _check_sr_level(closes, highs, lows, price, atr_v)
                fib_dir, _ = _check_fibonacci_level(highs, lows, price, atr_v)
                dt_dir,  _ = _detect_double_top_bottom(highs, lows, closes, atr_v)
                confirm_bull = (sr_dir in ("BUY","NEUTRAL") or fib_dir in ("BUY","NEUTRAL") or dt_dir == "BUY")
                confirm_bear = (sr_dir in ("SELL","NEUTRAL") or fib_dir in ("SELL","NEUTRAL") or dt_dir == "SELL")
                fbo_status = "BULL TRAP→BUY" if fbo_bull else ("BEAR TRAP→SELL" if fbo_bear else "None")
                _strategies[sid]["indicator"] = (
                    f"🎣 FalseBreakout | FBO:{fbo_status} | SR:{sr_dir} | Fib:{fib_dir} | DT:{dt_dir}"
                )
                if fbo_bull and confirm_bull: signal = "BUY"
                elif fbo_bear and confirm_bear: signal = "SELL"

            elif strategy == "PA Confluence":
                # Full 3-Pillar PA Strategy: Trend + Key Level + PA Signal (all books)
                atr_v = _atr(highs, lows, closes, 14)
                price = closes[-1]
                # PILLAR 1: TREND (H4 bias + current TF)
                h4_rates, _ = _get_rates(symbol, "H4", 100)
                h4_bull = h4_bear = False
                if h4_rates is not None and len(h4_rates) >= 50:
                    h4c    = [float(r["close"]) for r in h4_rates]
                    h4e50  = _ema(h4c, 50)
                    h4e200 = _ema(h4c, min(200, len(h4c)-1))
                    h4_bull = h4c[-1] > h4e50[-1] > h4e200[-1]
                    h4_bear = h4c[-1] < h4e50[-1] < h4e200[-1]
                ema50  = _ema(closes, min(50, len(closes)-1))
                st_dir = _supertrend(highs, lows, closes)
                tf_bull = price > ema50[-1] and st_dir[-1] == 1
                tf_bear = price < ema50[-1] and st_dir[-1] == -1
                trend_score = (2 if h4_bull else -2 if h4_bear else 0) + (1 if tf_bull else -1 if tf_bear else 0)
                trend_bull = trend_score >= 2
                trend_bear = trend_score <= -2
                # PILLAR 2: KEY LEVEL (multi-confluence)
                sr_dir,  _ = _check_sr_level(closes, highs, lows, price, atr_v)
                fib_dir, _ = _check_fibonacci_level(highs, lows, price, atr_v)
                ema_dir, _ = _check_21ema_bounce(closes, highs, lows, price, atr_v)
                ob_dir,  _ = _check_ob_zone(opens, closes, highs, lows, price, atr_v)
                liq_dir, _ = _check_liq_sweep(highs, lows, closes, atr_v)
                dt_dir,  _ = _detect_double_top_bottom(highs, lows, closes, atr_v)
                lb_score = sum([sr_dir=="BUY", fib_dir=="BUY", ema_dir=="BUY", ob_dir=="BUY", liq_dir=="BUY", dt_dir=="BUY"])
                ls_score = sum([sr_dir=="SELL", fib_dir=="SELL", ema_dir=="SELL", ob_dir=="SELL", liq_dir=="SELL", dt_dir=="SELL"])
                level_bull = lb_score >= 1
                level_bear = ls_score >= 1
                # PILLAR 3: PA SIGNAL (strength ≥ 0.70)
                pa = _detect_price_action(opens, closes, highs, lows)
                pa_bull_str = max((p[2] for p in pa if p[1]=="BUY"),  default=0)
                pa_bear_str = max((p[2] for p in pa if p[1]=="SELL"), default=0)
                pa_bull = pa_bull_str >= 0.70
                pa_bear = pa_bear_str >= 0.70
                bull_pa = max((p for p in pa if p[1]=="BUY"),  key=lambda x:x[2], default=("None","",0))[0]
                bear_pa = max((p for p in pa if p[1]=="SELL"), key=lambda x:x[2], default=("None","",0))[0]
                buy_pillars  = sum([trend_bull, level_bull, pa_bull])
                sell_pillars = sum([trend_bear, level_bear, pa_bear])
                _strategies[sid]["indicator"] = (
                    f"🏛️ PA Confluence | BUY:{buy_pillars}/3 SELL:{sell_pillars}/3 | "
                    f"Trend:{'▲' if trend_bull else '▼' if trend_bear else '—'} "
                    f"Level:B{lb_score}/S{ls_score} "
                    f"PA:{'✓' if pa_bull else '✗'}{bull_pa if pa_bull else bear_pa}"
                )
                if buy_pillars == 3:  signal = "BUY"
                elif sell_pillars == 3: signal = "SELL"

            elif strategy == "AI Signal Engine":
                # Primary AI trading system — calls the full _do_ai_analyze engine.
                # W1+D1+H4+H1 top-down analysis, 22 components, 3-Factor Gate.
                # Only trades when confidence >= ai_min_confidence AND all 3 factors align.
                ai_min_conf = int(cfg.get("ai_min_confidence", 62))
                try:
                    ai_result = _do_ai_analyze(symbol, tf)
                except Exception as _ae:
                    ai_result = {"error": str(_ae)}

                if isinstance(ai_result, dict) and "error" not in ai_result:
                    ai_sig    = ai_result.get("signal", "HOLD")
                    ai_conf   = ai_result.get("confidence", 0)
                    ai_3f     = ai_result.get("three_factor_pass", False)
                    ai_ct     = ai_result.get("counter_trend_block", False)
                    ai_3fd    = ai_result.get("three_factor_detail", "")
                    ai_w1     = ai_result.get("w1_bias", "?")
                    ai_d1     = ai_result.get("d1_bias", "?")
                    ai_h4     = ai_result.get("h4_bias", "?")
                    ai_bos    = ai_result.get("bos", "?")
                    ai_kz     = ai_result.get("killzone", "?")
                    ai_rsi    = ai_result.get("rsi", 0)
                    ai_nr7    = ai_result.get("nr7", False)
                    w1_icon   = "▲" if ai_w1=="BULLISH" else "▼" if ai_w1=="BEARISH" else "—"
                    d1_icon   = "▲" if ai_d1=="BULLISH" else "▼" if ai_d1=="BEARISH" else "—"
                    h4_icon   = "▲" if ai_h4=="BULLISH" else "▼" if ai_h4=="BEARISH" else "—"
                    _strategies[sid]["indicator"] = (
                        f"🧠 AIEngine {ai_sig} {ai_conf}% | "
                        f"W1:{w1_icon} D1:{d1_icon} H4:{h4_icon} | "
                        f"3F:{'✅' if ai_3f else '❌'} CT:{'🚫' if ai_ct else '—'} | "
                        f"BOS:{ai_bos} KZ:{ai_kz} RSI:{ai_rsi:.0f}"
                        f"{' NR7⚡' if ai_nr7 else ''}"
                    )
                    if ai_ct:
                        add_log(f"🚫 Counter-trend BLOCK (W1+D1 against signal) — skipped")
                    elif not ai_3f:
                        add_log(f"❌ 3-Factor Gate FAILED: {ai_3fd} — skipped ({ai_conf}%)")
                    elif ai_sig in ("BUY","SELL") and ai_conf >= ai_min_conf:
                        add_log(f"🧠 AI Signal Engine: {ai_sig} {ai_conf}% ✅ {ai_3fd}")
                        signal = ai_sig
                    else:
                        add_log(f"⏸ AI score {ai_conf}% < threshold {ai_min_conf}% — waiting")
                else:
                    _strategies[sid]["indicator"] = f"🧠 AIEngine — error: {ai_result.get('error','?')}"

            elif strategy == "M2B/M2S":
                # Two-legged pullback to rising/falling 20 EMA (PA Trading book)
                # Optimized filters: RSI 45-62 + EMA20>EMA50 → 59.3% WR, PF 1.91 (81 trades BT)
                ema20_v = _ema(closes, 20)
                ema50_v = _ema(closes, 50)
                rsi_c   = _rsi(closes, 14)
                atr_v   = _atr(highs, lows, closes, 14)

                ema_rising  = ema20_v[-1] > ema20_v[-9] > ema20_v[-17]
                ema_falling = ema20_v[-1] < ema20_v[-9] < ema20_v[-17]

                touched_bull = min(lows[-11:-1])   <= ema20_v[-6] + atr_v * 0.6
                touched_bear = max(highs[-11:-1])  >= ema20_v[-6] - atr_v * 0.6

                trend_bull = ema20_v[-1] > ema50_v[-1]
                trend_bear = ema20_v[-1] < ema50_v[-1]

                # D1 + H4 multi-TF trend filter (same as backtest)
                try:
                    _d1r = _mt5_call(lambda: mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 250))
                    _h4r = _mt5_call(lambda: mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H4, 0, 300))
                    _d1c = [float(r["close"]) for r in _d1r] if _d1r is not None and len(_d1r)>=52 else []
                    _h4c = [float(r["close"]) for r in _h4r] if _h4r is not None and len(_h4r)>=52 else []
                    _d1_bull = _d1_bear = _h4_bull = _h4_bear = False
                    if _d1c:
                        _e50d = _ema(_d1c, 50)[-1]; _e200d = _ema(_d1c, min(200,len(_d1c)-1))[-1]
                        _d1_bull = _d1c[-1] > _e50d > _e200d
                        _d1_bear = _d1c[-1] < _e50d < _e200d
                    if _h4c:
                        _e50h = _ema(_h4c, 50)[-1]; _e200h = _ema(_h4c, min(200,len(_h4c)-1))[-1]
                        _h4_bull = _h4c[-1] > _e50h > _e200h
                        _h4_bear = _h4c[-1] < _e50h < _e200h
                except Exception:
                    _d1_bull = _d1_bear = _h4_bull = _h4_bear = False

                _m2_hr    = datetime.now(timezone.utc).hour
                # Extended to include 03-04 UTC (90%+ WR confirmed by live data)
                _in_sess  = (3 <= _m2_hr < 5) or (7 <= _m2_hr < 11) or (13 <= _m2_hr < 16)
                # D1/H4: require at least ONE to confirm direction (not both — reduces missed trades)
                _tf_bull = (_d1_bull and not _h4_bear) or (_h4_bull and not _d1_bear)
                _tf_bear = (_d1_bear and not _h4_bull) or (_h4_bear and not _d1_bull)

                _m2b = (ema_rising  and closes[-1] > ema20_v[-1] and closes[-1] > closes[-2]
                        and touched_bull and closes[-1] > opens[-1]
                        and 45 < rsi_c < 62 and trend_bull
                        and _tf_bull and _in_sess)
                _m2s = (ema_falling and closes[-1] < ema20_v[-1] and closes[-1] < closes[-2]
                        and touched_bear and closes[-1] < opens[-1]
                        and 38 < rsi_c < 55 and trend_bear
                        and _tf_bear and _in_sess)

                _strategies[sid]["indicator"] = (
                    f"EMA20:{ema20_v[-1]:.2f} E50:{ema50_v[-1]:.2f} RSI:{rsi_c:.0f} | "
                    f"D1:{'▲' if _d1_bull else '▼' if _d1_bear else '—'} "
                    f"H4:{'▲' if _h4_bull else '▼' if _h4_bear else '—'} | "
                    f"Trend:{'▲' if ema_rising else '▼' if ema_falling else '—'} "
                    f"Touch:{'B✓' if touched_bull else '—'}/{'S✓' if touched_bear else '—'} "
                    f"Sess:{'✓' if _in_sess else '✗'}"
                )
                if _m2b:
                    signal = "BUY"
                    add_log(f"M2B: EMA bounce BUY | RSI:{rsi_c:.0f} D1▲ H4▲")
                elif _m2s:
                    signal = "SELL"
                    add_log(f"M2S: EMA bounce SELL | RSI:{rsi_c:.0f} D1▼ H4▼")

            elif strategy == "Trend Continuation":
                # EMA200 + Supertrend + pullback to EMA50 — 63.6% WR on H1 (backtest)
                # Fetch 250 bars for EMA200 warm-up (default loop only gets 100)
                try:
                    _tcr = _mt5_call(lambda: mt5.copy_rates_from_pos(
                        symbol, TF_MAP.get(tf, mt5.TIMEFRAME_H1), 0, 250))
                    _tc_c = [float(r['close']) for r in _tcr] if _tcr and len(_tcr)>=220 else closes
                    _tc_h = [float(r['high'])  for r in _tcr] if _tcr and len(_tcr)>=220 else highs
                    _tc_l = [float(r['low'])   for r in _tcr] if _tcr and len(_tcr)>=220 else lows
                    _tc_o = [float(r['open'])  for r in _tcr] if _tcr and len(_tcr)>=220 else opens
                except Exception:
                    _tc_c = closes; _tc_h = highs; _tc_l = lows; _tc_o = opens

                _tc_e50  = _ema(_tc_c, 50)
                _tc_e200 = _ema(_tc_c, min(200, len(_tc_c)-1))
                _tc_rsi  = _rsi(_tc_c, 14)
                _tc_atr  = _atr(_tc_h, _tc_l, _tc_c, 14)
                _tc_st   = _supertrend(_tc_h, _tc_l, _tc_c, period=10, mult=3.0)

                # D1 + H4 multi-TF trend filter
                try:
                    _tc_d1r = _mt5_call(lambda: mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 250))
                    _tc_h4r = _mt5_call(lambda: mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H4, 0, 300))
                    _tc_d1b = _tc_d1be = _tc_h4b = _tc_h4be = False
                    if _tc_d1r is not None and len(_tc_d1r)>=52:
                        _d1c2=[float(r['close']) for r in _tc_d1r]
                        _d1e50=_ema(_d1c2,50)[-1]; _d1e200=_ema(_d1c2,min(200,len(_d1c2)-1))[-1]
                        _tc_d1b  = _d1c2[-1]>_d1e50>_d1e200
                        _tc_d1be = _d1c2[-1]<_d1e50<_d1e200
                    if _tc_h4r is not None and len(_tc_h4r)>=52:
                        _h4c2=[float(r['close']) for r in _tc_h4r]
                        _h4e50=_ema(_h4c2,50)[-1]; _h4e200=_ema(_h4c2,min(200,len(_h4c2)-1))[-1]
                        _tc_h4b  = _h4c2[-1]>_h4e50>_h4e200
                        _tc_h4be = _h4c2[-1]<_h4e50<_h4e200
                except Exception:
                    _tc_d1b=_tc_d1be=_tc_h4b=_tc_h4be=False

                _tc_price  = _tc_c[-1]
                _tc_near50 = abs(_tc_price - _tc_e50[-1]) < _tc_atr * 0.8
                _tc_rsi_v  = _tc_rsi
                _tc_bull_c = _tc_c[-1] > _tc_o[-1]
                _tc_bear_c = _tc_c[-1] < _tc_o[-1]
                _tc_hr     = datetime.now(timezone.utc).hour
                # Extended to include 03-04 UTC (90%+ WR) — was missing this prime window
                _tc_sess   = (3 <= _tc_hr < 5) or (7 <= _tc_hr < 11) or (13 <= _tc_hr < 16)
                # Relax D1/H4: at least one must confirm, other must not oppose
                _tc_tf_bull = (_tc_d1b  and not _tc_h4be) or (_tc_h4b  and not _tc_d1be)
                _tc_tf_bear = (_tc_d1be and not _tc_h4b)  or (_tc_h4be and not _tc_d1b)

                _strategies[sid]["indicator"] = (
                    f"E50:{_tc_e50[-1]:.2f} E200:{_tc_e200[-1]:.2f} RSI:{_tc_rsi_v:.0f} "
                    f"ST:{'▲' if _tc_st[-1]==1 else '▼'} Near50:{'✓' if _tc_near50 else '✗'} | "
                    f"D1:{'▲' if _tc_d1b else '▼' if _tc_d1be else '—'} "
                    f"H4:{'▲' if _tc_h4b else '▼' if _tc_h4be else '—'} "
                    f"Sess:{'✓' if _tc_sess else '✗'}"
                )
                if (_tc_price > _tc_e200[-1] and _tc_st[-1]==1 and _tc_near50
                        and _tc_bull_c and 45 < _tc_rsi_v < 65
                        and _tc_tf_bull and _tc_sess):
                    signal = "BUY"
                    add_log(f"TrendCont BUY: near EMA50 in uptrend RSI:{_tc_rsi_v:.0f}")
                elif (_tc_price < _tc_e200[-1] and _tc_st[-1]==-1 and _tc_near50
                        and _tc_bear_c and 35 < _tc_rsi_v < 55
                        and _tc_tf_bear and _tc_sess):
                    signal = "SELL"
                    add_log(f"TrendCont SELL: near EMA50 in downtrend RSI:{_tc_rsi_v:.0f}")

            elif strategy == "Trend Continuation M15":
                # M15-specific redesign (NOT the H1 numbers) — re-optimized via Python
                # grid search: EMA50/100 (was 50/200), near=0.6xATR (was 0.8), wider RSI
                # zone, ATR min 1.0 (was 3.0), SL1.5x/TP2.5x. Backtested: 89 trades,
                # 58.4% WR, PF 2.16 on XAUUSDc M15 (10-month window).
                try:
                    _m15r = _mt5_call(lambda: mt5.copy_rates_from_pos(
                        symbol, TF_MAP.get(tf, mt5.TIMEFRAME_M15), 0, 150))
                    _m15_c = [float(r['close']) for r in _m15r] if _m15r and len(_m15r)>=120 else closes
                    _m15_h = [float(r['high'])  for r in _m15r] if _m15r and len(_m15r)>=120 else highs
                    _m15_l = [float(r['low'])   for r in _m15r] if _m15r and len(_m15r)>=120 else lows
                    _m15_o = [float(r['open'])  for r in _m15r] if _m15r and len(_m15r)>=120 else opens
                except Exception:
                    _m15_c = closes; _m15_h = highs; _m15_l = lows; _m15_o = opens

                _m15_eF   = _ema(_m15_c, 50)
                _m15_eS   = _ema(_m15_c, min(100, len(_m15_c)-1))
                _m15_rsi  = _rsi(_m15_c, 14)
                _m15_atr  = _atr(_m15_h, _m15_l, _m15_c, 14)
                _m15_st   = _supertrend(_m15_h, _m15_l, _m15_c, period=10, mult=3.0)

                try:
                    _m15_d1r = _mt5_call(lambda: mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 250))
                    _m15_h4r = _mt5_call(lambda: mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H4, 0, 300))
                    _m15_d1b = _m15_d1be = _m15_h4b = _m15_h4be = False
                    if _m15_d1r is not None and len(_m15_d1r)>=52:
                        _d1c3=[float(r['close']) for r in _m15_d1r]
                        _d1e50b=_ema(_d1c3,50)[-1]; _d1e200b=_ema(_d1c3,min(200,len(_d1c3)-1))[-1]
                        _m15_d1b  = _d1c3[-1]>_d1e50b>_d1e200b
                        _m15_d1be = _d1c3[-1]<_d1e50b<_d1e200b
                    if _m15_h4r is not None and len(_m15_h4r)>=52:
                        _h4c3=[float(r['close']) for r in _m15_h4r]
                        _h4e50b=_ema(_h4c3,50)[-1]; _h4e200b=_ema(_h4c3,min(200,len(_h4c3)-1))[-1]
                        _m15_h4b  = _h4c3[-1]>_h4e50b>_h4e200b
                        _m15_h4be = _h4c3[-1]<_h4e50b<_h4e200b
                except Exception:
                    _m15_d1b=_m15_d1be=_m15_h4b=_m15_h4be=False

                _m15_price  = _m15_c[-1]
                _m15_near   = abs(_m15_price - _m15_eF[-1]) < _m15_atr * 0.6
                _m15_bull_c = _m15_c[-1] > _m15_o[-1]
                _m15_bear_c = _m15_c[-1] < _m15_o[-1]
                _m15_hr     = datetime.now(timezone.utc).hour
                # Extended to include 03-04 UTC (90%+ WR window from live data)
                _m15_sess   = (3 <= _m15_hr < 5) or (7 <= _m15_hr < 11) or (13 <= _m15_hr < 16)
                # Relax D1/H4: at least one must confirm, other must not oppose
                _m15_tf_bull = (_m15_d1b  and not _m15_h4be) or (_m15_h4b  and not _m15_d1be)
                _m15_tf_bear = (_m15_d1be and not _m15_h4b)  or (_m15_h4be and not _m15_d1b)

                _strategies[sid]["indicator"] = (
                    f"EMA50:{_m15_eF[-1]:.2f} EMA100:{_m15_eS[-1]:.2f} RSI:{_m15_rsi:.0f} "
                    f"ST:{'▲' if _m15_st[-1]==1 else '▼'} Near:{'✓' if _m15_near else '✗'} | "
                    f"D1:{'▲' if _m15_d1b else '▼' if _m15_d1be else '—'} "
                    f"H4:{'▲' if _m15_h4b else '▼' if _m15_h4be else '—'} "
                    f"Sess:{'✓' if _m15_sess else '✗'}"
                )
                if (_m15_price > _m15_eS[-1] and _m15_st[-1]==1 and _m15_near
                        and _m15_bull_c and 40 < _m15_rsi < 68
                        and _m15_tf_bull and _m15_sess
                        and _m15_atr >= 1.0):
                    signal = "BUY"
                    add_log(f"TrendCont M15 BUY: near EMA50 in uptrend RSI:{_m15_rsi:.0f}")
                elif (_m15_price < _m15_eS[-1] and _m15_st[-1]==-1 and _m15_near
                        and _m15_bear_c and 32 < _m15_rsi < 60
                        and _m15_tf_bear and _m15_sess
                        and _m15_atr >= 1.0):
                    signal = "SELL"
                    add_log(f"TrendCont M15 SELL: near EMA50 in downtrend RSI:{_m15_rsi:.0f}")

            # Block trades during bad sessions or news
            if signal and (_session_block or _news_block):
                reason = f"{_utc_hour:02d}:00 UTC low-WR dead zone" if _session_block else "high-impact news"
                add_log(f"⛔ Signal {signal} blocked — {reason}")
                signal = None

            # ── AI Gate: multi-TF analysis must confirm signal direction ─────────
            if signal:
                # Monday has 65.1% WR vs 75%+ other days — raise threshold on Mondays
                _is_monday   = datetime.now(timezone.utc).weekday() == 0
                _ai_threshold = 65 if _is_monday else 55
                _ai_approved, _ai_score, _ai_reason = _check_ai_gate(symbol, signal, threshold=_ai_threshold)
                _strategies[sid]["indicator"] = (_strategies[sid].get("indicator","") +
                    f" | AI:{_ai_score}/100")
                if not _ai_approved:
                    add_log(f"🤖 AI Gate BLOCKED {signal} (score {_ai_score}/{_ai_threshold}) — {_ai_reason}")
                    signal = None
                else:
                    add_log(f"🤖 AI Gate OK {signal} (score {_ai_score}/{_ai_threshold}) — {_ai_reason}")

            if signal:
                is_buy    = signal == "BUY"
                entry     = tick.ask if is_buy else tick.bid
                sl_tp_ref = tick.bid if is_buy else tick.ask
                # ATR-based SL: use 1.5×ATR if it's larger than fixed pips (adaptive)
                # Per-strategy R:R override (default 1.5/3.0 = 2:1; backtested exceptions below)
                _sl_mult, _tp_mult = 1.5, 3.0
                if strategy == "Trend Continuation M15":
                    _sl_mult, _tp_mult = 1.5, 2.5   # ~1.67:1 — what the M15 optimizer found best
                _atr_val  = _atr(highs, lows, closes, 14)
                _atr_sl   = _atr_val * _sl_mult
                _atr_tp   = _atr_val * _tp_mult
                _fixed_sl = sl_pips * pip
                _fixed_tp = tp_pips * pip
                sl_dist   = max(_atr_sl, _fixed_sl, min_dist + sym_info.point)
                tp_dist   = max(_atr_tp, _fixed_tp, min_dist + sym_info.point)
                sl_val    = round(sl_tp_ref - sl_dist, sym_info.digits) if is_buy \
                            else round(sl_tp_ref + sl_dist, sym_info.digits)
                tp_val    = round(sl_tp_ref + tp_dist, sym_info.digits) if is_buy \
                            else round(sl_tp_ref - tp_dist, sym_info.digits)
                add_log(f"📊 Signal: {signal} @ {entry}  SL:{sl_val}({sl_dist/pip:.1f}p)  TP:{tp_val}({tp_dist/pip:.1f}p)")
                do_trade(signal, entry, sl_val, tp_val)

            # Breakeven+spread trail: when profit distance >= risk distance (1:1 R:R),
            # move SL just past entry by the current spread so the close still nets a
            # small real profit instead of landing exactly on cost (spread would eat it).
            def _trail_sl():
                """
                3-phase trailing SL:
                  Phase 1 (profit < 1R): SL untouched
                  Phase 2 (profit >= 1R, SL still behind entry): SL → entry + spread (small locked profit)
                  Phase 3 (SL at/above entry+spread): SL trails price at original risk distance
                """
                positions = mt5.positions_get(symbol=symbol)
                if not positions: return 0, 0
                spread = max(tick.ask - tick.bid, 0)
                be_moved = trail_moved = 0
                for pos in positions:
                    if not _match_position(pos, magic, strategy): continue
                    if not pos.sl: continue
                    risk = abs(pos.price_open - pos.sl)
                    if risk <= 0: continue
                    pt   = sym_info.point
                    digs = sym_info.digits
                    is_buy = pos.type == 0

                    if is_buy:
                        profit_dist = pos.price_current - pos.price_open
                        be_target   = round(pos.price_open + spread, digs)
                        at_be       = pos.sl >= be_target - pt  # SL already at/above entry+spread

                        if not at_be and profit_dist >= risk:
                            # Phase 2: hit 1:1 → move SL to entry+spread (locks a small profit)
                            r = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": symbol,
                                                "position": pos.ticket, "sl": be_target, "tp": pos.tp})
                            if r and r.retcode == mt5.TRADE_RETCODE_DONE: be_moved += 1

                        elif at_be:
                            # Phase 3: trail SL at (current_price - original_risk), only move up
                            trail_sl = round(pos.price_current - risk, digs)
                            if trail_sl > pos.sl + pt:
                                r = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": symbol,
                                                    "position": pos.ticket, "sl": trail_sl, "tp": pos.tp})
                                if r and r.retcode == mt5.TRADE_RETCODE_DONE: trail_moved += 1
                    else:
                        profit_dist = pos.price_open - pos.price_current
                        be_target   = round(pos.price_open - spread, digs)
                        at_be       = pos.sl <= be_target + pt  # SL already at/below entry-spread

                        if not at_be and profit_dist >= risk:
                            # Phase 2: hit 1:1 → move SL to entry-spread (locks a small profit)
                            r = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": symbol,
                                                "position": pos.ticket, "sl": be_target, "tp": pos.tp})
                            if r and r.retcode == mt5.TRADE_RETCODE_DONE: be_moved += 1

                        elif at_be:
                            # Phase 3: trail SL at (current_price + original_risk), only move down
                            trail_sl = round(pos.price_current + risk, digs)
                            if trail_sl < pos.sl - pt:
                                r = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": symbol,
                                                    "position": pos.ticket, "sl": trail_sl, "tp": pos.tp})
                                if r and r.retcode == mt5.TRADE_RETCODE_DONE: trail_moved += 1
                return be_moved, trail_moved
            try:
                be_n, tr_n = _mt5_call(_trail_sl)
                if be_n:  add_log(f"📌 {be_n} position(s) → breakeven+spread (1:1 R:R hit)")
                if tr_n:  add_log(f"🔒 {tr_n} position(s) SL trailed (locking profit)")
            except Exception:
                pass

            # Detect position closures (SL/TP hit) and log them
            def _cur_count(): return _count_open_positions(symbol, magic, strategy)
            try:
                cur_count = _mt5_call(_cur_count) or 0
                if prev_count > 0 and cur_count < prev_count:
                    freed = prev_count - cur_count
                    add_log(f"✅ {freed} position(s) closed (SL/TP hit) — {cur_count}/{max_trades} slots open")
                    _tg_notify(
                        f"<b>FarhanFX — Position Closed</b>\n"
                        f"📊 <b>{strategy}</b> | {symbol}\n"
                        f"✅ {freed} position(s) hit SL/TP\n"
                        f"Running P&amp;L: <code>${_strategies[sid].get('pnl', 0.0):.2f}</code>"
                    )
                prev_count = cur_count
            except Exception:
                pass

            # Update running P&L — match by OPENING deal comment, not closing
            def _update_pnl():
                from datetime import timedelta as _td
                df = datetime.now() - _td(days=30)
                deals = mt5.history_deals_get(df, datetime.now())
                if not deals:
                    return 0.0
                # Collect position_ids opened by this strategy
                our_pos = set()
                for d in deals:
                    if (d.entry == 0 and d.symbol == symbol and
                            (d.comment or "").startswith(f"FarhanFX-{strategy}")):
                        our_pos.add(d.position_id)
                # Sum closing-deal P&L for our positions
                total = 0.0
                for d in deals:
                    if d.entry == 1 and d.position_id in our_pos:
                        total += d.profit + d.commission + d.swap
                return round(total, 2)
            try:
                _strategies[sid]["pnl"] = _mt5_call(_update_pnl)
            except Exception:
                pass

        except Exception as e:
            add_log(f"⚠️ Error: {e}")
            _tg_notify(
                f"<b>FarhanFX — Strategy Error</b>\n"
                f"📊 <b>{strategy}</b> | {symbol}\n"
                f"⚠️ <code>{str(e)[:300]}</code>"
            )

        _time.sleep(30)

    add_log(f"Strategy '{strategy}' stopped")


class StrategyRequest(BaseModel):
    strategy:   str   # "MA Cross" | "RSI" | "Bollinger Bands" | "EMA Trend" | "Scalper"
    symbol:     str
    timeframe:  str
    volume:     float
    sl:         float = 20.0
    tp:         float = 40.0
    max_trades: int   = 2     # max concurrent open positions per strategy

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
        "pnl":       0.0,
        "indicator": "",
        "log":       log,
        "started":   datetime.now().strftime("%H:%M:%S"),
    }
    t = threading.Thread(target=_strategy_runner, args=(sid, cfg, stop_ev, log), daemon=True)
    _strategies[sid]["_stop"] = stop_ev
    t.start()
    _save_strategies_state()   # persist to disk
    return {"success": True, "id": sid}

@app.post("/api/strategy/stop/{sid}")
def stop_strategy(sid: str):
    if sid not in _strategies:
        return JSONResponse({"error": "Strategy not found"}, status_code=404)
    _strategies[sid]["_stop"].set()
    _strategies[sid]["status"] = "stopped"
    _save_strategies_state()   # update disk — remove stopped strategy
    return {"success": True}

def _stop_all_strategies_internal() -> int:
    stopped = 0
    for sid, s in _strategies.items():
        if s.get("status") == "running":
            s["_stop"].set()
            s["status"] = "stopped"
            stopped += 1
    _save_strategies_state()
    return stopped

@app.post("/api/strategy/stop_all")
def stop_all_strategies():
    """Stop every running strategy at once."""
    stopped = _stop_all_strategies_internal()
    return {"success": True, "stopped": stopped}

@app.get("/api/strategy/list")
def list_strategies():
    result = []
    for sid, s in _strategies.items():
        cfg        = s["config"]
        magic      = 234000 + hash(sid) % 1000
        max_trades = int(cfg.get("max_trades", 2))
        symbol     = cfg["symbol"]

        # Fetch live position data from MT5
        def _live_data(sym=symbol, mg=magic):
            positions = mt5.positions_get(symbol=sym)
            if not positions:
                return [], 0, 0.0
            mine = [p for p in positions if p.magic == mg]
            live_pnl = round(sum(p.profit for p in mine), 2)
            open_detail = [{
                "ticket": p.ticket,
                "type":   "BUY" if p.type == 0 else "SELL",
                "volume": p.volume,
                "open":   round(p.price_open, 5),
                "current": round(p.price_current, 5),
                "profit": round(p.profit, 2),
                "sl":     round(p.sl, 5),
                "tp":     round(p.tp, 5),
            } for p in mine]
            return open_detail, len(mine), live_pnl

        try:
            open_positions, open_count, live_pnl = _mt5_call(_live_data)
            if isinstance(open_positions, dict):  # mt5 error
                open_positions, open_count, live_pnl = [], 0, 0.0
        except Exception:
            open_positions, open_count, live_pnl = [], 0, 0.0

        result.append({
            "id":             sid,
            "strategy":       cfg["strategy"],
            "symbol":         symbol,
            "timeframe":      cfg["timeframe"],
            "volume":         cfg["volume"],
            "sl":             cfg.get("sl", 0),
            "tp":             cfg.get("tp", 0),
            "max_trades":     max_trades,
            "status":         s["status"],
            "trades":         s["trades"],
            "pnl":            round(s.get("pnl", 0.0), 2),
            "live_pnl":       live_pnl,
            "open_count":     open_count,
            "open_positions": open_positions,
            "indicator":      s.get("indicator", ""),
            "started":        s["started"],
            "log":            s["log"][-20:],
        })
    return result


@app.get("/api/algo/live_positions")
def get_algo_live_positions():
    """Return all currently open MT5 positions placed by algo strategies (FarhanFX comment)."""
    def fn():
        positions = mt5.positions_get()
        if not positions:
            return []
        result = []
        for p in positions:
            c = (p.comment or "")
            if not c.startswith("FarhanFX-") or "Close" in c:
                continue
            strategy = c[len("FarhanFX-"):]
            result.append({
                "ticket":   p.ticket,
                "symbol":   p.symbol,
                "type":     "BUY" if p.type == 0 else "SELL",
                "volume":   p.volume,
                "open":     round(p.price_open, 5),
                "current":  round(p.price_current, 5),
                "profit":   round(p.profit, 2),
                "sl":       round(p.sl, 5),
                "tp":       round(p.tp, 5),
                "strategy": strategy,
                "swap":     round(p.swap, 2),
            })
        return result
    return _mt5_call(fn)


_REPORT_CUTOFF_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_cutoff.json")

def _get_report_cutoff() -> datetime:
    """Trades before this timestamp are excluded from performance reports.
    Lets us start the report clean after a strategy logic change, instead of
    mixing stats from old/tuning-era trades with the finalized strategy."""
    try:
        with open(_REPORT_CUTOFF_FILE) as f:
            return datetime.fromisoformat(json.load(f)["cutoff"])
    except Exception:
        return datetime(2010, 1, 1)

def _set_report_cutoff(dt: datetime = None):
    dt = dt or datetime.now()
    with open(_REPORT_CUTOFF_FILE, "w") as f:
        json.dump({"cutoff": dt.isoformat()}, f)
    return dt

@app.post("/api/algo/report_cutoff/reset")
def reset_report_cutoff():
    """Reset the performance report to start counting from this moment —
    use after a strategy logic change so old tuning-era trades don't pollute stats."""
    dt = _set_report_cutoff()
    return {"success": True, "cutoff": dt.isoformat()}

@app.get("/api/algo/history")
def get_algo_history(days: int = 30):
    def fn():
        date_from = datetime(2010, 1, 1) if days == 0 else datetime.now() - timedelta(days=days)
        # NOTE: keep date_from broad for the deals query itself (so open_map
        # still resolves entry deals for positions opened before the cutoff
        # but closed after it) — the cutoff is applied later, only to the
        # CLOSE time of closed_result, so report stats start clean.
        _report_cutoff = _get_report_cutoff()

        _CRYPTO = ("BTC","ETH","LTC","XRP","BNB","SOL","ADA","DOGE","XMR","DOT","AVAX","MATIC")

        # Fix broker comment truncation (CXM Direct caps comments at 16 chars).
        # Use magic number → full strategy name as primary; prefix match as fallback.
        magic_to_name = {234000 + hash(sid) % 1000: s["config"]["strategy"]
                         for sid, s in _strategies.items()}
        # Permanently excluded strategies — trades hidden from all reports
        # (kept set: Trend Continuation, M2B/M2S, Pin Bar SR, Engulfing Trend,
        #  PA Confluence, AI Signal Engine — everything else backtested <50% WR)
        _EXCLUDED_STRATS = {
            "SMC Liquidity", "SMC Liq", "SMC",
            "Supertrend", "Triple Filter", "Ichimoku", "Scalper",
            "AI Confluence", "AI Agent", "Order Block",
            "Inside Bar", "False Breakout", "3-Bar Reversal",
            "EMA Trend", "EMA Cross", "MA Cross",
            "Bollinger Bands", "MACD", "RSI Divergence", "Stochastic", "ADX Filter",
        }
        # Include currently running + all known historical strategy names for prefix matching
        _KNOWN_NAMES = [
            "Supertrend", "Scalper", "Order Block", "Pin Bar SR", "Engulfing Trend",
            "Inside Bar", "False Breakout", "PA Confluence", "AI Confluence", "AI Agent",
            "Triple Filter", "EMA Trend", "EMA Cross", "Ichimoku",
            "Bollinger Bands", "MACD", "RSI Divergence", "Stochastic", "ADX Filter",
            "M2B/M2S", "Trend Continuation", "Trend Continuation M15", "AI Signal Engine",
        ]
        _all_strat_names = list({s["config"]["strategy"] for s in _strategies.values()} | set(_KNOWN_NAMES))

        def _resolve_name(truncated: str, magic: int) -> str:
            if magic in magic_to_name:
                return magic_to_name[magic]
            t = truncated.strip()
            for name in _all_strat_names:
                if name.startswith(t):
                    return name
            return t

        deals  = mt5.history_deals_get(date_from, datetime.now())
        if deals is None:
            return []

        orders    = mt5.history_orders_get(date_from, datetime.now()) or []
        order_map = {o.ticket: o for o in orders}

        def _exit_reason(d):
            if d.reason == 5: return "TP HIT"
            if d.reason == 6: return "STOP OUT"
            if d.reason == 4:
                open_deal = open_map.get(d.position_id)
                if open_deal:
                    is_buy   = (open_deal.type == 0)
                    is_trail = (d.price >= open_deal.price) if is_buy else (d.price <= open_deal.price)
                    return "TRAILING SL" if is_trail else "SL HIT"
                net = d.profit + d.commission + d.swap
                return "TRAILING SL" if net > 0 else "SL HIT"
            return "MANUAL"

        open_map: dict = {}
        closed: list   = []
        for d in deals:
            if d.type not in (0, 1):
                continue
            _is_crypto = any(x in d.symbol.upper() for x in _CRYPTO)
            if _is_crypto:
                continue
            c = (d.comment or "")
            if d.entry == 0:
                is_algo = (c.startswith("FarhanFX-") and "Close" not in c) or d.magic in magic_to_name
                if is_algo:
                    open_map[d.position_id] = d
            elif d.entry == 1:
                closed.append(d)

        closed_result = []
        for d in closed:
            if datetime.fromtimestamp(d.time) < _report_cutoff:
                continue
            open_d = open_map.get(d.position_id)
            if not open_d:
                continue
            sl_val = tp_val = None
            op_order = order_map.get(open_d.order)
            if op_order:
                sl_val = round(op_order.sl, 5) if op_order.sl else None
                tp_val = round(op_order.tp, 5) if op_order.tp else None
            open_comment = (open_d.comment or "")
            raw_name  = open_comment[len("FarhanFX-"):] if open_comment.startswith("FarhanFX-") else open_comment
            strat_name = _resolve_name(raw_name, open_d.magic)
            closed_result.append({
                "ticket":      d.position_id,
                "symbol":      d.symbol,
                "strategy":    strat_name,
                "type":        "BUY" if (open_d.type if open_d else d.type) == 0 else "SELL",
                "volume":      d.volume,
                "entry_price": round(open_d.price, 5) if open_d else None,
                "exit_price":  round(d.price, 5),
                "sl":          sl_val,
                "tp":          tp_val,
                "profit":      round(d.profit + d.commission + d.swap, 2),
                "comment":     open_comment,
                "exit_reason": _exit_reason(d),
                "time":        datetime.fromtimestamp(d.time).strftime("%Y-%m-%d %H:%M"),
            })
        closed_result = [r for r in closed_result if r["strategy"] not in _EXCLUDED_STRATS]
        closed_result.sort(key=lambda x: x["time"], reverse=True)

        # Include currently OPEN positions so they appear in performance & history table
        open_rows = []
        for p in (mt5.positions_get() or []):
            if any(x in p.symbol.upper() for x in _CRYPTO):
                continue
            c = (p.comment or "")
            is_algo = (c.startswith("FarhanFX-") and "Close" not in c) or p.magic in magic_to_name
            if not is_algo:
                continue
            raw_name  = c[len("FarhanFX-"):] if c.startswith("FarhanFX-") else ""
            strat_name = _resolve_name(raw_name, p.magic)
            open_rows.append({
                "ticket":      p.ticket,
                "symbol":      p.symbol,
                "strategy":    strat_name,
                "type":        "BUY" if p.type == 0 else "SELL",
                "volume":      p.volume,
                "entry_price": round(p.price_open, 5),
                "exit_price":  None,
                "sl":          round(p.sl, 5) if p.sl else None,
                "tp":          round(p.tp, 5) if p.tp else None,
                "profit":      round(p.profit + p.swap, 2),
                "comment":     c,
                "exit_reason": None,
                "time":        datetime.fromtimestamp(p.time).strftime("%Y-%m-%d %H:%M"),
            })

        open_rows = [r for r in open_rows if r["strategy"] not in _EXCLUDED_STRATS]
        # Open positions first (live), then closed sorted newest-first
        return open_rows + closed_result[:200]
    return _mt5_call(fn)

def _do_ai_analyze(symbol: str, tf: str = "H1") -> dict:
    """Full multi-timeframe AI analysis. Returns structured signal dict.
    Called by both the API endpoint and the AI Signal Engine strategy runner."""
    primary_rates, sym = _get_rates(symbol, tf, 300)
    h4_rates, _        = _get_rates(symbol, "H4", 200)
    d1_rates, _        = _get_rates(symbol, "D1", 100)
    w1_rates, _        = _get_rates(symbol, "W1",  60)

    if primary_rates is None or len(primary_rates) < 50:
        return {"error": f"Not enough data for {symbol}"}

    def get_tick_info():
        return mt5.symbol_info_tick(sym), mt5.symbol_info(sym)
    ti = _mt5_call(get_tick_info)
    tick, sym_inf = (ti if not isinstance(ti, dict) else (None, None))

    opens  = [float(r["open"])  for r in primary_rates]
    closes = [float(r["close"]) for r in primary_rates]
    highs  = [float(r["high"])  for r in primary_rates]
    lows   = [float(r["low"])   for r in primary_rates]
    price  = closes[-1]
    digits = sym_inf.digits if sym_inf else 5

    ema20  = _ema(closes, 20)
    ema50  = _ema(closes, 50)
    ema200 = _ema(closes, min(200, len(closes)-1))
    rsi14  = _rsi(closes, 14)
    macd_l, sig_l = _macd(closes)
    hist   = [m - s for m, s in zip(macd_l, sig_l)]
    st_dir = _supertrend(highs, lows, closes, period=10, mult=3.0)
    atr_v  = _atr(highs, lows, closes, 14)
    kv, dv = _stochastic(highs, lows, closes)

    # ── W1 Weekly trend bias (top-level filter)
    w1_bias = "NEUTRAL"
    if w1_rates is not None and len(w1_rates) >= 20:
        w1c = [float(r["close"]) for r in w1_rates]
        w1e20  = _ema(w1c, min(20, len(w1c)-1))
        w1e50  = _ema(w1c, min(50, len(w1c)-1))
        if w1c[-1] > w1e20[-1] > w1e50[-1]:   w1_bias = "BULLISH"
        elif w1c[-1] < w1e20[-1] < w1e50[-1]: w1_bias = "BEARISH"

    # ── H4 trend bias
    h4_bias = "NEUTRAL"
    if h4_rates is not None and len(h4_rates) >= 50:
        h4c = [float(r["close"]) for r in h4_rates]
        h4e50  = _ema(h4c, 50)
        h4e200 = _ema(h4c, min(200, len(h4c)-1))
        if h4c[-1]>h4e50[-1]>h4e200[-1]:   h4_bias="BULLISH"
        elif h4c[-1]<h4e50[-1]<h4e200[-1]: h4_bias="BEARISH"

    # ── D1 trend bias
    d1_bias = "NEUTRAL"
    if d1_rates is not None and len(d1_rates) >= 30:
        d1c = [float(r["close"]) for r in d1_rates]
        d1e50  = _ema(d1c, min(50, len(d1c)-1))
        d1e200 = _ema(d1c, min(200, len(d1c)-1))
        if d1c[-1]>d1e50[-1] and d1e50[-1]>d1e200[-1]:   d1_bias="BULLISH"
        elif d1c[-1]<d1e50[-1] and d1e50[-1]<d1e200[-1]: d1_bias="BEARISH"

    # ── SMC / ICT / PA helpers
    pa_patterns               = _detect_price_action(opens, closes, highs, lows)
    bos_dir, bos_detail       = _detect_bos(closes, highs, lows)
    sr_dir,  sr_detail        = _check_sr_level(closes, highs, lows, price, atr_v)
    fvg_dir, fvg_detail       = _check_fvg_zone(highs, lows, price)
    ob_dir,  ob_detail        = _check_ob_zone(opens, closes, highs, lows, price, atr_v)
    liq_dir, liq_detail       = _check_liq_sweep(highs, lows, closes, atr_v)
    fib_dir, fib_detail       = _check_fibonacci_level(highs, lows, price, atr_v)
    ema21_dir, ema21_detail   = _check_21ema_bounce(closes, highs, lows, price, atr_v)
    dt_dir, dt_detail         = _detect_double_top_bottom(highs, lows, closes, atr_v)
    tbr_dir, tbr_detail       = _detect_three_bar_reversal(opens, closes, highs, lows)
    m2_dir, m2_detail         = _detect_m2b_m2s(closes, highs, lows, atr_v)
    pv_dir, pv_detail         = _check_pivot_points(highs, lows, closes, price, atr_v)
    has_nr7                   = _check_nr7(highs, lows)
    kz_name, kz_level         = _get_ict_killzone()
    news_events               = _get_upcoming_news(symbol, minutes_ahead=120, minutes_past=30)

    # ── SIGNAL SCORING ──────────────────────────────────────────────────────────
    components = []

    # 1. W1 Weekly Trend (35 pts) — master filter; counter-trend = hard penalty
    if w1_bias == "BULLISH":
        components.append({"name":"W1 Trend","dir":"BUY","score":35,"max":35,"detail":"Weekly: bullish (price > EMA20 > EMA50)"})
    elif w1_bias == "BEARISH":
        components.append({"name":"W1 Trend","dir":"SELL","score":35,"max":35,"detail":"Weekly: bearish (price < EMA20 < EMA50)"})
    else:
        components.append({"name":"W1 Trend","dir":"NEUTRAL","score":0,"max":35,"detail":"Weekly: no clear trend"})

    # 2. D1 Trend (30 pts)
    if d1_bias=="BULLISH":
        components.append({"name":"D1 Trend","dir":"BUY","score":30,"max":30,"detail":"Daily: price > EMA50 > EMA200"})
    elif d1_bias=="BEARISH":
        components.append({"name":"D1 Trend","dir":"SELL","score":30,"max":30,"detail":"Daily: price < EMA50 < EMA200"})
    else:
        components.append({"name":"D1 Trend","dir":"NEUTRAL","score":0,"max":30,"detail":"Daily: no clear alignment"})

    # 3. H4 Trend (25 pts)
    if h4_bias=="BULLISH":
        components.append({"name":"H4 Trend","dir":"BUY","score":25,"max":25,"detail":"H4 price > EMA50 > EMA200"})
    elif h4_bias=="BEARISH":
        components.append({"name":"H4 Trend","dir":"SELL","score":25,"max":25,"detail":"H4 price < EMA50 < EMA200"})
    else:
        components.append({"name":"H4 Trend","dir":"NEUTRAL","score":0,"max":25,"detail":"No clear H4 alignment"})

    # 4. Price Action patterns (25 pts) — best pattern from full detection set
    # Augment PA list with NR7 if detected (breakout bias follows trend)
    nr7_extra = []
    if has_nr7:
        trend_for_nr7 = "BUY" if (d1_bias=="BULLISH" or h4_bias=="BULLISH") else ("SELL" if (d1_bias=="BEARISH" or h4_bias=="BEARISH") else "NEUTRAL")
        if trend_for_nr7 != "NEUTRAL":
            nr7_extra = [("NR7 Breakout Setup", trend_for_nr7, 0.82)]
    all_pa = (pa_patterns or []) + nr7_extra
    if all_pa:
        best = max(all_pa, key=lambda x: x[2])
        pa_sc = round(25 * best[2])
        components.append({"name":"Price Action","dir":best[1],"score":pa_sc,"max":25,"detail":best[0]})
    else:
        components.append({"name":"Price Action","dir":"NEUTRAL","score":0,"max":25,"detail":"No clear PA pattern"})

    # 5. Liquidity Sweep (20 pts)
    if liq_dir in ("BUY","SELL"):
        components.append({"name":"Liq Sweep","dir":liq_dir,"score":20,"max":20,"detail":liq_detail})
    else:
        components.append({"name":"Liq Sweep","dir":"NEUTRAL","score":0,"max":20,"detail":liq_detail})

    # 6. BOS / CHoCH (20 pts)
    if bos_dir in ("BUY","SELL"):
        components.append({"name":"BOS/CHoCH","dir":bos_dir,"score":20,"max":20,"detail":bos_detail})
    else:
        components.append({"name":"BOS/CHoCH","dir":"NEUTRAL","score":0,"max":20,"detail":bos_detail})

    # 7. Order Block (20 pts)
    if ob_dir in ("BUY","SELL"):
        components.append({"name":"Order Block","dir":ob_dir,"score":20,"max":20,"detail":ob_detail})
    else:
        components.append({"name":"Order Block","dir":"NEUTRAL","score":0,"max":20,"detail":ob_detail})

    # 8. Three-Bar Reversal (18 pts) — trapped trader reversal (PA book)
    if tbr_dir in ("BUY","SELL"):
        components.append({"name":"3-Bar Reversal","dir":tbr_dir,"score":18,"max":18,"detail":tbr_detail})
    else:
        components.append({"name":"3-Bar Reversal","dir":"NEUTRAL","score":0,"max":18,"detail":tbr_detail})

    # 9. M2B/M2S Setup (18 pts) — two-legged pullback to 20 EMA (PA book: highest WR setup)
    if m2_dir in ("BUY","SELL"):
        components.append({"name":"M2B/M2S","dir":m2_dir,"score":18,"max":18,"detail":m2_detail})
    else:
        components.append({"name":"M2B/M2S","dir":"NEUTRAL","score":0,"max":18,"detail":m2_detail})

    # 10. Fair Value Gap (15 pts)
    if fvg_dir in ("BUY","SELL"):
        components.append({"name":"Fair Value Gap","dir":fvg_dir,"score":15,"max":15,"detail":fvg_detail})
    else:
        components.append({"name":"Fair Value Gap","dir":"NEUTRAL","score":0,"max":15,"detail":fvg_detail})

    # 11. EMA Stack (15 pts)
    if price>ema20[-1]>ema50[-1]:
        sc2 = 15 if price>ema200[-1] else 9
        components.append({"name":"EMA Stack","dir":"BUY","score":sc2,"max":15,"detail":f"Price>{ema20[-1]:.{digits}f}>{ema50[-1]:.{digits}f}"})
    elif price<ema20[-1]<ema50[-1]:
        sc2 = 15 if price<ema200[-1] else 9
        components.append({"name":"EMA Stack","dir":"SELL","score":sc2,"max":15,"detail":f"Price<{ema20[-1]:.{digits}f}<{ema50[-1]:.{digits}f}"})
    else:
        components.append({"name":"EMA Stack","dir":"NEUTRAL","score":0,"max":15,"detail":"EMAs not aligned"})

    # 12. Supertrend (15 pts)
    st_txt = "BULLISH ▲" if st_dir[-1]==1 else "BEARISH ▼"
    components.append({"name":"Supertrend","dir":"BUY" if st_dir[-1]==1 else "SELL","score":15,"max":15,"detail":f"Direction: {st_txt}"})

    # 13. Fibonacci Level (15 pts)
    if fib_dir in ("BUY","SELL"):
        components.append({"name":"Fibonacci","dir":fib_dir,"score":15,"max":15,"detail":fib_detail})
    else:
        components.append({"name":"Fibonacci","dir":"NEUTRAL","score":0,"max":15,"detail":fib_detail})

    # 14. News Bias (15 pts)
    news_bias_dir = "NEUTRAL"
    news_bias_detail = "No recent high-impact news"
    for ev in news_events:
        if ev["impact"]=="High" and ev["direction"] in ("BULLISH","BEARISH"):
            news_bias_dir = "BUY" if ev["direction"]=="BULLISH" else "SELL"
            news_bias_detail = f"{ev['title']} ({ev['country']}) — {ev['direction']}"
            break
    if news_bias_dir in ("BUY","SELL"):
        components.append({"name":"News Bias","dir":news_bias_dir,"score":15,"max":15,"detail":news_bias_detail})
    else:
        components.append({"name":"News Bias","dir":"NEUTRAL","score":0,"max":15,"detail":news_bias_detail})

    # 15. MACD (12 pts)
    if hist[-1]>0 and hist[-1]>=hist[-2]:
        components.append({"name":"MACD","dir":"BUY","score":12,"max":12,"detail":f"Histogram rising: {hist[-1]:+.5f}"})
    elif hist[-1]<0 and hist[-1]<=hist[-2]:
        components.append({"name":"MACD","dir":"SELL","score":12,"max":12,"detail":f"Histogram falling: {hist[-1]:+.5f}"})
    else:
        components.append({"name":"MACD","dir":"NEUTRAL","score":0,"max":12,"detail":f"Histogram flat: {hist[-1]:+.5f}"})

    # 16. Daily Pivot Points (12 pts)
    if pv_dir in ("BUY","SELL"):
        components.append({"name":"Daily Pivot","dir":pv_dir,"score":12,"max":12,"detail":pv_detail})
    else:
        components.append({"name":"Daily Pivot","dir":"NEUTRAL","score":0,"max":12,"detail":pv_detail})

    # 17. 21 EMA Bounce (12 pts)
    if ema21_dir in ("BUY","SELL"):
        components.append({"name":"21 EMA Bounce","dir":ema21_dir,"score":12,"max":12,"detail":ema21_detail})
    else:
        components.append({"name":"21 EMA Bounce","dir":"NEUTRAL","score":0,"max":12,"detail":ema21_detail})

    # 18. Double Top/Bottom (12 pts)
    if dt_dir in ("BUY","SELL"):
        components.append({"name":"Double T/B","dir":dt_dir,"score":12,"max":12,"detail":dt_detail})
    else:
        components.append({"name":"Double T/B","dir":"NEUTRAL","score":0,"max":12,"detail":dt_detail})

    # 19. RSI Zone (10 pts)
    if 50<=rsi14<=68:
        components.append({"name":"RSI","dir":"BUY","score":10,"max":10,"detail":f"RSI {rsi14} — bullish zone"})
    elif 32<=rsi14<=50:
        components.append({"name":"RSI","dir":"SELL","score":10,"max":10,"detail":f"RSI {rsi14} — bearish zone"})
    else:
        components.append({"name":"RSI","dir":"NEUTRAL","score":0,"max":10,"detail":f"RSI {rsi14} — extreme zone"})

    # 20. Support / Resistance (10 pts)
    if sr_dir in ("BUY","SELL"):
        components.append({"name":"S/R Level","dir":sr_dir,"score":10,"max":10,"detail":sr_detail})
    else:
        components.append({"name":"S/R Level","dir":"NEUTRAL","score":0,"max":10,"detail":sr_detail})

    # 21. ICT Kill Zone (10 pts bonus)
    if kz_level=="HIGH":
        components.append({"name":"ICT Kill Zone","dir":"BUY","score":10,"max":10,"detail":f"Active: {kz_name} (HIGH probability)"})
    elif kz_level=="MEDIUM":
        components.append({"name":"ICT Kill Zone","dir":"BUY","score":5,"max":10,"detail":f"Active: {kz_name} (MEDIUM probability)"})
    else:
        components.append({"name":"ICT Kill Zone","dir":"NEUTRAL","score":0,"max":10,"detail":f"{kz_name} — off-hours"})

    # 22. Stochastic (8 pts)
    if kv[-2]<dv[-2] and kv[-1]>dv[-1] and kv[-1]<60:
        components.append({"name":"Stochastic","dir":"BUY","score":8,"max":8,"detail":f"%K {kv[-1]:.0f} crossed above %D {dv[-1]:.0f}"})
    elif kv[-2]>dv[-2] and kv[-1]<dv[-1] and kv[-1]>40:
        components.append({"name":"Stochastic","dir":"SELL","score":8,"max":8,"detail":f"%K {kv[-1]:.0f} crossed below %D {dv[-1]:.0f}"})
    else:
        components.append({"name":"Stochastic","dir":"NEUTRAL","score":0,"max":8,"detail":f"%K:{kv[-1]:.0f}  %D:{dv[-1]:.0f}"})

    # ── PRELIMINARY SCORE ────────────────────────────────────────────────────────
    buy_score  = sum(c["score"] for c in components if c["dir"]=="BUY")
    sell_score = sum(c["score"] for c in components if c["dir"]=="SELL")
    max_total  = sum(c["max"]   for c in components)

    buy_pct  = round(buy_score  / max_total * 100)
    sell_pct = round(sell_score / max_total * 100)

    news_block = any(ev["impact"]=="High" and 0<=ev["mins_from_now"]<=30 for ev in news_events)

    if buy_pct >= sell_pct:
        final_signal = "BUY";  confidence = buy_pct
    else:
        final_signal = "SELL"; confidence = sell_pct

    # ── COUNTER-TREND HARD BLOCK — evaluate on raw direction BEFORE gate ─────────
    # If W1+D1 both oppose the leading signal → never trade, regardless of score
    raw_dir = "BUY" if buy_pct >= sell_pct else "SELL"
    counter_trend_block = (
        (raw_dir == "BUY"  and w1_bias == "BEARISH" and d1_bias == "BEARISH") or
        (raw_dir == "SELL" and w1_bias == "BULLISH" and d1_bias == "BULLISH")
    )
    if counter_trend_block:
        confidence    = min(max(buy_pct, sell_pct), 28)
        final_signal  = "HOLD"

    if news_block and not counter_trend_block:
        final_signal = "WAIT"; confidence = max(buy_pct, sell_pct)

    # ── 3-FACTOR GATE (Candlestick Bible core framework) ─────────────────────────
    # Use raw_dir as reference direction so the gate makes sense even after "WAIT"
    _gate_dir = raw_dir
    # FACTOR 1: TREND — at least one HTF aligned with signal direction
    _trend_names = {"W1 Trend","D1 Trend","H4 Trend"}
    trend_ok  = any(c["dir"]==_gate_dir and c["score"]>0 for c in components if c["name"] in _trend_names)

    # FACTOR 2: LEVEL — price at a key level (S/R, EMA, Fib, OB, FVG, Pivot)
    _level_names = {"S/R Level","21 EMA Bounce","Fibonacci","Order Block","Fair Value Gap","Daily Pivot","Liq Sweep"}
    level_ok  = any(c["dir"]==_gate_dir and c["score"]>0 for c in components if c["name"] in _level_names)

    # FACTOR 3: SIGNAL — candlestick/pattern confirmation
    _signal_names = {"Price Action","3-Bar Reversal","M2B/M2S","Double T/B","BOS/CHoCH"}
    signal_ok = any(c["dir"]==_gate_dir and c["score"]>0 for c in components if c["name"] in _signal_names)

    three_factor_pass = trend_ok and level_ok and signal_ok
    three_factor_detail = (
        f"Trend:{'✅' if trend_ok else '❌'}  Level:{'✅' if level_ok else '❌'}  Signal:{'✅' if signal_ok else '❌'}"
    )

    # If 3-factor gate fails (and not already blocked) → cap confidence and suppress trade
    if not counter_trend_block and not three_factor_pass and final_signal not in ("WAIT",):
        confidence   = min(confidence, 48)
        final_signal = "HOLD"

    # ── FINAL TRADE SIGNAL (only BUY/SELL if confidence >= 60 and all gates pass) ─
    if final_signal not in ("WAIT","HOLD") and confidence < 60:
        final_signal = "HOLD"

    return {
        "symbol":              sym,
        "tf":                  tf,
        "price":               round(price, digits),
        "bid":                 round(tick.bid, digits) if tick else round(price, digits),
        "ask":                 round(tick.ask, digits) if tick else round(price, digits),
        "signal":              final_signal,
        "confidence":          confidence,
        "buy_score":           buy_pct,
        "sell_score":          sell_pct,
        "w1_bias":             w1_bias,
        "h4_bias":             h4_bias,
        "d1_bias":             d1_bias,
        "rsi":                 rsi14,
        "atr":                 round(atr_v, digits),
        "supertrend":          "BULLISH" if st_dir[-1]==1 else "BEARISH",
        "killzone":            kz_name,
        "kz_level":            kz_level,
        "news_block":          news_block,
        "news":                news_events[:8],
        "bos":                 bos_dir,
        "three_factor_pass":   three_factor_pass,
        "three_factor_detail": three_factor_detail,
        "counter_trend_block": counter_trend_block,
        "nr7":                 has_nr7,
        "components":          components,
    }


@app.get("/api/ai/analyze/{symbol}")
def ai_analyze(symbol: str, tf: str = "H1"):
    """Enhanced multi-timeframe AI analysis: W1+D1+H4+H1, 3-Factor Gate, Price Action + SMC + ICT + News."""
    # _do_ai_analyze calls _get_rates/_mt5_call internally — do NOT wrap in outer _mt5_call
    # to avoid nested thread timeouts killing the long multi-TF fetch.
    try:
        return _do_ai_analyze(symbol, tf)
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/news/calendar")
def get_news_calendar(symbol: str = "XAUUSDc"):
    """Return upcoming/recent high-impact news events for symbol's currencies."""
    events = _get_upcoming_news(symbol, minutes_ahead=480, minutes_past=60)
    return {"symbol": symbol, "currencies": _symbol_currencies(symbol), "events": events}


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
    commission:   float = 7.0    # round-trip commission per lot in $
    spread_pips:  float = 1.5    # average spread in pips
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

            elif strat == "Pin Bar SR":
                h = highs[:i+1]; l = lows[:i+1]; o = opens[:i+1]
                atr_bt = _atr(h, l, c, 14)
                pr = c[-1]
                e50 = _ema(c, min(50, len(c)-1)); e200 = _ema(c, min(200, len(c)-1))
                tb = pr > e50[-1] > e200[-1]; tb2 = pr < e50[-1] < e200[-1]
                sr_d, _  = _check_sr_level(c, h, l, pr, atr_bt)
                fib_d, _ = _check_fibonacci_level(h, l, pr, atr_bt)
                em_d, _  = _check_21ema_bounce(c, h, l, pr, atr_bt)
                lbull = (sr_d=="BUY"  or fib_d=="BUY"  or em_d=="BUY")
                lbear = (sr_d=="SELL" or fib_d=="SELL" or em_d=="SELL")
                pa_bt = _detect_price_action(o, c, h, l)
                pb = any(p[0] in ("Bullish Pin Bar","Hammer","Tweezer Bottom") and p[1]=="BUY"  for p in pa_bt)
                ps = any(p[0] in ("Bearish Pin Bar","Shooting Star","Tweezer Top") and p[1]=="SELL" for p in pa_bt)
                if tb and lbull and pb: sig = "BUY"
                elif tb2 and lbear and ps: sig = "SELL"

            elif strat == "Engulfing Trend":
                h = highs[:i+1]; l = lows[:i+1]; o = opens[:i+1]
                atr_bt = _atr(h, l, c, 14)
                pr = c[-1]
                e50 = _ema(c, min(50, len(c)-1)); e200 = _ema(c, min(200, len(c)-1))
                tb = pr > e50[-1] > e200[-1]; tb2 = pr < e50[-1] < e200[-1]
                sr_d, _  = _check_sr_level(c, h, l, pr, atr_bt)
                em_d, _  = _check_21ema_bounce(c, h, l, pr, atr_bt)
                fib_d, _ = _check_fibonacci_level(h, l, pr, atr_bt)
                lbull = (sr_d=="BUY"  or em_d=="BUY"  or fib_d=="BUY")
                lbear = (sr_d=="SELL" or em_d=="SELL" or fib_d=="SELL")
                pa_bt = _detect_price_action(o, c, h, l)
                eb = any(p[0] in ("Bullish Engulfing","Morning Star","Piercing Pattern") and p[1]=="BUY"  for p in pa_bt)
                es = any(p[0] in ("Bearish Engulfing","Evening Star","Dark Cloud Cover") and p[1]=="SELL" for p in pa_bt)
                if tb and lbull and eb: sig = "BUY"
                elif tb2 and lbear and es: sig = "SELL"

            elif strat == "Inside Bar Breakout":
                h = highs[:i+1]; l = lows[:i+1]; o = opens[:i+1]
                e50 = _ema(c, min(50, len(c)-1)); e200 = _ema(c, min(200, len(c)-1))
                st_bt = _supertrend(h, l, c)
                tb  = c[-1] > e50[-1] > e200[-1] and st_bt[-1] == 1
                tb2 = c[-1] < e50[-1] < e200[-1] and st_bt[-1] == -1
                pa_bt = _detect_price_action(o, c, h, l)
                ib_ok = any(p[0] == "Inside Bar" for p in pa_bt)
                mh = h[-3] if len(h) >= 3 else h[-2]
                ml = l[-3] if len(l) >= 3 else l[-2]
                if ib_ok and tb  and c[-1] > mh: sig = "BUY"
                elif ib_ok and tb2 and c[-1] < ml: sig = "SELL"

            elif strat == "IB False Breakout":
                h = highs[:i+1]; l = lows[:i+1]; o = opens[:i+1]
                atr_bt = _atr(h, l, c, 14)
                pr = c[-1]
                pa_bt = _detect_price_action(o, c, h, l)
                fbo_b = any(p[0] == "IB False Breakout" and p[1] == "BUY"  for p in pa_bt)
                fbo_s = any(p[0] == "IB False Breakout" and p[1] == "SELL" for p in pa_bt)
                if fbo_b: sig = "BUY"
                elif fbo_s: sig = "SELL"

            elif strat == "PA Confluence":
                h = highs[:i+1]; l = lows[:i+1]; o = opens[:i+1]
                atr_bt = _atr(h, l, c, 14)
                pr = c[-1]
                e50 = _ema(c, min(50, len(c)-1)); e200 = _ema(c, min(200, len(c)-1))
                st_bt = _supertrend(h, l, c)
                tb  = pr > e50[-1] and st_bt[-1] == 1
                tb2 = pr < e50[-1] and st_bt[-1] == -1
                sr_d, _  = _check_sr_level(c, h, l, pr, atr_bt)
                fib_d, _ = _check_fibonacci_level(h, l, pr, atr_bt)
                em_d, _  = _check_21ema_bounce(c, h, l, pr, atr_bt)
                lbull = (sr_d=="BUY"  or fib_d=="BUY"  or em_d=="BUY")
                lbear = (sr_d=="SELL" or fib_d=="SELL" or em_d=="SELL")
                pa_bt = _detect_price_action(o, c, h, l)
                pbs = max((p[2] for p in pa_bt if p[1]=="BUY"),  default=0)
                pss = max((p[2] for p in pa_bt if p[1]=="SELL"), default=0)
                pb2 = pbs >= 0.70; ps2 = pss >= 0.70
                if   sum([tb,  lbull, pb2]) == 3: sig = "BUY"
                elif sum([tb2, lbear, ps2]) == 3: sig = "SELL"

            signals[i] = sig

    # ── Simulate trades ────────────────────────────────────────────────────────
    capital      = req.capital
    equity       = capital
    sl_p         = req.sl_pips * pip
    tp_p         = req.tp_pips * pip
    risk_amt     = capital * req.risk_pct / 100
    spread_cost  = req.spread_pips * pip  # price cost of spread per unit

    # Lot size based on risk (approximate: $10 per pip per lot for major pairs)
    lot_size = max(0.01, round(risk_amt / (req.sl_pips * 10), 2))

    trades    = []
    equity_curve = [capital]
    position  = None   # {"side","entry","sl","tp","open_time","open_idx","lot"}

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
                gross = risk_amt * req.tp_pips / req.sl_pips if hit_tp else -risk_amt
                commission_cost = req.commission * position["lot"]
                spread_deduct   = spread_cost / pip * 10 * position["lot"] / max(req.sl_pips, 1) * risk_amt / req.sl_pips
                pnl = round(gross - commission_cost - spread_deduct, 2)
                equity += pnl
                trades.append({
                    "num":        len(trades) + 1,
                    "side":       position["side"],
                    "entry":      round(position["entry"], 5),
                    "exit":       round(position["tp"] if hit_tp else position["sl"], 5),
                    "result":     "WIN" if hit_tp else "LOSS",
                    "pnl":        pnl,
                    "gross":      round(gross, 2),
                    "commission": round(commission_cost, 2),
                    "open_time":  position["open_time"],
                    "close_time": times[i],
                    "duration":   f"{i - position['open_idx']} bars",
                })
                equity_curve.append(round(equity, 2))
                position = None

        # Open new trade on signal (only if no open position)
        if not position and signals[i]:
            # Apply spread: BUY fills at Ask (close + spread), SELL fills at Bid (close)
            entry = c_close + spread_cost if signals[i] == "BUY" else c_close
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
                "lot":       lot_size,
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
        "commission":   req.commission,
        "spread_pips":  req.spread_pips,
        "total_commission": round(sum(t.get("commission", 0) for t in trades), 2),
        "equity_curve": equity_curve,
        "dd_curve":     dd_curve,
        "trades":      trades[-50:],   # last 50 trades for table
    }


# ── CRYPTO EXCHANGE (Binance Futures + Bybit Perpetual + CoinSwitch) ────────────
import urllib.parse as _urlparse
import requests as _requests

try:
    import ccxt as _ccxt
    _CCXT_OK = True
except ImportError:
    _ccxt = None
    _CCXT_OK = False
    print("⚠  ccxt not installed — run: pip install ccxt")

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as _Ed25519Key
    _CS_OK = True
except ImportError:
    _Ed25519Key = None
    _CS_OK = False
    print("⚠  cryptography not installed — run: pip install cryptography")


# ── CoinSwitch Futures Client (Ed25519 auth, custom API) ────────────────────────
class CoinSwitchClient:
    BASE = "https://coinswitch.co"
    EX   = "EXCHANGE_2"
    # Top futures symbols — used to scan open positions (API requires symbol param)
    TOP_SYMBOLS = [
        "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","DOGEUSDT",
        "ADAUSDT","AVAXUSDT","MATICUSDT","DOTUSDT","LINKUSDT","LTCUSDT",
        "UNIUSDT","ATOMUSDT","APTUSDT","ARBUSDT","OPUSDT","SUIUSDT","INJUSDT",
    ]

    def __init__(self, api_key: str, api_secret: str):
        if not _CS_OK:
            raise RuntimeError("cryptography not installed — pip install cryptography")
        self.api_key = api_key
        self._sk     = _Ed25519Key.from_private_bytes(bytes.fromhex(api_secret))

    def _sign(self, method: str, path: str, params: dict = None):
        full = path
        if params:
            sep  = "&" if "?" in path else "?"
            full = path + sep + _urlparse.urlencode(params)
        decoded = _urlparse.unquote_plus(full)
        epoch   = str(int(datetime.now().timestamp() * 1000))
        msg     = (method.upper() + decoded + epoch).encode()
        sig     = self._sk.sign(msg).hex()
        return {
            "Content-Type":     "application/json",
            "X-AUTH-APIKEY":    self.api_key,
            "X-AUTH-SIGNATURE": sig,
            "X-AUTH-EPOCH":     epoch,
        }, decoded

    def _get(self, path: str, params: dict = None):
        h, dp = self._sign("GET", path, params)
        r = _requests.get(self.BASE + dp, headers=h, timeout=15)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict = None):
        h, dp = self._sign("POST", path)
        r = _requests.post(self.BASE + dp, json=body or {}, headers=h, timeout=15)
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str, params: dict = None):
        h, dp = self._sign("DELETE", path, params)
        r = _requests.delete(self.BASE + dp, headers=h, timeout=15)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _to_cs(symbol: str) -> str:
        """'BTC/USDT:USDT' → 'BTCUSDT'"""
        return symbol.split("/")[0] + "USDT"

    @staticmethod
    def _from_cs(symbol: str) -> str:
        """'BTCUSDT' → 'BTC/USDT:USDT'"""
        base = symbol[:-4] if symbol.upper().endswith("USDT") else symbol
        return f"{base}/USDT:USDT"

    # ── ccxt-compatible interface ──────────────────────────────────────────────

    def fetch_balance(self):
        # 1. Futures wallet (USDT)
        fw   = self._get("/trade/api/v2/futures/wallet_balance")
        fdata = fw.get("data", {})
        usdt_bal = {}
        for item in (fdata.get("base_asset_balances") or []):
            if (item.get("base_asset") or "").upper() == "USDT":
                usdt_bal = item.get("balances", {})
                break
        f_free = float(usdt_bal.get("total_available_balance", 0) or 0)
        f_tot  = float(usdt_bal.get("total_balance",           0) or 0)
        f_used = max(0.0, round(f_tot - f_free, 4))

        # 2. Main portfolio (all currencies)
        portfolio_currencies = {}
        try:
            pr = self._get("/trade/api/v2/user/portfolio")
            for item in (pr.get("data") or []):
                cur = item.get("currency", "")
                bal = float(item.get("main_balance", 0) or 0)
                if bal > 0:
                    portfolio_currencies[cur] = {
                        "free":  bal,
                        "used":  float(item.get("blocked_balance_order", 0) or 0),
                        "total": bal + float(item.get("blocked_balance_order", 0) or 0),
                    }
        except Exception:
            pass

        # 3. Unrealized PnL from positions
        upnl = 0.0
        try:
            for sym in self.TOP_SYMBOLS[:8]:
                pr2 = self._get("/trade/api/v2/futures/positions",
                                {"exchange": self.EX, "symbol": sym})
                for p in (pr2.get("data") or []):
                    if float(p.get("position_size") or 0) > 0:
                        upnl += float(p.get("unrealized_pnl") or
                                      p.get("unrealisedPnl") or 0)
        except Exception:
            pass

        result = {
            "USDT": {"free": round(f_free, 2), "used": round(f_used, 2), "total": round(f_tot, 2)},
            "info": {
                "totalUnrealizedProfit": round(upnl, 4),
                "portfolio": portfolio_currencies,
            },
        }
        # Also expose each portfolio currency at top level (like ccxt does)
        result.update(portfolio_currencies)
        return result

    def fetch_positions(self):
        result = []
        seen   = set()
        for cs_sym in self.TOP_SYMBOLS:
            try:
                d = self._get("/trade/api/v2/futures/positions",
                              {"exchange": self.EX, "symbol": cs_sym})
                for p in (d.get("data") or []):
                    if not p or p.get("position_id") in seen:
                        continue
                    size = float(p.get("position_size") or 0)
                    if size <= 0:
                        continue
                    seen.add(p.get("position_id"))
                    result.append({
                        "symbol":           self._from_cs(p.get("symbol", cs_sym)),
                        "side":             p.get("position_side", "LONG").lower(),
                        "contracts":        size,
                        "entryPrice":       float(p.get("avg_entry_price") or 0),
                        "markPrice":        float(p.get("mark_price")      or 0),
                        "unrealizedPnl":    float(p.get("unrealised_pnl")  or 0),
                        "percentage":       0,
                        "leverage":         int(float(p.get("leverage")    or 1)),
                        "liquidationPrice": float(p.get("liquidation_price") or 0),
                        "initialMargin":    float(p.get("position_margin")  or 0),
                    })
            except Exception:
                continue
        return result

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        body = {
            "exchange":   self.EX,
            "symbol":     self._to_cs(symbol),
            "side":       side.upper(),
            "order_type": type.upper(),
            "quantity":   amount,
        }
        if price and type.upper() == "LIMIT":
            body["price"] = price
        if params and params.get("reduceOnly"):
            body["reduce_only"] = True
        d = self._post("/trade/api/v2/futures/order", body).get("data", {})
        return {
            "id":     d.get("order_id"),
            "symbol": symbol,
            "side":   d.get("side", side).lower(),
            "amount": float(d.get("quantity") or amount),
            "price":  float(d.get("avg_execution_price") or price or 0) or None,
            "status": d.get("status", ""),
        }

    def set_leverage(self, leverage, symbol):
        try:
            self._post("/trade/api/v2/futures/leverage", {
                "exchange": self.EX,
                "symbol":   self._to_cs(symbol),
                "leverage": int(leverage),
            })
        except Exception:
            pass  # Cannot change leverage while position/order is open

    def fetch_open_orders(self, symbol=None):
        try:
            body = {"exchange": self.EX, "limit": 50}
            if symbol:
                body["symbol"] = self._to_cs(symbol)
            d      = self._post("/trade/api/v2/futures/orders/open", body)
            orders = d.get("data", {}).get("orders", []) or []
            return [{
                "id":     o.get("order_id"),
                "symbol": self._from_cs(o.get("symbol", "")),
                "side":   (o.get("side") or "").lower(),
                "type":   (o.get("order_type") or "").lower(),
                "amount": float(o.get("quantity") or 0),
                "price":  float(o.get("price") or 0) or None,
                "status": o.get("status", ""),
            } for o in orders]
        except Exception:
            return []

    def fetch_my_trades(self, symbol=None, limit=100):
        """Fetch closed/executed orders as trade history."""
        try:
            body = {"exchange": self.EX, "limit": min(limit, 50)}
            if symbol:
                body["symbol"] = self._to_cs(symbol)
            d      = self._post("/trade/api/v2/futures/orders/closed", body)
            orders = d.get("data", {}).get("orders", []) or []
            result = []
            for o in orders:
                st = o.get("status","")
                if st not in ("EXECUTED","PARTIALLY_EXECUTED"):
                    continue
                pnl = float(o.get("realised_pnl") or 0)
                fee = float(o.get("execution_fee") or 0)
                price = float(o.get("avg_execution_price") or 0)
                qty   = float(o.get("exec_quantity") or o.get("quantity") or 0)
                ts    = o.get("created_at", 0)
                result.append({
                    "id":       o.get("order_id"),
                    "symbol":   self._from_cs(o.get("symbol","")),
                    "side":     (o.get("side") or "").lower(),
                    "amount":   qty,
                    "price":    price,
                    "pnl":      pnl,
                    "fee":      fee,
                    "timestamp":ts,
                    "datetime": datetime.fromtimestamp(ts/1000).strftime("%Y-%m-%d %H:%M") if ts else "",
                })
            return result
        except Exception:
            return []


_EX_FILE   = "exchanges.json"
_active_ex = {}   # {"binance": ccxt.Exchange, "bybit": ccxt.Exchange, "coinswitch": CoinSwitchClient}


class ExConnectReq(BaseModel):
    exchange:   str           # "binance" | "bybit"
    api_key:    str
    api_secret: str
    testnet:    bool = False


class CryptoOrderReq(BaseModel):
    exchange:    str
    symbol:      str           # e.g. "BTC/USDT:USDT"
    side:        str           # "buy" | "sell"
    order_type:  str           # "market" | "limit"
    amount:      float         # in contracts (base coin)
    price:       Optional[float] = None
    leverage:    int  = 10
    reduce_only: bool = False


class CryptoCloseReq(BaseModel):
    exchange: str
    symbol:   str
    pos_side: str              # "long" | "short"
    amount:   float


class CryptoLeverageReq(BaseModel):
    exchange: str
    symbol:   str
    leverage: int


def _load_ex_cfg():
    try:
        with open(_EX_FILE) as f: return json.load(f)
    except: return {}

def _save_ex_cfg(data):
    with open(_EX_FILE, "w") as f: json.dump(data, f, indent=2)


def _build_exchange(name: str, key: str, secret: str, testnet: bool = False):
    name = name.lower()
    if name == "coinswitch":
        return CoinSwitchClient(key, secret)   # Ed25519 auth, no testnet
    if not _CCXT_OK:
        raise RuntimeError("ccxt not installed — pip install ccxt")
    if name == "binance":
        ex = _ccxt.binanceusdm({
            "apiKey": key, "secret": secret,
            "options": {"defaultType": "future"},
        })
    elif name == "bybit":
        ex = _ccxt.bybit({
            "apiKey": key, "secret": secret,
            "options": {"defaultType": "swap", "defaultSubType": "linear"},
        })
    else:
        raise ValueError(f"Unsupported exchange: {name}")
    if testnet:
        ex.set_sandbox_mode(True)
    return ex


def _restore_exchanges():
    """Try to reconnect saved exchanges on server startup."""
    if not _CCXT_OK:
        return
    cfg = _load_ex_cfg()
    for name, info in cfg.items():
        try:
            ex = _build_exchange(name, info["api_key"], info["api_secret"], info.get("testnet", False))
            ex.fetch_balance()
            _active_ex[name] = ex
            print(f"Crypto: {name} restored ✓")
        except Exception as e:
            print(f"Crypto: {name} restore failed — {e}")
    # After exchanges are ready, restore bots
    _load_saved_bots()


_BOTS_FILE = "bots.json"


def _save_bots():
    """Persist active bot configs (without timer objects) to disk."""
    try:
        saveable = {}
        for bid, bot in _crypto_bots.items():
            saveable[bid] = {k: v for k, v in bot.items()
                             if not callable(v) and k != 'trades'}
            # Keep last 100 trades for history
            saveable[bid]['trades'] = bot.get('trades', [])[-100:]
        with open(_BOTS_FILE, 'w') as f:
            json.dump(saveable, f, indent=2, default=str)
    except Exception as e:
        print(f"_save_bots error: {e}")


def _load_saved_bots():
    """Load persisted bots from disk and resume active ones."""
    import os as _os
    if not _os.path.exists(_BOTS_FILE):
        return
    try:
        with open(_BOTS_FILE) as f:
            data = json.load(f)
        keys = list(data.keys())
        for bid, bot in data.items():
            if bid in _crypto_bots:
                continue
            # Migrate old bots missing new fields
            bot.setdefault("max_open_trades", 2)
            bot.setdefault("open_trade_count", 1 if bot.get("open_side") else 0)
            _crypto_bots[bid] = bot
            if bot.get('status') == 'active':
                delay = 15 + keys.index(bid) * 3
                t = threading.Timer(delay, _bot_tick, args=[bid])
                t.daemon = True
                t.start()
                _bot_timers[bid] = t
                print(f"Algo bot {bid} ({bot.get('strategy')} {bot.get('symbol')}) resumed ✓")
    except Exception as e:
        print(f"_load_saved_bots error: {e}")


def _fmt_position(p):
    contracts = p.get("contracts") or p.get("contractSize") or 0
    try: contracts = float(contracts)
    except: contracts = 0
    upnl = p.get("unrealizedPnl") or 0
    try: upnl = round(float(upnl), 4)
    except: upnl = 0
    pct  = p.get("percentage") or 0
    try: pct = round(float(pct), 2)
    except: pct = 0
    liq  = p.get("liquidationPrice") or 0
    try: liq = float(liq)
    except: liq = 0
    margin = p.get("initialMargin") or 0
    try: margin = round(float(margin), 2)
    except: margin = 0
    return {
        "symbol":      p.get("symbol", ""),
        "side":        p.get("side", ""),
        "size":        contracts,
        "entry_price": p.get("entryPrice") or 0,
        "mark_price":  p.get("markPrice")  or 0,
        "pnl":         upnl,
        "pnl_pct":     pct,
        "leverage":    p.get("leverage") or 1,
        "liquidation": liq,
        "margin":      margin,
    }


@app.post("/api/crypto/connect")
def crypto_connect(req: ExConnectReq):
    if not _CCXT_OK:
        return JSONResponse(status_code=400, content={"error": "ccxt not installed — pip install ccxt"})
    try:
        ex  = _build_exchange(req.exchange, req.api_key, req.api_secret, req.testnet)
        bal = ex.fetch_balance()
        _active_ex[req.exchange.lower()] = ex
        cfg = _load_ex_cfg()
        cfg[req.exchange.lower()] = {
            "api_key": req.api_key, "api_secret": req.api_secret,
            "testnet": req.testnet
        }
        _save_ex_cfg(cfg)
        usdt = bal.get("USDT", {})
        return {
            "success":  True,
            "exchange": req.exchange.lower(),
            "balance":  round(float(usdt.get("free",  0)), 2),
            "total":    round(float(usdt.get("total", 0)), 2),
            "testnet":  req.testnet,
        }
    except _ccxt.AuthenticationError:
        return JSONResponse(status_code=400, content={"error": "Invalid API key or secret — check credentials"})
    except _ccxt.NetworkError as e:
        return JSONResponse(status_code=400, content={"error": f"Network error — {str(e)[:120]}"})
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})


@app.post("/api/crypto/disconnect/{exchange}")
def crypto_disconnect(exchange: str):
    _active_ex.pop(exchange.lower(), None)
    cfg = _load_ex_cfg()
    cfg.pop(exchange.lower(), None)
    _save_ex_cfg(cfg)
    return {"success": True}


@app.get("/api/crypto/status")
def crypto_status():
    cfg = _load_ex_cfg()
    return {
        "binance":     "binance"     in _active_ex,
        "bybit":       "bybit"       in _active_ex,
        "coinswitch":  "coinswitch"  in _active_ex,
        "saved":       list(cfg.keys()),
    }


@app.get("/api/crypto/debug_raw")
def crypto_debug_raw(exchange: str = "coinswitch", path: str = "/trade/api/v2/futures/wallet_balance"):
    ex = _active_ex.get(exchange.lower())
    if not ex:
        return JSONResponse(status_code=400, content={"error": f"{exchange} not connected"})
    try:
        if isinstance(ex, CoinSwitchClient):
            return ex._get(path)
        return {"error": "not a CoinSwitch client"}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.get("/api/crypto/balance")
def crypto_balance(exchange: str = "binance"):
    ex = _active_ex.get(exchange.lower())
    if not ex:
        return JSONResponse(status_code=400, content={"error": f"{exchange} not connected"})
    try:
        bal  = ex.fetch_balance()
        info = bal.get("info", {})

        # Unrealized PnL
        upnl = 0.0
        try:
            upnl = round(float(
                info.get("totalUnrealizedProfit") or
                info.get("result", {}).get("list", [{}])[0].get("totalUnrealisedPnl", 0)
            ), 2)
        except Exception:
            pass

        usdt = bal.get("USDT", {})
        result = {
            "free":  round(float(usdt.get("free",  0)), 2),
            "used":  round(float(usdt.get("used",  0)), 2),
            "total": round(float(usdt.get("total", 0)), 2),
            "upnl":  upnl,
        }

        # For CoinSwitch: also return the main portfolio currencies
        if isinstance(ex, CoinSwitchClient):
            portfolio = info.get("portfolio", {})
            if portfolio:
                result["portfolio"] = portfolio
                # If USDT futures is 0 but there are portfolio currencies, show them
                if result["total"] == 0:
                    # Sum all portfolio balances in their native currency
                    result["portfolio_summary"] = [
                        {"currency": cur, "balance": round(v["total"], 2)}
                        for cur, v in portfolio.items()
                    ]

        return result
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})


@app.get("/api/crypto/positions")
def crypto_positions(exchange: str = "binance"):
    ex = _active_ex.get(exchange.lower())
    if not ex:
        return JSONResponse(status_code=400, content={"error": f"{exchange} not connected"})
    try:
        raw  = ex.fetch_positions()
        open_pos = []
        for p in raw:
            size = p.get("contracts") or 0
            try: size = float(size)
            except: size = 0
            if size and size != 0:
                open_pos.append(_fmt_position(p))
        return open_pos
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})


@app.get("/api/crypto/orders")
def crypto_orders(exchange: str = "binance", symbol: str = "BTC/USDT:USDT"):
    ex = _active_ex.get(exchange.lower())
    if not ex:
        return JSONResponse(status_code=400, content={"error": f"{exchange} not connected"})
    try:
        orders = ex.fetch_open_orders(symbol)
        return [{
            "id":     o.get("id"),
            "symbol": o.get("symbol"),
            "side":   o.get("side"),
            "type":   o.get("type"),
            "amount": o.get("amount"),
            "price":  o.get("price"),
            "status": o.get("status"),
        } for o in orders]
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})


@app.post("/api/crypto/order")
def crypto_order(req: CryptoOrderReq):
    ex = _active_ex.get(req.exchange.lower())
    if not ex:
        return JSONResponse(status_code=400, content={"error": f"{req.exchange} not connected"})
    try:
        # Set leverage before placing
        try: ex.set_leverage(req.leverage, req.symbol)
        except: pass
        params = {}
        if req.reduce_only:
            params["reduceOnly"] = True
        order = ex.create_order(
            symbol=req.symbol,
            type=req.order_type,
            side=req.side,
            amount=req.amount,
            price=req.price if req.order_type == "limit" else None,
            params=params,
        )
        return {
            "success":  True,
            "order_id": order.get("id"),
            "symbol":   order.get("symbol"),
            "side":     order.get("side"),
            "amount":   order.get("amount"),
            "price":    order.get("price") or order.get("average"),
            "status":   order.get("status"),
        }
    except _ccxt.InsufficientFunds:
        return JSONResponse(status_code=400, content={"error": "Insufficient USDT margin"})
    except _ccxt.InvalidOrder as e:
        return JSONResponse(status_code=400, content={"error": f"Invalid order — {str(e)[:120]}"})
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})


@app.post("/api/crypto/close")
def crypto_close(req: CryptoCloseReq):
    ex = _active_ex.get(req.exchange.lower())
    if not ex:
        return JSONResponse(status_code=400, content={"error": f"{req.exchange} not connected"})
    try:
        close_side = "sell" if req.pos_side == "long" else "buy"
        order = ex.create_order(
            symbol=req.symbol,
            type="market",
            side=close_side,
            amount=req.amount,
            params={"reduceOnly": True},
        )
        return {"success": True, "order_id": order.get("id")}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})


@app.post("/api/crypto/leverage")
def crypto_set_leverage(req: CryptoLeverageReq):
    ex = _active_ex.get(req.exchange.lower())
    if not ex:
        return JSONResponse(status_code=400, content={"error": f"{req.exchange} not connected"})
    try:
        ex.set_leverage(req.leverage, req.symbol)
        return {"success": True, "leverage": req.leverage}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})


@app.get("/api/crypto/markets")
def crypto_markets(exchange: str = "binance"):
    ex = _active_ex.get(exchange.lower())
    fallback = [
        "BTC/USDT:USDT","ETH/USDT:USDT","BNB/USDT:USDT","SOL/USDT:USDT",
        "XRP/USDT:USDT","DOGE/USDT:USDT","ADA/USDT:USDT","AVAX/USDT:USDT",
        "MATIC/USDT:USDT","DOT/USDT:USDT","LINK/USDT:USDT","LTC/USDT:USDT",
        "UNI/USDT:USDT","ATOM/USDT:USDT","FIL/USDT:USDT","APT/USDT:USDT",
        "ARB/USDT:USDT","OP/USDT:USDT","SUI/USDT:USDT","INJ/USDT:USDT",
    ]
    if not ex:
        return fallback
    try:
        mkts = ex.load_markets()
        syms = sorted([
            s for s, m in mkts.items()
            if m.get("settle") == "USDT" and m.get("type") in ("swap", "future") and m.get("active")
        ])
        return syms if syms else fallback
    except:
        return fallback


# ── CRYPTO TRADE HISTORY ────────────────────────────────────────────────────────
@app.get("/api/crypto/history")
def crypto_history(exchange: str = "binance", symbol: str = "BTC/USDT:USDT", limit: int = 100):
    ex = _active_ex.get(exchange.lower())
    if not ex:
        return JSONResponse(status_code=400, content={"error": f"{exchange} not connected"})
    try:
        if isinstance(ex, CoinSwitchClient):
            return ex.fetch_my_trades(symbol, limit)
        raw = ex.fetch_my_trades(symbol, limit=min(limit, 1000))
        trades = []
        for t in raw:
            info = t.get("info", {})
            pnl  = float(info.get("realizedPnl") or info.get("closedPnl") or 0)
            fee  = float((t.get("fee") or {}).get("cost") or 0)
            ts   = t.get("timestamp") or 0
            trades.append({
                "id":       t.get("id"),
                "symbol":   t.get("symbol"),
                "side":     t.get("side"),
                "amount":   t.get("amount"),
                "price":    t.get("price"),
                "pnl":      round(pnl, 4),
                "fee":      round(fee, 4),
                "timestamp":ts,
                "datetime": t.get("datetime", "")[:16] if t.get("datetime") else "",
            })
        return trades
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})


# ── CRYPTO OHLCV (public Binance futures data) ───────────────────────────────────
_pub_mkt = None

def _get_pub_mkt():
    global _pub_mkt
    if not _pub_mkt and _CCXT_OK:
        try:
            _pub_mkt = _ccxt.binanceusdm({"options": {"defaultType": "future"}})
        except Exception:
            pass
    return _pub_mkt


@app.get("/api/crypto/ohlcv")
def crypto_ohlcv(symbol: str = "BTC/USDT:USDT", timeframe: str = "1h", limit: int = 100):
    pm = _get_pub_mkt()
    if not pm:
        return JSONResponse(status_code=400, content={"error": "Market data not available"})
    try:
        data = pm.fetch_ohlcv(symbol, timeframe, limit=limit)
        return [{"t": c[0], "o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": c[5]} for c in data]
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})


# ── CRYPTO ALGO BOTS ─────────────────────────────────────────────────────────────
import uuid as _uuid

_crypto_bots: dict = {}
_bot_timers:  dict = {}

_TF_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "1d": 86400,
}


class CryptoBotReq(BaseModel):
    exchange:       str
    symbol:         str
    strategy:       str          # ema_cross|rsi|breakout|macd_cross|bb_squeeze|supertrend|scalp|ai_score
    timeframe:      str   = "1h"
    risk_pct:       float = 1.0
    leverage:       int   = 10
    # EMA params
    fast_ema:       int   = 9
    slow_ema:       int   = 21
    # RSI params
    rsi_period:     int   = 14
    rsi_ob:         int   = 70
    rsi_os:         int   = 30
    # MACD params
    macd_fast:      int   = 12
    macd_slow:      int   = 26
    macd_signal:    int   = 9
    # BB params
    bb_period:      int   = 20
    bb_std:         float = 2.0
    # Supertrend / Scalp params
    atr_period:     int   = 14
    st_multiplier:  float = 3.0
    # AI strategy
    ai_min_score:   int   = 65   # 0-100, only trade if AI score >= this
    # Risk management (ATR-based)
    trailing_atr:   float = 0.0  # 0=off, e.g. 2.0 = trail by 2*ATR
    tp_atr:         float = 0.0  # 0=off, e.g. 3.0 = TP at 3*ATR
    adx_min:        int   = 0    # min ADX to take a trade (0=off)
    max_open_trades: int  = 2    # max simultaneous open trades per bot


# ── INDICATOR LIBRARY ────────────────────────────────────────────────────────────
def _ema_calc(data, period):
    if len(data) < period:
        return data[:]
    k = 2 / (period + 1)
    out = [sum(data[:period]) / period]
    for v in data[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _rsi_calc(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains  = [max(0.0, closes[i] - closes[i-1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i-1] - closes[i]) for i in range(1, len(closes))]
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    return round(100 - 100 / (1 + ag / al), 2) if al else 100.0


def _macd_calc(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None, None, None
    fe = _ema_calc(closes, fast)
    se = _ema_calc(closes, slow)
    # align lengths
    diff = len(fe) - len(se)
    fe   = fe[diff:] if diff > 0 else fe
    macd = [f - s for f, s in zip(fe, se)]
    sig  = _ema_calc(macd, signal)
    diff2 = len(macd) - len(sig)
    macd  = macd[diff2:] if diff2 > 0 else macd
    hist  = [m - s for m, s in zip(macd, sig)]
    return macd, sig, hist


def _atr_calc(highs, lows, closes, period=14):
    if len(closes) < 2:
        return 0.0
    trs = [max(highs[i] - lows[i],
               abs(highs[i] - closes[i-1]),
               abs(lows[i]  - closes[i-1]))
           for i in range(1, len(closes))]
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return round(atr, 6)


def _adx_calc(highs, lows, closes, period=14):
    if len(closes) < period * 2:
        return 25.0
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(closes)):
        h_diff = highs[i] - highs[i-1]
        l_diff = lows[i-1] - lows[i]
        plus_dm.append(h_diff if h_diff > l_diff and h_diff > 0 else 0.0)
        minus_dm.append(l_diff if l_diff > h_diff and l_diff > 0 else 0.0)
        tr_list.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))

    def _smooth(lst, p):
        s = sum(lst[:p])
        out = [s]
        for v in lst[p:]:
            s = s - s/p + v
            out.append(s)
        return out

    str_ = _smooth(tr_list, period)
    spdm = _smooth(plus_dm, period)
    smdm = _smooth(minus_dm, period)
    pdi  = [100 * p / t if t else 0 for p, t in zip(spdm, str_)]
    mdi  = [100 * m / t if t else 0 for m, t in zip(smdm, str_)]
    dx   = [100 * abs(p - m) / (p + m) if (p + m) else 0 for p, m in zip(pdi, mdi)]
    if len(dx) < period:
        return 25.0
    adx = sum(dx[:period]) / period
    for v in dx[period:]:
        adx = (adx * (period - 1) + v) / period
    return round(adx, 2)


def _bb_calc(closes, period=20, std_mult=2.0):
    if len(closes) < period:
        return None, None, None, None
    import math
    middle = sum(closes[-period:]) / period
    var    = sum((c - middle) ** 2 for c in closes[-period:]) / period
    sd     = math.sqrt(var)
    upper  = middle + std_mult * sd
    lower  = middle - std_mult * sd
    width  = (upper - lower) / middle * 100  # % width
    return round(upper,6), round(middle,6), round(lower,6), round(width,4)


def _supertrend_calc(highs, lows, closes, period=10, multiplier=3.0):
    if len(closes) < period + 1:
        return 1, closes[-1]  # default bullish
    atrs = []
    for i in range(1, len(closes)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        atrs.append(tr)
    # Smooth ATR
    atr = sum(atrs[:period]) / period
    for v in atrs[period:]:
        atr = (atr * (period-1) + v) / period

    hl2 = [(highs[i]+lows[i])/2 for i in range(len(closes))]
    upper_band = hl2[-1] + multiplier * atr
    lower_band = hl2[-1] - multiplier * atr

    # Simplified: compare close vs supertrend bands
    prev_close = closes[-2]
    curr_close = closes[-1]
    # Trend: 1=bullish (above lower), -1=bearish (below upper)
    if curr_close > upper_band and prev_close <= upper_band:
        return -1, upper_band   # bearish flip
    if curr_close < lower_band and prev_close >= lower_band:
        return 1, lower_band    # bullish flip
    # Sustained trend
    if curr_close > lower_band:
        return 1, lower_band
    return -1, upper_band


def _vwap_calc(ohlcv):
    cum_tp_vol, cum_vol = 0.0, 0.0
    for c in ohlcv:
        tp = (c[2] + c[3] + c[4]) / 3
        cum_tp_vol += tp * c[5]
        cum_vol    += c[5]
    return round(cum_tp_vol / cum_vol, 6) if cum_vol else 0.0


# ── AI SCORING ENGINE ─────────────────────────────────────────────────────────────
def _ai_full_analysis(ohlcv, bot_params=None):
    """Multi-indicator scoring engine. Returns score 0-100 + full breakdown."""
    if len(ohlcv) < 30:
        return {"ai_score": 50, "signal": "NEUTRAL", "confidence": "LOW", "components": {}}

    closes  = [c[4] for c in ohlcv]
    highs   = [c[2] for c in ohlcv]
    lows    = [c[3] for c in ohlcv]
    volumes = [c[5] for c in ohlcv]
    price   = closes[-1]
    score   = 0
    components = {}

    # 1. EMA TREND (0-25)
    fast_p = (bot_params or {}).get("fast_ema", 9)
    slow_p = (bot_params or {}).get("slow_ema", 21)
    fe     = _ema_calc(closes, fast_p)
    se     = _ema_calc(closes, slow_p)
    ema200 = _ema_calc(closes, min(200, len(closes)-1))
    trend_score = 0
    ema_detail  = []
    if fe and se and fe[-1] > se[-1]:
        trend_score += 12
        ema_detail.append(f"EMA{fast_p}>{slow_p} ✓")
    else:
        ema_detail.append(f"EMA{fast_p}<{slow_p}")
    if ema200 and price > ema200[-1]:
        trend_score += 8
        ema_detail.append("Above EMA200 ✓")
    if fe and len(fe) >= 2 and fe[-1] > fe[-2]:
        trend_score += 5
        ema_detail.append("EMA rising ✓")
    components["trend"] = {"score": trend_score, "max": 25, "detail": ", ".join(ema_detail)}
    score += trend_score

    # 2. RSI MOMENTUM (0-20)
    rsi = _rsi_calc(closes, 14)
    rsi_score = 0
    if 45 <= rsi <= 65:
        rsi_score = 20   # momentum zone
    elif 40 <= rsi < 45 or 65 < rsi <= 70:
        rsi_score = 14
    elif 30 <= rsi < 40:
        rsi_score = 16   # oversold recovery
    elif rsi > 70 and rsi <= 80:
        rsi_score = 8    # overbought
    elif rsi < 30:
        rsi_score = 18   # deep oversold = potential bounce
    rsi_zone = "Bullish" if rsi > 50 else "Bearish"
    components["momentum"] = {"score": rsi_score, "max": 20, "detail": f"RSI={rsi:.1f} ({rsi_zone})", "rsi": rsi}
    score += rsi_score

    # 3. MACD (0-20)
    macd_l, sig_l, hist = _macd_calc(closes, 12, 26, 9)
    macd_score = 0
    macd_detail = "N/A"
    if macd_l and sig_l and hist:
        if macd_l[-1] > sig_l[-1]:
            macd_score += 12
        if hist[-1] > 0:
            macd_score += 4
        if len(hist) >= 2 and hist[-1] > hist[-2]:
            macd_score += 4
        macd_detail = f"MACD={macd_l[-1]:.4f}, Signal={sig_l[-1]:.4f}, Hist={'↑' if hist[-1]>0 else '↓'}"
    components["macd"] = {"score": macd_score, "max": 20, "detail": macd_detail}
    score += macd_score

    # 4. BOLLINGER BANDS (0-15)
    upper, middle, lower, bw = _bb_calc(closes, 20, 2.0)
    bb_score = 0
    bb_detail = "N/A"
    if upper and lower and middle:
        pos = (price - lower) / (upper - lower) * 100  # 0-100% of band
        if 30 <= pos <= 70:
            bb_score = 15   # mid-band, healthy trend
        elif pos < 30:
            bb_score = 10   # near lower, bounce potential
        elif pos > 70:
            bb_score = 5    # near upper, caution
        bb_detail = f"Price@{pos:.0f}% of band, BW={bw:.2f}%"
    components["bb"] = {"score": bb_score, "max": 15, "detail": bb_detail}
    score += bb_score

    # 5. VOLUME (0-10)
    avg_vol    = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else sum(volumes) / len(volumes)
    curr_vol   = volumes[-1]
    vol_ratio  = curr_vol / avg_vol if avg_vol else 1
    vol_score  = min(10, int(vol_ratio * 6))
    components["volume"] = {"score": vol_score, "max": 10,
                            "detail": f"Vol={vol_ratio:.2f}x avg ({'High' if vol_ratio>1.2 else 'Normal'})"}
    score += vol_score

    # 6. ADX TREND STRENGTH (0-10 bonus, not penalized)
    adx = _adx_calc(highs, lows, closes, 14)
    adx_bonus = 0
    adx_detail = f"ADX={adx:.1f}"
    if adx > 40:
        adx_bonus = 10
        adx_detail += " (Strong trend)"
    elif adx > 25:
        adx_bonus = 6
        adx_detail += " (Trending)"
    else:
        adx_detail += " (Ranging)"
    components["adx"] = {"score": adx_bonus, "max": 10, "detail": adx_detail, "adx": adx}
    score += adx_bonus

    # 7. PRICE ACTION PATTERNS (0-20) — from Candlestick Bible + Cheat Sheet
    opens_arr = [c[1] for c in ohlcv]
    pa_patterns = _detect_price_action(opens_arr, closes, highs, lows)
    pa_score = 0; pa_detail = "No PA patterns"
    if pa_patterns:
        best_pa = max(pa_patterns, key=lambda x: x[2])
        pa_score = round(best_pa[2] * 20)
        pa_detail = f"{best_pa[0]} ({best_pa[1]}) str={best_pa[2]:.2f}"
    components["price_action"] = {"score": pa_score, "max": 20, "detail": pa_detail}
    score += pa_score

    # 8. FIBONACCI LEVEL (0-10) — 38.2/50/61.8% retracement key zones
    atr_val = _atr_calc(highs, lows, closes, 14)
    fib_d, fib_det = _check_fibonacci_level(highs, lows, price, atr_val)
    fib_score = 10 if fib_d in ("BUY","SELL") else 0
    components["fibonacci"] = {"score": fib_score, "max": 10, "detail": fib_det}
    score += fib_score

    # 9. 21 EMA BOUNCE (0-8) — dynamic support/resistance (Candlestick Bible)
    ema21_d, ema21_det = _check_21ema_bounce(closes, highs, lows, price, atr_val)
    ema21_score = 8 if ema21_d in ("BUY","SELL") else 0
    components["ema21_bounce"] = {"score": ema21_score, "max": 8, "detail": ema21_det}
    score += ema21_score

    # ATR for SL/TP suggestions
    atr = _atr_calc(highs, lows, closes, 14)
    vwap = _vwap_calc(ohlcv)

    # Score interpretation
    score = min(100, score)
    if score >= 75:
        signal, confidence = "STRONG BUY", "HIGH"
    elif score >= 62:
        signal, confidence = "BUY", "MEDIUM"
    elif score >= 50:
        signal, confidence = "NEUTRAL", "LOW"
    elif score >= 38:
        signal, confidence = "SELL", "MEDIUM"
    else:
        signal, confidence = "STRONG SELL", "HIGH"

    sl_long  = round(price - 2 * atr, 4)
    tp_long  = round(price + 3 * atr, 4)
    sl_short = round(price + 2 * atr, 4)
    tp_short = round(price - 3 * atr, 4)

    return {
        "ai_score":     score,
        "signal":       signal,
        "confidence":   confidence,
        "components":   components,
        "adx":          adx,
        "rsi":          rsi,
        "atr":          round(atr, 4),
        "vwap":         vwap,
        "price":        price,
        "sl_long":      sl_long,
        "tp_long":      tp_long,
        "sl_short":     sl_short,
        "tp_short":     tp_short,
        "macd":         round(macd_l[-1], 6) if macd_l else None,
        "macd_signal":  round(sig_l[-1], 6) if sig_l else None,
        "bb_upper":     upper,
        "bb_lower":     lower,
        "bb_width":     bw,
    }


# ── STRATEGY SIGNAL ENGINE ───────────────────────────────────────────────────────
def _get_bot_signal(bot, ohlcv):
    closes  = [c[4] for c in ohlcv]
    highs   = [c[2] for c in ohlcv]
    lows    = [c[3] for c in ohlcv]
    volumes = [c[5] for c in ohlcv]
    strategy = bot["strategy"]

    # ADX filter: skip if market is too choppy
    adx_min = bot.get("adx_min", 0)
    if adx_min > 0:
        adx = _adx_calc(highs, lows, closes, 14)
        bot["last_adx"] = adx
        if adx < adx_min:
            return None   # choppy market, no trade

    if strategy == "ema_cross":
        fe = _ema_calc(closes, bot["fast_ema"])
        se = _ema_calc(closes, bot["slow_ema"])
        if len(fe) < 2 or len(se) < 2:
            return None
        if fe[-2] <= se[-2] and fe[-1] > se[-1]:
            return "BUY"
        if fe[-2] >= se[-2] and fe[-1] < se[-1]:
            return "SELL"

    elif strategy == "rsi":
        rsi = _rsi_calc(closes, bot["rsi_period"])
        bot["last_rsi"] = rsi
        if rsi <= bot["rsi_os"]:
            return "BUY"
        if rsi >= bot["rsi_ob"]:
            return "SELL"

    elif strategy == "breakout":
        lb = min(20, len(closes) - 1)
        if closes[-1] > max(highs[-lb-1:-1]):
            return "BUY"
        if closes[-1] < min(lows[-lb-1:-1]):
            return "SELL"

    elif strategy == "macd_cross":
        macd_l, sig_l, hist = _macd_calc(closes, bot["macd_fast"], bot["macd_slow"], bot["macd_signal"])
        if not hist or len(hist) < 2:
            return None
        bot["last_macd"]  = round(macd_l[-1], 6)
        bot["last_macd_s"]= round(sig_l[-1], 6)
        # MACD line crosses signal line
        if hist[-2] <= 0 and hist[-1] > 0:
            return "BUY"
        if hist[-2] >= 0 and hist[-1] < 0:
            return "SELL"

    elif strategy == "bb_squeeze":
        # Price bounces off bands with volume confirmation
        upper, middle, lower, bw = _bb_calc(closes, bot["bb_period"], bot["bb_std"])
        if not upper:
            return None
        rsi = _rsi_calc(closes, 14)
        avg_vol = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 1
        vol_ok  = volumes[-1] > avg_vol * 1.2
        # Price touched lower band + RSI oversold + volume spike → BUY
        if closes[-2] <= lower * 1.002 and closes[-1] > lower and rsi < 45 and vol_ok:
            return "BUY"
        # Price touched upper band + RSI overbought + volume spike → SELL
        if closes[-2] >= upper * 0.998 and closes[-1] < upper and rsi > 55 and vol_ok:
            return "SELL"

    elif strategy == "supertrend":
        prev_dir, _ = _supertrend_calc(highs[:-1], lows[:-1], closes[:-1],
                                       bot.get("atr_period", 10), bot.get("st_multiplier", 3.0))
        curr_dir, _ = _supertrend_calc(highs, lows, closes,
                                       bot.get("atr_period", 10), bot.get("st_multiplier", 3.0))
        if prev_dir == -1 and curr_dir == 1:
            return "BUY"
        if prev_dir == 1 and curr_dir == -1:
            return "SELL"

    elif strategy == "scalp":
        # Fast scalp: 3/8 EMA cross + RSI momentum zone + volume
        fe3  = _ema_calc(closes, 3)
        fe8  = _ema_calc(closes, 8)
        fe21 = _ema_calc(closes, 21)
        rsi  = _rsi_calc(closes, 7)  # fast RSI
        avg_vol = sum(volumes[-10:]) / 10 if len(volumes) >= 10 else 1
        vol_ok  = volumes[-1] > avg_vol
        if (len(fe3) < 2 or len(fe8) < 2): return None
        # BUY: 3 crosses above 8, price > 21 EMA, RSI 45-65, volume ok
        if fe3[-2] <= fe8[-2] and fe3[-1] > fe8[-1] and closes[-1] > fe21[-1] and 45 < rsi < 65 and vol_ok:
            return "BUY"
        # SELL: 3 crosses below 8, price < 21 EMA, RSI 35-55
        if fe3[-2] >= fe8[-2] and fe3[-1] < fe8[-1] and closes[-1] < fe21[-1] and 35 < rsi < 55 and vol_ok:
            return "SELL"

    elif strategy == "ai_score":
        analysis = _ai_full_analysis(ohlcv, bot)
        bot["last_ai_score"]  = analysis["ai_score"]
        bot["last_ai_signal"] = analysis["signal"]
        threshold = bot.get("ai_min_score", 65)
        if analysis["ai_score"] >= threshold:
            return "BUY"
        if analysis["ai_score"] <= (100 - threshold):
            return "SELL"

    elif strategy == "pin_bar_sr":
        # Pin Bar at Key Level — 3-pillar (Candlestick Bible): Trend + Level + PA
        opens_a = [c[1] for c in ohlcv]
        atr_v = _atr_calc(highs, lows, closes, 14)
        price = closes[-1]
        fe50  = _ema_calc(closes, 50)
        fe200 = _ema_calc(closes, min(200, len(closes)-1))
        trend_bull = price > fe50[-1] > fe200[-1] if (fe50 and fe200) else False
        trend_bear = price < fe50[-1] < fe200[-1] if (fe50 and fe200) else False
        fib_dir, _ = _check_fibonacci_level(highs, lows, price, atr_v)
        ema_dir, _ = _check_21ema_bounce(closes, highs, lows, price, atr_v)
        # Simple SR: recent high/low proximity
        w = min(40, len(highs)); rh = max(highs[-w:]); rl = min(lows[-w:])
        tol = atr_v * 1.5
        sr_dir = "BUY" if abs(price-rl)<tol else ("SELL" if abs(price-rh)<tol else "NEUTRAL")
        level_bull = (sr_dir=="BUY" or fib_dir=="BUY" or ema_dir=="BUY")
        level_bear = (sr_dir=="SELL" or fib_dir=="SELL" or ema_dir=="SELL")
        pa = _detect_price_action(opens_a, closes, highs, lows)
        pb_bull = any(p[0] in ("Bullish Pin Bar","Hammer","Tweezer Bottom") and p[1]=="BUY"  for p in pa)
        pb_bear = any(p[0] in ("Bearish Pin Bar","Shooting Star","Tweezer Top") and p[1]=="SELL" for p in pa)
        if trend_bull and level_bull and pb_bull: return "BUY"
        if trend_bear and level_bear and pb_bear: return "SELL"

    elif strategy == "engulfing_trend":
        # Engulfing Bar at key level with trend
        opens_a = [c[1] for c in ohlcv]
        atr_v = _atr_calc(highs, lows, closes, 14)
        price = closes[-1]
        fe50  = _ema_calc(closes, 50)
        fe200 = _ema_calc(closes, min(200, len(closes)-1))
        trend_bull = price > fe50[-1] > fe200[-1] if (fe50 and fe200) else False
        trend_bear = price < fe50[-1] < fe200[-1] if (fe50 and fe200) else False
        fib_dir, _ = _check_fibonacci_level(highs, lows, price, atr_v)
        ema_dir, _ = _check_21ema_bounce(closes, highs, lows, price, atr_v)
        w = min(40, len(highs)); rh = max(highs[-w:]); rl = min(lows[-w:])
        tol = atr_v * 1.5
        sr_dir = "BUY" if abs(price-rl)<tol else ("SELL" if abs(price-rh)<tol else "NEUTRAL")
        level_bull = (sr_dir=="BUY" or fib_dir=="BUY" or ema_dir=="BUY")
        level_bear = (sr_dir=="SELL" or fib_dir=="SELL" or ema_dir=="SELL")
        pa = _detect_price_action(opens_a, closes, highs, lows)
        eng_bull = any(p[0] in ("Bullish Engulfing","Morning Star","Piercing Pattern") and p[1]=="BUY"  for p in pa)
        eng_bear = any(p[0] in ("Bearish Engulfing","Evening Star","Dark Cloud Cover") and p[1]=="SELL" for p in pa)
        if trend_bull and level_bull and eng_bull: return "BUY"
        if trend_bear and level_bear and eng_bear: return "SELL"

    elif strategy == "false_breakout":
        # Inside Bar False Breakout — institutional stop hunt trap (highest WR)
        opens_a = [c[1] for c in ohlcv]
        pa = _detect_price_action(opens_a, closes, highs, lows)
        fbo_bull = any(p[0] == "IB False Breakout" and p[1] == "BUY"  for p in pa)
        fbo_bear = any(p[0] == "IB False Breakout" and p[1] == "SELL" for p in pa)
        if fbo_bull: return "BUY"
        if fbo_bear: return "SELL"

    elif strategy == "pa_confluence":
        # Full 3-Pillar PA: Trend + Key Level + PA Signal (all books synthesis)
        opens_a = [c[1] for c in ohlcv]
        atr_v = _atr_calc(highs, lows, closes, 14)
        price = closes[-1]
        fe50  = _ema_calc(closes, 50)
        fe200 = _ema_calc(closes, min(200, len(closes)-1))
        fe21  = _ema_calc(closes, 21)
        curr_dir, _ = _supertrend_calc(highs, lows, closes)
        trend_bull = (price > fe50[-1] > fe200[-1] if (fe50 and fe200) else False) and curr_dir == 1
        trend_bear = (price < fe50[-1] < fe200[-1] if (fe50 and fe200) else False) and curr_dir == -1
        fib_dir, _ = _check_fibonacci_level(highs, lows, price, atr_v)
        ema_dir, _ = _check_21ema_bounce(closes, highs, lows, price, atr_v)
        dt_dir, _  = _detect_double_top_bottom(highs, lows, closes, atr_v)
        w = min(40, len(highs)); rh = max(highs[-w:]); rl = min(lows[-w:])
        tol = atr_v * 1.5
        sr_dir = "BUY" if abs(price-rl)<tol else ("SELL" if abs(price-rh)<tol else "NEUTRAL")
        level_bull = (sr_dir=="BUY" or fib_dir=="BUY" or ema_dir=="BUY" or dt_dir=="BUY")
        level_bear = (sr_dir=="SELL" or fib_dir=="SELL" or ema_dir=="SELL" or dt_dir=="SELL")
        pa = _detect_price_action(opens_a, closes, highs, lows)
        pa_bull_str = max((p[2] for p in pa if p[1]=="BUY"),  default=0)
        pa_bear_str = max((p[2] for p in pa if p[1]=="SELL"), default=0)
        pa_bull = pa_bull_str >= 0.70; pa_bear = pa_bear_str >= 0.70
        if sum([trend_bull, level_bull, pa_bull]) == 3: return "BUY"
        if sum([trend_bear, level_bear, pa_bear]) == 3: return "SELL"

    return None


def _bot_tick(bot_id):
    bot = _crypto_bots.get(bot_id)
    if not bot or bot["status"] != "active":
        return
    try:
        pm = _get_pub_mkt()
        if not pm:
            return
        limit = max(100, (bot.get("slow_ema", 21) or 21) + 30)
        ohlcv  = pm.fetch_ohlcv(bot["symbol"], bot["timeframe"], limit=limit)
        signal = _get_bot_signal(bot, ohlcv)
        price  = float(ohlcv[-1][4])
        bot["last_run"]    = datetime.now().strftime("%H:%M:%S")
        bot["last_price"]  = price

        # Trailing stop / TP check (exit open position if hit)
        atr = _atr_calc([c[2] for c in ohlcv], [c[3] for c in ohlcv], [c[4] for c in ohlcv], 14)
        if bot.get("open_side") and (bot.get("trailing_atr", 0) > 0 or bot.get("tp_atr", 0) > 0):
            ep    = bot.get("open_entry_price", price)
            oside = bot["open_side"]
            trail = bot.get("trailing_atr", 0) * atr
            tp    = bot.get("tp_atr", 0) * atr
            ex    = _active_ex.get(bot["exchange"])
            if ex:
                should_exit = False
                exit_reason = ""
                if oside == "BUY":
                    bot["open_peak"] = max(bot.get("open_peak", ep), price)
                    if trail > 0 and price < bot["open_peak"] - trail:
                        should_exit, exit_reason = True, "trailing_stop"
                    if tp > 0 and price >= ep + tp:
                        should_exit, exit_reason = True, "take_profit"
                else:
                    bot["open_trough"] = min(bot.get("open_trough", ep), price)
                    if trail > 0 and price > bot["open_trough"] + trail:
                        should_exit, exit_reason = True, "trailing_stop"
                    if tp > 0 and price <= ep - tp:
                        should_exit, exit_reason = True, "take_profit"
                if should_exit:
                    try:
                        close_side = "sell" if oside == "BUY" else "buy"
                        for p in ex.fetch_positions():
                            if p.get("symbol") == bot["symbol"] and float(p.get("contracts") or 0) > 0:
                                ex.create_order(bot["symbol"], "market", close_side,
                                                float(p["contracts"]), params={"reduceOnly": True})
                        bot["open_side"]        = None
                        bot["open_entry_price"] = None
                        bot["open_trade_count"] = 0
                        if bot.get("trades"):
                            bot["trades"][-1]["exit_reason"] = exit_reason
                            bot["trades"][-1]["exit_price"]  = round(price, 4)
                            bot["trades"][-1]["status"]      = "closed"
                        threading.Thread(target=_save_bots, daemon=True).start()
                    except Exception:
                        pass

        if signal:
            ex = _active_ex.get(bot["exchange"])
            if not ex:
                return

            # Step 1: Close any opposite-side positions (signal flip)
            opp_closed = False
            try:
                for p in ex.fetch_positions():
                    sym_ok = p.get("symbol") == bot["symbol"]
                    pside  = (p.get("side") or "").lower()
                    sz     = float(p.get("contracts") or 0)
                    if sym_ok and sz > 0:
                        if (signal == "BUY" and pside == "short") or (signal == "SELL" and pside == "long"):
                            cs = "buy" if pside == "short" else "sell"
                            ex.create_order(bot["symbol"], "market", cs, sz, params={"reduceOnly": True})
                            opp_closed = True
            except Exception:
                pass

            if opp_closed:
                # Signal flip: mark last trade exited and reset counter
                if bot.get("trades"):
                    bot["trades"][-1]["exit_reason"] = "signal_flip"
                    bot["trades"][-1]["exit_price"]  = round(price, 4)
                    bot["trades"][-1]["status"]      = "closed"
                bot["open_trade_count"] = 0
                bot["open_side"]        = None

            # Step 2: Gate — if already at max open trades, skip opening new one
            max_t = bot.get("max_open_trades", 2)
            cur_t = bot.get("open_trade_count", 0)
            if cur_t >= max_t:
                bot["total_signals"] += 1
                bot["last_signal"]    = signal
                bot["last_error"]     = f"⏸ Max {max_t} trades open — waiting for SL/TP to close"
                threading.Thread(target=_save_bots, daemon=True).start()
                return

            # Step 3: Size the order
            bal      = ex.fetch_balance()
            free     = float((bal.get("USDT") or {}).get("free") or 0)
            risk_usd = free * bot["risk_pct"] / 100
            if price <= 0: return
            atr_pct     = (atr / price) * 100 if price else 0
            size_factor = min(1.0, 0.5 / atr_pct) if atr_pct > 0.5 else 1.0
            amount      = round((risk_usd * bot["leverage"] * size_factor) / price, 4)
            if amount <= 0: return

            try:
                ex.set_leverage(bot["leverage"], bot["symbol"])
            except Exception:
                pass

            # Step 4: Place order
            side  = "buy" if signal == "BUY" else "sell"
            order = ex.create_order(bot["symbol"], "market", side, amount)
            entry = {
                "time":     datetime.now().strftime("%Y-%m-%d %H:%M"),
                "signal":   signal,
                "price":    round(price, 4),
                "amount":   amount,
                "order_id": order.get("id", ""),
                "pnl":      0,
                "status":   "open",
            }
            if bot.get("tp_atr", 0) > 0 or bot.get("trailing_atr", 0) > 0:
                entry["sl"] = round(price - 2*atr, 4) if signal == "BUY" else round(price + 2*atr, 4)
                entry["tp"] = round(price + bot["tp_atr"]*atr, 4) if signal == "BUY" else round(price - bot["tp_atr"]*atr, 4)
            bot["trades"].append(entry)
            bot["total_signals"]    += 1
            bot["last_signal"]       = signal
            bot["open_side"]         = signal
            bot["open_entry_price"]  = price
            bot["open_peak"]         = price
            bot["open_trough"]       = price
            bot["open_trade_count"]  = cur_t + 1
            bot["last_error"]        = None
            threading.Thread(target=_save_bots, daemon=True).start()

    except Exception as e:
        bot["last_error"] = str(e)[:200]
    finally:
        if _crypto_bots.get(bot_id, {}).get("status") == "active":
            interval = _TF_SECONDS.get(bot.get("timeframe", "1h"), 3600)
            t = threading.Timer(interval, _bot_tick, args=[bot_id])
            t.daemon = True
            t.start()
            _bot_timers[bot_id] = t


@app.post("/api/crypto/algo/start")
def crypto_algo_start(req: CryptoBotReq):
    ex = _active_ex.get(req.exchange.lower())
    if not ex:
        return JSONResponse(status_code=400, content={"error": f"{req.exchange} not connected"})
    bid = str(_uuid.uuid4())[:8]
    _crypto_bots[bid] = {
        "id": bid, "exchange": req.exchange.lower(),
        "symbol": req.symbol, "strategy": req.strategy,
        "timeframe": req.timeframe, "risk_pct": req.risk_pct,
        "leverage": req.leverage,
        # EMA
        "fast_ema": req.fast_ema, "slow_ema": req.slow_ema,
        # RSI
        "rsi_period": req.rsi_period, "rsi_ob": req.rsi_ob, "rsi_os": req.rsi_os,
        # MACD
        "macd_fast": req.macd_fast, "macd_slow": req.macd_slow, "macd_signal": req.macd_signal,
        # BB
        "bb_period": req.bb_period, "bb_std": req.bb_std,
        # Supertrend / ATR
        "atr_period": req.atr_period, "st_multiplier": req.st_multiplier,
        # AI
        "ai_min_score": req.ai_min_score,
        # Risk management
        "trailing_atr": req.trailing_atr, "tp_atr": req.tp_atr, "adx_min": req.adx_min,
        # Risk management
        "max_open_trades": req.max_open_trades,
        # Runtime state
        "status": "active", "trades": [], "total_signals": 0,
        "last_signal": None, "last_run": None, "last_rsi": None,
        "last_adx": None, "last_macd": None, "last_ai_score": None, "last_ai_signal": None,
        "last_price": None, "last_error": None, "open_side": None, "open_entry_price": None,
        "open_trade_count": 0,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    t = threading.Timer(3, _bot_tick, args=[bid])
    t.daemon = True
    t.start()
    _bot_timers[bid] = t
    _save_bots()
    return {"success": True, "bot_id": bid}


@app.get("/api/crypto/algo/analyze")
def crypto_algo_analyze(symbol: str = "BTC/USDT:USDT", timeframe: str = "1h",
                        fast_ema: int = 9, slow_ema: int = 21):
    pm = _get_pub_mkt()
    if not pm:
        return JSONResponse(status_code=400, content={"error": "Market data not available"})
    try:
        ohlcv = pm.fetch_ohlcv(symbol, timeframe, limit=200)
        result = _ai_full_analysis(ohlcv, {"fast_ema": fast_ema, "slow_ema": slow_ema})
        return result
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})


@app.post("/api/crypto/algo/stop/{bot_id}")
def crypto_algo_stop(bot_id: str):
    if bot_id not in _crypto_bots:
        return JSONResponse(status_code=404, content={"error": "Bot not found"})
    _crypto_bots[bot_id]["status"] = "stopped"
    t = _bot_timers.pop(bot_id, None)
    if t:
        t.cancel()
    _save_bots()
    return {"success": True}


@app.delete("/api/crypto/algo/{bot_id}")
def crypto_algo_delete(bot_id: str):
    t = _bot_timers.pop(bot_id, None)
    if t:
        t.cancel()
    _crypto_bots.pop(bot_id, None)
    _save_bots()
    return {"success": True}


@app.get("/api/crypto/algo/history")
def crypto_algo_history():
    """Return all trade records from all bots, newest first."""
    rows = []
    for bid, b in _crypto_bots.items():
        for t in b.get("trades", []):
            rows.append({
                "bot_id":   bid,
                "exchange": b.get("exchange", ""),
                "symbol":   b.get("symbol", ""),
                "strategy": b.get("strategy", ""),
                **t,
            })
    rows.sort(key=lambda x: x.get("time", ""), reverse=True)
    return rows[:300]


@app.get("/api/crypto/algo/list")
def crypto_algo_list(live: bool = False):
    result = []
    for b in _crypto_bots.values():
        entry = {k: v for k, v in b.items() if k != "trades"} | {
            "trade_count":  len(b.get("trades", [])),
            "recent_trades": b.get("trades", [])[-5:],
            "live_pnl":     None,
            "live_size":    None,
            "live_mark":    None,
        }
        # Fetch live unrealized PnL for active in-position bots
        if live and b.get("open_side") and b.get("status") == "active":
            ex = _active_ex.get(b["exchange"])
            if ex:
                try:
                    for p in ex.fetch_positions():
                        if p.get("symbol") == b["symbol"] and float(p.get("contracts") or 0) > 0:
                            entry["live_pnl"]  = round(float(p.get("unrealizedPnl") or 0), 4)
                            entry["live_size"] = float(p.get("contracts", 0))
                            entry["live_mark"] = float(p.get("markPrice") or b.get("last_price") or 0)
                            break
                except Exception:
                    pass
        result.append(entry)
    return result


# ── FOREX BOT (Grid Recovery, isolated process + own MT5 account) ──────────────
# Runs as a separate subprocess (forex_bot.py) with its own MT5 terminal
# connection, deliberately isolated from the main _mt5_worker connection that
# drives the live strategies above. This avoids the single-connection-per-
# process limitation of the MetaTrader5 module ever switching the main
# account's login out from under the live strategies.
import subprocess as _subprocess
import sys as _sys

_FB_DIR          = os.path.dirname(os.path.abspath(__file__))
_FB_CONFIG_FILE  = os.path.join(_FB_DIR, "forex_bot_config.json")
_FB_STATUS_FILE  = os.path.join(_FB_DIR, "forex_bot_status.json")
_FB_STOP_FLAG    = os.path.join(_FB_DIR, "forex_bot_stop.flag")
_FB_PROTECTED_LOGIN = 698085  # never allow the bot to touch the main live account
_forex_bot_proc = None

class ForexBotConnectRequest(BaseModel):
    login:         int
    password:      str
    server:        str
    terminal_path: str = ""

@app.post("/api/forexbot/connect")
def forexbot_connect(req: ForexBotConnectRequest):
    """Validate credentials with a one-off connection in a separate process
    (so it never touches the main MT5 connection), then save config."""
    if req.login == _FB_PROTECTED_LOGIN:
        return JSONResponse({"error": "This is the main live trading account — Forex Bot must use a different account"}, status_code=400)

    check_script = (
        "import MetaTrader5 as mt5, json, sys\n"
        f"kwargs = {{'timeout': 20000}}\n"
        f"path = {req.terminal_path!r}\n"
        "if path: kwargs['path'] = path\n"
        "ok = mt5.initialize(**kwargs)\n"
        "if not ok:\n"
        "    print(json.dumps({'error': str(mt5.last_error())})); sys.exit(0)\n"
        f"logged = mt5.login(login={req.login}, password={req.password!r}, server={req.server!r}, timeout=25000)\n"
        "if not logged:\n"
        "    print(json.dumps({'error': str(mt5.last_error())})); mt5.shutdown(); sys.exit(0)\n"
        "info = mt5.account_info()\n"
        "mt5.shutdown()\n"
        "if info is None:\n"
        "    print(json.dumps({'error': 'no account info'}))\n"
        "else:\n"
        "    print(json.dumps({'login': info.login, 'name': info.name, 'server': info.server, 'balance': info.balance, 'currency': info.currency, 'leverage': info.leverage}))\n"
    )
    try:
        result = _subprocess.run(
            [_sys.executable, "-c", check_script],
            capture_output=True, text=True, timeout=40
        )
        out = (result.stdout or "").strip().splitlines()
        data = json.loads(out[-1]) if out else {"error": "no output from connection check"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    if "error" in data:
        return JSONResponse(data, status_code=400)
    if data.get("login") == _FB_PROTECTED_LOGIN:
        return JSONResponse({"error": "Resolved to the main live trading account — refused"}, status_code=400)

    with open(_FB_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"login": req.login, "password": req.password, "server": req.server,
                   "terminal_path": req.terminal_path}, f)
    return {"success": True, "account": data}


class ForexBotStartRequest(BaseModel):
    symbol:     str = "XAUUSD"
    timeframe:  str = "M15"
    base_lot:   float = 0.01
    max_steps:  int = 12
    multiplier: float = 1.68
    tp_usd:     float = 0.0

@app.post("/api/forexbot/start")
def forexbot_start(req: ForexBotStartRequest):
    global _forex_bot_proc
    if not os.path.exists(_FB_CONFIG_FILE):
        return JSONResponse({"error": "Connect an account first"}, status_code=400)
    if _forex_bot_proc is not None and _forex_bot_proc.poll() is None:
        return JSONResponse({"error": "Forex Bot is already running"}, status_code=400)

    with open(_FB_CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.update(req.model_dump())
    with open(_FB_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f)

    if os.path.exists(_FB_STOP_FLAG):
        os.remove(_FB_STOP_FLAG)

    script_path = os.path.join(_FB_DIR, "forex_bot.py")
    _forex_bot_proc = _subprocess.Popen(
        [_sys.executable, script_path],
        cwd=_FB_DIR,
        creationflags=getattr(_subprocess, "CREATE_NO_WINDOW", 0),
    )
    return {"success": True, "pid": _forex_bot_proc.pid}

@app.post("/api/forexbot/stop")
def forexbot_stop():
    global _forex_bot_proc
    with open(_FB_STOP_FLAG, "w") as f:
        f.write("stop")
    # Give it a few seconds to exit gracefully; the dashboard will poll status.
    if _forex_bot_proc is not None:
        try:
            _forex_bot_proc.wait(timeout=10)
        except Exception:
            _forex_bot_proc.terminate()
        _forex_bot_proc = None
    return {"success": True}

@app.get("/api/forexbot/status")
def forexbot_status():
    alive = _forex_bot_proc is not None and _forex_bot_proc.poll() is None
    data = {"process_alive": alive, "status": "stopped" if not alive else "running"}
    if os.path.exists(_FB_STATUS_FILE):
        try:
            with open(_FB_STATUS_FILE, encoding="utf-8") as f:
                data.update(json.load(f))
            data["process_alive"] = alive
        except Exception:
            pass
    return data


@app.get("/")
def serve_index():
    return FileResponse("index.html", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    })


# ── TELEGRAM SIGNAL BOT ─────────────────────────────────────────────────────────
import re as _re
import urllib.request as _urllib_req
import urllib.error   as _urllib_err

_TG_FILE     = "telegram_cfg.json"
_TG_SIGNALS  : list = []          # last 100 parsed signals
_TG_RUNNING  = False
_TG_THREAD   : threading.Thread | None = None
_TG_OFFSET   = 0

_TG_CFG_DEF = {
    "token":          "",
    "chat_id":        "",            # accept signals only from this chat (blank = any)
    "exchanges":      ["binance"],   # which exchanges to place on
    "auto_trade":     False,
    "amount":         0.01,          # contracts per trade
    "leverage":       10,
    "enabled":        False,
    "notify_enabled": True,          # outgoing alerts: trade open/close, errors, drawdown
    "drawdown_pct":   5.0,           # alert when equity drops this % below session peak
}


def _load_tg_cfg() -> dict:
    try:
        with open(_TG_FILE) as f:
            cfg = json.load(f)
        for k, v in _TG_CFG_DEF.items():
            cfg.setdefault(k, v)
        return cfg
    except Exception:
        return dict(_TG_CFG_DEF)


def _save_tg_cfg(cfg: dict):
    with open(_TG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def _tg_api(token: str, method: str, params: dict | None = None) -> dict:
    url  = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps(params or {}).encode()
    req  = _urllib_req.Request(url, data=body,
                               headers={"Content-Type": "application/json"})
    try:
        with _urllib_req.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except _urllib_err.HTTPError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _tg_notify(message: str):
    """Send an outgoing alert to the configured Telegram chat (trade open/close,
    strategy errors, drawdown warnings). Silently no-ops if not configured —
    this must never break the strategy loop it's called from."""
    try:
        cfg = _load_tg_cfg()
        token = cfg.get("token", "")
        chat_id = cfg.get("chat_id", "")
        if not token or not chat_id or not cfg.get("notify_enabled", True):
            return
        _tg_api(token, "sendMessage", {
            "chat_id": chat_id, "text": message, "parse_mode": "HTML",
        })
    except Exception:
        pass


_DD_PEAK_EQUITY: float = 0.0
_DD_LAST_ALERT:  float = 0.0   # timestamp of last drawdown alert

def _drawdown_monitor():
    global _DD_PEAK_EQUITY, _DD_LAST_ALERT
    _time.sleep(30)   # wait for MT5 to connect before first check
    while True:
        try:
            cfg = _load_tg_cfg()
            if cfg.get("token") and cfg.get("chat_id") and cfg.get("notify_enabled", True):
                info = _mt5_call(lambda: mt5.account_info())
                if info and not isinstance(info, dict):
                    eq = info.equity
                    if eq > _DD_PEAK_EQUITY:
                        _DD_PEAK_EQUITY = eq
                    if _DD_PEAK_EQUITY > 0:
                        dd_pct = (_DD_PEAK_EQUITY - eq) / _DD_PEAK_EQUITY * 100
                        threshold = float(cfg.get("drawdown_pct", 5.0))
                        now = _time.time()
                        if dd_pct >= threshold and (now - _DD_LAST_ALERT) > 3600:
                            _DD_LAST_ALERT = now
                            _tg_notify(
                                f"<b>FarhanFX — Drawdown Alert</b>\n"
                                f"⚠️ Equity dropped <b>{dd_pct:.1f}%</b> from peak\n"
                                f"Peak: <code>${_DD_PEAK_EQUITY:.2f}</code>  "
                                f"Now: <code>${eq:.2f}</code>"
                            )
        except Exception:
            pass
        _time.sleep(300)   # check every 5 minutes


threading.Thread(target=_drawdown_monitor, daemon=True, name="dd-monitor").start()


# ── Capital Protection: Buffer/Ratchet system ───────────────────────────
# Inspired by how prop-fund managers treat allocated capital: never risk
# the core capital itself — only a small "buffer" above a hard floor.
# Hit the buffer target -> bank it, ratchet the floor up. Hit the floor
# from below -> hard-stop everything and force a cool-off period.
_CAP_PROT_FILE = "capital_protection.json"
_CAP_PROT_DEF = {
    "enabled":          False,
    "starting_capital":  0.0,
    "buffer_amount":     0.0,
    "liquidation_line":  0.0,   # hard floor — breach stops all strategies
    "target_line":       0.0,  # liquidation_line + buffer_amount
    "status":            "inactive",   # inactive | active | liquidated
    "liquidated_at":      0.0,         # unix timestamp
    "break_days":         7,
}

def _load_cap_prot() -> dict:
    try:
        with open(_CAP_PROT_FILE) as f:
            cfg = json.load(f)
        for k, v in _CAP_PROT_DEF.items():
            cfg.setdefault(k, v)
        return cfg
    except Exception:
        return dict(_CAP_PROT_DEF)

def _save_cap_prot(cfg: dict):
    with open(_CAP_PROT_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def _capital_protection_monitor():
    _time.sleep(35)
    while True:
        try:
            cfg = _load_cap_prot()
            if cfg.get("enabled") and cfg.get("status") == "active":
                info = _mt5_call(lambda: mt5.account_info())
                if info and not isinstance(info, dict):
                    eq     = info.equity
                    target = cfg.get("target_line", 0.0)
                    floor  = cfg.get("liquidation_line", 0.0)
                    buf    = cfg.get("buffer_amount", 0.0)
                    if target > 0 and eq >= target:
                        cfg["liquidation_line"] = target
                        cfg["target_line"]      = target + buf
                        _save_cap_prot(cfg)
                        _tg_notify(
                            f"<b>FarhanFX — Buffer Target Hit 🎯</b>\n"
                            f"Equity reached <code>${eq:.2f}</code>\n"
                            f"New floor locked at <code>${target:.2f}</code> — withdraw "
                            f"${buf:.2f} to bank it.\n"
                            f"Next target: <code>${cfg['target_line']:.2f}</code>"
                        )
                    elif floor > 0 and eq <= floor:
                        stopped = _stop_all_strategies_internal()
                        cfg["status"]         = "liquidated"
                        cfg["liquidated_at"]  = _time.time()
                        _save_cap_prot(cfg)
                        _tg_notify(
                            f"<b>FarhanFX — Liquidation Line Hit 🛑</b>\n"
                            f"Equity dropped to <code>${eq:.2f}</code> — floor breached.\n"
                            f"Stopped {stopped} running strategy(ies). Mandatory "
                            f"{cfg['break_days']}-day break — no trading.\n"
                            f"Review your setups before resuming."
                        )
        except Exception:
            pass
        _time.sleep(120)

threading.Thread(target=_capital_protection_monitor, daemon=True, name="cap-protection").start()


class CapProtectionReq(BaseModel):
    enabled:           bool
    starting_capital:  float
    buffer_amount:      float
    break_days:        int = 7

@app.post("/api/capital-protection/config")
def cap_protection_save(req: CapProtectionReq):
    cfg = _load_cap_prot()
    capital_changed = (cfg.get("starting_capital") != req.starting_capital or
                        cfg.get("buffer_amount") != req.buffer_amount)
    cfg["enabled"]          = req.enabled
    cfg["break_days"]       = req.break_days
    cfg["starting_capital"] = req.starting_capital
    cfg["buffer_amount"]    = req.buffer_amount
    if capital_changed or cfg.get("status") in ("inactive", None):
        cfg["liquidation_line"] = req.starting_capital
        cfg["target_line"]      = req.starting_capital + req.buffer_amount
    if req.enabled:
        if cfg.get("status") != "liquidated":
            cfg["status"] = "active"
    else:
        cfg["status"] = "inactive"
    _save_cap_prot(cfg)
    return {"success": True, **cfg}

@app.get("/api/capital-protection/status")
def cap_protection_status():
    cfg = _load_cap_prot()
    info = _mt5_call(lambda: mt5.account_info())
    equity = info.equity if info and not isinstance(info, dict) else None
    days_left = 0
    if cfg.get("status") == "liquidated":
        elapsed   = _time.time() - cfg.get("liquidated_at", 0)
        remaining = cfg.get("break_days", 7) * 86400 - elapsed
        days_left = max(0, round(remaining / 86400, 1))
    return {**cfg, "equity": equity, "break_days_left": days_left}

@app.post("/api/capital-protection/reset")
def cap_protection_reset():
    """Manually resume after the cool-off break, or start a fresh cycle."""
    cfg = _load_cap_prot()
    cfg["liquidation_line"] = cfg.get("starting_capital", 0.0)
    cfg["target_line"]      = cfg.get("starting_capital", 0.0) + cfg.get("buffer_amount", 0.0)
    cfg["status"]           = "active" if cfg.get("enabled") else "inactive"
    cfg["liquidated_at"]    = 0.0
    _save_cap_prot(cfg)
    return {"success": True, **cfg}


def _normalise_symbol(raw: str) -> str:
    """Convert 'BTC', 'BTCUSDT', 'BTC/USDT', 'BTC-USDT' → 'BTC/USDT:USDT'"""
    raw = raw.upper().replace("-", "/").replace("_", "/")
    if "/" not in raw:
        # strip trailing USDT/BUSD/USDC
        for q in ("USDT", "BUSD", "USDC"):
            if raw.endswith(q) and len(raw) > len(q):
                raw = raw[:-len(q)] + "/" + q
                break
        else:
            raw = raw + "/USDT"
    if ":" not in raw:
        quote = raw.split("/")[1] if "/" in raw else "USDT"
        raw = raw + ":" + quote
    return raw


def _parse_tg_signal(text: str) -> dict | None:
    """
    Parse common Telegram signal formats. Returns dict or None.
    Handles formats like:
        🟢 LONG BTC/USDT  /  BUY BTCUSDT  /  #BTC LONG
    Entry: 63500 / Entry: 63000-63500
    TP1: 64000   TP: 64000, 65000
    SL: 62000
    Leverage: 10x
    """
    t = text.strip()

    # Direction
    side = None
    if _re.search(r'\b(LONG|BUY)\b', t, _re.I):
        side = "buy"
    elif _re.search(r'\b(SHORT|SELL)\b', t, _re.I):
        side = "sell"
    if side is None:
        return None   # not a signal

    # Symbol — look for coin name patterns
    sym_raw = None
    # Pattern: BTC/USDT, BTC-USDT, BTCUSDT, BTC/USDT:USDT
    m = _re.search(r'#?([A-Z]{2,10})[/-]?(USDT|BUSD|USDC|BTC)\b', t, _re.I)
    if m:
        sym_raw = m.group(1).upper() + "/" + m.group(2).upper()
    else:
        # Try: "#BTCUSDT" or "BTC" standalone near LONG/SHORT
        m = _re.search(r'#([A-Z]{2,10})', t, _re.I)
        if m:
            sym_raw = m.group(1).upper()
    if not sym_raw:
        return None

    symbol = _normalise_symbol(sym_raw)

    # Entry — take first number in entry range
    entry = None
    m = _re.search(r'entry\s*[:\-–]?\s*([\d,.]+)', t, _re.I)
    if m:
        entry = float(m.group(1).replace(",", ""))
    else:
        # Try "@ 63500"
        m = _re.search(r'@\s*([\d,.]+)', t, _re.I)
        if m:
            entry = float(m.group(1).replace(",", ""))

    # TP — collect all TP values, use first
    tps = _re.findall(r'(?:tp\d*|take\s*profit)\s*[:\-–]?\s*([\d,.]+)', t, _re.I)
    tp = float(tps[0].replace(",", "")) if tps else None

    # SL
    sl = None
    m = _re.search(r'(?:sl|stop\s*loss)\s*[:\-–]?\s*([\d,.]+)', t, _re.I)
    if m:
        sl = float(m.group(1).replace(",", ""))

    # Leverage
    leverage = 10
    m = _re.search(r'(?:leverage|lev)\s*[:\-–]?\s*(\d+)\s*[xX]?', t, _re.I)
    if not m:
        m = _re.search(r'(\d+)\s*[xX]', t)
    if m:
        leverage = int(m.group(1))

    return {
        "symbol":   symbol,
        "side":     side,
        "entry":    entry,
        "tp":       tp,
        "tps":      [float(x.replace(",","")) for x in tps],
        "sl":       sl,
        "leverage": leverage,
        "raw":      t[:400],
    }


def _tg_execute_signal(sig: dict, cfg: dict) -> list:
    """Place orders on all configured exchanges. Returns list of results."""
    results = []
    for exname in cfg.get("exchanges", []):
        ex = _active_ex.get(exname.lower())
        if not ex:
            results.append({"exchange": exname, "ok": False, "error": "not connected"})
            continue
        try:
            lev = sig.get("leverage") or cfg.get("leverage", 10)
            amt = cfg.get("amount", 0.01)
            try:
                ex.set_leverage(lev, sig["symbol"])
            except Exception:
                pass
            order = ex.create_order(
                symbol=sig["symbol"],
                type="market",
                side=sig["side"],
                amount=amt,
            )
            results.append({
                "exchange": exname,
                "ok":       True,
                "order_id": order.get("id"),
                "side":     sig["side"],
                "amount":   amt,
            })
        except Exception as e:
            results.append({"exchange": exname, "ok": False, "error": str(e)[:150]})
    return results


def _tg_poll_loop():
    global _TG_OFFSET, _TG_RUNNING
    print("Telegram bot: polling started")
    while _TG_RUNNING:
        cfg = _load_tg_cfg()
        token = cfg.get("token", "")
        if not token:
            _time.sleep(5)
            continue
        try:
            resp = _tg_api(token, "getUpdates", {
                "offset":          _TG_OFFSET,
                "timeout":         20,
                "allowed_updates": ["message", "channel_post"],
            })
        except Exception:
            _time.sleep(5)
            continue

        if not resp.get("ok"):
            _time.sleep(10)
            continue

        for upd in resp.get("result", []):
            _TG_OFFSET = upd["update_id"] + 1
            # Get message from either personal chat or channel
            msg = upd.get("message") or upd.get("channel_post") or {}
            text = msg.get("text") or msg.get("caption") or ""
            chat_id = str(msg.get("chat", {}).get("id", ""))

            # Filter by allowed chat_id if configured
            allowed = cfg.get("chat_id", "").strip()
            if allowed and chat_id != allowed:
                continue

            if not text:
                continue

            sig = _parse_tg_signal(text)
            if not sig:
                continue

            # Build record
            ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rec  = {
                "time":     ts,
                "symbol":   sig["symbol"],
                "side":     sig["side"],
                "entry":    sig.get("entry"),
                "tp":       sig.get("tp"),
                "sl":       sig.get("sl"),
                "leverage": sig.get("leverage"),
                "raw":      sig.get("raw", "")[:200],
                "status":   "received",
                "results":  [],
                "chat_id":  chat_id,
            }

            if cfg.get("auto_trade") and cfg.get("enabled"):
                results = _tg_execute_signal(sig, cfg)
                rec["results"] = results
                rec["status"]  = "executed" if any(r["ok"] for r in results) else "failed"
            else:
                rec["status"] = "signal"   # received but not auto-traded

            _TG_SIGNALS.insert(0, rec)
            if len(_TG_SIGNALS) > 100:
                _TG_SIGNALS.pop()

            print(f"Telegram signal: {sig['side'].upper()} {sig['symbol']} → {rec['status']}")

    print("Telegram bot: polling stopped")


def _start_tg_bot():
    global _TG_RUNNING, _TG_THREAD
    if _TG_RUNNING:
        return
    _TG_RUNNING = True
    _TG_THREAD  = threading.Thread(target=_tg_poll_loop, daemon=True)
    _TG_THREAD.start()


def _stop_tg_bot():
    global _TG_RUNNING
    _TG_RUNNING = False


# Auto-start if config exists and enabled
try:
    _tg_startup_cfg = _load_tg_cfg()
    if _tg_startup_cfg.get("token") and _tg_startup_cfg.get("enabled"):
        _start_tg_bot()
except Exception:
    pass


class TgConfigReq(BaseModel):
    token:           str
    chat_id:         str   = ""
    exchanges:       list  = ["binance"]
    auto_trade:      bool  = False
    amount:          float = 0.01
    leverage:        int   = 10
    enabled:         bool  = True
    notify_enabled:  bool  = True
    drawdown_pct:    float = 5.0


@app.post("/api/telegram/config")
def tg_config_save(req: TgConfigReq):
    cfg = req.model_dump()
    _save_tg_cfg(cfg)
    if cfg["enabled"] and cfg["token"]:
        _stop_tg_bot()
        _time.sleep(0.5)
        _start_tg_bot()
        return {"ok": True, "status": "Bot started"}
    else:
        _stop_tg_bot()
        return {"ok": True, "status": "Bot stopped"}


@app.get("/api/telegram/config")
def tg_config_get():
    cfg = _load_tg_cfg()
    cfg.pop("token", None)   # don't expose token via GET
    cfg["running"] = _TG_RUNNING
    return cfg


@app.get("/api/telegram/status")
def tg_status():
    cfg = _load_tg_cfg()
    return {
        "running":    _TG_RUNNING,
        "enabled":    cfg.get("enabled", False),
        "auto_trade": cfg.get("auto_trade", False),
        "exchanges":  cfg.get("exchanges", []),
        "chat_id":    cfg.get("chat_id", ""),
        "has_token":  bool(cfg.get("token")),
    }


@app.get("/api/telegram/signals")
def tg_signals_list():
    return _TG_SIGNALS[:50]


@app.post("/api/telegram/test")
def tg_test(req: TgConfigReq):
    """Verify the bot token is valid."""
    r = _tg_api(req.token, "getMe")
    if r.get("ok"):
        return {"ok": True, "bot_name": r["result"].get("username")}
    return JSONResponse(status_code=400, content={"error": r.get("error", "Invalid token")})


@app.post("/api/telegram/execute/{idx}")
def tg_execute_manual(idx: int):
    """Manually execute a received signal by index."""
    if idx < 0 or idx >= len(_TG_SIGNALS):
        return JSONResponse(status_code=404, content={"error": "Signal not found"})
    rec = _TG_SIGNALS[idx]
    cfg = _load_tg_cfg()
    sig = {
        "symbol":   rec["symbol"],
        "side":     rec["side"],
        "leverage": rec.get("leverage", 10),
    }
    results = _tg_execute_signal(sig, cfg)
    rec["results"] = results
    rec["status"]  = "executed" if any(r["ok"] for r in results) else "failed"
    return {"ok": True, "results": results}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
