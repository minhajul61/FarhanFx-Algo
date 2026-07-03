import asyncio
import base64
import hashlib
import json
import os
import secrets
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel


# ── FASTAPI APP ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load persisted Telegram signals from previous sessions
    _TG_SIGNALS.extend(_load_tg_signals())
    # Restore saved crypto exchange connections in background
    threading.Thread(target=_restore_exchanges, daemon=True).start()
    # Restore Indian Market bots (deferred so _indian_bots dict is ready)
    threading.Thread(target=_load_indian_bots, daemon=True).start()
    print("FarhanFX Algo API — http://127.0.0.1:8000")
    yield


app = FastAPI(title="FarhanFX Algo API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_CLIENT_ALLOWED_PREFIXES = ("/api/auth/", "/api/crypto/", "/api/payment/")

@app.middleware("http")
async def _role_gate(request: Request, call_next):
    """Client-role accounts (self-registered crypto users) may only reach
    /api/auth/*, /api/crypto/*, and /api/payment/* (submitting payment proof)
    — everything else (MT5/forex/strategies/reports, the owner's personal
    trading data, /api/admin/*) is off-limits to them. Admin sessions,
    unauthenticated requests, and unrecognized tokens are left untouched —
    this only adds a new restriction for the new client-role tokens this
    feature introduces."""
    path = request.url.path
    if path.startswith("/api/") and not path.startswith(_CLIENT_ALLOWED_PREFIXES):
        token = (request.headers.get("authorization") or "").replace("Bearer ", "").strip()
        sess = _auth_sessions.get(token)
        if sess and sess.get("role") == "client":
            return JSONResponse({"error": "Forbidden"}, status_code=403)
    return await call_next(request)


# ── ACCOUNT stub — MT5 removed; watchdog polls this endpoint and expects HTTP 200 ─

@app.get("/api/account")
def get_account():
    """Stub: MT5 forex trading has been removed from this server.
    Returns HTTP 200 so the watchdog at C:\\FarhanFX\\watchdog.ps1 keeps the
    server running (it restarts the server on non-200 responses)."""
    return JSONResponse({"error": "MT5 not available"}, status_code=200)


# ── USER AUTH ───────────────────────────────────────────────────────────────────

_USERS_FILE         = "users.json"
_AUTH_SESSIONS_FILE = "auth_sessions.json"


def _load_auth_sessions() -> dict:
    try:
        with open(_AUTH_SESSIONS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_auth_sessions():
    try:
        with open(_AUTH_SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(_auth_sessions, f, indent=2)
    except Exception:
        pass


# Sessions used to live in memory only, so every server restart (e.g. every
# deploy) silently logged everyone out — persisting them here means a
# restart no longer forces a fresh login.
_auth_sessions: dict = _load_auth_sessions()   # token -> {"username": str, "created": str}

class AuthLoginRequest(BaseModel):
    username: str
    password: str

class AuthRegisterRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""
    contact: str = ""           # phone / Telegram / email — how to reach this client
    risk_accepted: bool = False

class AuthChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

def _hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()

_CLIENT_TRIAL_DAYS = 7  # default free trial length for self-registered clients

def _is_client_expired(user: dict) -> bool:
    """Admin accounts never expire. Clients with no expiry set are treated as
    expired (defensive — every client should get one at registration)."""
    if user.get("role") != "client":
        return False
    expiry = user.get("expiry")
    if not expiry:
        return True
    try:
        return datetime.fromisoformat(expiry) <= datetime.now()
    except Exception:
        return True

def _days_left(user: dict):
    """Whole days remaining until expiry, or None for admin/no-expiry accounts."""
    if user.get("role") != "client" or not user.get("expiry"):
        return None
    try:
        delta = datetime.fromisoformat(user["expiry"]) - datetime.now()
        return max(0, delta.days)
    except Exception:
        return None

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
            "display_name": "Admin",
            "role": "admin",
        }]
        _save_users(data)
    else:
        # Migration: existing accounts created before "role" existed default to admin —
        # this is the owner's own pre-existing account, never a self-registered client.
        changed = False
        for u in data["users"]:
            if "role" not in u:
                u["role"] = "admin"
                changed = True
        if changed:
            _save_users(data)

_ensure_default_user()

def _make_session(username: str, display_name: str, role: str) -> str:
    token = secrets.token_urlsafe(32)
    _auth_sessions[token] = {"username": username, "display_name": display_name,
                              "role": role, "created": datetime.now().isoformat()}
    _save_auth_sessions()
    return token

@app.post("/api/auth/login")
def auth_login(req: AuthLoginRequest):
    data  = _load_users()
    user  = next((u for u in data.get("users", []) if u["username"] == req.username), None)
    if not user or _hash_pw(req.password, user["salt"]) != user["password_hash"]:
        return JSONResponse({"error": "Invalid username or password"}, status_code=401)
    if _is_client_expired(user):
        return JSONResponse({"error": "Service expired — contact admin to renew"}, status_code=403)
    display_name = user.get("display_name", req.username)
    role = user.get("role", "admin")
    token = _make_session(req.username, display_name, role)
    return {"token": token, "username": req.username, "display_name": display_name, "role": role,
            "days_left": _days_left(user)}

@app.post("/api/auth/register")
def auth_register(req: AuthRegisterRequest):
    username = req.username.strip()
    if not username or not req.password:
        return JSONResponse({"error": "Username and password are required"}, status_code=400)
    if len(req.password) < 6:
        return JSONResponse({"error": "Password must be at least 6 characters"}, status_code=400)
    if not req.contact.strip():
        return JSONResponse({"error": "A contact (phone, Telegram, or email) is required"}, status_code=400)
    if not req.risk_accepted:
        return JSONResponse({"error": "You must accept the risk disclaimer to register"}, status_code=400)
    data = _load_users()
    if any(u["username"].lower() == username.lower() for u in data.get("users", [])):
        return JSONResponse({"error": "Username already taken"}, status_code=400)
    salt = secrets.token_hex(16)
    display_name = req.display_name.strip() or username
    expiry = (datetime.now() + timedelta(days=_CLIENT_TRIAL_DAYS)).isoformat()
    # Self-registration always creates a client account — never admin.
    data.setdefault("users", []).append({
        "username": username,
        "salt": salt,
        "password_hash": _hash_pw(req.password, salt),
        "display_name": display_name,
        "role": "client",
        "expiry": expiry,
        "contact": req.contact.strip(),
        "risk_accepted_at": datetime.now().isoformat(),
    })
    _save_users(data)
    token = _make_session(username, display_name, "client")
    return {"token": token, "username": username, "display_name": display_name, "role": "client", "expiry": expiry}

@app.get("/api/auth/verify")
def auth_verify(authorization: str = Header(default=None)):
    token = (authorization or "").replace("Bearer ", "").strip()
    sess  = _auth_sessions.get(token)
    if not sess:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    # Re-check expiry against the live user record (not the cached session) so an
    # admin revoking/shortening access takes effect on the client's next page load.
    data = _load_users()
    user = next((u for u in data.get("users", []) if u["username"] == sess["username"]), None)
    if user and _is_client_expired(user):
        _auth_sessions.pop(token, None)
        _save_auth_sessions()
        return JSONResponse({"error": "Service expired — contact admin to renew"}, status_code=403)
    return {"username": sess["username"], "display_name": sess.get("display_name", sess["username"]),
            "role": sess.get("role", "admin"), "days_left": _days_left(user) if user else None}

@app.post("/api/auth/logout")
def auth_logout(authorization: str = Header(default=None)):
    token = (authorization or "").replace("Bearer ", "").strip()
    _auth_sessions.pop(token, None)
    _save_auth_sessions()
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

def _get_current_user(authorization: str = Header(default=None)) -> dict:
    token = (authorization or "").replace("Bearer ", "").strip()
    sess  = _auth_sessions.get(token)
    if not sess:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return sess

def _require_admin(current_user: dict = Depends(_get_current_user)) -> dict:
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return current_user


class SetClientExpiryRequest(BaseModel):
    days: int   # access expires `days` from now (0 = revoke immediately)

@app.get("/api/admin/clients")
def admin_list_clients(admin: dict = Depends(_require_admin)):
    data = _load_users()
    clients = []
    for u in data.get("users", []):
        if u.get("role") != "client":
            continue
        clients.append({
            "username":     u["username"],
            "display_name": u.get("display_name", u["username"]),
            "contact":      u.get("contact", ""),
            "expiry":       u.get("expiry"),
            "expired":      _is_client_expired(u),
            "days_left":    _days_left(u),
        })
    clients.sort(key=lambda c: c["expiry"] or "")
    return clients

@app.post("/api/admin/clients/{username}/set_expiry")
def admin_set_client_expiry(username: str, req: SetClientExpiryRequest, admin: dict = Depends(_require_admin)):
    data = _load_users()
    user = next((u for u in data.get("users", []) if u["username"] == username), None)
    if not user or user.get("role") != "client":
        return JSONResponse({"error": "Client not found"}, status_code=404)
    expiry = (datetime.now() + timedelta(days=req.days)).isoformat()
    user["expiry"] = expiry
    _save_users(data)
    return {"success": True, "username": username, "expiry": expiry}


# ── PAYMENT PROOF SUBMISSION / APPROVAL ─────────────────────────────────────────
# Client uploads a payment screenshot (sent as base64 JSON, no multipart dep
# needed); admin reviews it and approves to set the client's expiry, or rejects.
_PAYMENT_FILE        = "payment_requests.json"
_PAYMENT_PROOFS_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "payment_proofs")
_MAX_PROOF_BYTES     = 5 * 1024 * 1024  # 5MB

def _load_payment_requests() -> dict:
    try:
        with open(_PAYMENT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_payment_requests(data: dict):
    with open(_PAYMENT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

class PaymentSubmitRequest(BaseModel):
    days_requested: int
    image_base64:   str   # raw base64 or a data: URL — either is accepted
    note:           str = ""

@app.post("/api/payment/submit")
def payment_submit(req: PaymentSubmitRequest, current_user: dict = Depends(_get_current_user)):
    if req.days_requested <= 0:
        return JSONResponse({"error": "Invalid number of days"}, status_code=400)
    raw = req.image_base64
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        img_bytes = base64.b64decode(raw)
    except Exception:
        return JSONResponse({"error": "Invalid image data"}, status_code=400)
    if not img_bytes:
        return JSONResponse({"error": "Image is empty"}, status_code=400)
    if len(img_bytes) > _MAX_PROOF_BYTES:
        return JSONResponse({"error": "Image too large (max 5MB)"}, status_code=400)
    os.makedirs(_PAYMENT_PROOFS_DIR, exist_ok=True)
    req_id = str(uuid.uuid4())[:8]
    fname  = f"{req_id}.jpg"
    with open(os.path.join(_PAYMENT_PROOFS_DIR, fname), "wb") as f:
        f.write(img_bytes)
    data = _load_payment_requests()
    data[req_id] = {
        "id": req_id, "username": current_user["username"],
        "days_requested": req.days_requested, "note": req.note.strip()[:300],
        "image_file": fname, "status": "pending",
        "submitted_at": datetime.now().isoformat(),
    }
    _save_payment_requests(data)
    return {"success": True, "request_id": req_id}

@app.get("/api/payment/my_status")
def payment_my_status(current_user: dict = Depends(_get_current_user)):
    users = _load_users()
    user = next((u for u in users.get("users", []) if u["username"] == current_user["username"]), None)
    if not user:
        return JSONResponse({"error": "Account not found"}, status_code=404)
    return {
        "username":  user["username"],
        "role":      user.get("role", "admin"),
        "expiry":    user.get("expiry"),
        "expired":   _is_client_expired(user),
        "days_left": _days_left(user),
    }

@app.get("/api/payment/my_requests")
def payment_my_requests(current_user: dict = Depends(_get_current_user)):
    data = _load_payment_requests()
    mine = [r for r in data.values() if r["username"] == current_user["username"]]
    return sorted(mine, key=lambda r: r["submitted_at"], reverse=True)

@app.get("/api/admin/payment_requests")
def admin_list_payment_requests(admin: dict = Depends(_require_admin)):
    data = _load_payment_requests()
    return sorted(data.values(), key=lambda r: r["submitted_at"], reverse=True)

@app.get("/api/admin/payment_requests/{request_id}/image")
def admin_view_payment_image(request_id: str, admin: dict = Depends(_require_admin)):
    data = _load_payment_requests()
    req = data.get(request_id)
    if not req:
        return JSONResponse({"error": "Not found"}, status_code=404)
    path = os.path.join(_PAYMENT_PROOFS_DIR, req["image_file"])
    if not os.path.exists(path):
        return JSONResponse({"error": "Image file missing"}, status_code=404)
    return FileResponse(path)

@app.post("/api/admin/payment_requests/{request_id}/approve")
def admin_approve_payment(request_id: str, admin: dict = Depends(_require_admin)):
    data = _load_payment_requests()
    req = data.get(request_id)
    if not req or req["status"] != "pending":
        return JSONResponse({"error": "Request not found or already processed"}, status_code=404)
    users = _load_users()
    user = next((u for u in users.get("users", []) if u["username"] == req["username"]), None)
    if not user:
        return JSONResponse({"error": "Client account no longer exists"}, status_code=404)
    expiry = (datetime.now() + timedelta(days=req["days_requested"])).isoformat()
    user["expiry"] = expiry
    _save_users(users)
    req["status"]      = "approved"
    req["resolved_at"]  = datetime.now().isoformat()
    _save_payment_requests(data)
    return {"success": True, "expiry": expiry}

@app.post("/api/admin/payment_requests/{request_id}/reject")
def admin_reject_payment(request_id: str, admin: dict = Depends(_require_admin)):
    data = _load_payment_requests()
    req = data.get(request_id)
    if not req or req["status"] != "pending":
        return JSONResponse({"error": "Request not found or already processed"}, status_code=404)
    req["status"]     = "rejected"
    req["resolved_at"] = datetime.now().isoformat()
    _save_payment_requests(data)
    return {"success": True}


@app.post("/api/admin/restart")
def admin_restart(admin: dict = Depends(_require_admin)):
    """Restart the server process in-place (admin only)."""
    import sys, os
    def _do():
        import time
        time.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=_do, daemon=False).start()
    return {"status": "restarting"}


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


def _set_margin_mode_safe(ex, symbol: str, mode: str = "isolated"):
    """Best-effort isolated-margin guard so one bad leveraged trade can't
    draw down the whole account. Silently no-ops on failure (some symbols
    reject the call if a position is already open, which is fine — it
    just means the mode was set on an earlier call) and on CoinSwitch,
    whose wrapper doesn't expose a margin-mode endpoint here — verify
    isolated margin manually in the CoinSwitch app for that case."""
    if isinstance(ex, CoinSwitchClient):
        return
    try:
        ex.set_margin_mode(mode, symbol)
    except Exception:
        pass


_EX_FILE   = "exchanges.json"
_active_ex = {}   # {username: {"binance": ccxt.Exchange, "bybit": ..., "coinswitch": CoinSwitchClient}}


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
    """Returns {username: {exchange_name: {api_key, api_secret, testnet}}}.
    Migrates the old single-tenant flat format ({exchange_name: config}) to
    {"admin": <old dict>} the first time it's loaded, so the owner's existing
    connection survives the multi-tenant upgrade."""
    try:
        with open(_EX_FILE) as f: data = json.load(f)
    except: return {}
    if data and all(isinstance(v, dict) and "api_key" in v for v in data.values()):
        data = {"admin": data}
        _save_ex_cfg(data)
    return data

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
    """Try to reconnect every user's saved exchanges on server startup."""
    if not _CCXT_OK:
        return
    cfg = _load_ex_cfg()
    for username, exchanges in cfg.items():
        for name, info in exchanges.items():
            try:
                ex = _build_exchange(name, info["api_key"], info["api_secret"], info.get("testnet", False))
                ex.fetch_balance()
                _active_ex.setdefault(username, {})[name] = ex
                print(f"Crypto: {username}/{name} restored ✓")
            except Exception as e:
                print(f"Crypto: {username}/{name} restore failed — {e}")
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
            # Each bot loads independently — one bad/corrupt entry must never
            # abort the loop and silently drop every bot that comes after it
            # (this happened in practice: a UnicodeEncodeError printing the
            # "resumed" line for one bot killed the whole load on Windows
            # consoles without UTF-8 stdout).
            try:
                # Migrate old bots missing new fields
                bot.setdefault("max_open_trades", 2)
                bot.setdefault("open_trade_count", 1 if bot.get("open_side") else 0)
                bot.setdefault("username", "admin")  # pre-multi-tenant bots belong to the owner
                bot.setdefault("margin_mode", "isolated")
                bot.setdefault("mode", "live")   # pre-existing bots were all real-money; default accordingly
                bot.setdefault("demo_balance", 1000.0)
                bot.setdefault("demo_equity", bot.get("demo_balance", 1000.0))
                bot.setdefault("open_amount", 0)
                bot.setdefault("bo_lookback", 20)
                bot.setdefault("dc_period", 55)
                bot.setdefault("dc_ema", 150)
                bot.setdefault("fixed_amount", 0.0)
                bot.setdefault("fixed_usd", 0.0)
                bot.setdefault("vwap_period", 14)
                bot.setdefault("vwap_std", 2.5)
                bot.setdefault("group_id", bid)   # pre-multi-coin bots are their own group of one
                bot.setdefault("group_name", "")
                _crypto_bots[bid] = bot
                if bot.get('status') == 'active':
                    delay = 15 + keys.index(bid) * 3
                    t = threading.Timer(delay, _bot_tick, args=[bid])
                    t.daemon = True
                    t.start()
                    _bot_timers[bid] = t
                    try:
                        print(f"Algo bot {bid} ({bot.get('strategy')} {bot.get('symbol')}) resumed [OK]")
                    except Exception:
                        pass
            except Exception as e:
                print(f"_load_saved_bots: failed to load bot {bid}: {e}")
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
def crypto_connect(req: ExConnectReq, current_user: dict = Depends(_get_current_user)):
    if not _CCXT_OK:
        return JSONResponse(status_code=400, content={"error": "ccxt not installed — pip install ccxt"})
    try:
        ex  = _build_exchange(req.exchange, req.api_key, req.api_secret, req.testnet)
        bal = ex.fetch_balance()
        uname = current_user["username"]
        _active_ex.setdefault(uname, {})[req.exchange.lower()] = ex
        cfg = _load_ex_cfg()
        cfg.setdefault(uname, {})[req.exchange.lower()] = {
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
def crypto_disconnect(exchange: str, current_user: dict = Depends(_get_current_user)):
    uname = current_user["username"]
    _active_ex.get(uname, {}).pop(exchange.lower(), None)
    cfg = _load_ex_cfg()
    cfg.get(uname, {}).pop(exchange.lower(), None)
    _save_ex_cfg(cfg)
    return {"success": True}


@app.get("/api/crypto/status")
def crypto_status(current_user: dict = Depends(_get_current_user)):
    uname = current_user["username"]
    cfg = _load_ex_cfg()
    mine = _active_ex.get(uname, {})
    return {
        "binance":     "binance"     in mine,
        "bybit":       "bybit"       in mine,
        "coinswitch":  "coinswitch"  in mine,
        "saved":       list(cfg.get(uname, {}).keys()),
    }


@app.get("/api/crypto/debug_raw")
def crypto_debug_raw(exchange: str = "coinswitch", path: str = "/trade/api/v2/futures/wallet_balance",
                      current_user: dict = Depends(_get_current_user)):
    ex = _active_ex.get(current_user["username"], {}).get(exchange.lower())
    if not ex:
        return JSONResponse(status_code=400, content={"error": f"{exchange} not connected"})
    try:
        if isinstance(ex, CoinSwitchClient):
            return ex._get(path)
        return {"error": "not a CoinSwitch client"}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.get("/api/crypto/balance")
def crypto_balance(exchange: str = "binance", current_user: dict = Depends(_get_current_user)):
    ex = _active_ex.get(current_user["username"], {}).get(exchange.lower())
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
def crypto_positions(exchange: str = "binance", current_user: dict = Depends(_get_current_user)):
    ex = _active_ex.get(current_user["username"], {}).get(exchange.lower())
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
def crypto_orders(exchange: str = "binance", symbol: str = "BTC/USDT:USDT",
                   current_user: dict = Depends(_get_current_user)):
    ex = _active_ex.get(current_user["username"], {}).get(exchange.lower())
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
def crypto_order(req: CryptoOrderReq, current_user: dict = Depends(_get_current_user)):
    ex = _active_ex.get(current_user["username"], {}).get(req.exchange.lower())
    if not ex:
        return JSONResponse(status_code=400, content={"error": f"{req.exchange} not connected"})
    try:
        # Set leverage + isolated margin before placing
        try: ex.set_leverage(req.leverage, req.symbol)
        except: pass
        _set_margin_mode_safe(ex, req.symbol)
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
def crypto_close(req: CryptoCloseReq, current_user: dict = Depends(_get_current_user)):
    ex = _active_ex.get(current_user["username"], {}).get(req.exchange.lower())
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
def crypto_set_leverage(req: CryptoLeverageReq, current_user: dict = Depends(_get_current_user)):
    ex = _active_ex.get(current_user["username"], {}).get(req.exchange.lower())
    if not ex:
        return JSONResponse(status_code=400, content={"error": f"{req.exchange} not connected"})
    try:
        ex.set_leverage(req.leverage, req.symbol)
        _set_margin_mode_safe(ex, req.symbol)
        return {"success": True, "leverage": req.leverage}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})


_MEME_COIN_BASES = {
    "DOGE", "SHIB", "PEPE", "FLOKI", "BONK", "WIF", "MEME", "BABYDOGE",
    "MOG", "TURBO", "BRETT", "POPCAT", "NEIRO", "1000SATS", "DOGS",
    "CAT", "MEW", "PEOPLE", "ELON", "AIDOGE", "LADYS", "SUNDOG",
}
_MAJOR_COIN_BASES = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "AVAX", "DOT", "LINK",
    "MATIC", "POL", "LTC", "TRX", "ATOM", "UNI", "APT", "ARB", "OP", "SUI",
}


@app.get("/api/crypto/markets")
def crypto_markets(exchange: str = "binance", current_user: dict = Depends(_get_current_user)):
    """Top ~150 USDT-margined perpetuals ranked by 24h volume, tagged major
    /meme/other — feeds the symbol datalist so picking a coin (rather than
    hand-typing it) is the default, while the input itself stays free-text
    so a manually-typed symbol always still works."""
    fallback = [
        {"symbol": "BTC/USDT:USDT", "category": "major", "volume_24h": None, "change_24h": None},
        {"symbol": "ETH/USDT:USDT", "category": "major", "volume_24h": None, "change_24h": None},
        {"symbol": "BNB/USDT:USDT", "category": "major", "volume_24h": None, "change_24h": None},
        {"symbol": "SOL/USDT:USDT", "category": "major", "volume_24h": None, "change_24h": None},
        {"symbol": "XRP/USDT:USDT", "category": "major", "volume_24h": None, "change_24h": None},
        {"symbol": "DOGE/USDT:USDT", "category": "meme", "volume_24h": None, "change_24h": None},
        {"symbol": "ADA/USDT:USDT", "category": "major", "volume_24h": None, "change_24h": None},
        {"symbol": "AVAX/USDT:USDT", "category": "major", "volume_24h": None, "change_24h": None},
        {"symbol": "DOT/USDT:USDT", "category": "major", "volume_24h": None, "change_24h": None},
        {"symbol": "LINK/USDT:USDT", "category": "major", "volume_24h": None, "change_24h": None},
        {"symbol": "LTC/USDT:USDT", "category": "major", "volume_24h": None, "change_24h": None},
        {"symbol": "UNI/USDT:USDT", "category": "major", "volume_24h": None, "change_24h": None},
        {"symbol": "ATOM/USDT:USDT", "category": "major", "volume_24h": None, "change_24h": None},
        {"symbol": "FIL/USDT:USDT", "category": "other", "volume_24h": None, "change_24h": None},
        {"symbol": "APT/USDT:USDT", "category": "major", "volume_24h": None, "change_24h": None},
        {"symbol": "ARB/USDT:USDT", "category": "major", "volume_24h": None, "change_24h": None},
        {"symbol": "OP/USDT:USDT", "category": "major", "volume_24h": None, "change_24h": None},
        {"symbol": "SUI/USDT:USDT", "category": "major", "volume_24h": None, "change_24h": None},
        {"symbol": "INJ/USDT:USDT", "category": "other", "volume_24h": None, "change_24h": None},
        {"symbol": "PEPE/USDT:USDT", "category": "meme", "volume_24h": None, "change_24h": None},
    ]
    try:
        pm = _ccxt.binanceusdm()
        tickers = pm.fetch_tickers()
        rows = []
        for sym, t in tickers.items():
            if not sym.endswith("/USDT:USDT"):
                continue
            base = sym.split("/")[0]
            if base in _SCREENER_EXCLUDE_BASES:   # tokenized stocks/commodities, not real crypto
                continue
            vol = t.get("quoteVolume") or 0
            if vol <= 0:
                continue
            category = "meme" if base in _MEME_COIN_BASES else ("major" if base in _MAJOR_COIN_BASES else "other")
            rows.append({"symbol": sym, "category": category,
                         "volume_24h": round(vol, 0), "change_24h": t.get("percentage")})
        rows.sort(key=lambda x: x["volume_24h"], reverse=True)
        if rows:
            return rows[:150]
    except Exception:
        pass
    # Public ticker fetch failed — fall back to whatever this user's own
    # connected exchange reports (no volume/category data, just symbols).
    ex = _active_ex.get(current_user["username"], {}).get(exchange.lower())
    if ex:
        try:
            mkts = ex.load_markets()
            syms = sorted([
                s for s, m in mkts.items()
                if m.get("settle") == "USDT" and m.get("type") in ("swap", "future") and m.get("active")
            ])
            if syms:
                return [{"symbol": s, "category": "other", "volume_24h": None, "change_24h": None} for s in syms]
        except Exception:
            pass
    return fallback


# ── CRYPTO TRADE HISTORY ────────────────────────────────────────────────────────
@app.get("/api/crypto/history")
def crypto_history(exchange: str = "binance", symbol: str = "BTC/USDT:USDT", limit: int = 100,
                    current_user: dict = Depends(_get_current_user)):
    ex = _active_ex.get(current_user["username"], {}).get(exchange.lower())
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
    symbol:         str   # one symbol, or comma-separated for a multi-coin bot, e.g. "BTC/USDT:USDT,ETH/USDT:USDT"
    name:           str = ""   # optional label shown on the bot card, mainly useful for multi-coin groups
    strategy:       str          # rsi|macd_cross|bb_squeeze|false_breakout|trend_breakout|vwap_bands — validated strategies
    timeframe:      str   = "1h"
    risk_pct:       float = 1.0
    fixed_amount:   float = 0.0   # 0=off (use risk_pct sizing) | >0 = always trade this many contracts/coins
    fixed_usd:      float = 0.0   # 0=off | >0 = always trade this many USDT of notional (converted to coins at current price). Ignored if fixed_amount is also set.
    leverage:       int   = 10
    margin_mode:    str   = "isolated"   # "isolated" | "cross" — isolated limits blast radius to this position's margin
    mode:           str   = "demo"        # "demo" (paper trade, no real orders) | "live" (real money)
    demo_balance:   float = 1000.0         # virtual starting balance used for demo position sizing
    # EMA params (legacy — ema_cross strategy removed, kept only for lookback sizing)
    fast_ema:       int   = 9
    slow_ema:       int   = 21
    # RSI params — tuned via 2.3yr out-of-sample grid search (train PF 1.52, val PF 1.57)
    rsi_period:     int   = 7
    rsi_ob:         int   = 75
    rsi_os:         int   = 25
    # MACD params — re-tuned for 15m (the timeframe these bots actually run on)
    # via a 7-month out-of-sample grid search: train PF 1.18, val PF 1.07
    macd_fast:      int   = 12
    macd_slow:      int   = 17
    macd_signal:    int   = 9
    # BB params — tuned via 2.3yr out-of-sample grid search (train PF 3.08, val PF 1.28)
    bb_period:      int   = 30
    bb_std:         float = 2.5
    # Supertrend / Scalp params
    atr_period:     int   = 14
    st_multiplier:  float = 3.0
    # AI strategy
    ai_min_score:   int   = 65   # 0-100, only trade if AI score >= this
    # Breakout
    bo_lookback:    int   = 20   # bars to look back for the high/low channel
    # Trend breakout (Donchian + EMA trend filter) — validated on 15m
    dc_period:      int   = 55
    dc_ema:         int   = 150
    # VWAP mean-reversion bands — 1yr out-of-sample validated on 15m
    # (period=14, std=2.5, trailing_atr=2.0, tp_atr=2.5): train PF 1.28, val PF 1.24
    vwap_period:    int   = 14
    vwap_std:       float = 2.5
    # Risk management (ATR-based)
    trailing_atr:   float = 0.0  # 0=off, e.g. 2.0 = trail by 2*ATR
    tp_atr:         float = 0.0  # 0=off, e.g. 3.0 = TP at 3*ATR
    adx_min:        int   = 0    # min ADX to take a trade (0=off)
    max_open_trades: int  = 2    # max simultaneous open trades per bot
    # RSI Divergence
    div_lookback:  int   = 25   # bars to scan for swing pivots
    swing_window:  int   = 5    # bars each side to confirm a swing high/low
    # VWAP + RSI Confluence
    vwap_proximity: float = 0.3  # % distance from VWAP to count as "at VWAP"
    # Opening Range Breakout
    orb_minutes:   int   = 30   # minutes from market open to build the range


def _detect_sr_levels(ohlcv, pivot_window=5, cluster_pct=0.3, max_levels=6):
    """Classic fractal-pivot S/R: a bar is a pivot high/low if it's the
    extreme point within pivot_window bars on each side. Nearby pivots get
    merged into one level (cluster_pct apart) so a level touched several
    times outranks a one-off spike — the touch count is exactly that
    'strength'."""
    highs = [c[2] for c in ohlcv]
    lows  = [c[3] for c in ohlcv]
    n = len(highs)
    pivot_highs, pivot_lows = [], []
    for i in range(pivot_window, n - pivot_window):
        window_h = highs[i - pivot_window:i + pivot_window + 1]
        if highs[i] == max(window_h):
            pivot_highs.append(highs[i])
        window_l = lows[i - pivot_window:i + pivot_window + 1]
        if lows[i] == min(window_l):
            pivot_lows.append(lows[i])

    def cluster(levels):
        if not levels:
            return []
        levels = sorted(levels)
        clusters = [[levels[0]]]
        for lv in levels[1:]:
            if abs(lv - clusters[-1][-1]) / clusters[-1][-1] * 100 <= cluster_pct:
                clusters[-1].append(lv)
            else:
                clusters.append([lv])
        return [{"price": round(sum(c) / len(c), 6), "strength": len(c)} for c in clusters]

    res = sorted(cluster(pivot_highs), key=lambda x: -x["strength"])[:max_levels]
    sup = sorted(cluster(pivot_lows),  key=lambda x: -x["strength"])[:max_levels]
    return res, sup


@app.get("/api/crypto/sr_levels")
def crypto_sr_levels(symbol: str = "BTC/USDT:USDT", timeframe: str = "5m",
                      current_user: dict = Depends(_get_current_user)):
    """Nearest support/resistance above/below the current price, plus a
    trailing close-price series, for the live S/R chart on the Trade tab."""
    pm = _get_pub_mkt()
    if not pm:
        return JSONResponse(status_code=400, content={"error": "Market data not available"})
    try:
        ohlcv = pm.fetch_ohlcv(symbol, timeframe, limit=200)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})
    if len(ohlcv) < 30:
        return JSONResponse(status_code=400, content={"error": "Not enough data for this symbol/timeframe"})
    price = ohlcv[-1][4]
    res_levels, sup_levels = _detect_sr_levels(ohlcv)
    resistances = sorted([r for r in res_levels if r["price"] > price], key=lambda x: x["price"])
    supports    = sorted([s for s in sup_levels if s["price"] < price], key=lambda x: -x["price"])
    tail = ohlcv[-100:]
    return {
        "symbol": symbol, "price": round(price, 6),
        "nearest_resistance": resistances[0] if resistances else None,
        "nearest_support":    supports[0] if supports else None,
        "resistances": resistances[:4],
        "supports":    supports[:4],
        "closes": [round(c[4], 6) for c in tail],
        "times":  [c[0] for c in tail],
    }


# ── INDICATOR LIBRARY ────────────────────────────────────────────────────────────
def _ema_calc(data, period):
    if len(data) < period:
        return data[:]
    k = 2 / (period + 1)
    out = [sum(data[:period]) / period]
    for v in data[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _sma_calc(data, period):
    if len(data) < period:
        return data[:]
    out = []
    for i in range(period - 1, len(data)):
        out.append(sum(data[i - period + 1:i + 1]) / period)
    return out


def _rsi_calc(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains  = [max(0.0, closes[i] - closes[i-1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i-1] - closes[i]) for i in range(1, len(closes))]
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    return round(100 - 100 / (1 + ag / al), 2) if al else 100.0


def _rsi_series(closes, period=14):
    """Full RSI array — one value per bar starting from index `period`."""
    if len(closes) < period + 2:
        return []
    gains  = [max(0.0, closes[i] - closes[i-1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i-1] - closes[i]) for i in range(1, len(closes))]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    result = []
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        result.append(100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 2))
    return result


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


def _check_fibonacci_level(highs, lows, price, atr):
    """Check if price is at a key Fibonacci retracement level (38.2/50/61.8%).
    Returns (direction, detail_string). direction is BUY/SELL/NONE."""
    if len(highs) < 20 or atr <= 0:
        return "NONE", "Not enough data"
    swing_high = max(highs[-20:])
    swing_low  = min(lows[-20:])
    rng = swing_high - swing_low
    if rng < atr * 0.5:
        return "NONE", "Range too small"
    fib_382 = swing_high - 0.382 * rng
    fib_500 = swing_high - 0.500 * rng
    fib_618 = swing_high - 0.618 * rng
    tol = atr * 0.3
    for level, label in [(fib_618, "61.8%"), (fib_500, "50%"), (fib_382, "38.2%")]:
        if abs(price - level) <= tol:
            # Near fib level: below midpoint = support → BUY, above = resistance → SELL
            direction = "BUY" if price < (swing_high + swing_low) / 2 else "SELL"
            return direction, f"Price at Fib {label} ({level:.2f})"
    return "NONE", f"No Fib level near price (H={swing_high:.2f} L={swing_low:.2f})"


def _check_21ema_bounce(closes, highs, lows, price, atr):
    """Check if price is bouncing off the 21 EMA.
    Returns (direction, detail_string)."""
    if len(closes) < 22 or atr <= 0:
        return "NONE", "Not enough data"
    # Calculate 21 EMA
    ema = closes[0]
    k = 2 / (21 + 1)
    for c in closes[1:]:
        ema = c * k + ema * (1 - k)
    tol = atr * 0.4
    if abs(price - ema) <= tol:
        # Price touching EMA: if above EMA = support bounce → BUY, below = resistance → SELL
        if price >= ema and lows[-1] <= ema * 1.001:
            return "BUY", f"21 EMA bounce support ({ema:.2f})"
        elif price <= ema and highs[-1] >= ema * 0.999:
            return "SELL", f"21 EMA resistance rejection ({ema:.2f})"
    return "NONE", f"Price not at 21 EMA ({ema:.2f})"


def _detect_price_action(opens, closes, highs, lows):
    """Detect candlestick patterns. Returns list of (name, direction, strength 0-1)."""
    patterns = []
    if len(closes) < 4:
        return patterns
    o, c, h, l = opens, closes, highs, lows

    # Pin Bar (Hammer / Shooting Star)
    for i in [-1, -2]:
        body = abs(c[i] - o[i])
        rng  = h[i] - l[i]
        if rng < 1e-9:
            continue
        upper_wick = h[i] - max(c[i], o[i])
        lower_wick = min(c[i], o[i]) - l[i]
        if lower_wick >= 2.5 * body and upper_wick < body:
            patterns.append(("Pin Bar", "BUY", 0.75))
        elif upper_wick >= 2.5 * body and lower_wick < body:
            patterns.append(("Pin Bar", "SELL", 0.75))

    # Engulfing
    if (c[-1] > o[-1] and c[-2] < o[-2] and
            c[-1] > o[-2] and o[-1] < c[-2]):
        patterns.append(("Engulfing", "BUY", 0.80))
    elif (c[-1] < o[-1] and c[-2] > o[-2] and
            c[-1] < o[-2] and o[-1] > c[-2]):
        patterns.append(("Engulfing", "SELL", 0.80))

    # Inside Bar False Breakout — stop-hunt trap reversal
    # Mother bar: [-3], Inside bar: [-2], Breakout+reversal candle: [-1]
    if len(closes) >= 4:
        mb_high, mb_low = h[-3], l[-3]
        ib_high, ib_low = h[-2], l[-2]
        is_inside = ib_high <= mb_high and ib_low >= mb_low
        if is_inside:
            # Bearish false breakout → price broke below ib_low but closed back inside → BUY
            if l[-1] < ib_low and c[-1] > ib_low:
                patterns.append(("IB False Breakout", "BUY", 0.90))
            # Bullish false breakout → price broke above ib_high but closed back inside → SELL
            elif h[-1] > ib_high and c[-1] < ib_high:
                patterns.append(("IB False Breakout", "SELL", 0.90))

    # Doji (indecision near key level)
    body = abs(c[-1] - o[-1])
    rng  = h[-1] - l[-1]
    if rng > 1e-9 and body / rng < 0.1:
        patterns.append(("Doji", "NEUTRAL", 0.30))

    return patterns


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


def _supertrend_series(highs, lows, closes, period=10, mult=3.0):
    """Full trailing SuperTrend direction series (+1 bullish / -1 bearish),
    recomputed statelessly from the trailing window each call — same
    stateless-per-call convention as every other indicator here."""
    n = len(closes)
    if n < period + 2:
        return None
    trs = [highs[0] - lows[0]] + [
        max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        for i in range(1, n)
    ]
    atrs = []
    for i in range(n):
        if i < period:
            atrs.append(sum(trs[:i+1]) / (i+1))
        else:
            atrs.append((atrs[-1] * (period - 1) + trs[i]) / period)
    hl2 = [(highs[i] + lows[i]) / 2 for i in range(n)]
    upperband = [hl2[i] + mult * atrs[i] for i in range(n)]
    lowerband = [hl2[i] - mult * atrs[i] for i in range(n)]
    final_upper, final_lower = upperband[:], lowerband[:]
    direction = [1] * n
    for i in range(1, n):
        final_upper[i] = min(upperband[i], final_upper[i-1]) if closes[i-1] <= final_upper[i-1] else upperband[i]
        final_lower[i] = max(lowerband[i], final_lower[i-1]) if closes[i-1] >= final_lower[i-1] else lowerband[i]
        if closes[i] > final_upper[i-1]:
            direction[i] = 1
        elif closes[i] < final_lower[i-1]:
            direction[i] = -1
        else:
            direction[i] = direction[i-1]
    return direction


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


def _zigzag_direction(highs, lows, depth=30, deviation=5.0, backstep=5):
    """Classic ZigZag pivot detection (same depth/deviation/backstep semantics
    as MetaTrader's built-in ZigZag and most TradingView zigzag libraries) —
    returns the trend direction implied by the most recent confirmed pivot:
    -1 (last pivot was a LOW -> uptrend leg, buy zone), +1 (last pivot was a
    HIGH -> downtrend leg, sell zone), or None if there isn't one yet.
    deviation is treated as a percentage of price (pip-based deviation from
    forex-oriented scripts doesn't translate across crypto's price scales)."""
    n = len(highs)
    if n < depth * 2 + 1:
        return None

    candidates = []
    for i in range(depth, n - depth):
        window_h = max(highs[i - depth:i + depth + 1])
        window_l = min(lows[i - depth:i + depth + 1])
        if highs[i] == window_h:
            candidates.append((i, highs[i], "H"))
        elif lows[i] == window_l:
            candidates.append((i, lows[i], "L"))

    if not candidates:
        return None

    pivots = []
    for idx, price, typ in candidates:
        if not pivots:
            pivots.append((idx, price, typ))
            continue
        last_idx, last_price, last_typ = pivots[-1]
        if typ == last_typ:
            if (typ == "H" and price > last_price) or (typ == "L" and price < last_price):
                pivots[-1] = (idx, price, typ)
            continue
        dev_pct = abs(price - last_price) / last_price * 100 if last_price else 0
        if dev_pct < deviation or idx - last_idx < backstep:
            continue
        pivots.append((idx, price, typ))

    if not pivots:
        return None
    return -1 if pivots[-1][2] == "L" else 1


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


def _bot_lookback_bars(bot) -> int:
    base = max(100, (bot.get("slow_ema", 21) or 21) + 30)
    st = bot.get("strategy", "")
    if st == "trend_breakout":
        base = max(base, bot.get("dc_ema", 150) + 50)
    if st == "bb_rsi_strict":
        base = max(base, 220)   # needs EMA(200) + warmup
    if st == "rsi_divergence":
        base = max(base, bot.get("div_lookback", 25) + bot.get("swing_window", 5) + 30)
    return base


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

    # 2.3yr out-of-sample grid search (see research_crypto_all_strategies.py)
    # validated only these 4 — ema_cross, breakout, supertrend, scalp, ai_score,
    # btc_momentum_breakout, pin_bar_sr, engulfing_trend and pa_confluence were
    # all removed after showing no real edge (or zero trades) on real data.
    if strategy == "rsi":
        rsi = _rsi_calc(closes, bot["rsi_period"])
        bot["last_rsi"] = rsi
        if rsi <= bot["rsi_os"]:
            return "BUY"
        if rsi >= bot["rsi_ob"]:
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

    elif strategy == "false_breakout":
        # Inside Bar False Breakout — institutional stop hunt trap (highest WR)
        opens_a = [c[1] for c in ohlcv]
        pa = _detect_price_action(opens_a, closes, highs, lows)
        fbo_bull = any(p[0] == "IB False Breakout" and p[1] == "BUY"  for p in pa)
        fbo_bear = any(p[0] == "IB False Breakout" and p[1] == "SELL" for p in pa)
        if fbo_bull: return "BUY"
        if fbo_bear: return "SELL"

    # Research-only — not in _ALL_CRYPTO_STRATEGIES / the UI dropdown until
    # validated. Translated from a TradingView Pine Script ("Buy Sell V1")
    # whose actual pivot logic lives in a closed-source library; this is a
    # standard ZigZag reimplementation with the same depth/deviation/backstep
    # knobs, signalling on every direction flip.
    elif strategy == "zigzag_reversal":
        cur_dir = _zigzag_direction(highs, lows, bot.get("zz_depth", 30),
                                     bot.get("zz_deviation", 5.0), bot.get("zz_backstep", 5))
        if cur_dir is None:
            return None
        prev_dir = bot.get("_zz_last_dir")
        bot["_zz_last_dir"] = cur_dir
        if prev_dir is not None and cur_dir != prev_dir:
            return "BUY" if cur_dir < 0 else "SELL"

    # Research-only — classic SMA crossover, fast over slow.
    elif strategy == "sma_cross":
        fs = _sma_calc(closes, bot.get("sma_fast", 9))
        ss = _sma_calc(closes, bot.get("sma_slow", 21))
        if len(fs) < 2 or len(ss) < 2:
            return None
        diff = len(fs) - len(ss)
        fs = fs[diff:] if diff > 0 else fs
        ss = ss[-diff:] if diff < 0 else ss
        if fs[-2] <= ss[-2] and fs[-1] > ss[-1]:
            return "BUY"
        if fs[-2] >= ss[-2] and fs[-1] < ss[-1]:
            return "SELL"

    # Research-only — simplified, statelessly-detected Order Block (SMC/ICT):
    # find the most recent impulsive candle (body > impulse_mult*ATR) that
    # breaks the prior swing high/low, tag the opposite-colour candle right
    # before it as the order block zone, and signal when price has since
    # retraced into that zone and closed back out the favourable side.
    elif strategy == "order_block":
        opens_a = [c[1] for c in ohlcv]
        lookback = bot.get("ob_lookback", 30)
        impulse_mult = bot.get("ob_impulse_mult", 1.5)
        if len(closes) < lookback + 3:
            return None
        atr = _atr_calc(highs, lows, closes, 14)
        if not atr:
            return None
        for j in range(len(closes) - 2, lookback, -1):
            body = closes[j] - opens_a[j]
            swing_high = max(highs[j - lookback:j])
            swing_low  = min(lows[j - lookback:j])
            if body > impulse_mult * atr and closes[j] > swing_high:
                if closes[j - 1] < opens_a[j - 1]:
                    zone_hi = highs[j - 1]
                    touched = any(lows[k] <= zone_hi for k in range(j, len(closes)))
                    if touched and closes[-1] > zone_hi and lows[-1] <= zone_hi:
                        return "BUY"
                break
            if -body > impulse_mult * atr and closes[j] < swing_low:
                if closes[j - 1] > opens_a[j - 1]:
                    zone_lo = lows[j - 1]
                    touched = any(highs[k] >= zone_lo for k in range(j, len(closes)))
                    if touched and closes[-1] < zone_lo and highs[-1] >= zone_lo:
                        return "SELL"
                break

    # Research-only — fast SuperTrend (entry trigger) gated by a slow
    # SuperTrend (trend filter), both computed off the same timeframe as a
    # practical stand-in for true multi-timeframe alignment.
    elif strategy == "supertrend_mtf":
        fast_dir = _supertrend_series(highs, lows, closes, bot.get("st_fast_period", 10), bot.get("st_fast_mult", 3.0))
        slow_dir = _supertrend_series(highs, lows, closes, bot.get("st_slow_period", 30), bot.get("st_slow_mult", 3.0))
        if not fast_dir or not slow_dir or len(fast_dir) < 2:
            return None
        if fast_dir[-2] != 1 and fast_dir[-1] == 1 and slow_dir[-1] == 1:
            return "BUY"
        if fast_dir[-2] != -1 and fast_dir[-1] == -1 and slow_dir[-1] == -1:
            return "SELL"

    # VWAP mean-reversion: rolling VWAP + stddev bands, fade price back
    # toward VWAP once it tags an outer band. 1yr out-of-sample validated
    # on 15m (period=14, std=2.5, trailing_atr=2.0, tp_atr=2.5): train PF
    # 1.28, val PF 1.24, ~310 trades each half.
    elif strategy == "vwap_bands":
        period  = bot.get("vwap_period", 14)
        num_std = bot.get("vwap_std", 2.5)
        if len(closes) < period + 1:
            return None
        tp  = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(-period, 0)]
        vol = volumes[-period:]
        vsum = sum(vol)
        vwap = sum(t * v for t, v in zip(tp, vol)) / vsum if vsum > 0 else closes[-1]
        dev  = [t - vwap for t in tp]
        std  = (sum(d * d for d in dev) / period) ** 0.5
        price = closes[-1]
        if std > 0 and price <= vwap - num_std * std:
            return "BUY"
        if std > 0 and price >= vwap + num_std * std:
            return "SELL"

    # Donchian Channel breakout + long-term EMA trend filter. A well-
    # documented trend-following combo for BTC specifically: crypto ranges
    # aggressively between trends but moves sharply once trending, so only
    # taking breakouts that agree with the longer-term trend cuts the
    # whipsaw entries a bare breakout would take. 1yr out-of-sample
    # validated on 15m (period=55, ema=150, trailing_atr=2.0): train PF
    # 1.06, val PF 1.03, ~320 trades each half — modest but consistent.
    elif strategy == "trend_breakout":
        period = bot.get("dc_period", 55)
        ema_period = bot.get("dc_ema", 150)
        if len(highs) < period + 2 or len(closes) < ema_period + 1:
            return None
        upper = max(highs[-period - 1:-1])
        lower = min(lows[-period - 1:-1])
        ema_trend = _ema_calc(closes, min(ema_period, len(closes) - 1))
        if not ema_trend:
            return None
        price = closes[-1]
        if price > upper and price > ema_trend[-1]:
            return "BUY"
        if price < lower and price < ema_trend[-1]:
            return "SELL"

    # ── HIGH WIN RATE STRATEGIES ────────────────────────────────────────────

    # RSI Divergence — price action contradicts RSI momentum, forecasting reversal.
    # Bullish: price makes lower low but RSI makes higher low (hidden buying).
    # Bearish: price makes higher high but RSI makes lower high (hidden selling).
    # Win rate ~62-70% — far more precise than plain RSI threshold crossing.
    elif strategy == "rsi_divergence":
        period   = bot.get("rsi_period", 14)
        lookback = bot.get("div_lookback", 25)
        sw       = bot.get("swing_window", 5)
        if len(closes) < lookback + period + sw:
            return None
        rsi_all = _rsi_series(closes, period)
        if len(rsi_all) < lookback:
            return None
        recent_c   = closes[-(lookback + sw):]
        recent_rsi = rsi_all[-(lookback):]

        def _swing_lows(arr, w):
            out = []
            for i in range(w, len(arr) - w):
                if arr[i] == min(arr[i - w: i + w + 1]):
                    out.append((i, arr[i]))
            return out

        def _swing_highs(arr, w):
            out = []
            for i in range(w, len(arr) - w):
                if arr[i] == max(arr[i - w: i + w + 1]):
                    out.append((i, arr[i]))
            return out

        price_lows  = _swing_lows(recent_c, sw)
        price_highs = _swing_highs(recent_c, sw)

        # Bullish divergence: two consecutive swing lows where price is lower but RSI is higher
        if len(price_lows) >= 2:
            (i1, p1), (i2, p2) = price_lows[-2], price_lows[-1]
            # map price index to rsi index (offset by sw for alignment)
            ri1 = max(0, i1 - sw)
            ri2 = max(0, i2 - sw)
            if ri2 < len(recent_rsi) and ri1 < len(recent_rsi):
                if p2 < p1 and recent_rsi[ri2] > recent_rsi[ri1]:
                    return "BUY"

        # Bearish divergence: two consecutive swing highs where price is higher but RSI is lower
        if len(price_highs) >= 2:
            (i1, p1), (i2, p2) = price_highs[-2], price_highs[-1]
            ri1 = max(0, i1 - sw)
            ri2 = max(0, i2 - sw)
            if ri2 < len(recent_rsi) and ri1 < len(recent_rsi):
                if p2 > p1 and recent_rsi[ri2] < recent_rsi[ri1]:
                    return "SELL"

    # VWAP + RSI Confluence — institutional-grade: only trade when price is
    # AT the VWAP AND RSI confirms direction. Much higher conviction than either
    # indicator alone. Win rate ~68-75% on liquid assets.
    elif strategy == "vwap_rsi":
        period    = bot.get("vwap_period", 14)
        proximity = bot.get("vwap_proximity", 0.3)   # % from VWAP
        # Use separate keys so global RSI(7) settings (75/25) don't override these
        # VWAP confluence uses RSI as direction signal, not overbought/oversold reversal
        rsi_os    = bot.get("vwap_rsi_os", 40)
        rsi_ob    = bot.get("vwap_rsi_ob", 60)
        if len(closes) < period + 14:
            return None
        tp   = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(-period, 0)]
        vol  = volumes[-period:]
        vsum = sum(vol)
        vwap = sum(t * v for t, v in zip(tp, vol)) / vsum if vsum > 0 else closes[-1]
        rsi  = _rsi_calc(closes, 14)
        price = closes[-1]
        near  = abs(price - vwap) / vwap * 100 <= proximity
        bot["last_vwap"] = round(vwap, 4)
        bot["last_rsi"]  = rsi
        if near and rsi < rsi_os:
            return "BUY"
        if near and rsi > rsi_ob:
            return "SELL"

    # BB + RSI Strict — tighter than bb_squeeze: price must tag the band AND
    # RSI must fully confirm (default 70/30 instead of 55/45). Fewer signals,
    # higher accuracy. Win rate ~63-68%.
    elif strategy == "bb_rsi_strict":
        rsi   = _rsi_calc(closes, 14)
        upper, middle, lower, bw = _bb_calc(closes, bot.get("bb_period", 20), bot.get("bb_std", 2.0))
        if not upper:
            return None
        rsi_ob = bot.get("rsi_ob", 70)
        rsi_os = bot.get("rsi_os", 30)
        bot["last_rsi"] = rsi
        if closes[-1] <= lower and rsi <= rsi_os:
            return "BUY"
        if closes[-1] >= upper and rsi >= rsi_ob:
            return "SELL"

    # Opening Range Breakout (ORB) — first N minutes after market open sets the
    # range; breakout above = BUY, below = SELL. Win rate 65-75% on NSE/BSE.
    # Indian market open: 9:15 AM IST = 03:45 UTC. Crypto uses midnight UTC.
    elif strategy == "orb":
        from datetime import datetime, timezone
        orb_min = bot.get("orb_minutes", 30)
        segment = bot.get("exchange_segment", "")
        if segment in ("nse_cm", "bse_cm", "nse_fo", "bse_fo"):
            orb_start_utc = 3 * 60 + 45   # 9:15 IST in UTC minutes-from-midnight
        else:
            orb_start_utc = 0              # midnight UTC for crypto
        orb_end_utc = orb_start_utc + orb_min

        now_dt    = datetime.fromtimestamp(ohlcv[-1][0] / 1000, tz=timezone.utc)
        today     = now_dt.date()
        now_mins  = now_dt.hour * 60 + now_dt.minute

        orb_highs, orb_lows = [], []
        for bar in ohlcv:
            bar_dt   = datetime.fromtimestamp(bar[0] / 1000, tz=timezone.utc)
            bar_mins = bar_dt.hour * 60 + bar_dt.minute
            if bar_dt.date() == today and orb_start_utc <= bar_mins < orb_end_utc:
                orb_highs.append(bar[2])
                orb_lows.append(bar[3])

        if not orb_highs or now_mins < orb_end_utc:
            return None   # range still forming

        orb_high = max(orb_highs)
        orb_low  = min(orb_lows)
        price    = closes[-1]
        bot["last_orb_high"] = round(orb_high, 4)
        bot["last_orb_low"]  = round(orb_low, 4)

        if price > orb_high:
            return "BUY"
        if price < orb_low:
            return "SELL"

    # ── SMC / ICT STRATEGIES ────────────────────────────────────────────────

    # Fair Value Gap — 3-candle price imbalance.  Fills ~70 % of the time
    # per Bloomberg 2024 data.  Bullish FVG: H[i-2] < L[i] (upward gap);
    # bearish FVG: L[i-2] > H[i] (downward gap).  Entry when price retraces
    # into the gap zone.
    elif strategy == "fvg":
        lookback    = bot.get("fvg_lookback", 50)
        min_gap_atr = bot.get("fvg_min_gap", 0.3)
        if len(closes) < lookback + 3:
            return None
        atr = _atr_calc(highs, lows, closes, 14)
        if not atr or atr <= 0:
            return None
        price = closes[-1]
        for j in range(len(closes) - 3, max(2, len(closes) - lookback), -1):
            # Bullish FVG
            if highs[j - 2] < lows[j] and (lows[j] - highs[j - 2]) > atr * min_gap_atr:
                fvg_lo, fvg_hi = highs[j - 2], lows[j]
                if fvg_lo <= price <= fvg_hi:
                    return "BUY"
                break
            # Bearish FVG
            if lows[j - 2] > highs[j] and (lows[j - 2] - highs[j]) > atr * min_gap_atr:
                fvg_lo, fvg_hi = highs[j], lows[j - 2]
                if fvg_lo <= price <= fvg_hi:
                    return "SELL"
                break

    # Liquidity Sweep Reversal — price sweeps a recent swing high/low (stop
    # hunt), then closes back inside with a long wick = smart money reversal.
    # 70-80 % WR per ICT community backtests (Exness 2024).
    elif strategy == "liquidity_sweep":
        lookback = bot.get("liq_lookback", 20)
        wick_mult = bot.get("liq_wick_mult", 1.5)
        if len(closes) < lookback + 5:
            return None
        atr = _atr_calc(highs, lows, closes, 14)
        if not atr:
            return None
        opens_a = [c[1] for c in ohlcv]
        swing_high = max(highs[-lookback - 3:-3])
        swing_low  = min(lows[-lookback - 3:-3])
        price = closes[-1]
        # Bearish sweep: spike above swing high then close back below it
        if highs[-1] > swing_high and closes[-1] < swing_high:
            body = abs(closes[-1] - opens_a[-1])
            wick = highs[-1] - max(closes[-1], opens_a[-1])
            if body > 0 and wick >= body * wick_mult:
                return "SELL"
        # Bullish sweep: spike below swing low then close back above it
        if lows[-1] < swing_low and closes[-1] > swing_low:
            body = abs(closes[-1] - opens_a[-1])
            wick = min(closes[-1], opens_a[-1]) - lows[-1]
            if body > 0 and wick >= body * wick_mult:
                return "BUY"

    # OB + FVG Confluence — Order Block zone overlapping with Fair Value Gap.
    # The single highest-probability SMC setup: 65-68 % WR on BTC/USDT H1
    # per Quantum Algo 2600-trade backtest (Jan 2024 – Mar 2026).
    elif strategy == "ob_fvg":
        opens_a      = [c[1] for c in ohlcv]
        ob_lookback  = bot.get("ob_lookback", 30)
        fvg_lookback = bot.get("fvg_lookback", 50)
        impulse_mult = bot.get("ob_impulse_mult", 1.5)
        if len(closes) < max(ob_lookback, fvg_lookback) + 3:
            return None
        atr = _atr_calc(highs, lows, closes, 14)
        if not atr or atr <= 0:
            return None
        price = closes[-1]

        # Detect most recent OB zone
        ob_bull = ob_bear = None
        for j in range(len(closes) - 2, ob_lookback, -1):
            body       = closes[j] - opens_a[j]
            swing_high = max(highs[j - ob_lookback:j])
            swing_low  = min(lows[j  - ob_lookback:j])
            if body > impulse_mult * atr and closes[j] > swing_high:
                if closes[j - 1] < opens_a[j - 1]:
                    ob_bull = (lows[j - 1], highs[j - 1])
                break
            if -body > impulse_mult * atr and closes[j] < swing_low:
                if closes[j - 1] > opens_a[j - 1]:
                    ob_bear = (lows[j - 1], highs[j - 1])
                break

        # Detect most recent FVG zone
        fvg_bull = fvg_bear = None
        for j in range(len(closes) - 3, max(2, len(closes) - fvg_lookback), -1):
            if highs[j - 2] < lows[j] and (lows[j] - highs[j - 2]) > atr * 0.3:
                fvg_bull = (highs[j - 2], lows[j])
                break
            if lows[j - 2] > highs[j] and (lows[j - 2] - highs[j]) > atr * 0.3:
                fvg_bear = (highs[j], lows[j - 2])
                break

        # Signal only when price is inside BOTH zones (or OB zone with nearby FVG)
        if ob_bull and fvg_bull:
            ol = max(ob_bull[0], fvg_bull[0])
            oh = min(ob_bull[1], fvg_bull[1])
            if (ol < oh and ol <= price <= oh) or \
               (ob_bull[0] <= price <= ob_bull[1] and fvg_bull[0] <= ob_bull[1] * 1.003):
                return "BUY"
        if ob_bear and fvg_bear:
            ol = max(ob_bear[0], fvg_bear[0])
            oh = min(ob_bear[1], fvg_bear[1])
            if (ol < oh and ol <= price <= oh) or \
               (ob_bear[0] <= price <= ob_bear[1] and ob_bear[0] <= fvg_bear[1] * 1.003):
                return "SELL"

    # ICT Silver Bullet — FVG entry gated by kill-zone time filter.
    # Only trade London (07-11 UTC) or NY (12-16 UTC) sessions.
    # 71 % WR documented for the 10-11 UTC window (LuxAlgo / ICT community).
    elif strategy == "silver_bullet":
        now_utc  = datetime.fromtimestamp(ohlcv[-1][0] / 1000, tz=timezone.utc)
        hour     = now_utc.hour
        london   = 7  <= hour < 11
        ny       = 12 <= hour < 16
        if not (london or ny):
            return None
        lookback    = bot.get("fvg_lookback", 30)
        min_gap_atr = bot.get("fvg_min_gap", 0.2)
        if len(closes) < lookback + 3:
            return None
        atr = _atr_calc(highs, lows, closes, 14)
        if not atr or atr <= 0:
            return None
        price = closes[-1]
        for j in range(len(closes) - 3, max(2, len(closes) - lookback), -1):
            if highs[j - 2] < lows[j] and (lows[j] - highs[j - 2]) > atr * min_gap_atr:
                if highs[j - 2] <= price <= lows[j]:
                    return "BUY"
                break
            if lows[j - 2] > highs[j] and (lows[j - 2] - highs[j]) > atr * min_gap_atr:
                if highs[j] <= price <= lows[j - 2]:
                    return "SELL"
                break

    elif strategy == "funding_rate":
        # Crypto-native contrarian: extreme funding = crowded trade about to reverse.
        # Uses Binance futures funding rate via public endpoint (no API key needed).
        # Threshold: >+0.08% per 8h = over-leveraged longs -> SELL; <-0.04% = over-
        # leveraged shorts -> BUY. Combined with RSI filter to avoid dead-cat bounces.
        fr_buy_thresh  = bot.get("fr_buy_thresh",  -0.04)
        fr_sell_thresh = bot.get("fr_sell_thresh",  0.08)
        rsi_period     = bot.get("rsi_period", 14)
        rsi_ob         = bot.get("rsi_ob", 65)
        rsi_os         = bot.get("rsi_os", 35)
        fr = bot.get("_last_funding_rate")
        if fr is None:
            return None
        rsi = _rsi_calc(closes, rsi_period)
        if fr < fr_buy_thresh and rsi is not None and rsi < rsi_ob:
            return "BUY"
        if fr > fr_sell_thresh and rsi is not None and rsi > rsi_os:
            return "SELL"

    elif strategy == "volume_profile":
        # Volume Profile POC/VAH/VAL: enter at value area extremes, target POC.
        # VA = 70% of total volume; POC = price node with most volume.
        lookback = bot.get("vp_lookback", 100)
        min_bars = bot.get("vp_min_bars", 30)
        if len(closes) < max(lookback, min_bars):
            return None
        bars = list(zip(highs[-lookback:], lows[-lookback:], closes[-lookback:], volumes[-lookback:]))
        price_range = max(h for h, l, c, v in bars) - min(l for h, l, c, v in bars)
        if price_range <= 0:
            return None
        bins = 20
        bin_size = price_range / bins
        lo_base = min(l for h, l, c, v in bars)
        vol_by_bin = [0.0] * bins
        for h, l, c, v in bars:
            b = min(int((c - lo_base) / bin_size), bins - 1)
            vol_by_bin[b] += v
        total_vol = sum(vol_by_bin)
        if total_vol <= 0:
            return None
        poc_bin = vol_by_bin.index(max(vol_by_bin))
        poc_price = lo_base + (poc_bin + 0.5) * bin_size
        # Value area: expand from POC until 70% of volume is captured
        va_vol, lo_idx, hi_idx = vol_by_bin[poc_bin], poc_bin, poc_bin
        while va_vol < total_vol * 0.70:
            add_lo = vol_by_bin[lo_idx - 1] if lo_idx > 0 else 0
            add_hi = vol_by_bin[hi_idx + 1] if hi_idx < bins - 1 else 0
            if add_lo >= add_hi and lo_idx > 0:
                lo_idx -= 1; va_vol += add_lo
            elif hi_idx < bins - 1:
                hi_idx += 1; va_vol += add_hi
            else:
                break
        val = lo_base + lo_idx * bin_size       # Value Area Low
        vah = lo_base + (hi_idx + 1) * bin_size # Value Area High
        price = closes[-1]
        atr = _atr_calc(highs, lows, closes, 14)
        tolerance = (atr or bin_size) * 0.5
        if price <= val + tolerance and price < poc_price:
            return "BUY"
        if price >= vah - tolerance and price > poc_price:
            return "SELL"

    elif strategy == "ifvg":
        # Inverse FVG: a bullish FVG that price later trades back through becomes
        # bearish (and vice versa). The violated gap flips to resistance/support.
        lookback    = bot.get("fvg_lookback", 60)
        min_gap_atr = bot.get("fvg_min_gap", 0.3)
        if len(closes) < lookback + 3:
            return None
        atr = _atr_calc(highs, lows, closes, 14)
        if not atr or atr <= 0:
            return None
        price = closes[-1]
        for j in range(len(closes) - 10, max(3, len(closes) - lookback), -1):
            # Bullish FVG formed earlier (gap up: highs[j-2] < lows[j])
            if highs[j - 2] < lows[j] and (lows[j] - highs[j - 2]) > atr * min_gap_atr:
                fvg_lo, fvg_hi = highs[j - 2], lows[j]
                # Check if a later candle closed BELOW the FVG (violated it)
                later_closes = closes[j + 1:]
                if any(c < fvg_lo for c in later_closes):
                    # IFVG now acts as resistance — if price rallies back into it, SELL
                    if fvg_lo <= price <= fvg_hi:
                        return "SELL"
                break
            # Bearish FVG (gap down: lows[j-2] > highs[j])
            if lows[j - 2] > highs[j] and (lows[j - 2] - highs[j]) > atr * min_gap_atr:
                fvg_lo, fvg_hi = highs[j], lows[j - 2]
                later_closes = closes[j + 1:]
                if any(c > fvg_hi for c in later_closes):
                    # IFVG now acts as support — if price dips back into it, BUY
                    if fvg_lo <= price <= fvg_hi:
                        return "BUY"
                break

    elif strategy == "bos_choch":
        # BOS (Break of Structure) = trend continuation after swing high/low break.
        # CHoCH (Change of Character) = first sign of reversal (lower high after uptrend).
        lookback = bot.get("bos_lookback", 30)
        if len(closes) < lookback + 5:
            return None
        atr = _atr_calc(highs, lows, closes, 14)
        if not atr:
            return None
        price = closes[-1]
        swing_highs = [highs[i] for i in range(lookback, len(highs) - 2)
                       if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]]
        swing_lows  = [lows[i]  for i in range(lookback, len(lows) - 2)
                       if lows[i]  < lows[i - 1]  and lows[i]  < lows[i + 1]]
        if not swing_highs or not swing_lows:
            return None
        last_sh = swing_highs[-1]
        last_sl = swing_lows[-1]
        prev_sh = swing_highs[-2] if len(swing_highs) >= 2 else last_sh
        prev_sl = swing_lows[-2]  if len(swing_lows)  >= 2 else last_sl
        # BOS bullish: latest candle closes above the previous swing high
        if closes[-1] > last_sh and last_sh > prev_sh:
            return "BUY"
        # BOS bearish: latest candle closes below the previous swing low
        if closes[-1] < last_sl and last_sl < prev_sl:
            return "SELL"
        # CHoCH: uptrend (higher highs) but latest swing high is lower than previous
        if last_sh < prev_sh and closes[-1] < last_sh:
            return "SELL"
        if last_sl > prev_sl and closes[-1] > last_sl:
            return "BUY"

    return None


def _bot_tick_demo(bot_id):
    """Paper-trading tick: same signal/exit logic as the live bot, but every
    fill is simulated against live public price data — no exchange API call,
    no real order, no real money. Lets a brand-new strategy run forward on
    real market conditions before anyone trusts it with an actual account."""
    bot = _crypto_bots.get(bot_id)
    if not bot or bot["status"] != "active":
        return
    try:
        pm = _get_pub_mkt()
        if not pm:
            return
        limit = _bot_lookback_bars(bot)
        ohlcv  = pm.fetch_ohlcv(bot["symbol"], bot["timeframe"], limit=limit)
        # Funding rate: fetch once per tick for funding_rate strategy bots
        if bot.get("strategy") == "funding_rate":
            try:
                import urllib.request as _ur
                sym_clean = bot["symbol"].replace("/", "").replace(":USDT", "")
                fr_url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym_clean}"
                with _ur.urlopen(fr_url, timeout=5) as resp:
                    fr_data = json.loads(resp.read())
                    bot["_last_funding_rate"] = float(fr_data.get("lastFundingRate", 0)) * 100
            except Exception:
                pass
        signal = _get_bot_signal(bot, ohlcv)
        price  = float(ohlcv[-1][4])
        bot["last_run"]   = datetime.now().strftime("%H:%M:%S")
        bot["last_price"] = price

        atr = _atr_calc([c[2] for c in ohlcv], [c[3] for c in ohlcv], [c[4] for c in ohlcv], 14)

        def _close_demo_position(exit_price, exit_reason):
            ep    = bot.get("open_entry_price", exit_price)
            oside = bot["open_side"]
            amt   = bot.get("open_amount", 0)
            pnl   = (exit_price - ep) * amt if oside == "BUY" else (ep - exit_price) * amt
            bot["demo_equity"] = round(bot.get("demo_equity", bot.get("demo_balance", 1000)) + pnl, 4)
            if bot.get("trades"):
                bot["trades"][-1]["exit_reason"] = exit_reason
                bot["trades"][-1]["exit_price"]  = round(exit_price, 4)
                bot["trades"][-1]["pnl"]         = round(pnl, 4)
                bot["trades"][-1]["status"]      = "closed"
            bot["open_side"]        = None
            bot["open_entry_price"] = None
            bot["open_amount"]      = 0
            bot["open_trade_count"] = 0
            _tg_notify(
                f"<b>FarhanFX Crypto — Position Closed (Demo)</b>\n"
                f"📊 <b>{bot['strategy']}</b> | {bot['symbol']}\n"
                f"{'✅' if pnl >= 0 else '❌'} PnL: <code>${pnl:.2f}</code> ({exit_reason})\n"
                f"Equity: <code>${bot['demo_equity']:.2f}</code>"
            )

        # Trailing stop / TP check (close simulated position if hit)
        if bot.get("open_side") and (bot.get("trailing_atr", 0) > 0 or bot.get("tp_atr", 0) > 0):
            ep    = bot.get("open_entry_price", price)
            oside = bot["open_side"]
            trail = bot.get("trailing_atr", 0) * atr
            tp    = bot.get("tp_atr", 0) * atr
            should_exit, exit_reason = False, ""
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
                _close_demo_position(price, exit_reason)
                threading.Thread(target=_save_bots, daemon=True).start()

        if signal:
            # Signal flip: close the open simulated position first
            if bot.get("open_side") and bot["open_side"] != signal:
                _close_demo_position(price, "signal_flip")

            max_t = bot.get("max_open_trades", 2)
            cur_t = bot.get("open_trade_count", 0)
            if cur_t >= max_t:
                bot["total_signals"] += 1
                bot["last_signal"]    = signal
                bot["last_error"]     = f"⏸ Max {max_t} trades open — waiting for SL/TP to close"
                threading.Thread(target=_save_bots, daemon=True).start()
                return

            if price <= 0: return
            if bot.get("fixed_amount", 0) > 0:
                amount = bot["fixed_amount"]
            elif bot.get("fixed_usd", 0) > 0:
                amount = round(bot["fixed_usd"] / price, 6)
            else:
                equity      = bot.get("demo_equity", bot.get("demo_balance", 1000))
                risk_usd    = equity * bot["risk_pct"] / 100
                atr_pct     = (atr / price) * 100 if price else 0
                size_factor = min(1.0, 0.5 / atr_pct) if atr_pct > 0.5 else 1.0
                amount      = round((risk_usd * bot["leverage"] * size_factor) / price, 4)
            if amount <= 0: return

            if bot.get("open_side") == signal and cur_t > 0:
                # Reinforcing the same side — fold into the existing position
                # (weighted-average entry) instead of leaving an earlier
                # "open" trade record that would never get closed or priced.
                old_amt, old_entry = bot["open_amount"], bot["open_entry_price"]
                total_amt = old_amt + amount
                avg_entry = (old_entry*old_amt + price*amount) / total_amt if total_amt else price
                bot["open_amount"]      = round(total_amt, 6)
                bot["open_entry_price"] = round(avg_entry, 4)
                if bot["trades"]:
                    bot["trades"][-1]["amount"] = bot["open_amount"]
                    bot["trades"][-1]["price"]  = bot["open_entry_price"]
                bot["open_trade_count"] = cur_t + 1
                bot["total_signals"]   += 1
                bot["last_signal"]      = signal
                bot["last_error"]       = None
                threading.Thread(target=_save_bots, daemon=True).start()
                return

            entry = {
                "time":     datetime.now().strftime("%Y-%m-%d %H:%M"),
                "mode":     bot.get("mode", "demo"),
                "signal":   signal,
                "price":    round(price, 4),
                "amount":   amount,
                "order_id": "demo",
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
            bot["open_amount"]       = amount
            bot["open_peak"]         = price
            bot["open_trough"]       = price
            bot["open_trade_count"]  = cur_t + 1
            bot["last_error"]        = None
            _tg_notify(
                f"<b>FarhanFX Crypto — Trade Opened (Demo)</b>\n"
                f"📊 <b>{bot['strategy']}</b> | {bot['symbol']}\n"
                f"{'🟢 BUY' if signal == 'BUY' else '🔴 SELL'} @ <code>${price:.4f}</code>\n"
                f"Amount: <code>{amount}</code>"
            )
            threading.Thread(target=_save_bots, daemon=True).start()

    except Exception as e:
        bot["last_error"] = str(e)[:200]
    finally:
        if _crypto_bots.get(bot_id, {}).get("status") == "active":
            interval = _TF_SECONDS.get(bot.get("timeframe", "1h"), 3600)
            t = threading.Timer(interval, _bot_tick_demo, args=[bot_id])
            t.daemon = True
            t.start()
            _bot_timers[bot_id] = t


def _bot_tick(bot_id):
    bot = _crypto_bots.get(bot_id)
    if not bot or bot["status"] != "active":
        return
    if bot.get("mode", "live") == "demo":
        return _bot_tick_demo(bot_id)
    try:
        pm = _get_pub_mkt()
        if not pm:
            return
        limit = _bot_lookback_bars(bot)
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
            ex    = _active_ex.get(bot.get("username", "admin"), {}).get(bot["exchange"])
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
                        closed_pnl = 0.0
                        for p in ex.fetch_positions():
                            if p.get("symbol") == bot["symbol"] and float(p.get("contracts") or 0) > 0:
                                closed_pnl = float(p.get("unrealizedPnl") or 0)
                                ex.create_order(bot["symbol"], "market", close_side,
                                                float(p["contracts"]), params={"reduceOnly": True})
                        bot["open_side"]        = None
                        bot["open_entry_price"] = None
                        bot["open_trade_count"] = 0
                        if bot.get("trades"):
                            bot["trades"][-1]["exit_reason"] = exit_reason
                            bot["trades"][-1]["exit_price"]  = round(price, 4)
                            bot["trades"][-1]["pnl"]         = round(closed_pnl, 4)
                            bot["trades"][-1]["status"]      = "closed"
                        _tg_notify(
                            f"<b>FarhanFX Crypto — Position Closed (LIVE)</b>\n"
                            f"📊 <b>{bot['strategy']}</b> | {bot['symbol']}\n"
                            f"{'✅' if closed_pnl >= 0 else '❌'} PnL: <code>${closed_pnl:.2f}</code> ({exit_reason})"
                        )
                        threading.Thread(target=_save_bots, daemon=True).start()
                    except Exception:
                        pass

        if signal:
            ex = _active_ex.get(bot.get("username", "admin"), {}).get(bot["exchange"])
            if not ex:
                return

            # Step 1: Close any opposite-side positions (signal flip)
            opp_closed = False
            opp_pnl    = 0.0
            try:
                for p in ex.fetch_positions():
                    sym_ok = p.get("symbol") == bot["symbol"]
                    pside  = (p.get("side") or "").lower()
                    sz     = float(p.get("contracts") or 0)
                    if sym_ok and sz > 0:
                        if (signal == "BUY" and pside == "short") or (signal == "SELL" and pside == "long"):
                            opp_pnl = float(p.get("unrealizedPnl") or 0)
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
                    bot["trades"][-1]["pnl"]         = round(opp_pnl, 4)
                    bot["trades"][-1]["status"]      = "closed"
                bot["open_trade_count"] = 0
                bot["open_side"]        = None
                _tg_notify(
                    f"<b>FarhanFX Crypto — Position Closed (LIVE)</b>\n"
                    f"📊 <b>{bot['strategy']}</b> | {bot['symbol']}\n"
                    f"{'✅' if opp_pnl >= 0 else '❌'} PnL: <code>${opp_pnl:.2f}</code> (signal_flip)"
                )

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
            if price <= 0: return
            if bot.get("fixed_amount", 0) > 0:
                amount = bot["fixed_amount"]
            elif bot.get("fixed_usd", 0) > 0:
                amount = round(bot["fixed_usd"] / price, 6)
            else:
                bal         = ex.fetch_balance()
                free        = float((bal.get("USDT") or {}).get("free") or 0)
                risk_usd    = free * bot["risk_pct"] / 100
                atr_pct     = (atr / price) * 100 if price else 0
                size_factor = min(1.0, 0.5 / atr_pct) if atr_pct > 0.5 else 1.0
                amount      = round((risk_usd * bot["leverage"] * size_factor) / price, 4)
            if amount <= 0: return

            try:
                ex.set_leverage(bot["leverage"], bot["symbol"])
            except Exception:
                pass
            _set_margin_mode_safe(ex, bot["symbol"], bot.get("margin_mode", "isolated"))

            # Step 4: Place order
            side  = "buy" if signal == "BUY" else "sell"
            order = ex.create_order(bot["symbol"], "market", side, amount)

            if bot.get("open_side") == signal and cur_t > 0:
                # Reinforcing the same side — fold into the existing position
                # (weighted-average entry) instead of leaving an earlier
                # "open" trade record that would never get closed or priced.
                old_amt, old_entry = bot.get("open_amount", 0), bot.get("open_entry_price", price)
                total_amt = old_amt + amount
                avg_entry = (old_entry*old_amt + price*amount) / total_amt if total_amt else price
                bot["open_amount"]      = round(total_amt, 6)
                bot["open_entry_price"] = round(avg_entry, 4)
                if bot["trades"]:
                    bot["trades"][-1]["amount"] = bot["open_amount"]
                    bot["trades"][-1]["price"]  = bot["open_entry_price"]
                bot["open_trade_count"] = cur_t + 1
                bot["total_signals"]   += 1
                bot["last_signal"]      = signal
                bot["last_error"]       = None
                threading.Thread(target=_save_bots, daemon=True).start()
                return

            entry = {
                "time":     datetime.now().strftime("%Y-%m-%d %H:%M"),
                "mode":     bot.get("mode", "live"),
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
            bot["open_amount"]       = amount
            bot["open_peak"]         = price
            bot["open_trough"]       = price
            bot["open_trade_count"]  = cur_t + 1
            bot["last_error"]        = None
            _tg_notify(
                f"<b>FarhanFX Crypto — Trade Opened (LIVE)</b>\n"
                f"📊 <b>{bot['strategy']}</b> | {bot['symbol']}\n"
                f"{'🟢 BUY' if signal == 'BUY' else '🔴 SELL'} @ <code>${price:.4f}</code>\n"
                f"Amount: <code>{amount}</code>"
            )
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
def crypto_algo_start(req: CryptoBotReq, current_user: dict = Depends(_get_current_user)):
    uname = current_user["username"]
    # Live mode needs a real connected exchange to place orders on. Demo mode
    # runs purely off public market data — no exchange connection required,
    # so a strategy can be paper-traded before the user ever adds API keys.
    if req.mode == "live":
        ex = _active_ex.get(uname, {}).get(req.exchange.lower())
        if not ex:
            return JSONResponse(status_code=400, content={"error": f"{req.exchange} not connected — connect it or switch this bot to Demo mode"})
    # A comma-separated symbol list creates one independent bot per symbol
    # — same proven single-symbol tick/signal/exit logic for each, just
    # grouped under one group_id so the UI can show them as a single
    # "Multiple Coins" card (mirroring how a multi-symbol bot is presented
    # elsewhere) instead of juggling several positions inside one bot.
    symbols = [s.strip() for s in req.symbol.split(",") if s.strip()]
    if not symbols:
        return JSONResponse(status_code=400, content={"error": "At least one symbol is required"})
    # Duplicate guard: same strategy + symbol + timeframe already running for this user
    for sym in symbols:
        for existing in _crypto_bots.values():
            if (existing.get("username") == uname
                    and existing.get("strategy") == req.strategy
                    and existing.get("symbol") == sym
                    and existing.get("timeframe") == req.timeframe
                    and existing.get("status") == "active"):
                return JSONResponse(status_code=400, content={
                    "error": f"{req.strategy.upper()} on {sym} {req.timeframe} is already running. "
                             f"Use a different pair or timeframe to run a second bot."
                })
    group_id = str(_uuid.uuid4())[:8]
    bot_ids = []
    for sym in symbols:
        bid = str(_uuid.uuid4())[:8]
        bot_ids.append(bid)
        _crypto_bots[bid] = {
            "id": bid, "username": uname, "exchange": req.exchange.lower(),
            "symbol": sym, "strategy": req.strategy,
            "group_id": group_id, "group_name": req.name.strip(),
            "timeframe": req.timeframe, "risk_pct": req.risk_pct,
            "fixed_amount": req.fixed_amount, "fixed_usd": req.fixed_usd,
            "leverage": req.leverage, "margin_mode": req.margin_mode,
            "mode": req.mode, "demo_balance": req.demo_balance,
            "demo_equity": req.demo_balance,
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
            # Breakout
            "bo_lookback": req.bo_lookback,
            # Trend breakout
            "dc_period": req.dc_period, "dc_ema": req.dc_ema,
            # VWAP bands
            "vwap_period": req.vwap_period, "vwap_std": req.vwap_std,
            # High WR strategy params
            "div_lookback": req.div_lookback, "swing_window": req.swing_window,
            "vwap_proximity": req.vwap_proximity,
            "vwap_rsi_os": 40, "vwap_rsi_ob": 60,   # VWAP+RSI uses direction thresholds, NOT reversal
            "orb_minutes": req.orb_minutes,
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
    return {"success": True, "bot_id": bot_ids[0], "bot_ids": bot_ids, "group_id": group_id}


@app.post("/api/crypto/algo/kill_all")
def crypto_algo_kill_all(current_user: dict = Depends(_get_current_user)):
    """Panic button: stop every one of this user's active bots AND flatten
    any open position immediately (unlike Stop, which just pauses new
    signals and leaves an existing position running)."""
    uname = current_user["username"]
    stopped, closed, errors = 0, 0, []
    for bid, bot in list(_crypto_bots.items()):
        if bot.get("username") != uname or bot.get("status") != "active":
            continue
        try:
            if bot.get("open_side"):
                try:
                    if bot.get("mode", "live") == "demo":
                        price = bot.get("last_price") or bot.get("open_entry_price")
                        if price:
                            ep, amt, oside = bot.get("open_entry_price", price), bot.get("open_amount", 0), bot["open_side"]
                            pnl = (price - ep) * amt if oside == "BUY" else (ep - price) * amt
                            bot["demo_equity"] = round(bot.get("demo_equity", bot.get("demo_balance", 1000)) + pnl, 4)
                            if bot.get("trades"):
                                bot["trades"][-1].update(exit_reason="emergency_kill", exit_price=round(price, 4),
                                                          pnl=round(pnl, 4), status="closed")
                            closed += 1
                    else:
                        ex = _active_ex.get(uname, {}).get(bot["exchange"])
                        if ex:
                            close_side = "sell" if bot["open_side"] == "BUY" else "buy"
                            for p in ex.fetch_positions():
                                if p.get("symbol") == bot["symbol"] and float(p.get("contracts") or 0) > 0:
                                    ex.create_order(bot["symbol"], "market", close_side,
                                                     float(p["contracts"]), params={"reduceOnly": True})
                            if bot.get("trades"):
                                bot["trades"][-1].update(exit_reason="emergency_kill", status="closed")
                            closed += 1
                except Exception as e:
                    errors.append(f"{bid}: close failed ({e})")
                bot["open_side"] = None
                bot["open_entry_price"] = None
                bot["open_amount"] = 0
                bot["open_trade_count"] = 0
            bot["status"] = "stopped"
            t = _bot_timers.pop(bid, None)
            if t:
                t.cancel()
            stopped += 1
        except Exception as e:
            errors.append(f"{bid}: {e}")
    _save_bots()
    if stopped:
        _tg_notify(f"<b>FarhanFX Crypto — 🛑 EMERGENCY KILL ALL</b>\n{stopped} bot(s) stopped, {closed} open position(s) flattened.")
    return {"success": True, "stopped": stopped, "closed": closed, "errors": errors}


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
def crypto_algo_stop(bot_id: str, current_user: dict = Depends(_get_current_user)):
    bot = _crypto_bots.get(bot_id)
    if not bot or bot.get("username") != current_user["username"]:
        return JSONResponse(status_code=404, content={"error": "Bot not found"})
    bot["status"] = "stopped"
    t = _bot_timers.pop(bot_id, None)
    if t:
        t.cancel()
    _save_bots()
    return {"success": True}


@app.post("/api/crypto/algo/{bot_id}/promote")
def crypto_algo_promote(bot_id: str, current_user: dict = Depends(_get_current_user)):
    """Switch a demo (paper-trading) bot to live real-money trading. Requires
    the exchange to be connected and closes out any open simulated position
    first — a promoted bot always starts its live life flat."""
    bot = _crypto_bots.get(bot_id)
    if not bot or bot.get("username") != current_user["username"]:
        return JSONResponse(status_code=404, content={"error": "Bot not found"})
    if bot.get("mode", "live") != "demo":
        return JSONResponse(status_code=400, content={"error": "Bot is already live"})
    ex = _active_ex.get(current_user["username"], {}).get(bot["exchange"])
    if not ex:
        return JSONResponse(status_code=400, content={"error": f"{bot['exchange']} not connected — connect it before promoting to live"})
    if bot.get("open_side"):
        if bot.get("trades"):
            bot["trades"][-1]["exit_reason"] = "promoted_to_live"
            bot["trades"][-1]["exit_price"]  = bot.get("last_price")
            bot["trades"][-1]["status"]      = "closed"
        bot["open_side"]        = None
        bot["open_entry_price"] = None
        bot["open_amount"]      = 0
        bot["open_trade_count"] = 0
    bot["mode"] = "live"
    _save_bots()
    return {"success": True, "mode": "live"}


@app.post("/api/crypto/algo/toggle_all_mode")
def crypto_algo_toggle_all_mode(current_user: dict = Depends(_get_current_user)):
    """Flip every active bot between Demo and Live in one click. Demo->Live
    bots need their exchange connected (same requirement as the per-bot
    Promote button) — any that aren't connected are skipped, not failed.
    Live->Demo bots get their real position flattened first, same as Kill
    All, since a live position can't be carried into paper trading."""
    uname = current_user["username"]
    promoted, demoted, skipped = 0, 0, []
    for bid, bot in list(_crypto_bots.items()):
        if bot.get("username") != uname or bot.get("status") != "active":
            continue
        try:
            if bot.get("mode", "live") == "demo":
                ex = _active_ex.get(uname, {}).get(bot["exchange"])
                if not ex:
                    skipped.append(f"{bid}: {bot['exchange']} not connected")
                    continue
                if bot.get("open_side"):
                    if bot.get("trades"):
                        bot["trades"][-1].update(exit_reason="toggled_to_live", exit_price=bot.get("last_price"), status="closed")
                    bot["open_side"] = None
                    bot["open_entry_price"] = None
                    bot["open_amount"] = 0
                    bot["open_trade_count"] = 0
                bot["mode"] = "live"
                promoted += 1
            else:
                if bot.get("open_side"):
                    try:
                        ex = _active_ex.get(uname, {}).get(bot["exchange"])
                        if ex:
                            close_side = "sell" if bot["open_side"] == "BUY" else "buy"
                            for p in ex.fetch_positions():
                                if p.get("symbol") == bot["symbol"] and float(p.get("contracts") or 0) > 0:
                                    ex.create_order(bot["symbol"], "market", close_side,
                                                     float(p["contracts"]), params={"reduceOnly": True})
                    except Exception as e:
                        skipped.append(f"{bid}: close failed ({e})")
                    if bot.get("trades"):
                        bot["trades"][-1].update(exit_reason="toggled_to_demo", status="closed")
                    bot["open_side"] = None
                    bot["open_entry_price"] = None
                    bot["open_amount"] = 0
                    bot["open_trade_count"] = 0
                bot["mode"] = "demo"
                demoted += 1
        except Exception as e:
            skipped.append(f"{bid}: {e}")
    _save_bots()
    if promoted or demoted:
        _tg_notify(f"<b>FarhanFX Crypto — ⇄ Toggle Live/Paper</b>\n{promoted} bot(s) → LIVE, {demoted} bot(s) → DEMO.")
    return {"success": True, "promoted": promoted, "demoted": demoted, "skipped": skipped}


@app.delete("/api/crypto/algo/{bot_id}")
def crypto_algo_delete(bot_id: str, current_user: dict = Depends(_get_current_user)):
    bot = _crypto_bots.get(bot_id)
    if not bot or bot.get("username") != current_user["username"]:
        return JSONResponse(status_code=404, content={"error": "Bot not found"})
    t = _bot_timers.pop(bot_id, None)
    if t:
        t.cancel()
    _crypto_bots.pop(bot_id, None)
    _save_bots()
    return {"success": True}


@app.get("/api/crypto/algo/history")
def crypto_algo_history(current_user: dict = Depends(_get_current_user)):
    """Return this user's trade records from their own bots, newest first."""
    rows = []
    for bid, b in _crypto_bots.items():
        if b.get("username") != current_user["username"]:
            continue
        for t in b.get("trades", []):
            rows.append({
                "bot_id":   bid,
                "mode":     b.get("mode", "live"),
                "exchange": b.get("exchange", ""),
                "symbol":   b.get("symbol", ""),
                "strategy": b.get("strategy", ""),
                **t,
            })
    rows.sort(key=lambda x: x.get("time", ""), reverse=True)
    return rows[:300]


@app.get("/api/crypto/reports/summary")
def crypto_reports_summary(mode: str = "live", current_user: dict = Depends(_get_current_user)):
    """Full analytics across this user's crypto bots — by-session win rate,
    by-symbol breakdown (for a pie chart), daily equity curve, best day/month.
    Same shape regardless of mode so Demo and Live can be compared the same
    way. mode: 'live' (default) | 'demo' | 'all'."""
    uname = current_user["username"]
    closed_trades = []
    open_pnl_total = 0.0
    for b in _crypto_bots.values():
        if b.get("username") != uname:
            continue
        if mode != "all" and b.get("mode", "live") != mode:
            continue
        for t in b.get("trades", []):
            if t.get("status") == "closed":
                closed_trades.append({**t, "symbol": b.get("symbol", ""), "strategy": b.get("strategy", "")})
        if b.get("open_side") and b.get("last_price"):
            ep, amt, mark = b.get("open_entry_price", 0), b.get("open_amount", 0), b["last_price"]
            if b.get("mode", "live") == "demo":
                open_pnl_total += (mark - ep) * amt if b["open_side"] == "BUY" else (ep - mark) * amt
            elif b.get("status") == "active":
                ex = _active_ex.get(uname, {}).get(b["exchange"])
                if ex:
                    try:
                        for p in ex.fetch_positions():
                            if p.get("symbol") == b["symbol"] and float(p.get("contracts") or 0) > 0:
                                open_pnl_total += float(p.get("unrealizedPnl") or 0)
                                break
                    except Exception:
                        pass

    if not closed_trades:
        return {
            "summary": {"total_trades": 0, "win_rate": None, "profit_factor": None, "net_pnl": 0.0,
                        "open_pnl": round(open_pnl_total, 4)},
            "by_session": [], "by_symbol": [], "daily": [], "monthly": [],
            "best_day": {}, "best_month": {}, "equity_curve": [],
        }

    report = _aggregate_trade_report(closed_trades)
    return {
        "summary": {**report["stats"], "total_trades": len(closed_trades), "open_pnl": round(open_pnl_total, 4)},
        "by_session": report["by_session"],
        "by_symbol": report["by_symbol"],
        "daily": report["daily"],
        "monthly": report["monthly"],
        "best_day": report["best_day"],
        "best_month": report["best_month"],
        "equity_curve": report["equity_curve"],
    }


def _session_name(hour: int) -> str:
    if 0 <= hour < 7:
        return "Asian"
    if 7 <= hour < 13:
        return "London"
    if 13 <= hour < 21:
        return "New York"
    return "Off-Hours"


def _aggregate_trade_report(closed_trades: list) -> dict:
    """Shared aggregation: turns a flat list of closed trades (each needs at
    least time/pnl/symbol) into session/symbol/daily/monthly breakdowns, best
    day/month, an equity curve, and win-rate/profit-factor/net-pnl stats.
    Used by both the live/demo reports endpoint and the strategy backtester."""
    def _parse_time(t):
        try:
            return datetime.strptime(t["time"], "%Y-%m-%d %H:%M")
        except Exception:
            return datetime.now()

    closed_trades = sorted(closed_trades, key=lambda t: t.get("time", ""))

    by_session = {s: {"session": s, "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
                  for s in ["Asian", "London", "New York", "Off-Hours"]}
    by_symbol: dict = {}
    daily:     dict = {}
    monthly:   dict = {}
    running = 0.0
    equity_curve = []

    for t in closed_trades:
        pnl = t.get("pnl", 0.0)
        dt  = _parse_time(t)
        sess = _session_name(dt.hour)
        by_session[sess]["trades"] += 1
        by_session[sess]["pnl"]     = round(by_session[sess]["pnl"] + pnl, 4)
        if pnl > 0: by_session[sess]["wins"]   += 1
        else:       by_session[sess]["losses"] += 1

        sym = t.get("symbol", "")
        by_symbol.setdefault(sym, {"symbol": sym, "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        by_symbol[sym]["trades"] += 1
        by_symbol[sym]["pnl"]     = round(by_symbol[sym]["pnl"] + pnl, 4)
        if pnl > 0: by_symbol[sym]["wins"]   += 1
        else:       by_symbol[sym]["losses"] += 1

        dkey = dt.strftime("%Y-%m-%d")
        daily.setdefault(dkey, {"date": dkey, "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        daily[dkey]["trades"] += 1
        daily[dkey]["pnl"]     = round(daily[dkey]["pnl"] + pnl, 4)
        if pnl > 0: daily[dkey]["wins"]   += 1
        else:       daily[dkey]["losses"] += 1

        mkey = dt.strftime("%Y-%m")
        monthly.setdefault(mkey, {"month": mkey, "label": dt.strftime("%b %Y"), "trades": 0, "wins": 0, "pnl": 0.0})
        monthly[mkey]["trades"] += 1
        monthly[mkey]["pnl"]     = round(monthly[mkey]["pnl"] + pnl, 4)
        if pnl > 0: monthly[mkey]["wins"] += 1

        running += pnl
        equity_curve.append({"time": t.get("time", ""), "equity": round(running, 4)})

    for s in by_session.values():
        s["win_rate"] = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] else None
    for s in by_symbol.values():
        s["win_rate"] = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] else None

    daily_list   = sorted(daily.values(),   key=lambda x: x["date"])
    monthly_list = sorted(monthly.values(), key=lambda x: x["month"])
    best_day   = max(daily_list,   key=lambda x: x["pnl"]) if daily_list   else {}
    best_month = max(monthly_list, key=lambda x: x["pnl"]) if monthly_list else {}

    return {
        "stats": _bot_stats(closed_trades),
        "by_session": list(by_session.values()),
        "by_symbol": sorted(by_symbol.values(), key=lambda x: x["trades"], reverse=True),
        "daily": daily_list,
        "monthly": monthly_list,
        "best_day": best_day,
        "best_month": best_month,
        "equity_curve": equity_curve,
    }


def _bot_stats(trades: list) -> dict:
    """Win rate / profit factor / net PnL from a bot's closed trades — same
    shape whether the trades are demo (simulated) or live (real fills)."""
    closed = [t for t in trades if t.get("status") == "closed"]
    if not closed:
        return {"closed_trades": 0, "win_rate": None, "profit_factor": None, "net_pnl": 0.0}
    wins   = [t["pnl"] for t in closed if t.get("pnl", 0) > 0]
    losses = [t["pnl"] for t in closed if t.get("pnl", 0) <= 0]
    gross_w, gross_l = sum(wins), abs(sum(losses))
    return {
        "closed_trades": len(closed),
        "win_rate":      round(len(wins) / len(closed) * 100, 1),
        "profit_factor": round(gross_w / gross_l, 2) if gross_l else (999 if gross_w else None),
        "net_pnl":       round(sum(t.get("pnl", 0) for t in closed), 4),
    }


# ── STRATEGY BACKTESTER — 1-year historical run, all strategies at once ──────
_ALL_CRYPTO_STRATEGIES = ["rsi", "macd_cross", "bb_squeeze", "false_breakout", "trend_breakout", "vwap_bands",
                          "rsi_divergence", "vwap_rsi", "bb_rsi_strict", "orb",
                          "fvg", "liquidity_sweep", "ob_fvg", "silver_bullet",
                          "funding_rate", "volume_profile", "ifvg", "bos_choch"]

_backtest_data_cache: dict = {}   # f"{symbol}:{timeframe}:{days}" -> {"data": [...], "fetched_at": ts}


def _fetch_backtest_ohlcv(symbol: str = "BTC/USDT", timeframe: str = "1h", days: int = 365) -> list:
    """1 year (default) of historical OHLCV from Binance public spot data —
    cached for an hour since a full fetch takes a while and the same window
    is reused across all 13 strategies in one backtest run."""
    cache_key = f"{symbol}:{timeframe}:{days}"
    cached = _backtest_data_cache.get(cache_key)
    if cached and (_time.time() - cached["fetched_at"]) < 3600:
        return cached["data"]

    ex = _ccxt.binance()
    since = ex.parse8601((datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    all_rows = []
    while True:
        batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
        if not batch:
            break
        all_rows += batch
        if len(batch) < 1000:
            break
        since = batch[-1][0] + 1
        if len(all_rows) > 20000:
            break
    _backtest_data_cache[cache_key] = {"data": all_rows, "fetched_at": _time.time()}
    return all_rows


def _run_backtest_strategy(strategy: str, ohlcv: list, symbol: str, timeframe: str,
                            demo_balance: float = 1000.0, risk_pct: float = 1.0,
                            leverage: int = 10, param_overrides: dict | None = None) -> dict:
    """Synchronous, bar-by-bar replay of the exact same signal + demo-fill
    logic the live paper-trading bot uses (_bot_tick_demo), but over historical
    candles instead of a live feed. Every bar only sees a bounded trailing
    window — the same size a live/demo bot would actually fetch for this
    strategy (see _bot_lookback_bars) — so there's no lookahead bias and the
    backtest is a faithful preview of how the bot behaves for real.
    param_overrides lets the strategy-research grid search reuse this exact
    simulation loop with different indicator settings instead of duplicating it."""
    bot = {
        "strategy": strategy, "symbol": symbol, "timeframe": timeframe,
        "risk_pct": risk_pct, "leverage": leverage, "max_open_trades": 2,
        "demo_balance": demo_balance, "demo_equity": demo_balance,
        "fast_ema": 9, "slow_ema": 21,
        "rsi_period": 7, "rsi_ob": 75, "rsi_os": 25,
        "macd_fast": 12, "macd_slow": 17, "macd_signal": 9,
        "bb_period": 30, "bb_std": 2.5,
        "atr_period": 14, "st_multiplier": 3.0,
        "ai_min_score": 65, "trailing_atr": 0.0, "tp_atr": 0.0, "adx_min": 0,
        "bo_lookback": 20, "dc_period": 55, "dc_ema": 150, "fixed_amount": 0.0, "fixed_usd": 0.0,
        "vwap_period": 14, "vwap_std": 2.5,
        "open_side": None, "open_entry_price": None, "open_amount": 0,
        "open_trade_count": 0, "open_peak": None, "open_trough": None,
        "trades": [],
    }
    if strategy == "trend_breakout":
        bot["trailing_atr"] = 2.0  # exits via trailing stop, not signal-flip
    if strategy == "vwap_bands":
        bot["trailing_atr"], bot["tp_atr"] = 2.0, 2.5  # exits via TP/trailing stop, not signal-flip
    if param_overrides:
        bot.update(param_overrides)
    window = _bot_lookback_bars(bot)

    def _close(exit_price, exit_reason):
        ep, oside, amt = bot["open_entry_price"], bot["open_side"], bot["open_amount"]
        pnl = (exit_price - ep) * amt if oside == "BUY" else (ep - exit_price) * amt
        bot["demo_equity"] = round(bot["demo_equity"] + pnl, 4)
        if bot["trades"]:
            bot["trades"][-1]["exit_reason"] = exit_reason
            bot["trades"][-1]["exit_price"]  = round(exit_price, 4)
            bot["trades"][-1]["pnl"]         = round(pnl, 4)
            bot["trades"][-1]["status"]      = "closed"
        bot["open_side"] = None
        bot["open_entry_price"] = None
        bot["open_amount"] = 0
        bot["open_trade_count"] = 0

    equity_peak = demo_balance
    max_dd_pct  = 0.0
    n = len(ohlcv)

    for i in range(window - 1, n):
        win = ohlcv[i - window + 1:i + 1]
        closes = [c[4] for c in win]
        highs  = [c[2] for c in win]
        lows   = [c[3] for c in win]
        price  = closes[-1]
        ts     = win[-1][0]

        try:
            signal = _get_bot_signal(bot, win)
        except Exception:
            signal = None
        try:
            atr = _atr_calc(highs, lows, closes, 14)
        except Exception:
            atr = 0

        if bot["open_side"] and (bot["trailing_atr"] > 0 or bot["tp_atr"] > 0):
            ep, oside = bot["open_entry_price"], bot["open_side"]
            trail = bot["trailing_atr"] * atr
            tp    = bot["tp_atr"] * atr
            should_exit, exit_reason = False, ""
            if oside == "BUY":
                bot["open_peak"] = max(bot["open_peak"], price)
                if trail > 0 and price < bot["open_peak"] - trail:
                    should_exit, exit_reason = True, "trailing_stop"
                if tp > 0 and price >= ep + tp:
                    should_exit, exit_reason = True, "take_profit"
            else:
                bot["open_trough"] = min(bot["open_trough"], price)
                if trail > 0 and price > bot["open_trough"] + trail:
                    should_exit, exit_reason = True, "trailing_stop"
                if tp > 0 and price <= ep - tp:
                    should_exit, exit_reason = True, "take_profit"
            if should_exit:
                _close(price, exit_reason)

        if signal:
            if bot["open_side"] and bot["open_side"] != signal:
                _close(price, "signal_flip")
            if bot["open_trade_count"] < bot["max_open_trades"] and price > 0:
                if bot.get("fixed_amount", 0) > 0:
                    amount = bot["fixed_amount"]
                elif bot.get("fixed_usd", 0) > 0:
                    amount = round(bot["fixed_usd"] / price, 6)
                else:
                    equity      = bot["demo_equity"]
                    risk_usd    = equity * bot["risk_pct"] / 100
                    atr_pct     = (atr / price) * 100 if price else 0
                    size_factor = min(1.0, 0.5 / atr_pct) if atr_pct > 0.5 else 1.0
                    amount      = round((risk_usd * bot["leverage"] * size_factor) / price, 6)
                if amount > 0 and bot["open_side"] == signal and bot["open_trade_count"] > 0:
                    # Reinforcing the same side — fold into the existing
                    # position instead of leaving an earlier "open" trade
                    # record that would never get closed/priced.
                    old_amt, old_entry = bot["open_amount"], bot["open_entry_price"]
                    total_amt = old_amt + amount
                    avg_entry = (old_entry*old_amt + price*amount) / total_amt if total_amt else price
                    bot["open_amount"]      = round(total_amt, 6)
                    bot["open_entry_price"] = round(avg_entry, 4)
                    if bot["trades"]:
                        bot["trades"][-1]["amount"] = bot["open_amount"]
                        bot["trades"][-1]["price"]  = bot["open_entry_price"]
                    bot["open_trade_count"] += 1
                elif amount > 0:
                    bot["trades"].append({
                        "time":     datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M"),
                        "signal":   signal,
                        "price":    round(price, 4),
                        "amount":   amount,
                        "order_id": "backtest",
                        "pnl":      0,
                        "status":   "open",
                    })
                    bot["open_side"]        = signal
                    bot["open_entry_price"] = price
                    bot["open_amount"]      = amount
                    bot["open_peak"]        = price
                    bot["open_trough"]      = price
                    bot["open_trade_count"] += 1

        if bot["demo_equity"] > equity_peak:
            equity_peak = bot["demo_equity"]
        elif equity_peak > 0:
            dd = (equity_peak - bot["demo_equity"]) / equity_peak * 100
            if dd > max_dd_pct:
                max_dd_pct = dd

    closed = [t for t in bot["trades"] if t.get("status") == "closed"]
    report = _aggregate_trade_report([{**t, "symbol": symbol} for t in closed]) if closed else {
        "stats": {"closed_trades": 0, "win_rate": None, "profit_factor": None, "net_pnl": 0.0},
        "best_day": {}, "best_month": {}, "equity_curve": [],
    }
    return {
        "strategy":         strategy,
        "total_trades":     len(closed),
        "win_rate":         report["stats"]["win_rate"],
        "profit_factor":    report["stats"]["profit_factor"],
        "net_pnl":          report["stats"]["net_pnl"],
        "net_pnl_pct":      round(report["stats"]["net_pnl"] / demo_balance * 100, 2) if demo_balance else 0,
        "final_equity":     bot["demo_equity"],
        "max_drawdown_pct": round(max_dd_pct, 2),
        "best_day":         report["best_day"],
        "best_month":       report["best_month"],
        "equity_curve":     report["equity_curve"],
    }


@app.get("/api/crypto/backtest/all")
def crypto_backtest_all(symbol: str = "BTC/USDT", timeframe: str = "1h", days: int = 365,
                         current_user: dict = Depends(_get_current_user)):
    """Run every crypto strategy through a 1-year (default) historical replay
    in paper-trading mode and return a side-by-side performance report — lets
    you pick a strategy before pointing real money (or even a live demo bot)
    at it."""
    if not _CCXT_OK:
        return JSONResponse(status_code=503, content={"error": "ccxt not available on this server"})
    try:
        ohlcv = _fetch_backtest_ohlcv(symbol, timeframe, days)
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": f"Failed to fetch historical data: {e}"})
    if len(ohlcv) < 250:
        return JSONResponse(status_code=502, content={"error": "Not enough historical data returned"})

    results = []
    for strategy in _ALL_CRYPTO_STRATEGIES:
        try:
            results.append(_run_backtest_strategy(strategy, ohlcv, symbol, timeframe))
        except Exception as e:
            results.append({"strategy": strategy, "error": str(e)[:200]})

    results.sort(key=lambda r: r.get("net_pnl", -1e18) if "error" not in r else -1e18, reverse=True)

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "days": days,
        "bars": len(ohlcv),
        "data_from": datetime.utcfromtimestamp(ohlcv[0][0] / 1000).strftime("%Y-%m-%d"),
        "data_to":   datetime.utcfromtimestamp(ohlcv[-1][0] / 1000).strftime("%Y-%m-%d"),
        "results": results,
    }


# ── COIN SCREENER — finds high-volume coins, then re-validates every
# strategy's already-known-good params against THAT coin's own history
# before calling anything "approved". Volume alone never implies edge (see
# the multi-symbol research earlier this engagement: BTC-validated params
# failed train/validation on ETH/SOL/BNB/XRP) — this is read-only and never
# starts a bot by itself; that decision stays manual. ─────────────────────
_STRATEGY_DEFAULTS = {
    "rsi":            ("15m", {"rsi_period": 7, "rsi_ob": 75, "rsi_os": 25}),
    "macd_cross":     ("15m", {"macd_fast": 12, "macd_slow": 17, "macd_signal": 9}),
    "bb_squeeze":     ("1h",  {"bb_period": 30, "bb_std": 2.5}),
    "false_breakout": ("1h",  {}),
    "trend_breakout": ("15m", {"dc_period": 55, "dc_ema": 150, "trailing_atr": 2.0, "tp_atr": 0.0}),
    "vwap_bands":      ("15m", {"vwap_period": 14, "vwap_std": 2.5, "trailing_atr": 2.0, "tp_atr": 2.5}),
    "fvg":             ("1h",  {"fvg_lookback": 50, "fvg_min_gap": 0.3, "trailing_atr": 1.5, "tp_atr": 2.0}),
    "liquidity_sweep": ("1h",  {"liq_lookback": 20, "liq_wick_mult": 1.5, "trailing_atr": 1.5, "tp_atr": 2.5}),
    "ob_fvg":          ("1h",  {"ob_lookback": 30, "fvg_lookback": 50, "ob_impulse_mult": 1.5, "trailing_atr": 1.5, "tp_atr": 2.5}),
    "silver_bullet":   ("15m", {"fvg_lookback": 30, "fvg_min_gap": 0.2, "trailing_atr": 1.5, "tp_atr": 2.0}),
    "funding_rate":    ("1h",  {"fr_buy_thresh": -0.04, "fr_sell_thresh": 0.08, "rsi_period": 14, "rsi_ob": 65, "rsi_os": 35, "trailing_atr": 1.5, "tp_atr": 2.0}),
    "volume_profile":  ("1h",  {"vp_lookback": 100, "vp_min_bars": 30, "trailing_atr": 1.5, "tp_atr": 2.0}),
    "ifvg":            ("1h",  {"fvg_lookback": 60, "fvg_min_gap": 0.3, "trailing_atr": 1.5, "tp_atr": 2.0}),
    "bos_choch":       ("15m", {"bos_lookback": 30, "trailing_atr": 1.5, "tp_atr": 2.5}),
}
_SCREENER_EXCLUDE_BASES = {
    "XAU", "XAG", "CL", "UKOIL", "USOIL",                                  # commodities
    "SOXL", "SKHYNIX", "MU", "NVDA", "TSLA", "MSFT", "AAPL", "SNDK", "SPCX", # tokenized stocks
}


def _screener_top_coins(limit: int = 12) -> list:
    """Top USDT-margined perpetual futures by 24h quote volume — excludes
    BTC/ETH (already have dedicated bots) and non-crypto commodity perps
    Binance also lists alongside them."""
    ex = _ccxt.binanceusdm()
    tickers = ex.fetch_tickers()
    rows = []
    for sym, t in tickers.items():
        if not sym.endswith("/USDT:USDT"):
            continue
        base = sym.split("/")[0]
        if base in ("BTC", "ETH") or base in _SCREENER_EXCLUDE_BASES:
            continue
        vol = t.get("quoteVolume") or 0
        if vol > 0:
            rows.append({"symbol": sym, "base": base, "volume_24h": round(vol, 0),
                         "change_24h": t.get("percentage")})
    rows.sort(key=lambda x: x["volume_24h"], reverse=True)
    return rows[:limit]


def _screener_validate_coin(symbol: str, days: int = 120) -> dict:
    """Re-run every strategy's validated params against this coin's own
    recent history (train/validation split) — same pass bar used throughout
    this engagement: PF > 1.1 on both halves with a minimum trade count."""
    results = {}
    for strategy, (tf, params) in _STRATEGY_DEFAULTS.items():
        try:
            ohlcv = _fetch_backtest_ohlcv(symbol, tf, days)
            if len(ohlcv) < 300:
                results[strategy] = {"status": "insufficient_data"}
                continue
            half = len(ohlcv) // 2
            train, val = ohlcv[:half], ohlcv[half:]
            train_r = _run_backtest_strategy(strategy, train, symbol, tf, param_overrides=params)
            val_r   = _run_backtest_strategy(strategy, val, symbol, tf, param_overrides=params)
            passed = (train_r["total_trades"] >= 10 and val_r["total_trades"] >= 5 and
                      (train_r["profit_factor"] or 0) > 1.1 and (val_r["profit_factor"] or 0) > 1.1)
            results[strategy] = {
                "status": "pass" if passed else "fail",
                "timeframe": tf,
                "train_pf": train_r["profit_factor"], "val_pf": val_r["profit_factor"],
                "train_trades": train_r["total_trades"], "val_trades": val_r["total_trades"],
                "val_net_pnl": val_r["net_pnl"],
            }
        except Exception as e:
            results[strategy] = {"status": "error", "error": str(e)[:150]}
    return results


@app.get("/api/crypto/screener/scan")
def crypto_screener_scan(limit: int = 12, days: int = 120,
                          current_user: dict = Depends(_get_current_user)):
    """Screen top-volume coins and re-validate every strategy's existing
    params against each one's own history. Read-only report — does not
    start, stop, or modify any bot."""
    if not _CCXT_OK:
        return JSONResponse(status_code=503, content={"error": "ccxt not available on this server"})
    try:
        coins = _screener_top_coins(limit)
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": f"Failed to fetch market list: {e}"})

    report = []
    for coin in coins:
        validation = _screener_validate_coin(coin["symbol"], days)
        approved = [s for s, r in validation.items() if r.get("status") == "pass"]
        report.append({**coin, "validation": validation, "approved_strategies": approved})

    report.sort(key=lambda c: len(c["approved_strategies"]), reverse=True)
    return {"scanned": len(report), "days": days, "coins": report}


@app.get("/api/crypto/algo/list")
def crypto_algo_list(live: bool = False, current_user: dict = Depends(_get_current_user)):
    result = []
    for b in _crypto_bots.values():
        if b.get("username") != current_user["username"]:
            continue
        entry = {k: v for k, v in b.items() if k != "trades"} | {
            "trade_count":  len(b.get("trades", [])),
            "recent_trades": b.get("trades", [])[-5:],
            "stats":        _bot_stats(b.get("trades", [])),
            "live_pnl":     None,
            "live_size":    None,
            "live_mark":    None,
        }
        # Demo bots: show simulated unrealized P&L on the open paper position
        if b.get("mode", "live") == "demo" and b.get("open_side") and b.get("last_price"):
            ep, amt, mark = b.get("open_entry_price", 0), b.get("open_amount", 0), b["last_price"]
            upnl = (mark - ep) * amt if b["open_side"] == "BUY" else (ep - mark) * amt
            entry["live_pnl"]  = round(upnl, 4)
            entry["live_size"] = amt
            entry["live_mark"] = mark
        # Fetch live unrealized PnL for active in-position LIVE bots
        elif live and b.get("open_side") and b.get("status") == "active":
            ex = _active_ex.get(current_user["username"], {}).get(b["exchange"])
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

try:
    from telethon import TelegramClient as _TelethonClient
    from telethon import events as _telethon_events
    from telethon.tl.types import PeerChannel as _TelethonPeerChannel
    _TELETHON_OK = True
except ImportError:
    _TelethonClient = None
    _telethon_events = None
    _TelethonPeerChannel = None
    _TELETHON_OK = False

_TG_FILE         = "telegram_cfg.json"
_TG_SIGNALS_FILE = "telegram_signals.json"
_TG_SIGNALS  : list = []          # last 100 parsed signals, persisted to disk
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
    "mode":           "demo",        # "demo" (paper trade, no real orders) | "live" (real money)
    "demo_balance":   1000.0,        # virtual starting balance for demo mode
}

# ── Telegram demo (paper-trade) state — separate file since it's runtime
# state, not configuration, and grows over time as signals come in.
_TG_DEMO_FILE = "telegram_demo_state.json"


def _load_tg_demo_state() -> dict:
    try:
        with open(_TG_DEMO_FILE) as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("equity", _TG_CFG_DEF["demo_balance"])
    st.setdefault("positions", {})   # symbol -> {side, entry, amount}
    st.setdefault("trades", [])      # closed trades: {time, symbol, side, entry, exit, amount, pnl}
    return st


def _save_tg_demo_state(st: dict):
    with open(_TG_DEMO_FILE, "w") as f:
        json.dump(st, f, indent=2)


def _load_tg_signals() -> list:
    try:
        with open(_TG_SIGNALS_FILE) as f:
            data = json.load(f)
            if isinstance(data, list):
                return data[:200]
    except Exception:
        pass
    return []


def _save_tg_signals():
    try:
        with open(_TG_SIGNALS_FILE, "w") as f:
            json.dump(_TG_SIGNALS[:200], f, indent=2, default=str)
    except Exception:
        pass



def _load_tg_cfg() -> dict:
    try:
        with open(_TG_FILE, encoding="utf-8-sig") as f:
            cfg = json.load(f)
        for k, v in _TG_CFG_DEF.items():
            cfg.setdefault(k, v)
        return cfg
    except Exception:
        return dict(_TG_CFG_DEF)


def _save_tg_cfg(cfg: dict):
    with open(_TG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ── TELEGRAM USERBOT — reads channels the user is a member of but not an
# admin of (the Bot API can only read channels a bot is an admin in). Logs
# in once as the user's own Telegram account (see telegram_userbot_cfg.json
# + telegram_userbot_session.session, created by a one-time interactive
# login), then listens for new posts in one specific channel and feeds them
# into the exact same parse → demo/live execution pipeline as the Bot API
# poller, so both sources show up in the same Signal Log / reports.
_TG_USERBOT_FILE = "telegram_userbot_cfg.json"
_TG_USERBOT_SESSION = "telegram_userbot_session"


def _load_tg_userbot_cfg() -> dict | None:
    try:
        with open(_TG_USERBOT_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        if cfg.get("api_id") and cfg.get("api_hash") and cfg.get("channel"):
            return cfg
    except Exception:
        pass
    return None


def _tg_userbot_thread():
    if not _TELETHON_OK:
        print("Telegram userbot: telethon not installed, skipping")
        return
    cfg = _load_tg_userbot_cfg()
    if not cfg:
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    client = _TelethonClient(_TG_USERBOT_SESSION, cfg["api_id"], cfg["api_hash"], loop=loop)
    # Normalise the channel ID. Users typically copy the full -100XXXXXXXXX form
    # from Telegram; we may have stored it with or without the leading minus.
    # PeerChannel expects the raw ID (no 100-prefix), so strip it if present.
    _ch = int(cfg["channel"])
    if _ch > 0:
        _ch = -_ch          # ensure negative
    # Now _ch is like -1001161903127; PeerChannel needs just 1161903127
    _ch_raw = abs(_ch) - 1000000000000 if abs(_ch) > 1000000000000 else abs(_ch)
    channel_peer = _TelethonPeerChannel(_ch_raw)

    @client.on(_telethon_events.NewMessage(chats=channel_peer))
    async def _on_message(event):
        try:
            text = event.message.message or ""
            sig = _parse_tg_signal(text)
            if not sig:
                return
            tg_cfg = _load_tg_cfg()
            if not (tg_cfg.get("auto_trade") and tg_cfg.get("enabled")):
                return
            rec = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": sig["symbol"], "side": sig["side"], "entry": sig.get("entry"),
                "tp": sig.get("tp"), "sl": sig.get("sl"), "leverage": sig.get("leverage"),
                "raw": sig.get("raw", "")[:200], "status": "received", "results": [],
                "chat_id": str(cfg["channel"]), "source": "userbot",
            }
            is_demo = tg_cfg.get("mode", "demo") == "demo"
            results = _tg_execute_signal_demo(sig, tg_cfg) if is_demo else _tg_execute_signal(sig, tg_cfg)
            rec["results"] = results
            rec["mode"]    = "demo" if is_demo else "live"
            rec["status"]  = ("demo_executed" if is_demo else "executed") if any(r["ok"] for r in results) else "failed"
            _TG_SIGNALS.insert(0, rec)
            if len(_TG_SIGNALS) > 100:
                _TG_SIGNALS.pop()
            _save_tg_signals()
            print(f"Telegram userbot signal: {sig['side'].upper()} {sig['symbol']} → {rec['status']}")
            if any(r["ok"] for r in results):
                _tg_notify(
                    f"<b>FarhanFX — Signal Executed ({'Demo' if is_demo else 'LIVE'})</b>\n"
                    f"📨 Source: userbot ({cfg.get('channel_title', 'channel')})\n"
                    f"{'🟢 BUY' if sig['side']=='buy' else '🔴 SELL'} {sig['symbol']}"
                )
        except Exception as e:
            print(f"Telegram userbot handler error: {e}")

    try:
        client.start()
        # Populate entity cache so get_entity can resolve the channel by raw ID
        loop.run_until_complete(client.get_dialogs())
        entity = loop.run_until_complete(client.get_entity(channel_peer))
        print(f"Telegram userbot: listening on channel {getattr(entity, 'title', cfg['channel'])}")
        client.run_until_disconnected()
    except Exception as e:
        print(f"Telegram userbot error: {e}")
    finally:
        try: loop.run_until_complete(client.disconnect())
        except: pass


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
    days_left = 0
    if cfg.get("status") == "liquidated":
        elapsed   = _time.time() - cfg.get("liquidated_at", 0)
        remaining = cfg.get("break_days", 7) * 86400 - elapsed
        days_left = max(0, round(remaining / 86400, 1))
    return {**cfg, "equity": None, "break_days_left": days_left}

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


# Words that show up in nearly every signal-channel post but are never the
# coin itself — disclaimer hashtags, trade jargon, promo-post fluff. Used to
# stop both the hashtag and the headline-scan symbol fallbacks from grabbing
# the wrong token (e.g. "#nfa" = "not financial advice", not a ticker).
_TG_SYM_STOPWORDS = {
    "SCALP", "TRADE", "TRADES", "LONG", "SHORT", "BUY", "SELL", "ENTRY", "TARGET", "TARGETS",
    "LEVERAGE", "STOP", "LOSS", "TYPE", "DIRECTION", "DONE", "PROFIT", "BOOK", "SHIFT", "NOW",
    "ZOOM", "LIVE", "FOR", "AND", "JOIN", "WE", "ARE", "USDT", "BUSD", "USDC", "BTC", "SETUP",
    "SIGNAL", "SIGNALS", "NEW", "USERS", "WELCOME", "EVENT", "REWARD", "REWARDS", "TOTAL",
    "POOL", "CAMPAIGN", "EXCLUSIVE", "STEP", "VALID", "ON", "IN", "TO", "AT", "SL", "TP",
    "NFA", "DYOR", "ATH", "ATL", "RIP", "FOMO", "ALL", "DONE", "TRADER", "ALGO", "PRIME",
}


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

    # Status updates on an already-open trade ("TARGET 3 ✅ ... ALL TARGETS
    # DONE") mention LONG/SHORT too but are not a new entry — skip them
    # before they get mistaken for one.
    if _re.search(r'\b(all\s+targets?|target\s*\d*)\b.{0,15}\b(done|hit|achieved)\b', t, _re.I):
        return None

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
        # Try a "#COIN" hashtag — but skip disclaimer hashtags every signal
        # channel tacks on (#nfa #dyor etc.), which would otherwise get
        # mistaken for the coin itself.
        for tok in _re.findall(r'#([A-Z]{2,10})', t, _re.I):
            if tok.upper() not in _TG_SYM_STOPWORDS:
                sym_raw = tok.upper()
                break
        if not sym_raw:
            # Some channels just write the bare ticker with no "/USDT" suffix
            # and no hashtag, e.g. "SCALP TRADE - WIF" or "WLD SCALP TRADE".
            # Scan the headline (first line) for an all-caps 2-6 letter token
            # that isn't common signal-post jargon.
            first_line = t.split("\n", 1)[0]
            for tok in _re.findall(r'\b[A-Z]{2,6}\b', first_line):
                if tok not in _TG_SYM_STOPWORDS:
                    sym_raw = tok
                    break
    if not sym_raw:
        return None

    symbol = _normalise_symbol(sym_raw)

    # Entry — take first number in entry range (some channels write "$0.081")
    entry = None
    m = _re.search(r'entry\s*[:\-–]?\s*\$?\s*([\d,.]+)', t, _re.I)
    if m:
        entry = float(m.group(1).replace(",", ""))
    else:
        # Try "@ 63500"
        m = _re.search(r'@\s*\$?\s*([\d,.]+)', t, _re.I)
        if m:
            entry = float(m.group(1).replace(",", ""))

    # TP — collect all TP values, use first
    tps = _re.findall(r'(?:tp\d*|take\s*profit|targets?\d*)\s*[:\-–]?\s*\$?\s*([\d,.]+)', t, _re.I)
    tp = float(tps[0].replace(",", "")) if tps else None

    # SL
    sl = None
    m = _re.search(r'(?:sl|stop\s*loss)\s*[:\-–]?\s*\$?\s*([\d,.]+)', t, _re.I)
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
        ex = _active_ex.get("admin", {}).get(exname.lower())
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
            _set_margin_mode_safe(ex, sig["symbol"])
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


def _tg_execute_signal_demo(sig: dict, cfg: dict) -> list:
    """Paper-trade version of _tg_execute_signal — same fixed-contract sizing
    and same flip-on-opposite-signal behaviour, but fills against the live
    public price instead of placing a real order. Lets you see how a
    Telegram channel's signals would have performed before risking money on
    them."""
    pm = _get_pub_mkt()
    if not pm:
        return [{"exchange": "demo", "ok": False, "error": "Market data not available"}]
    try:
        last = pm.fetch_ohlcv(sig["symbol"], "1m", limit=1)
        price = float(last[-1][4])
    except Exception as e:
        return [{"exchange": "demo", "ok": False, "error": f"Price fetch failed: {e}"[:150]}]

    st = _load_tg_demo_state()
    sym = sig["symbol"]
    pos = st["positions"].get(sym)

    if pos and pos["side"] != sig["side"]:
        pnl = (price - pos["entry"]) * pos["amount"] if pos["side"] == "buy" else (pos["entry"] - price) * pos["amount"]
        st["equity"] = round(st["equity"] + pnl, 4)
        st["trades"].append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"), "symbol": sym,
            "side": pos["side"], "entry": pos["entry"], "exit": price,
            "amount": pos["amount"], "pnl": round(pnl, 4), "status": "closed",
        })
        del st["positions"][sym]

    amt = cfg.get("amount", 0.01)
    st["positions"][sym] = {"side": sig["side"], "entry": price, "amount": amt}
    _save_tg_demo_state(st)
    return [{"exchange": "demo", "ok": True, "order_id": "demo", "side": sig["side"], "amount": amt}]


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
                "source":   "bot_api",
            }

            if cfg.get("auto_trade") and cfg.get("enabled"):
                is_demo = cfg.get("mode", "demo") == "demo"
                results = _tg_execute_signal_demo(sig, cfg) if is_demo else _tg_execute_signal(sig, cfg)
                rec["results"] = results
                rec["mode"]    = "demo" if is_demo else "live"
                rec["status"]  = ("demo_executed" if is_demo else "executed") if any(r["ok"] for r in results) else "failed"
                if any(r["ok"] for r in results):
                    _tg_notify(
                        f"<b>FarhanFX — Signal Executed ({'Demo' if is_demo else 'LIVE'})</b>\n"
                        f"📨 Source: bot\n"
                        f"{'🟢 BUY' if sig['side']=='buy' else '🔴 SELL'} {sig['symbol']}"
                    )
            else:
                rec["status"] = "signal"   # received but not auto-traded

            _TG_SIGNALS.insert(0, rec)
            if len(_TG_SIGNALS) > 100:
                _TG_SIGNALS.pop()
            _save_tg_signals()

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

# Auto-start the userbot channel listener if it's configured (separate from
# the Bot API poller above — see _tg_userbot_thread's docstring)
try:
    if _load_tg_userbot_cfg():
        threading.Thread(target=_tg_userbot_thread, daemon=True).start()
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
    mode:            str   = "demo"        # "demo" (paper trade) | "live" (real money)
    demo_balance:    float = 1000.0


@app.post("/api/telegram/config")
def tg_config_save(req: TgConfigReq):
    cfg = req.model_dump()
    if not cfg.get("token"):
        # Token is never sent back to the browser (tg_config_get strips it),
        # so a blank token here means "keep the existing one", not "clear it" —
        # otherwise every settings change would force re-pasting the token.
        cfg["token"] = _load_tg_cfg().get("token", "")
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
        "running":      _TG_RUNNING,
        "enabled":      cfg.get("enabled", False),
        "auto_trade":   cfg.get("auto_trade", False),
        "exchanges":    cfg.get("exchanges", []),
        "chat_id":      cfg.get("chat_id", ""),
        "has_token":    bool(cfg.get("token")),
        "mode":         cfg.get("mode", "demo"),
        "demo_balance": cfg.get("demo_balance", 1000.0),
        "drawdown_pct": cfg.get("drawdown_pct", 5.0),
        "notify_enabled": cfg.get("notify_enabled", True),
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
    is_demo = cfg.get("mode", "demo") == "demo"
    results = _tg_execute_signal_demo(sig, cfg) if is_demo else _tg_execute_signal(sig, cfg)
    rec["results"] = results
    rec["mode"]    = "demo" if is_demo else "live"
    rec["status"]  = ("demo_executed" if is_demo else "executed") if any(r["ok"] for r in results) else "failed"
    return {"ok": True, "results": results}


_UB_AUTH_CLIENT = None   # holds pending Telethon client during OTP flow
_UB_AUTH_PHONE  = ""


@app.post("/api/telegram/userbot/send_code")
async def ub_send_code(request: Request):
    req = await request.json()
    global _UB_AUTH_CLIENT, _UB_AUTH_PHONE
    if not _TELETHON_OK:
        return JSONResponse(status_code=400, content={"error": "telethon not installed"})
    try:
        with open(_TG_USERBOT_FILE, encoding="utf-8") as _f:
            cfg = json.load(_f)
    except Exception:
        cfg = {}
    api_id   = cfg.get("api_id") or 0
    api_hash = cfg.get("api_hash") or ""
    phone    = req.get("phone", "").strip()
    if not api_id or not api_hash:
        return JSONResponse(status_code=400, content={"error": "api_id / api_hash not configured"})
    if not phone:
        return JSONResponse(status_code=400, content={"error": "phone required"})
    try:
        import os as _os
        base_dir = _os.path.dirname(_os.path.abspath(__file__))
        # Use a TEMP session name to avoid the locked main session file
        temp_sess = _os.path.join(base_dir, "tg_ub_auth_temp")
        for ext in ("", ".session"):
            try: _os.remove(temp_sess + ext)
            except: pass
        client = _TelethonClient(temp_sess, api_id, api_hash)
        await client.connect()
        await client.send_code_request(phone)
        _UB_AUTH_CLIENT = client
        _UB_AUTH_PHONE  = phone
        # Save phone to config
        ub_cfg = {}
        try:
            with open(_TG_USERBOT_FILE, encoding="utf-8-sig") as f:
                ub_cfg = json.load(f)
        except: pass
        ub_cfg["phone"] = phone
        with open(_TG_USERBOT_FILE, "w", encoding="utf-8") as f:
            json.dump(ub_cfg, f, indent=2)
        return {"ok": True, "message": f"OTP sent to {phone}"}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.post("/api/telegram/userbot/verify_code")
async def ub_verify_code(request: Request):
    req = await request.json()
    global _UB_AUTH_CLIENT, _UB_AUTH_PHONE
    if not _UB_AUTH_CLIENT:
        return JSONResponse(status_code=400, content={"error": "Send code first"})
    code = str(req.get("code", "")).strip()
    if not code:
        return JSONResponse(status_code=400, content={"error": "code required"})
    try:
        await _UB_AUTH_CLIENT.sign_in(_UB_AUTH_PHONE, code)
        await _UB_AUTH_CLIENT.disconnect()
        _UB_AUTH_CLIENT = None
        # Replace main session with the freshly authenticated temp session
        import os as _os
        base_dir = _os.path.dirname(_os.path.abspath(__file__))
        temp_sess = _os.path.join(base_dir, "tg_ub_auth_temp.session")
        main_sess = _os.path.join(base_dir, _TG_USERBOT_SESSION + ".session")
        for ext in ("", ".session"):
            try: _os.remove(_os.path.join(base_dir, _TG_USERBOT_SESSION + ext))
            except: pass
        try: _os.rename(temp_sess, main_sess)
        except: pass
        # Kick off a fresh userbot thread with the new valid session
        if _load_tg_userbot_cfg():
            threading.Thread(target=_tg_userbot_thread, daemon=True).start()
        return {"ok": True, "message": "Authenticated! Userbot is reconnecting — should be active in ~10 seconds."}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.get("/api/telegram/demo_stats")
def tg_demo_stats():
    st = _load_tg_demo_state()
    closed = st.get("trades", [])
    stats = _bot_stats(closed)
    open_positions = [{"symbol": sym, **pos} for sym, pos in st.get("positions", {}).items()]
    return {
        "equity": st.get("equity", 1000.0),
        "open_positions": open_positions,
        **stats,
    }


# ── INDIAN MARKET (Kotak Neo) ────────────────────────────────────────────────
# Separate broker/asset class from the existing forex (MT5) and crypto (ccxt)
# integrations — NSE/BSE equities, indices, and F&O via Kotak's official
# neo_api_client SDK. Kept fully independent of FOREX BOT / FOREX ALGO.
try:
    from neo_api_client import NeoAPI as _NeoAPI
    _NEO_OK = True
except ImportError:
    _NeoAPI = None
    _NEO_OK = False

import pyotp as _pyotp

_NEO_CFG_FILE = "kotak_neo_cfg.json"
_active_neo: dict = {}   # {username: NeoAPI client} — live, logged-in sessions


def _load_neo_cfg() -> dict:
    try:
        with open(_NEO_CFG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_neo_cfg(cfg: dict):
    with open(_NEO_CFG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def _neo_err(r):
    """The SDK is inconsistent about error shape across calls — client-side
    validation uses {"error": [{"message": ...}]}, server/session errors use
    {"Error": ...} or {"Error Message": ...}. Check all three."""
    if not isinstance(r, dict):
        return None
    if r.get("error"):
        e = r["error"]
        return e[0].get("message", str(e)) if isinstance(e, list) else str(e)
    if "Error Message" in r:
        return r["Error Message"]
    if "Error" in r:
        return str(r["Error"])
    return None


def _neo_login(cfg: dict):
    """Full 2-step login (totp_login + totp_validate). A fresh 30-second
    TOTP code is generated from the stored secret on every call, so the
    bot can re-authenticate unattended after a restart instead of needing
    someone to type in a one-time code by hand."""
    client = _NeoAPI(environment=cfg.get("environment", "prod"),
                      consumer_key=cfg["consumer_key"], access_token=cfg["access_token"])
    totp_code = _pyotp.TOTP(cfg["totp_secret"]).now()
    r1 = client.totp_login(mobile_number=cfg["mobile_number"], ucc=cfg["ucc"], totp=totp_code)
    err = _neo_err(r1)
    if err:
        raise Exception(f"TOTP login failed: {err}")
    r2 = client.totp_validate(mpin=cfg["mpin"])
    err = _neo_err(r2)
    if err:
        raise Exception(f"MPIN validation failed: {err}")
    return client


def _get_neo_client(username: str):
    """Cached live client, or a fresh re-login from saved config if there
    isn't one yet (e.g. right after a server restart)."""
    client = _active_neo.get(username)
    if client:
        return client
    cfg = _load_neo_cfg().get(username)
    if not cfg:
        return None
    client = _neo_login(cfg)
    _active_neo[username] = client
    return client


class KotakNeoConnectReq(BaseModel):
    consumer_key:  str
    access_token:  str
    mobile_number: str   # with country code, e.g. +919876543210
    ucc:           str   # Unique Client Code (Profile section in the app)
    mpin:          str
    totp_secret:   str   # base32 secret behind the TOTP QR code — NOT a one-time code
    environment:   str = "prod"   # "prod" | "uat"


@app.post("/api/indian/connect")
def indian_connect(req: KotakNeoConnectReq, current_user: dict = Depends(_get_current_user)):
    if not _NEO_OK:
        return JSONResponse(status_code=400, content={"error": "neo_api_client not installed on the server"})
    uname = current_user["username"]
    cfg_dict = req.model_dump()
    try:
        client = _neo_login(cfg_dict)
        limits = client.limits()
        err = _neo_err(limits)
        if err:
            raise Exception(err)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:250]})
    _active_neo[uname] = client
    cfg = _load_neo_cfg()
    cfg[uname] = cfg_dict
    _save_neo_cfg(cfg)
    return {"success": True}


@app.post("/api/indian/disconnect")
def indian_disconnect(current_user: dict = Depends(_get_current_user)):
    uname = current_user["username"]
    _active_neo.pop(uname, None)
    cfg = _load_neo_cfg()
    cfg.pop(uname, None)
    _save_neo_cfg(cfg)
    return {"success": True}


@app.get("/api/indian/status")
def indian_status(current_user: dict = Depends(_get_current_user)):
    cfg = _load_neo_cfg().get(current_user["username"])
    if not cfg:
        return {"connected": False}
    return {"connected": True, "ucc": cfg.get("ucc", ""), "environment": cfg.get("environment", "prod")}


@app.get("/api/indian/positions")
def indian_positions(current_user: dict = Depends(_get_current_user)):
    client = _get_neo_client(current_user["username"])
    if not client:
        return JSONResponse(status_code=400, content={"error": "Not connected — connect your Kotak Neo account first"})
    try:
        r = client.positions()
        err = _neo_err(r)
        if err:
            return JSONResponse(status_code=400, content={"error": err})
        return r.get("data", r) if isinstance(r, dict) else r
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})


@app.get("/api/indian/holdings")
def indian_holdings(current_user: dict = Depends(_get_current_user)):
    client = _get_neo_client(current_user["username"])
    if not client:
        return JSONResponse(status_code=400, content={"error": "Not connected — connect your Kotak Neo account first"})
    try:
        r = client.holdings()
        err = _neo_err(r)
        if err:
            return JSONResponse(status_code=400, content={"error": err})
        return r.get("data", r) if isinstance(r, dict) else r
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})


@app.get("/api/indian/limits")
def indian_limits(current_user: dict = Depends(_get_current_user)):
    client = _get_neo_client(current_user["username"])
    if not client:
        return JSONResponse(status_code=400, content={"error": "Not connected — connect your Kotak Neo account first"})
    try:
        r = client.limits()
        err = _neo_err(r)
        if err:
            return JSONResponse(status_code=400, content={"error": err})
        return r.get("data", r) if isinstance(r, dict) else r
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})


@app.get("/api/indian/search_scrip")
def indian_search_scrip(exchange_segment: str = "nse_cm", symbol: str = "",
                         current_user: dict = Depends(_get_current_user)):
    """exchange_segment: nse_cm (NSE equity) | bse_cm (BSE equity) |
    nse_fo (NSE F&O — index/stock futures & options) | bse_fo."""
    client = _get_neo_client(current_user["username"])
    if not client:
        return JSONResponse(status_code=400, content={"error": "Not connected — connect your Kotak Neo account first"})
    try:
        r = client.search_scrip(exchange_segment=exchange_segment, symbol=symbol)
        err = _neo_err(r)
        if err:
            return JSONResponse(status_code=400, content={"error": err})
        return r if isinstance(r, list) else r.get("data", [])
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})


class IndianOrderReq(BaseModel):
    exchange_segment: str          # nse_cm | bse_cm | nse_fo | bse_fo
    trading_symbol:   str          # exact symbol from search_scrip, e.g. "RELIANCE-EQ"
    transaction_type: str          # "B" (buy) | "S" (sell)
    quantity:         str
    product:          str = "MIS"  # MIS (intraday) | CNC (delivery) | NRML (F&O carry-forward)
    order_type:       str = "MKT"  # MKT | L (limit) | SL | SL-M
    price:            str = "0"
    trigger_price:    str = "0"
    validity:         str = "DAY"  # DAY | IOC


@app.post("/api/indian/order")
def indian_place_order(req: IndianOrderReq, current_user: dict = Depends(_get_current_user)):
    client = _get_neo_client(current_user["username"])
    if not client:
        return JSONResponse(status_code=400, content={"error": "Not connected — connect your Kotak Neo account first"})
    try:
        r = client.place_order(
            exchange_segment=req.exchange_segment, product=req.product,
            price=req.price, order_type=req.order_type, quantity=req.quantity,
            validity=req.validity, trading_symbol=req.trading_symbol,
            transaction_type=req.transaction_type, trigger_price=req.trigger_price,
        )
        err = _neo_err(r)
        if err:
            return JSONResponse(status_code=400, content={"error": err})
        return r
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})


@app.post("/api/indian/order/{order_id}/cancel")
def indian_cancel_order(order_id: str, current_user: dict = Depends(_get_current_user)):
    client = _get_neo_client(current_user["username"])
    if not client:
        return JSONResponse(status_code=400, content={"error": "Not connected — connect your Kotak Neo account first"})
    try:
        r = client.cancel_order(order_id=order_id)
        err = _neo_err(r)
        if err:
            return JSONResponse(status_code=400, content={"error": err})
        return r
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})


@app.get("/api/indian/order_report")
def indian_order_report(current_user: dict = Depends(_get_current_user)):
    client = _get_neo_client(current_user["username"])
    if not client:
        return JSONResponse(status_code=400, content={"error": "Not connected — connect your Kotak Neo account first"})
    try:
        r = client.order_report()
        err = _neo_err(r)
        if err:
            return JSONResponse(status_code=400, content={"error": err})
        return r.get("data", r) if isinstance(r, dict) else r
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})


@app.get("/api/indian/dashboard")
def indian_dashboard(current_user: dict = Depends(_get_current_user)):
    uname = current_user["username"]
    client = _active_neo.get(uname)
    if not client:
        cfg = _load_neo_cfg().get(uname)
        if cfg:
            try: client = _neo_login(cfg); _active_neo[uname] = client
            except Exception: pass
    result: dict = {"connected": client is not None}

    def _safe(fn):
        try:
            r = fn()
            if isinstance(r, dict):
                err = _neo_err(r)
                if err: return None
                return r.get("data", r)
            return r if isinstance(r, list) else None
        except Exception:
            return None

    if client:
        result["limits"]    = _safe(client.limits)    or {}
        result["positions"] = _safe(client.positions) or []
        result["holdings"]  = _safe(client.holdings)  or []
        result["orders"]    = _safe(client.order_report) or []
        result["trades_live"] = _safe(client.trade_report) or []
    else:
        result["limits"] = {}; result["positions"] = []; result["holdings"] = []; result["orders"] = []; result["trades_live"] = []

    # Bot stats
    bots = list(_indian_bots.values())
    active_bots = [b for b in bots if b.get("status") == "active"]

    # Build enriched trade list with live unrealized P&L for open trades
    enriched_trades = []
    total_unrealized = 0.0
    for b in bots:
        is_opt = b.get("options_bot", False)
        lot_size = _INDIAN_LOT_SIZES.get(b.get("symbol","").upper(), 1) if is_opt else 1
        qty = int(b.get("quantity", 1)) * lot_size if is_opt else int(b.get("quantity", 1))
        for t in b.get("trades", []):
            t2 = dict(t)
            if t2.get("status") == "open":
                cur = b.get("last_price")
                ep  = b.get("open_entry_price") or t2.get("price")
                side = b.get("open_side", "BUY")
                if cur and ep:
                    if is_opt:
                        opt_dir = b.get("_current_opt_dir", "CE")
                        raw = (cur - ep) if opt_dir == "CE" else (ep - cur)
                        upnl = round(raw * 0.5 * lot_size * int(b.get("quantity",1)), 2)
                    else:
                        upnl = round((cur - ep) * qty if side == "BUY" else (ep - cur) * qty, 2)
                    t2["unrealized_pnl"] = upnl
                    total_unrealized += upnl
            enriched_trades.append(t2)

    closed_trades = [t for t in enriched_trades if t.get("status") == "closed"]
    wins = [t for t in closed_trades if (t.get("pnl") or 0) > 0]
    total_pnl = sum(t.get("pnl", 0) for t in closed_trades)

    result["bots"] = [
        {k: v for k, v in b.items() if not k.startswith("_")}
        for b in bots
    ]
    result["bot_stats"] = {
        "active": len(active_bots),
        "total":  len(bots),
        "trades": len(closed_trades),
        "wins":   len(wins),
        "win_rate": round(len(wins) / len(closed_trades) * 100, 1) if closed_trades else 0,
        "total_pnl": round(total_pnl, 2),
        "unrealized_pnl": round(total_unrealized, 2),
        "all_trades": sorted(enriched_trades, key=lambda x: x.get("time",""), reverse=True)[:50],
    }
    return result


@app.get("/api/indian/options_chain")
def indian_options_chain(symbol: str = "NIFTY", strikes: int = 10,
                          current_user: dict = Depends(_get_current_user)):
    import datetime as _dt
    yf_map = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "FINNIFTY": "^CNXFIN"}
    yf_sym = yf_map.get(symbol.upper(), "^NSEI")
    spot = None
    if _YF_OK:
        try:
            df = _yf.download(yf_sym, period="1d", interval="1m", progress=False, auto_adjust=True)
            if not df.empty:
                if hasattr(df.columns, "levels"):
                    df.columns = df.columns.get_level_values(0)
                spot = float(df["Close"].iloc[-1])
        except Exception:
            pass
    if spot is None:
        return JSONResponse(status_code=400, content={"error": "Spot price fetch failed for " + symbol})
    today = _dt.date.today()
    days_to_thu = (3 - today.weekday()) % 7
    expiry = today + _dt.timedelta(days=days_to_thu)
    expiry_part = f"{expiry.month}{expiry.day:02d}"
    yy = expiry.strftime("%y")
    inc = 100 if "BANK" in symbol.upper() else 50
    atm = round(spot / inc) * inc
    strike_list = []
    for i in range(-strikes, strikes + 1):
        s = int(atm + i * inc)
        strike_list.append({
            "strike": s,
            "ce_symbol": f"{symbol.upper()}{yy}{expiry_part}{s}CE",
            "pe_symbol": f"{symbol.upper()}{yy}{expiry_part}{s}PE",
            "atm": s == int(atm),
        })
    return {
        "spot": round(spot, 2),
        "atm_strike": int(atm),
        "expiry": str(expiry),
        "expiry_display": expiry.strftime("%d %b %Y"),
        "symbol": symbol.upper(),
        "strikes": strike_list,
    }


# ── INDIAN MARKET ALGO BOTS ───────────────────────────────────────────────────
try:
    import yfinance as _yf
    _YF_OK = True
except ImportError:
    _yf = None
    _YF_OK = False

_indian_bots: dict = {}
_indian_bot_timers: dict = {}
_INDIAN_TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
_INDIAN_BOTS_FILE = "indian_bots.json"


def _save_indian_bots():
    try:
        import os as _os
        saveable = {}
        for bid, bot in _indian_bots.items():
            saveable[bid] = {k: v for k, v in bot.items() if not callable(v)}
            saveable[bid]['trades'] = bot.get('trades', [])[-200:]
        with open(_INDIAN_BOTS_FILE, 'w') as f:
            json.dump(saveable, f, indent=2, default=str)
    except Exception as e:
        print(f"_save_indian_bots error: {e}")


def _load_indian_bots():
    import os as _os
    if not _os.path.exists(_INDIAN_BOTS_FILE):
        return
    try:
        with open(_INDIAN_BOTS_FILE) as f:
            data = json.load(f)
        keys = list(data.keys())
        for bid, bot in data.items():
            if bid in _indian_bots:
                continue
            try:
                bot.setdefault("username", "admin")
                bot.setdefault("trades", [])
                bot.setdefault("demo_equity", bot.get("demo_balance", 100000.0))
                bot.setdefault("options_bot", False)
                bot.setdefault("options_direction", "auto")
                bot.setdefault("sl_pct", 0.0)
                _indian_bots[bid] = bot
                if bot.get("status") == "active":
                    delay = 10 + keys.index(bid) * 5
                    t = threading.Timer(delay, _indian_bot_tick, args=[bid])
                    t.daemon = True
                    t.start()
                    _indian_bot_timers[bid] = t
                    print(f"Indian bot resumed: {bid} ({bot.get('symbol')} {bot.get('strategy')})")
            except Exception as e:
                print(f"_load_indian_bots skip {bid}: {e}")
    except Exception as e:
        print(f"_load_indian_bots error: {e}")
_INDIAN_TF_YF      = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "1d": "1d"}
_INDIAN_TF_PERIOD  = {"1m": "1d", "5m": "5d", "15m": "5d", "1h": "60d", "1d": "2y"}


class IndianBotReq(BaseModel):
    symbol:           str              # NSE ticker e.g. "RELIANCE"
    exchange_segment: str = "nse_cm"   # nse_cm | bse_cm | nse_fo | bse_fo
    strategy:         str = "rsi"
    timeframe:        str = "15m"
    mode:             str = "demo"     # demo | live
    product:          str = "MIS"      # MIS | CNC | NRML
    quantity:         str = "1"        # shares / lots (must be string for Kotak Neo)
    demo_balance:     float = 100000.0  # virtual INR balance
    rsi_period:  int   = 7;  rsi_ob:   int   = 75;  rsi_os:   int   = 25
    macd_fast:   int   = 12; macd_slow: int   = 17;  macd_signal: int = 9
    bb_period:   int   = 30; bb_std:   float = 2.5
    dc_period:   int   = 55; dc_ema:   int   = 150
    vwap_period: int   = 14; vwap_std: float = 2.5
    tp_pct:        float = 0.0   # take-profit: % gain from entry (0=off)
    trailing_pct:  float = 0.0   # trailing stop: % from peak/trough (0=off)
    sl_pct:        float = 0.0   # hard stop-loss % from entry (0=off)
    div_lookback:  int   = 25
    swing_window:  int   = 5
    vwap_proximity: float = 0.3
    orb_minutes:   int   = 30
    # Options bot fields
    options_bot:       bool = False
    options_direction: str  = "auto"   # "auto" | "CE" | "PE"


_INDIAN_LOT_SIZES = {"NIFTY": 75, "BANKNIFTY": 30, "FINNIFTY": 65, "MIDCPNIFTY": 120, "SENSEX": 20}

_FO_INDEX_MAP = {
    "NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "FINNIFTY": "^CNXFIN",
    "MIDCPNIFTY": "^NSEMDCP50", "SENSEX": "^BSESN",
}

def _fetch_indian_ohlcv(symbol: str, exchange_segment: str, timeframe: str, bars: int = 200):
    if not _YF_OK:
        return None
    # Strip expiry/option suffix for F&O: NIFTY25JULFUT → NIFTY, RELIANCE25JULFUT → RELIANCE
    base = symbol.upper()
    if exchange_segment in ("nse_fo", "bse_fo"):
        import re as _re
        base = _re.sub(r'\d{2}[A-Z0-9]+(FUT|CE|PE)$', '', base)
        yf_sym = _FO_INDEX_MAP.get(base, base + ".NS")
    elif exchange_segment in ("nse_cm",):
        yf_sym = base + ".NS"
    elif exchange_segment in ("bse_cm",):
        yf_sym = base + ".BO"
    else:
        yf_sym = base + ".NS"
    interval = _INDIAN_TF_YF.get(timeframe, "15m")
    period   = _INDIAN_TF_PERIOD.get(timeframe, "5d")
    try:
        df = _yf.download(yf_sym, period=period, interval=interval, progress=False, auto_adjust=True)
        if df.empty:
            return None
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        ohlcv = []
        for ts, row in df.iterrows():
            o, h, l, c, v = (float(row.get(k, 0) or 0) for k in ("Open", "High", "Low", "Close", "Volume"))
            if c != c or c == 0:  # NaN / zero close → skip
                continue
            ohlcv.append([int(ts.timestamp() * 1000), o, h, l, c, v])
        if len(ohlcv) < 5:
            return None
        return ohlcv[-bars:] if len(ohlcv) > bars else ohlcv
    except Exception:
        return None


def _atm_option_symbol(underlying: str, direction: str, spot: float) -> str:
    import datetime as _dt
    today = _dt.date.today()
    expiry = today + _dt.timedelta(days=(3 - today.weekday()) % 7)
    expiry_part = f"{expiry.month}{expiry.day:02d}"
    yy = expiry.strftime("%y")
    inc = 100 if "BANK" in underlying.upper() else 50
    atm = int(round(spot / inc) * inc)
    return f"{underlying.upper()}{yy}{expiry_part}{atm}{direction}"


def _indian_bot_tick(bot_id):
    bot = _indian_bots.get(bot_id)
    if not bot or bot["status"] != "active":
        return
    try:
        is_opt = bot.get("options_bot", False)
        # For options bots, always fetch underlying index data (not the option symbol)
        fetch_sym = bot["symbol"]
        fetch_seg = "nse_fo" if is_opt else bot["exchange_segment"]
        ohlcv = _fetch_indian_ohlcv(fetch_sym, fetch_seg, bot["timeframe"])
        if not ohlcv or len(ohlcv) < 20:
            bot["last_error"] = "Not enough OHLCV data — yfinance may be unavailable or market closed"
            return
        signal = _get_bot_signal(bot, ohlcv)
        price  = float(ohlcv[-1][4])
        bot["last_run"]   = datetime.now().strftime("%H:%M:%S")
        bot["last_price"] = round(price, 2)

        # For options bot: determine CE/PE from signal, construct option symbol
        opt_symbol = None
        if is_opt and signal:
            forced = bot.get("options_direction", "auto")
            if forced == "CE":
                opt_dir = "CE"
            elif forced == "PE":
                opt_dir = "PE"
            else:  # auto
                opt_dir = "CE" if signal == "BUY" else "PE"
            opt_symbol = _atm_option_symbol(bot["symbol"], opt_dir, price)
            bot["_current_opt_symbol"] = opt_symbol
            bot["_current_opt_dir"] = opt_dir
            signal = "BUY"  # options are always bought (long only)

        lot_size = _INDIAN_LOT_SIZES.get(bot["symbol"].upper(), 1) if is_opt else 1
        qty = int(bot.get("quantity", 1)) * lot_size if is_opt else int(bot.get("quantity", 1))

        def _close_indian_pos(exit_price, exit_reason, live_client=None):
            ep   = bot.get("open_entry_price", exit_price)
            side = bot["open_side"]
            if is_opt:
                # Options PnL: underlying move * delta(0.5) * lot_size * qty_lots
                opt_dir = bot.get("_current_opt_dir", "CE")
                raw_move = (exit_price - ep) if opt_dir == "CE" else (ep - exit_price)
                qty_lots = int(bot.get("quantity", 1))
                pnl = raw_move * 0.5 * lot_size * qty_lots
            else:
                pnl = (exit_price - ep) * qty if side == "BUY" else (ep - exit_price) * qty
            bot["demo_equity"] = round(bot.get("demo_equity", bot.get("demo_balance", 100000)) + pnl, 2)
            if bot.get("trades"):
                bot["trades"][-1].update(exit_price=round(exit_price, 2),
                                         exit_reason=exit_reason,
                                         pnl=round(pnl, 2), status="closed")
            bot["open_side"]         = None
            bot["open_entry_price"]  = None
            bot["open_peak"]         = None
            bot["open_trough"]       = None
            close_sym = bot.get("_current_opt_symbol", bot["symbol"]) if is_opt else bot["symbol"]
            if live_client:
                try:
                    tx = "S"  # always sell to close (options always bought)
                    if not is_opt:
                        tx = "S" if side == "BUY" else "B"
                    live_client.place_order(
                        exchange_segment="nse_fo" if is_opt else bot["exchange_segment"],
                        trading_symbol=close_sym,
                        transaction_type=tx, quantity=str(qty),
                        order_type="MKT", product=bot.get("product", "MIS"),
                        price="0", trigger_price="0", validity="DAY"
                    )
                except Exception:
                    pass
            mode_label = "Live" if bot.get("mode") == "live" else "Demo"
            _tg_notify(
                f"<b>FarhanFX Indian — Closed ({mode_label})</b>\n"
                f"📊 <b>{bot['strategy']}</b> | {bot['symbol']}\n"
                f"{'✅' if pnl >= 0 else '❌'} PnL: <code>₹{pnl:.2f}</code> ({exit_reason})"
            )
            _save_indian_bots()

        # TP / SL / trailing-stop exit check
        if bot.get("open_side"):
            ep         = bot.get("open_entry_price", price)
            oside      = bot["open_side"]
            tp_pct     = bot.get("tp_pct", 0)
            trail_pct  = bot.get("trailing_pct", 0)
            sl_pct     = bot.get("sl_pct", 0)
            live_cl    = None
            if bot.get("mode") == "live":
                try: live_cl = _get_neo_client(bot.get("username", "admin"))
                except Exception: pass
            should_exit, exit_reason = False, ""
            # For options: BUY CE → bullish (profit when price rises); BUY PE → bearish (profit when price falls)
            opt_dir = bot.get("_current_opt_dir", "CE") if is_opt else None
            is_bullish = (oside == "BUY" and not is_opt) or (is_opt and opt_dir == "CE")
            if is_bullish:
                bot["open_peak"] = max(bot.get("open_peak", ep), price)
                if tp_pct > 0 and price >= ep * (1 + tp_pct / 100):
                    should_exit, exit_reason = True, "take_profit"
                elif sl_pct > 0 and price <= ep * (1 - sl_pct / 100):
                    should_exit, exit_reason = True, "stop_loss"
                elif trail_pct > 0 and price < bot["open_peak"] * (1 - trail_pct / 100):
                    should_exit, exit_reason = True, "trailing_stop"
            else:
                bot["open_trough"] = min(bot.get("open_trough", ep), price)
                if tp_pct > 0 and price <= ep * (1 - tp_pct / 100):
                    should_exit, exit_reason = True, "take_profit"
                elif sl_pct > 0 and price >= ep * (1 + sl_pct / 100):
                    should_exit, exit_reason = True, "stop_loss"
                elif trail_pct > 0 and price > bot["open_trough"] * (1 + trail_pct / 100):
                    should_exit, exit_reason = True, "trailing_stop"
            if should_exit:
                _close_indian_pos(price, exit_reason, live_cl)

        if signal:
            live_cl = None
            if bot.get("mode") == "live":
                try:
                    live_cl = _get_neo_client(bot.get("username", "admin"))
                except Exception as e:
                    bot["last_error"] = f"Neo login: {str(e)[:100]}"
                    return

            # For options: close existing option position if direction changes
            if is_opt and bot.get("open_side"):
                prev_dir = bot.get("_current_opt_dir", "CE")
                new_dir  = bot.get("_current_opt_dir", "CE")  # already set above
                if opt_symbol and prev_dir != new_dir:
                    _close_indian_pos(price, "direction_flip", live_cl)
            elif not is_opt and bot.get("open_side") and bot["open_side"] != signal:
                _close_indian_pos(price, "signal_flip", live_cl)

            # Skip if already in position
            if bot.get("open_side"):
                bot["total_signals"] = bot.get("total_signals", 0) + 1
                bot["last_signal"]   = signal
                return

            # Place order
            order_id = "demo"
            trade_sym = opt_symbol if is_opt and opt_symbol else bot["symbol"]
            trade_seg = "nse_fo" if is_opt else bot["exchange_segment"]
            if live_cl:
                try:
                    r   = live_cl.place_order(
                        exchange_segment=trade_seg,
                        trading_symbol=trade_sym,
                        transaction_type="B",  # always BUY (CE or PE — buy options only)
                        quantity=str(qty), order_type="MKT",
                        product=bot.get("product", "MIS"),
                        price="0", trigger_price="0", validity="DAY"
                    )
                    err = _neo_err(r)
                    if err:
                        bot["last_error"] = f"Order failed: {err}"
                        return
                    order_id = ((r or {}).get("data") or {}).get("nOrdNo", "live")
                except Exception as e:
                    bot["last_error"] = str(e)[:200]
                    return

            entry = {
                "time":        datetime.now().strftime("%Y-%m-%d %H:%M"),
                "mode":        bot.get("mode", "demo"),
                "signal":      signal,
                "price":       round(price, 2),
                "amount":      qty,
                "order_id":    order_id,
                "pnl":         0,
                "status":      "open",
                "exit_price":  None,
                "exit_reason": None,
            }
            bot.setdefault("trades", []).append(entry)
            bot["total_signals"]    = bot.get("total_signals", 0) + 1
            bot["last_signal"]      = signal
            bot["open_side"]        = signal
            bot["open_entry_price"] = price
            bot["open_peak"]        = price
            bot["open_trough"]      = price
            bot["last_error"]       = None
            mode_label = "Live" if bot.get("mode") == "live" else "Demo"
            disp_sym = trade_sym if is_opt else bot["symbol"]
            disp_dir = f"{'📈 CE BUY' if bot.get('_current_opt_dir')=='CE' else '📉 PE BUY'}" if is_opt else f"{'🟢 BUY' if signal == 'BUY' else '🔴 SELL'}"
            _tg_notify(
                f"<b>FarhanFX Indian — {'Options ' if is_opt else ''}Trade Opened ({mode_label})</b>\n"
                f"📊 <b>{bot['strategy']}</b> | {disp_sym}\n"
                f"{disp_dir} @ <code>₹{price:.2f}</code> (underlying)\n"
                f"Qty: <code>{int(bot.get('quantity',1))} lot{'s' if int(bot.get('quantity',1))>1 else ''}</code> | Product: {bot.get('product','MIS')}"
            )

    except Exception as e:
        bot["last_error"] = str(e)[:200]
    finally:
        if _indian_bots.get(bot_id, {}).get("status") == "active":
            interval = _INDIAN_TF_SECONDS.get(bot.get("timeframe", "15m"), 900)
            t = threading.Timer(interval, _indian_bot_tick, args=[bot_id])
            t.daemon = True
            t.start()
            _indian_bot_timers[bot_id] = t


@app.post("/api/indian/algo/start")
def indian_algo_start(req: IndianBotReq, current_user: dict = Depends(_get_current_user)):
    if not _YF_OK:
        return JSONResponse(status_code=400, content={"error": "yfinance not installed on server — run: pip install yfinance"})
    uname = current_user["username"]
    if req.mode == "live":
        try:
            _get_neo_client(uname)
        except Exception as e:
            return JSONResponse(status_code=400, content={"error": f"Kotak Neo not connected: {e}"})
    bid = str(uuid.uuid4())[:8]
    bot = req.dict()
    bot.update({
        "id": bid, "username": uname, "status": "active",
        "total_signals": 0, "last_signal": None, "last_error": None,
        "last_run": None, "last_price": None,
        "open_side": None, "open_entry_price": None,
        "open_peak": None, "open_trough": None,
        "trades": [], "demo_equity": req.demo_balance,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    _indian_bots[bid] = bot
    t = threading.Timer(3, _indian_bot_tick, args=[bid])
    t.daemon = True
    t.start()
    _indian_bot_timers[bid] = t
    _save_indian_bots()
    return {"id": bid, "status": "started"}


@app.post("/api/indian/algo/stop/{bot_id}")
def indian_algo_stop(bot_id: str, current_user: dict = Depends(_get_current_user)):
    bot = _indian_bots.get(bot_id)
    if not bot:
        return JSONResponse(status_code=404, content={"error": "Bot not found"})
    bot["status"] = "stopped"
    t = _indian_bot_timers.pop(bot_id, None)
    if t:
        t.cancel()
    _save_indian_bots()
    return {"status": "stopped"}


@app.get("/api/indian/algo/bots")
def indian_algo_bots(current_user: dict = Depends(_get_current_user)):
    uname = current_user["username"]
    result = []
    for bot in _indian_bots.values():
        if bot.get("username") != uname:
            continue
        result.append({
            "id":             bot["id"],
            "symbol":         bot["symbol"],
            "exchange_segment": bot.get("exchange_segment", "nse_cm"),
            "strategy":       bot["strategy"],
            "timeframe":      bot["timeframe"],
            "mode":           bot.get("mode", "demo"),
            "product":        bot.get("product", "MIS"),
            "quantity":       bot.get("quantity", "1"),
            "status":         bot["status"],
            "last_run":       bot.get("last_run"),
            "last_price":     bot.get("last_price"),
            "last_signal":    bot.get("last_signal"),
            "last_error":     bot.get("last_error"),
            "total_signals":  bot.get("total_signals", 0),
            "open_side":      bot.get("open_side"),
            "demo_equity":    bot.get("demo_equity", bot.get("demo_balance", 100000)),
            "trades_count":   len(bot.get("trades", [])),
            "created":        bot.get("created"),
        })
    return result


@app.get("/api/indian/algo/history")
def indian_algo_history(current_user: dict = Depends(_get_current_user)):
    uname = current_user["username"]
    rows = []
    for bot in _indian_bots.values():
        if bot.get("username") != uname:
            continue
        is_opt  = bot.get("options_bot", False)
        lot_sz  = _INDIAN_LOT_SIZES.get(bot.get("symbol","").upper(), 1) if is_opt else 1
        qty     = int(bot.get("quantity", 1)) * lot_sz if is_opt else int(bot.get("quantity", 1))
        cur_px  = bot.get("last_price")
        ep_bot  = bot.get("open_entry_price")
        side    = bot.get("open_side", "BUY")
        for tr in bot.get("trades", []):
            row = {
                "bot_id":   bot["id"],
                "symbol":   bot["symbol"],
                "strategy": bot["strategy"],
                "timeframe":bot["timeframe"],
                "product":  bot.get("product", "MIS"),
                **tr,
            }
            if tr.get("status") == "open" and cur_px and ep_bot:
                if is_opt:
                    opt_dir = bot.get("_current_opt_dir", "CE")
                    raw = (cur_px - ep_bot) if opt_dir == "CE" else (ep_bot - cur_px)
                    row["unrealized_pnl"] = round(raw * 0.5 * lot_sz * int(bot.get("quantity",1)), 2)
                else:
                    row["unrealized_pnl"] = round((cur_px - ep_bot) * qty if side == "BUY" else (ep_bot - cur_px) * qty, 2)
                row["current_price"] = cur_px
            rows.append(row)
    rows.sort(key=lambda r: r.get("time", ""), reverse=True)
    return rows


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
