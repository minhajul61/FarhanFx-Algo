import asyncio
import hashlib
import json
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


def _mt5_worker():
    """Runs forever on its own thread, executing MT5 calls."""
    # timeout=8000ms — don't hang if MT5 terminal is not running yet
    ok = mt5.initialize(timeout=8000)
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
        if not mt5.initialize(timeout=10000):
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
        return {"date": None, "balance": None}

def _save_today_start(date_str, balance):
    try:
        with open(_TODAY_FILE, "w") as f:
            _json.dump({"date": date_str, "balance": balance}, f)
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

        if stored["date"] != today_str or stored["balance"] is None:
            _save_today_start(today_str, info.balance)
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
def get_reports(months: int = 12):
    """Full analytics: monthly, daily, symbol, day-of-week, session breakdown."""
    def fn():
        now        = datetime.now()
        date_from  = now - timedelta(days=months * 31)
        # Fetch full history for monthly chart (up to 10 years back)
        date_wide  = datetime(max(now.year - 10, 2010), 1, 1)
        deals_all  = mt5.history_deals_get(date_wide, now + timedelta(seconds=60))
        if deals_all is None:
            return {"monthly": [], "monthly_chart": [], "by_symbol": [], "by_session": [],
                    "daily_pnl": [], "summary": {}}

        cutoff_ts = date_from.timestamp()

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

                if d.time >= cutoff_ts:
                    out_list.append(d)

        # ── Accumulators ──────────────────────────────────────────────────────
        monthly:    dict = {}
        by_symbol:  dict = {}
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
            "summary":       summary,
            "monthly":       monthly_list,
            "monthly_chart": monthly_chart_list,
            "daily":         daily_list[:60],
            "by_symbol":     sym_list,
            "by_dow":        dow_list,
            "by_session":    sess_list,
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
            add_log(f"✅ {side} #{result.order} @ {result.price}  SL:{sl_val}  TP:{tp_val}")
            _strategies[sid]["trades"] += 1
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

            if strategy == "MA Cross":
                fast = _ema(closes, 20)
                slow = _ema(closes, 50)
                if fast[-2] < slow[-2] and fast[-1] > slow[-1]:
                    signal = "BUY"
                elif fast[-2] > slow[-2] and fast[-1] < slow[-1]:
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

            elif strategy == "Supertrend":
                direction = _supertrend(highs, lows, closes, period=10, mult=3.0)
                trend_txt = "▲ BULLISH" if direction[-1] == 1 else "▼ BEARISH"
                _strategies[sid]["indicator"] = f"Supertrend: {trend_txt}"
                # Direction flip = entry signal
                if direction[-2] == -1 and direction[-1] == 1:
                    signal = "BUY"
                elif direction[-2] == 1 and direction[-1] == -1:
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

                if buy_pts >= 65 and buy_pts > sell_pts:
                    signal = "BUY"
                elif sell_pts >= 65 and sell_pts > buy_pts:
                    signal = "SELL"

            if signal:
                is_buy    = signal == "BUY"
                entry     = tick.ask if is_buy else tick.bid
                # SL/TP reference from BID (buy closes at bid) or ASK (sell closes at ask)
                sl_tp_ref = tick.bid if is_buy else tick.ask
                sl_dist   = max(sl_pips * pip, min_dist + sym_info.point)
                tp_dist   = max(tp_pips * pip, min_dist + sym_info.point)
                sl_val    = round(sl_tp_ref - sl_dist, sym_info.digits) if is_buy \
                            else round(sl_tp_ref + sl_dist, sym_info.digits)
                tp_val    = round(sl_tp_ref + tp_dist, sym_info.digits) if is_buy \
                            else round(sl_tp_ref - tp_dist, sym_info.digits)
                add_log(f"📊 Signal: {signal} @ {entry}  SL:{sl_val}  TP:{tp_val}  (pip={pip})")
                do_trade(signal, entry, sl_val, tp_val)

            # Update running P&L from deal history
            def _update_pnl():
                from datetime import timedelta as _td
                df = datetime.now() - _td(days=30)
                deals = mt5.history_deals_get(df, datetime.now())
                if not deals:
                    return 0.0
                total = 0.0
                for d in deals:
                    if d.entry == 1 and (d.comment or "").startswith(f"FarhanFX-{strategy}") and d.symbol == symbol:
                        total += d.profit + d.commission + d.swap
                return round(total, 2)
            try:
                _strategies[sid]["pnl"] = _mt5_call(_update_pnl)
            except Exception:
                pass

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
        "pnl":       0.0,
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
            "sl":        s["config"].get("sl", 0),
            "tp":        s["config"].get("tp", 0),
            "status":    s["status"],
            "trades":    s["trades"],
            "pnl":       round(s.get("pnl", 0.0), 2),
            "indicator": s.get("indicator", ""),
            "started":   s["started"],
            "log":       s["log"][-20:],
        })
    return result


@app.get("/api/algo/history")
def get_algo_history(days: int = 30):
    def fn():
        date_from = datetime.now() - timedelta(days=days)
        deals = mt5.history_deals_get(date_from, datetime.now())
        if deals is None:
            return []
        # Build position_id -> open deal map for entry prices
        open_map: dict = {}
        closed = []
        for d in deals:
            if d.type not in (0, 1):
                continue
            if not (d.comment or "").startswith("FarhanFX"):
                continue
            if d.entry == 0:   # IN (opening)
                open_map[d.position_id] = d
            elif d.entry == 1: # OUT (closing)
                closed.append(d)
        result = []
        for d in closed:
            open_d = open_map.get(d.position_id)
            result.append({
                "ticket":      d.position_id,
                "symbol":      d.symbol,
                "type":        "BUY" if (open_d.type if open_d else d.type) == 0 else "SELL",
                "volume":      d.volume,
                "entry_price": open_d.price if open_d else None,
                "exit_price":  d.price,
                "profit":      round(d.profit + d.commission + d.swap, 2),
                "comment":     d.comment,
                "time":        datetime.fromtimestamp(d.time).strftime("%Y-%m-%d %H:%M"),
            })
        result.sort(key=lambda x: x["time"], reverse=True)
        return result[:200]
    return _mt5_call(fn)

@app.get("/api/ai/analyze/{symbol}")
def ai_analyze(symbol: str, tf: str = "H1"):
    """Full multi-timeframe AI market analysis.
    Calls _get_rates directly (each call uses its own _mt5_call internally).
    Never nests _mt5_call — that deadlocks the single worker thread.
    """
    # Step 1: fetch rates — each _get_rates uses _mt5_call internally, called sequentially
    primary_rates, sym = _get_rates(symbol, tf, 200)
    h4_rates,      _   = _get_rates(symbol, "H4", 100)

    if primary_rates is None or len(primary_rates) < 30:
        return {"error": f"Not enough data for {symbol}"}

    # Step 2: single _mt5_call for tick + symbol info only
    def get_tick_info():
        return mt5.symbol_info_tick(sym), mt5.symbol_info(sym)
    ti = _mt5_call(get_tick_info)
    tick, sym_inf = (ti if not isinstance(ti, dict) else (None, None))

    # Step 3: all calculations in pure Python — no more MT5 calls
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

    # H4 trend bias (pure Python after rates fetched)
    h4_bias = "NEUTRAL"
    if h4_rates is not None and len(h4_rates) >= 50:
        h4c    = [float(r["close"]) for r in h4_rates]
        h4e50  = _ema(h4c, 50)
        h4e200 = _ema(h4c, min(200, len(h4c)-1))
        if h4c[-1] > h4e50[-1] > h4e200[-1]:   h4_bias = "BULLISH"
        elif h4c[-1] < h4e50[-1] < h4e200[-1]: h4_bias = "BEARISH"

    # ── SIGNAL SCORING ──────────────────────────────────────────────────────────
    components = []

    # 1. H4 Trend (25 pts)
    if h4_bias == "BULLISH":
        components.append({"name":"H4 Trend","dir":"BUY","score":25,"max":25,"detail":"H4 price > EMA50 > EMA200"})
    elif h4_bias == "BEARISH":
        components.append({"name":"H4 Trend","dir":"SELL","score":25,"max":25,"detail":"H4 price < EMA50 < EMA200"})
    else:
        components.append({"name":"H4 Trend","dir":"NEUTRAL","score":0,"max":25,"detail":"No clear H4 alignment"})

    # 2. EMA Stack (20 pts)
    if price > ema20[-1] > ema50[-1]:
        sc = 20 if price > ema200[-1] else 12
        components.append({"name":"EMA Stack","dir":"BUY","score":sc,"max":20,"detail":f"Price>{ema20[-1]:.{digits}f}>{ema50[-1]:.{digits}f}"})
    elif price < ema20[-1] < ema50[-1]:
        sc = 20 if price < ema200[-1] else 12
        components.append({"name":"EMA Stack","dir":"SELL","score":sc,"max":20,"detail":f"Price<{ema20[-1]:.{digits}f}<{ema50[-1]:.{digits}f}"})
    else:
        components.append({"name":"EMA Stack","dir":"NEUTRAL","score":0,"max":20,"detail":"EMAs not aligned"})

    # 3. Supertrend (20 pts)
    if st_dir[-1] == 1:
        components.append({"name":"Supertrend","dir":"BUY","score":20,"max":20,"detail":"Direction: BULLISH ▲"})
    else:
        components.append({"name":"Supertrend","dir":"SELL","score":20,"max":20,"detail":"Direction: BEARISH ▼"})

    # 4. MACD Histogram (15 pts)
    if hist[-1] > 0 and hist[-1] >= hist[-2]:
        components.append({"name":"MACD","dir":"BUY","score":15,"max":15,"detail":f"Histogram rising: {hist[-1]:+.5f}"})
    elif hist[-1] < 0 and hist[-1] <= hist[-2]:
        components.append({"name":"MACD","dir":"SELL","score":15,"max":15,"detail":f"Histogram falling: {hist[-1]:+.5f}"})
    else:
        components.append({"name":"MACD","dir":"NEUTRAL","score":0,"max":15,"detail":f"Histogram flat: {hist[-1]:+.5f}"})

    # 5. RSI Zone (10 pts)
    if 50 <= rsi14 <= 68:
        components.append({"name":"RSI","dir":"BUY","score":10,"max":10,"detail":f"RSI {rsi14} — bullish zone"})
    elif 32 <= rsi14 <= 50:
        components.append({"name":"RSI","dir":"SELL","score":10,"max":10,"detail":f"RSI {rsi14} — bearish zone"})
    else:
        components.append({"name":"RSI","dir":"NEUTRAL","score":0,"max":10,"detail":f"RSI {rsi14} — extreme"})

    # 6. Stochastic (10 pts)
    if kv[-2] < dv[-2] and kv[-1] > dv[-1] and kv[-1] < 60:
        components.append({"name":"Stochastic","dir":"BUY","score":10,"max":10,"detail":f"%K {kv[-1]:.0f} crossed above %D {dv[-1]:.0f}"})
    elif kv[-2] > dv[-2] and kv[-1] < dv[-1] and kv[-1] > 40:
        components.append({"name":"Stochastic","dir":"SELL","score":10,"max":10,"detail":f"%K {kv[-1]:.0f} crossed below %D {dv[-1]:.0f}"})
    else:
        components.append({"name":"Stochastic","dir":"NEUTRAL","score":0,"max":10,"detail":f"%K:{kv[-1]:.0f}  %D:{dv[-1]:.0f}"})

    # ── FINAL SCORE ─────────────────────────────────────────────────────────────
    buy_score  = sum(c["score"] for c in components if c["dir"] == "BUY")
    sell_score = sum(c["score"] for c in components if c["dir"] == "SELL")
    max_total  = sum(c["max"]   for c in components)  # always 100

    buy_pct  = round(buy_score  / max_total * 100)
    sell_pct = round(sell_score / max_total * 100)

    if buy_pct >= 60 and buy_pct > sell_pct:
        final_signal = "BUY";  confidence = buy_pct
    elif sell_pct >= 60 and sell_pct > buy_pct:
        final_signal = "SELL"; confidence = sell_pct
    else:
        final_signal = "HOLD"; confidence = max(buy_pct, sell_pct)

    return {
        "symbol":     sym,
        "tf":         tf,
        "price":      round(price, digits),
        "bid":        round(tick.bid, digits) if tick else round(price, digits),
        "ask":        round(tick.ask, digits) if tick else round(price, digits),
        "signal":     final_signal,
        "confidence": confidence,
        "buy_score":  buy_pct,
        "sell_score": sell_pct,
        "h4_bias":    h4_bias,
        "rsi":        rsi14,
        "atr":        round(atr_v, digits),
        "supertrend": "BULLISH" if st_dir[-1] == 1 else "BEARISH",
        "components": components,
    }


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


# ── CRYPTO EXCHANGE (Binance Futures + Bybit Perpetual) ─────────────────────────
try:
    import ccxt as _ccxt
    _CCXT_OK = True
except ImportError:
    _ccxt = None
    _CCXT_OK = False
    print("⚠  ccxt not installed — run: pip install ccxt")

_EX_FILE     = "exchanges.json"
_active_ex   = {}          # {"binance": ccxt.Exchange, "bybit": ccxt.Exchange}


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
    if not _CCXT_OK:
        raise RuntimeError("ccxt not installed — pip install ccxt")
    name = name.lower()
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
        return JSONResponse(400, {"error": "ccxt not installed — pip install ccxt"})
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
        return JSONResponse(400, {"error": "Invalid API key or secret — check credentials"})
    except _ccxt.NetworkError as e:
        return JSONResponse(400, {"error": f"Network error — {str(e)[:120]}"})
    except Exception as e:
        return JSONResponse(400, {"error": str(e)[:200]})


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
        "binance": "binance" in _active_ex,
        "bybit":   "bybit"   in _active_ex,
        "saved":   list(cfg.keys()),
    }


@app.get("/api/crypto/balance")
def crypto_balance(exchange: str = "binance"):
    ex = _active_ex.get(exchange.lower())
    if not ex:
        return JSONResponse(400, {"error": f"{exchange} not connected"})
    try:
        bal  = ex.fetch_balance()
        usdt = bal.get("USDT", {})
        # Unrealized PnL (Binance futures returns it in info)
        upnl = 0
        try:
            info = bal.get("info", {})
            upnl = round(float(
                info.get("totalUnrealizedProfit") or
                info.get("result", {}).get("list", [{}])[0].get("totalUnrealisedPnl", 0)
            ), 2)
        except: pass
        return {
            "free":  round(float(usdt.get("free",  0)), 2),
            "used":  round(float(usdt.get("used",  0)), 2),
            "total": round(float(usdt.get("total", 0)), 2),
            "upnl":  upnl,
        }
    except Exception as e:
        return JSONResponse(400, {"error": str(e)[:200]})


@app.get("/api/crypto/positions")
def crypto_positions(exchange: str = "binance"):
    ex = _active_ex.get(exchange.lower())
    if not ex:
        return JSONResponse(400, {"error": f"{exchange} not connected"})
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
        return JSONResponse(400, {"error": str(e)[:200]})


@app.get("/api/crypto/orders")
def crypto_orders(exchange: str = "binance", symbol: str = "BTC/USDT:USDT"):
    ex = _active_ex.get(exchange.lower())
    if not ex:
        return JSONResponse(400, {"error": f"{exchange} not connected"})
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
        return JSONResponse(400, {"error": str(e)[:200]})


@app.post("/api/crypto/order")
def crypto_order(req: CryptoOrderReq):
    ex = _active_ex.get(req.exchange.lower())
    if not ex:
        return JSONResponse(400, {"error": f"{req.exchange} not connected"})
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
        return JSONResponse(400, {"error": "Insufficient USDT margin"})
    except _ccxt.InvalidOrder as e:
        return JSONResponse(400, {"error": f"Invalid order — {str(e)[:120]}"})
    except Exception as e:
        return JSONResponse(400, {"error": str(e)[:200]})


@app.post("/api/crypto/close")
def crypto_close(req: CryptoCloseReq):
    ex = _active_ex.get(req.exchange.lower())
    if not ex:
        return JSONResponse(400, {"error": f"{req.exchange} not connected"})
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
        return JSONResponse(400, {"error": str(e)[:200]})


@app.post("/api/crypto/leverage")
def crypto_set_leverage(req: CryptoLeverageReq):
    ex = _active_ex.get(req.exchange.lower())
    if not ex:
        return JSONResponse(400, {"error": f"{req.exchange} not connected"})
    try:
        ex.set_leverage(req.leverage, req.symbol)
        return {"success": True, "leverage": req.leverage}
    except Exception as e:
        return JSONResponse(400, {"error": str(e)[:200]})


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


@app.get("/")
def serve_index():
    return FileResponse("index.html", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    })

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
